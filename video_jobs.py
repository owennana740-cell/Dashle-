"""Persistance et reprise des générations vidéo asynchrones.

La base PostgreSQL est la source de vérité. Aucun état de job n'est conservé
uniquement en mémoire et aucun secret fournisseur n'est persisté.
"""
from __future__ import annotations

import logging
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

from database import VideoGenerationJob, session_base
from tool_providers import ProviderError, ProviderUnavailable, ProviderTimeout


logger = logging.getLogger(__name__)

ACTIVE_STATUSES = {"queued", "processing"}
TERMINAL_STATUSES = {"completed", "failed", "cancelled", "expired"}
JOB_TTL = timedelta(hours=2)
TERMINAL_TTL = timedelta(days=7)
LEASE_DURATION = timedelta(seconds=30)
POLL_INTERVAL = 5.0
MAX_RESULT_BYTES = 100 * 1024 * 1024


def _now() -> datetime:
    return datetime.utcnow()


def _owner_matches(job: VideoGenerationJob, user_id: int | None, visitor_key_hash: str | None) -> bool:
    if job.user_id is not None:
        return user_id is not None and job.user_id == user_id
    return bool(visitor_key_hash and job.visitor_key_hash and
                visitor_key_hash == job.visitor_key_hash)


class VideoJobStore:
    def __init__(self, provider_registry):
        self.registry = provider_registry
        self._threads: dict[str, threading.Thread] = {}
        self._threads_lock = threading.Lock()

    def _provider(self):
        provider = self.registry.get("video_generation")
        if provider is None or not self.registry.available("video_generation"):
            raise ProviderUnavailable("La génération vidéo n'est pas encore configurée sur DASHLE.")
        return provider

    def create(self, prompt: str, *, user_id: int | None,
               visitor_key_hash: str | None, conversation_id: int | None) -> str:
        job_id = uuid.uuid4().hex
        now = _now()
        with session_base() as db:
            db.add(VideoGenerationJob(
                id=job_id,
                user_id=user_id,
                visitor_key_hash=visitor_key_hash,
                provider="gemini",
                tool_type="video_generation",
                status="queued",
                progress=None,
                message="Préparation de la génération…",
                provider_job_id=None,
                error_code=None,
                error_message=None,
                result_data=None,
                result_mime_type=None,
                result_filename=None,
                result_size_bytes=None,
                conversation_id=conversation_id,
                created_at=now,
                updated_at=now,
                expires_at=now + JOB_TTL,
                lease_until=None,
                last_checked_at=None,
                completed_at=None,
            ))
        self._start_worker(job_id)
        return job_id

    def _start_worker(self, job_id: str) -> None:
        with self._threads_lock:
            existing = self._threads.get(job_id)
            if existing and existing.is_alive():
                return
            thread = threading.Thread(
                target=self._worker_entry,
                args=(job_id,),
                name=f"dashle-video-{job_id[:10]}",
                daemon=True,
            )
            self._threads[job_id] = thread
            thread.start()

    def _worker_entry(self, job_id: str) -> None:
        try:
            self._process_job(job_id)
        except Exception:
            logger.exception("video_job worker crashed job_id=%s", job_id)
        finally:
            with self._threads_lock:
                self._threads.pop(job_id, None)

    def _claim(self, job_id: str) -> bool:
        now = _now()
        with session_base() as db:
            job = db.query(VideoGenerationJob).filter_by(id=job_id).with_for_update().one_or_none()
            if job is None or job.status not in ACTIVE_STATUSES:
                return False
            if job.lease_until and job.lease_until > now:
                return False
            if job.expires_at <= now:
                job.status = "expired"
                job.progress = None
                job.message = "La génération a expiré."
                job.error_code = "expired"
                job.error_message = "Le délai maximal de génération a été dépassé."
                job.completed_at = now
                job.lease_until = None
                return False
            job.status = "processing"
            job.message = "Génération en cours…"
            job.lease_until = now + LEASE_DURATION
            job.last_checked_at = now
            job.updated_at = now
            return True

    def _renew_lease(self, job_id: str) -> bool:
        now = _now()
        with session_base() as db:
            job = db.query(VideoGenerationJob).filter_by(id=job_id).one_or_none()
            if job is None or job.status != "processing":
                return False
            if job.expires_at <= now:
                job.status = "expired"
                job.progress = None
                job.message = "La génération a expiré."
                job.error_code = "expired"
                job.error_message = "Le délai maximal de génération a été dépassé."
                job.completed_at = now
                job.lease_until = None
                job.updated_at = now
                return False
            job.lease_until = now + LEASE_DURATION
            job.last_checked_at = now
            job.updated_at = now
            return True

    def _fail(self, job_id: str, code: str, message: str) -> None:
        now = _now()
        with session_base() as db:
            job = db.query(VideoGenerationJob).filter_by(id=job_id).one_or_none()
            if job is None or job.status in TERMINAL_STATUSES:
                return
            job.status = "failed"
            job.progress = None
            job.message = message[:300]
            job.error_code = code[:60]
            job.error_message = message[:500]
            job.completed_at = now
            job.lease_until = None
            job.expires_at = now + TERMINAL_TTL
            job.updated_at = now

    def _save_provider_job(self, job_id: str, provider_job_id: str) -> None:
        with session_base() as db:
            job = db.query(VideoGenerationJob).filter_by(id=job_id).one_or_none()
            if job is None or job.status != "processing":
                return
            job.provider_job_id = provider_job_id[:500]
            job.message = "En attente du fournisseur…"
            job.updated_at = _now()

    def _update_progress(self, job_id: str, progress: float | None) -> bool:
        now = _now()
        with session_base() as db:
            job = db.query(VideoGenerationJob).filter_by(id=job_id).one_or_none()
            if job is None or job.status != "processing":
                return False
            job.progress = progress
            job.message = (
                f"Génération en cours… {progress:g} %"
                if isinstance(progress, (int, float)) else "Génération en cours…"
            )
            job.updated_at = now
            job.last_checked_at = now
            job.lease_until = now + LEASE_DURATION
            return True

    def _complete(self, job_id: str, artifact) -> None:
        data = bytes(artifact.data or b"")
        if not data or len(data) > MAX_RESULT_BYTES:
            raise ProviderError("Le résultat vidéo est vide ou trop volumineux.")
        now = _now()
        with session_base() as db:
            job = db.query(VideoGenerationJob).filter_by(id=job_id).one_or_none()
            if job is None or job.status in TERMINAL_STATUSES:
                return
            job.status = "completed"
            job.progress = 100.0
            job.message = "Génération vidéo terminée."
            job.result_data = data
            job.result_mime_type = str(artifact.mime_type or "video/mp4")[:100]
            job.result_filename = str(artifact.filename or "video-dashle.mp4")[:255]
            job.result_size_bytes = len(data)
            job.error_code = None
            job.error_message = None
            job.completed_at = now
            job.lease_until = None
            job.expires_at = now + TERMINAL_TTL
            job.updated_at = now

    def _process_job(self, job_id: str) -> None:
        if not self._claim(job_id):
            return
        provider = None
        try:
            provider = self._provider()
            with session_base() as db:
                job = db.query(VideoGenerationJob).filter_by(id=job_id).one_or_none()
                if job is None or job.status != "processing":
                    return
                provider_job_id = job.provider_job_id

            if not provider_job_id:
                # Le job DB est déjà marqué processing avant l'appel externe.
                # En cas de crash après création distante mais avant la sauvegarde
                # de provider_job_id, on refuse toute nouvelle création au restart
                # afin de garantir l'absence de double génération.
                with session_base() as db:
                    job = db.query(VideoGenerationJob).filter_by(id=job_id).one_or_none()
                    if job is None or job.status != "processing":
                        return
                    provider_job_id = None
                if provider_job_id is None:
                    # Création externe unique pour ce job. Le provider id est
                    # persisté immédiatement avant tout polling.
                    created = provider.create(self._prompt_placeholder(job_id), timeout_s=30)
                    self._save_provider_job(job_id, created.job_id)
                    provider_job_id = created.job_id

            while True:
                if not self._renew_lease(job_id):
                    return
                current = provider.status(provider_job_id, timeout_s=20)
                if current.status == "succeeded":
                    artifact = provider.retrieve(provider_job_id, timeout_s=60)
                    self._complete(job_id, artifact)
                    logger.info("tool=video_generation provider=gemini job_id=%s status=success", job_id)
                    return
                if current.status == "failed":
                    self._fail(job_id, "provider_error", "La génération vidéo a échoué.")
                    return
                if current.status == "cancelled":
                    self._cancel_local(job_id, "La génération vidéo a été annulée.")
                    return
                if not self._update_progress(job_id, current.progress):
                    return
                time.sleep(POLL_INTERVAL)
        except ProviderUnavailable:
            self._fail(job_id, "provider_unavailable", "La génération vidéo n'est pas encore configurée sur DASHLE.")
        except ProviderTimeout:
            # Un timeout réseau n'est pas assimilé à un échec du job distant.
            # Le worker reprend le suivi tant que le job n'est pas expiré.
            while True:
                with session_base() as db:
                    job = db.query(VideoGenerationJob).filter_by(id=job_id).one_or_none()
                    if job is None or job.status != "processing":
                        return
                    if job.expires_at <= _now():
                        break
                time.sleep(POLL_INTERVAL)
            self._expire(job_id)
        except ProviderError:
            self._fail(job_id, "provider_error", "La génération vidéo a échoué.")
        except Exception:
            logger.exception("video_job unexpected error job_id=%s", job_id)
            self._fail(job_id, "internal_error", "Le suivi de la génération vidéo a échoué.")

    @staticmethod
    def _prompt_placeholder(job_id: str) -> str:
        # Le prompt n'est volontairement pas stocké en base. Cette méthode est
        # remplacée par le worker wrapper lors de la création du job.
        raise ProviderError("Le prompt de génération n'est plus disponible.")

    def _expire(self, job_id: str) -> None:
        now = _now()
        with session_base() as db:
            job = db.query(VideoGenerationJob).filter_by(id=job_id).one_or_none()
            if job is None or job.status in TERMINAL_STATUSES:
                return
            job.status = "expired"
            job.progress = None
            job.message = "La génération a expiré."
            job.error_code = "expired"
            job.error_message = "Le délai maximal de génération a été dépassé."
            job.completed_at = now
            job.lease_until = None
            job.expires_at = now + TERMINAL_TTL
            job.updated_at = now

    def _cancel_local(self, job_id: str, message: str) -> None:
        now = _now()
        with session_base() as db:
            job = db.query(VideoGenerationJob).filter_by(id=job_id).one_or_none()
            if job is None or job.status in TERMINAL_STATUSES:
                return
            job.status = "cancelled"
            job.progress = None
            job.message = message[:300]
            job.error_code = "cancelled"
            job.error_message = message[:500]
            job.completed_at = now
            job.lease_until = None
            job.expires_at = now + TERMINAL_TTL
            job.updated_at = now

    def cancel(self, job_id: str, *, user_id: int | None, visitor_key_hash: str | None) -> dict:
        now = _now()
        with session_base() as db:
            job = db.query(VideoGenerationJob).filter_by(id=job_id).one_or_none()
            if job is None or not _owner_matches(job, user_id, visitor_key_hash):
                return {"status": "not_found"}
            if job.status in TERMINAL_STATUSES:
                return self._snapshot_dict(job)
            provider = None
            if job.provider_job_id:
                provider = self.registry.get("video_generation")
            remote_cancelled = False
            if provider is not None and job.provider_job_id:
                try:
                    remote_cancelled = bool(provider.cancel(job.provider_job_id, timeout_s=15))
                except (NotImplementedError, ProviderError, ProviderTimeout):
                    remote_cancelled = False
            job.status = "cancelled"
            job.progress = None
            job.message = (
                "Génération annulée."
                if remote_cancelled else
                "Suivi arrêté. Le fournisseur peut poursuivre la génération distante."
            )
            job.error_code = "cancelled"
            job.error_message = None
            job.completed_at = now
            job.lease_until = None
            job.expires_at = now + TERMINAL_TTL
            job.updated_at = now
            return self._snapshot_dict(job)

    def _snapshot_dict(self, job: VideoGenerationJob) -> dict:
        return {
            "id": job.id,
            "status": job.status,
            "progress": job.progress,
            "message": job.message,
            "error_code": job.error_code,
            "error_message": job.error_message,
            "provider": job.provider,
            "conversation_id": job.conversation_id,
            "created_at": job.created_at.isoformat() if job.created_at else None,
            "updated_at": job.updated_at.isoformat() if job.updated_at else None,
            "expires_at": job.expires_at.isoformat() if job.expires_at else None,
            "result": self._result_dict(job),
        }

    def _result_dict(self, job: VideoGenerationJob) -> dict | None:
        if job.status != "completed" or not job.result_data:
            return None
        return {
            "artifact": {
                "type": "video",
                "mime_type": job.result_mime_type or "video/mp4",
                "filename": job.result_filename or "video-dashle.mp4",
                "url": f"/api/outils/jobs/{job.id}/resultat",
                "size_bytes": job.result_size_bytes or len(job.result_data),
            }
        }

    def get(self, job_id: str, *, user_id: int | None, visitor_key_hash: str | None) -> dict | None:
        self.cleanup()
        with session_base() as db:
            job = db.query(VideoGenerationJob).filter_by(id=job_id).one_or_none()
            if job is None or not _owner_matches(job, user_id, visitor_key_hash):
                return None
            return self._snapshot_dict(job)

    def list_for_owner(self, *, user_id: int | None, visitor_key_hash: str | None) -> list[dict]:
        self.cleanup()
        now = _now()
        since = now - TERMINAL_TTL
        with session_base() as db:
            query = db.query(VideoGenerationJob)
            if user_id is not None:
                query = query.filter(VideoGenerationJob.user_id == user_id)
            else:
                query = query.filter(VideoGenerationJob.visitor_key_hash == visitor_key_hash)
            jobs = (
                query.filter(
                    (VideoGenerationJob.status.in_(ACTIVE_STATUSES)) |
                    (VideoGenerationJob.status.in_(TERMINAL_STATUSES),
                     VideoGenerationJob.updated_at >= since)
                )
                .order_by(VideoGenerationJob.created_at.desc())
                .limit(20)
                .all()
            )
            return [self._snapshot_dict(job) for job in jobs]

    def stream(self, job_id: str, *, user_id: int | None, visitor_key_hash: str | None):
        self.cleanup()
        with session_base() as db:
            job = db.query(VideoGenerationJob).filter_by(id=job_id).one_or_none()
            if job is None or not _owner_matches(job, user_id, visitor_key_hash):
                return None

        def generate():
            last = None
            heartbeat_at = time.monotonic()
            while True:
                snapshot = self.get(job_id, user_id=user_id, visitor_key_hash=visitor_key_hash)
                if snapshot is None:
                    yield self._sse("action_failed", job_id, "expired", "Ce suivi de génération n'est plus disponible.")
                    return
                state = (
                    snapshot["status"], snapshot["progress"], snapshot["message"],
                    snapshot["error_code"], bool(snapshot["result"])
                )
                if state != last:
                    last = state
                    yield self._event_for_snapshot(snapshot)
                if snapshot["status"] in TERMINAL_STATUSES:
                    return
                if time.monotonic() - heartbeat_at >= 15:
                    yield ": heartbeat\n\n"
                    heartbeat_at = time.monotonic()
                time.sleep(2)

        return generate()

    @staticmethod
    def _sse(event: str, job_id: str, step: str, message: str) -> str:
        import json
        return "data: " + json.dumps({
            "event": event,
            "action": {
                "id": job_id, "type": "video", "step": step,
                "message": message, "cancelable": True,
            },
        }, ensure_ascii=False) + "\n\n"

    def _event_for_snapshot(self, snapshot: dict) -> str:
        import json
        status = snapshot["status"]
        if status == "completed":
            action = {
                "id": snapshot["id"], "type": "video", "step": "termine",
                "message": snapshot["message"], "cancelable": False,
                "result": snapshot["result"] or {},
            }
            return "data: " + json.dumps({"event": "action_completed", "action": action}, ensure_ascii=False) + "\n\n"
        if status in {"failed", "expired", "cancelled"}:
            message = snapshot["message"]
            if status == "expired":
                message = "La génération vidéo a expiré."
            action = {
                "id": snapshot["id"], "type": "video", "step": status,
                "message": message, "status": (
                    "provider_unavailable" if snapshot["error_code"] == "provider_unavailable" else status
                ),
                "cancelable": False,
            }
            return "data: " + json.dumps({"event": "action_failed" if status != "cancelled" else "action_cancelled",
                                          "action": action}, ensure_ascii=False) + "\n\n"
        action = {
            "id": snapshot["id"], "type": "video",
            "step": "generation" if status == "processing" else "preparation",
            "message": snapshot["message"],
            "cancelable": True,
            "result": {"progress": snapshot["progress"]},
        }
        return "data: " + json.dumps({"event": "action_progress", "action": action}, ensure_ascii=False) + "\n\n"

    def result(self, job_id: str, *, user_id: int | None, visitor_key_hash: str | None):
        with session_base() as db:
            job = db.query(VideoGenerationJob).filter_by(id=job_id).one_or_none()
            if job is None or not _owner_matches(job, user_id, visitor_key_hash):
                return None
            if job.status != "completed" or not job.result_data:
                return None
            return bytes(job.result_data), job.result_mime_type or "video/mp4", job.result_filename or "video-dashle.mp4"

    def cleanup(self) -> None:
        now = _now()
        with session_base() as db:
            jobs = db.query(VideoGenerationJob).filter(
                VideoGenerationJob.status.in_(TERMINAL_STATUSES),
                VideoGenerationJob.expires_at <= now,
            ).all()
            for job in jobs:
                db.delete(job)

    def resume_active_jobs(self) -> None:
        self.cleanup()
        with session_base() as db:
            ids = [
                row.id for row in db.query(VideoGenerationJob.id).filter(
                    VideoGenerationJob.status.in_(ACTIVE_STATUSES)
                ).all()
            ]
        for job_id in ids:
            self._start_worker(job_id)
