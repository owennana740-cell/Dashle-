"""Persistance PostgreSQL et reprise des générations vidéo asynchrones."""
from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from datetime import datetime, timedelta

from sqlalchemy import and_, or_

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
    return bool(visitor_key_hash and job.visitor_key_hash and visitor_key_hash == job.visitor_key_hash)


class VideoJobStore:
    def __init__(self, provider_registry, session_context=None):
        self.registry = provider_registry
        self._session_context = session_context or session_base
        self._threads: dict[str, threading.Thread] = {}
        self._threads_lock = threading.Lock()

    def _provider(self):
        provider = self.registry.get("video_generation")
        if provider is None or not self.registry.available("video_generation"):
            raise ProviderUnavailable("La génération vidéo n'est pas encore configurée sur DASHLE.")
        return provider

    def create(self, prompt: str, *, user_id: int | None,
               visitor_key_hash: str | None, conversation_id: int | None) -> str:
        # Refuser immédiatement un provider absent, sans créer un job fantôme.
        self._provider()
        job_id = uuid.uuid4().hex
        now = _now()
        with self._session_context() as db:
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
                prompt=str(prompt or "")[:24000],
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
        with self._session_context() as db:
            job = db.query(VideoGenerationJob).filter_by(id=job_id).with_for_update().one_or_none()
            if job is None or job.status not in ACTIVE_STATUSES:
                return False
            if job.lease_until and job.lease_until > now:
                return False
            if job.expires_at <= now:
                self._mark_expired_locked(job, now)
                return False
            job.status = "processing"
            job.message = "Génération en cours…"
            job.lease_until = now + LEASE_DURATION
            job.last_checked_at = now
            job.updated_at = now
            return True

    @staticmethod
    def _mark_expired_locked(job: VideoGenerationJob, now: datetime) -> None:
        job.status = "expired"
        job.progress = None
        job.message = "La génération a expiré."
        job.error_code = "expired"
        job.error_message = "Le délai maximal de génération a été dépassé."
        job.completed_at = now
        job.lease_until = None
        job.expires_at = now + TERMINAL_TTL
        job.updated_at = now

    def _renew_lease(self, job_id: str) -> bool:
        now = _now()
        with self._session_context() as db:
            job = db.query(VideoGenerationJob).filter_by(id=job_id).one_or_none()
            if job is None or job.status != "processing":
                return False
            if job.expires_at <= now:
                self._mark_expired_locked(job, now)
                return False
            job.lease_until = now + LEASE_DURATION
            job.last_checked_at = now
            job.updated_at = now
            return True

    def _save_provider_job(self, job_id: str, provider_job_id: str, progress: float | None = None) -> None:
        with self._session_context() as db:
            job = db.query(VideoGenerationJob).filter_by(id=job_id).one_or_none()
            if job is None or job.status != "processing":
                return
            job.provider_job_id = provider_job_id[:500]
            job.prompt = None
            job.progress = progress
            job.message = "En attente du fournisseur…" if progress is None else f"Génération en cours… {progress:g} %"
            job.updated_at = _now()

    def _update_progress(self, job_id: str, progress: float | None) -> bool:
        now = _now()
        with self._session_context() as db:
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

    def _fail(self, job_id: str, code: str, message: str) -> None:
        now = _now()
        with self._session_context() as db:
            job = db.query(VideoGenerationJob).filter_by(id=job_id).one_or_none()
            if job is None or job.status in TERMINAL_STATUSES:
                return
            job.status = "failed"
            job.progress = None
            job.message = message[:300]
            job.error_code = code[:60]
            job.error_message = message[:500]
            job.prompt = None
            job.completed_at = now
            job.lease_until = None
            job.expires_at = now + TERMINAL_TTL
            job.updated_at = now

    def _expire(self, job_id: str) -> None:
        now = _now()
        with self._session_context() as db:
            job = db.query(VideoGenerationJob).filter_by(id=job_id).one_or_none()
            if job is None or job.status in TERMINAL_STATUSES:
                return
            self._mark_expired_locked(job, now)
            job.prompt = None

    def _cancel_local(self, job_id: str, message: str) -> None:
        now = _now()
        with self._session_context() as db:
            job = db.query(VideoGenerationJob).filter_by(id=job_id).one_or_none()
            if job is None or job.status in TERMINAL_STATUSES:
                return
            job.status = "cancelled"
            job.progress = None
            job.message = message[:300]
            job.error_code = "cancelled"
            job.error_message = None
            job.prompt = None
            job.completed_at = now
            job.lease_until = None
            job.expires_at = now + TERMINAL_TTL
            job.updated_at = now

    def _complete(self, job_id: str, artifact) -> None:
        data = bytes(artifact.data or b"")
        if not data or len(data) > MAX_RESULT_BYTES:
            raise ProviderError("Le résultat vidéo est vide ou trop volumineux.")
        now = _now()
        with self._session_context() as db:
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
            job.prompt = None
            job.completed_at = now
            job.lease_until = None
            job.expires_at = now + TERMINAL_TTL
            job.updated_at = now

    def _process_job(self, job_id: str) -> None:
        if not self._claim(job_id):
            return
        try:
            provider = self._provider()
            with self._session_context() as db:
                job = db.query(VideoGenerationJob).filter_by(id=job_id).one_or_none()
                if job is None or job.status != "processing":
                    return
                provider_job_id = job.provider_job_id
                prompt = job.prompt

            if not provider_job_id:
                if not prompt:
                    # Crash window après création distante et avant sauvegarde de
                    # provider_job_id : ne jamais recréer une génération à l'aveugle.
                    self._fail(
                        job_id, "recovery_interrupted",
                        "La génération a été interrompue avant la sauvegarde de son identifiant fournisseur.",
                    )
                    return
                created = provider.create(prompt, timeout_s=30)
                self._save_provider_job(job_id, created.job_id, created.progress)
                provider_job_id = created.job_id

            while True:
                if not self._renew_lease(job_id):
                    return
                try:
                    current = provider.status(provider_job_id, timeout_s=20)
                except ProviderTimeout:
                    # Timeout réseau : le job distant reste potentiellement actif.
                    # On conserve son identifiant et on reprend le suivi.
                    time.sleep(POLL_INTERVAL)
                    continue

                if current.status == "succeeded":
                    try:
                        artifact = provider.retrieve(provider_job_id, timeout_s=60)
                    except ProviderTimeout:
                        time.sleep(POLL_INTERVAL)
                        continue
                    self._complete(job_id, artifact)
                    logger.info(
                        "tool=video_generation provider=gemini job_id=%s status=success",
                        job_id,
                    )
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
            self._fail(
                job_id, "provider_unavailable",
                "La génération vidéo n'est pas encore configurée sur DASHLE.",
            )
        except ProviderTimeout:
            self._expire(job_id)
        except ProviderError:
            self._fail(job_id, "provider_error", "La génération vidéo a échoué.")
        except Exception:
            logger.exception("video_job unexpected error job_id=%s", job_id)
            self._fail(job_id, "internal_error", "Le suivi de la génération vidéo a échoué.")

    def cancel(self, job_id: str, *, user_id: int | None, visitor_key_hash: str | None) -> dict:
        now = _now()
        with self._session_context() as db:
            job = db.query(VideoGenerationJob).filter_by(id=job_id).one_or_none()
            if job is None or not _owner_matches(job, user_id, visitor_key_hash):
                return {"status": "not_found"}
            if job.status in TERMINAL_STATUSES:
                return self._snapshot_dict(job)
            remote_cancelled = False
            provider = self.registry.get("video_generation") if job.provider_job_id else None
            if provider is not None and job.provider_job_id:
                try:
                    remote_cancelled = bool(provider.cancel(job.provider_job_id, timeout_s=15))
                except (NotImplementedError, ProviderError, ProviderTimeout):
                    remote_cancelled = False
            job.status = "cancelled"
            job.progress = None
            job.message = (
                "Génération annulée." if remote_cancelled
                else "Suivi arrêté. Le fournisseur peut poursuivre la génération distante."
            )
            job.error_code = "cancelled"
            job.error_message = None
            job.prompt = None
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
        return {"artifact": {
            "type": "video",
            "mime_type": job.result_mime_type or "video/mp4",
            "filename": job.result_filename or "video-dashle.mp4",
            "url": f"/api/outils/jobs/{job.id}/resultat",
            "size_bytes": job.result_size_bytes or len(job.result_data),
        }}

    def get(self, job_id: str, *, user_id: int | None, visitor_key_hash: str | None) -> dict | None:
        self.cleanup()
        with self._session_context() as db:
            job = db.query(VideoGenerationJob).filter_by(id=job_id).one_or_none()
            if job is None or not _owner_matches(job, user_id, visitor_key_hash):
                return None
            return self._snapshot_dict(job)

    def list_for_owner(self, *, user_id: int | None, visitor_key_hash: str | None) -> list[dict]:
        self.cleanup()
        since = _now() - TERMINAL_TTL
        with self._session_context() as db:
            query = db.query(VideoGenerationJob)
            if user_id is not None:
                query = query.filter(VideoGenerationJob.user_id == user_id)
            else:
                query = query.filter(VideoGenerationJob.visitor_key_hash == visitor_key_hash)
            active = VideoGenerationJob.status.in_(ACTIVE_STATUSES)
            recent_terminal = and_(
                VideoGenerationJob.status.in_(TERMINAL_STATUSES),
                VideoGenerationJob.updated_at >= since,
            )
            jobs = query.filter(or_(active, recent_terminal)).order_by(
                VideoGenerationJob.created_at.desc()
            ).limit(20).all()
            return [self._snapshot_dict(job) for job in jobs]

    def stream(self, job_id: str, *, user_id: int | None, visitor_key_hash: str | None):
        if self.get(job_id, user_id=user_id, visitor_key_hash=visitor_key_hash) is None:
            return None

        def generate():
            last = None
            heartbeat_at = time.monotonic()
            while True:
                snapshot = self.get(
                    job_id, user_id=user_id, visitor_key_hash=visitor_key_hash
                )
                if snapshot is None:
                    yield self._sse(
                        "action_failed", job_id, "expired",
                        "Ce suivi de génération n'est plus disponible.",
                    )
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
                    yield ": heartbeat\\n\\n"
                    heartbeat_at = time.monotonic()
                time.sleep(2)

        return generate()

    @staticmethod
    def _sse(event: str, job_id: str, step: str, message: str) -> str:
        return "data: " + json.dumps({
            "event": event,
            "action": {
                "id": job_id, "type": "video", "step": step,
                "message": message, "cancelable": True,
            },
        }, ensure_ascii=False) + "\\n\\n"

    def _event_for_snapshot(self, snapshot: dict) -> str:
        status = snapshot["status"]
        if status == "completed":
            action = {
                "id": snapshot["id"], "type": "video", "step": "termine",
                "message": snapshot["message"], "cancelable": False,
                "result": snapshot["result"] or {},
            }
            return "data: " + json.dumps({"event": "action_completed", "action": action}, ensure_ascii=False) + "\\n\\n"
        if status in {"failed", "expired", "cancelled"}:
            message = snapshot["message"]
            if status == "expired":
                message = "La génération vidéo a expiré."
            action = {
                "id": snapshot["id"], "type": "video", "step": status,
                "message": message,
                "status": "provider_unavailable" if snapshot["error_code"] == "provider_unavailable" else status,
                "cancelable": False,
            }
            event = "action_cancelled" if status == "cancelled" else "action_failed"
            return "data: " + json.dumps({"event": event, "action": action}, ensure_ascii=False) + "\\n\\n"
        action = {
            "id": snapshot["id"], "type": "video",
            "step": "generation" if status == "processing" else "preparation",
            "message": snapshot["message"],
            "cancelable": True,
            "result": {"progress": snapshot["progress"]},
        }
        return "data: " + json.dumps({"event": "action_progress", "action": action}, ensure_ascii=False) + "\\n\\n"

    def result(self, job_id: str, *, user_id: int | None, visitor_key_hash: str | None):
        with self._session_context() as db:
            job = db.query(VideoGenerationJob).filter_by(id=job_id).one_or_none()
            if job is None or not _owner_matches(job, user_id, visitor_key_hash):
                return None
            if job.status != "completed" or not job.result_data:
                return None
            return (
                bytes(job.result_data),
                job.result_mime_type or "video/mp4",
                job.result_filename or "video-dashle.mp4",
            )

    def cleanup(self) -> None:
        now = _now()
        with self._session_context() as db:
            jobs = db.query(VideoGenerationJob).filter(
                VideoGenerationJob.status.in_(TERMINAL_STATUSES),
                VideoGenerationJob.expires_at <= now,
            ).all()
            for job in jobs:
                db.delete(job)

    def resume_active_jobs(self) -> None:
        self.cleanup()
        with self._session_context() as db:
            ids = [
                row.id for row in db.query(VideoGenerationJob.id).filter(
                    VideoGenerationJob.status.in_(ACTIVE_STATUSES)
                ).all()
            ]
        for job_id in ids:
            self._start_worker(job_id)
