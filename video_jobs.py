"""Persistance PostgreSQL et reprise des jobs vidéo Dashle."""
from __future__ import annotations
import hashlib, logging, os, secrets, threading, time
from datetime import datetime, timedelta
from database import VideoGenerationJob, session_base
from tool_providers import ProviderError, ProviderTimeout, ProviderUnavailable, VideoGenerationProvider

LOGGER = logging.getLogger(__name__)
ACTIVE_STATUSES = {"queued", "processing"}
TERMINAL_STATUSES = {"completed", "failed", "cancelled", "expired"}
RETENTION_HOURS = max(1, int(os.environ.get("DASHLE_VIDEO_JOB_RETENTION_HOURS", "24")))
MAX_RUNTIME_MINUTES = max(5, int(os.environ.get("DASHLE_VIDEO_JOB_MAX_RUNTIME_MINUTES", "60")))
LEASE_SECONDS = max(10, int(os.environ.get("DASHLE_VIDEO_JOB_LEASE_SECONDS", "30")))
POLL_SECONDS = max(1, float(os.environ.get("DASHLE_VIDEO_JOB_POLL_SECONDS", "2")))

def hash_owner_token(token: str) -> str:
    return hashlib.sha256(str(token).encode("utf-8")).hexdigest()

def ensure_owner_token(session: dict) -> str:
    token = session.get("video_job_owner_token")
    if not isinstance(token, str) or len(token) < 32:
        token = secrets.token_urlsafe(32)
        session["video_job_owner_token"] = token
        session.modified = True
    return token

def _retention_deadline(now):
    return now + timedelta(hours=RETENTION_HOURS)


def _scoped_idempotency_key(idempotency_key, *, user_id, owner_token_hash):
    """Isole la déduplication d'action entre propriétaires différents."""
    owner = "user:" + str(user_id) if user_id is not None else "visitor:" + str(owner_token_hash or "")
    raw = owner + "|" + str(idempotency_key)[:128]
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()

def _mark_creation_failed(job_id, message):
    now = datetime.utcnow()
    with session_base() as db:
        job = db.query(VideoGenerationJob).filter_by(id=job_id).with_for_update().one_or_none()
        if job:
            job.status, job.status_message = "failed", "La génération a échoué."
            job.error_message, job.completed_at = str(message)[:500], now
            job.updated_at, job.expires_at = now, _retention_deadline(now)

def create_job(provider, *, prompt, user_id, owner_token_hash, conversation_id, idempotency_key):
    now = datetime.utcnow()
    scoped_key = _scoped_idempotency_key(idempotency_key, user_id=user_id, owner_token_hash=owner_token_hash)
    with session_base() as db:
        existing = db.query(VideoGenerationJob).filter_by(idempotency_key=scoped_key).with_for_update().one_or_none()
        if existing:
            job_id = existing.id
        else:
            job_id = secrets.token_urlsafe(24)
            db.add(VideoGenerationJob(
                id=job_id, user_id=user_id, owner_token_hash=owner_token_hash,
                conversation_id=conversation_id, provider="veo", tool="video_generation",
                status="queued", progress=None, status_message="Préparation de la génération…",
                provider_job_id=None, idempotency_key=scoped_key,
                prompt=str(prompt)[:24000], created_at=now, updated_at=now,
                expires_at=now + timedelta(minutes=MAX_RUNTIME_MINUTES),
            ))
    _spawn_active_job(job_id, provider)
    LOGGER.info("tool=video_generation provider=veo job_id=%s status=queued", job_id)
    return job_id

def _persist_creation(job_id, worker, remote):
    now = datetime.utcnow()
    with session_base() as db:
        job = db.query(VideoGenerationJob).filter_by(id=job_id).with_for_update().one_or_none()
        if not job or job.lease_owner != worker:
            return False
        job.provider_job_id = remote.job_id
        job.progress = remote.progress
        job.prompt = None
        job.status = "processing" if remote.status == "running" else "queued"
        job.status_message = (
            f"Génération en cours… {remote.progress:g} %" if isinstance(remote.progress, (int, float))
            else "En attente du fournisseur…"
        )
        job.updated_at = now
        job.lease_owner = job.lease_until = None
    return True

def _create_remote(job_id, provider):
    worker = _claim(job_id)
    if worker is None:
        return
    try:
        with session_base() as db:
            job = db.get(VideoGenerationJob, job_id)
            prompt = job.prompt if job else None
            if job and job.lease_owner == worker:
                job.lease_until = datetime.utcnow() + timedelta(seconds=max(LEASE_SECONDS, 60))
        if not prompt:
            _release(job_id, worker)
            return
        try:
            remote = provider.create(prompt, timeout_s=30)
            if _persist_creation(job_id, worker, remote):
                _spawn_monitor(job_id, provider)
        except (ProviderTimeout, ProviderUnavailable):
            _release(job_id, worker)
            LOGGER.warning("tool=video_generation provider=veo job_id=%s status=retryable_creation_error", job_id)
        except ProviderError as exc:
            _mark_creation_failed(job_id, str(exc))
            LOGGER.warning("tool=video_generation provider=veo job_id=%s status=creation_error", job_id)
    except Exception:
        _release(job_id, worker)
        LOGGER.exception("tool=video_generation provider=veo job_id=%s status=unexpected_creation_error", job_id)

def _spawn_active_job(job_id, provider):
    if provider is None:
        return
    with session_base() as db:
        job = db.get(VideoGenerationJob, job_id)
        if not job or job.status not in ACTIVE_STATUSES:
            return
        has_remote = bool(job.provider_job_id)
    if has_remote:
        _spawn_monitor(job_id, provider)
    else:
        threading.Thread(
            target=_create_remote, args=(job_id, provider),
            name=f"dashle-video-create-{job_id[:8]}", daemon=True
        ).start()

def _claim(job_id):
    worker, now = secrets.token_hex(16), datetime.utcnow()
    with session_base() as db:
        job = db.query(VideoGenerationJob).filter_by(id=job_id).with_for_update().one_or_none()
        if not job or job.status not in ACTIVE_STATUSES:
            return None
        if job.expires_at <= now:
            job.status, job.status_message = "expired", "La génération a expiré."
            job.error_message, job.updated_at = "Job vidéo expiré avant sa finalisation.", now
            job.lease_owner = job.lease_until = None
            return None
        if job.lease_until and job.lease_until > now and job.lease_owner:
            return None
        job.lease_owner, job.lease_until = worker, now + timedelta(seconds=LEASE_SECONDS)
        job.updated_at = now
    return worker

def _release(job_id, worker):
    with session_base() as db:
        job = db.query(VideoGenerationJob).filter_by(id=job_id).one_or_none()
        if job and job.lease_owner == worker:
            job.lease_owner = job.lease_until = None
            job.updated_at = datetime.utcnow()

def _persist_remote(job_id, worker, remote):
    now = datetime.utcnow()
    with session_base() as db:
        job = db.query(VideoGenerationJob).filter_by(id=job_id).with_for_update().one_or_none()
        if not job or job.lease_owner != worker:
            return False
        job.last_checked_at, job.updated_at, job.progress = now, now, remote.progress
        if remote.status == "succeeded":
            job.status, job.status_message = "processing", "Finalisation…"
            return True
        if remote.status == "failed":
            job.status, job.status_message = "failed", "La génération a échoué."
            job.error_message, job.completed_at = (remote.error or "Le fournisseur vidéo a échoué.")[:500], now
            job.expires_at, job.lease_owner, job.lease_until = _retention_deadline(now), None, None
            return False
        if remote.status == "cancelled":
            job.status, job.status_message = "cancelled", "Génération annulée."
            job.completed_at, job.expires_at = now, _retention_deadline(now)
            job.lease_owner, job.lease_until = None, None
            return False
        job.status = "processing"
        job.status_message = f"Génération en cours… {remote.progress:g} %" if isinstance(remote.progress, (int, float)) else "Génération en cours…"
        job.lease_until = now + timedelta(seconds=max(LEASE_SECONDS, 90))
    return False

def _store_artifact(job_id, worker, artifact):
    now = datetime.utcnow()
    with session_base() as db:
        job = db.query(VideoGenerationJob).filter_by(id=job_id).with_for_update().one_or_none()
        if not job or job.lease_owner != worker:
            return
        job.result_data, job.result_mime_type = artifact.data, artifact.mime_type
        job.result_filename = artifact.filename or "video-dashle.mp4"
        job.status, job.progress, job.status_message = "completed", 100.0, "Génération terminée."
        job.completed_at, job.updated_at, job.expires_at = now, now, _retention_deadline(now)
        job.lease_owner = job.lease_until = None

def _monitor(job_id, provider):
    while True:
        worker = _claim(job_id)
        if worker is None:
            with session_base() as db:
                job = db.get(VideoGenerationJob, job_id)
                if not job or job.status not in ACTIVE_STATUSES:
                    return
            time.sleep(POLL_SECONDS)
            continue
        try:
            with session_base() as db:
                job = db.get(VideoGenerationJob, job_id)
                provider_job_id = job.provider_job_id if job else None
            if not provider_job_id:
                _release(job_id, worker)
                time.sleep(POLL_SECONDS)
                continue
            try:
                remote = provider.status(provider_job_id, timeout_s=20)
                if _persist_remote(job_id, worker, remote):
                    artifact = provider.retrieve(provider_job_id, timeout_s=60)
                    _store_artifact(job_id, worker, artifact)
                    LOGGER.info("tool=video_generation provider=veo job_id=%s status=completed", job_id)
                    return
                with session_base() as db:
                    job = db.get(VideoGenerationJob, job_id)
                    if not job or job.status not in ACTIVE_STATUSES:
                        return
                _release(job_id, worker)
            except (ProviderTimeout, ProviderUnavailable):
                _release(job_id, worker)
                LOGGER.warning("tool=video_generation provider=veo job_id=%s status=retryable_error", job_id)
            except ProviderError as exc:
                with session_base() as db:
                    job = db.query(VideoGenerationJob).filter_by(id=job_id).with_for_update().one_or_none()
                    if job and job.lease_owner == worker:
                        now = datetime.utcnow()
                        job.status, job.status_message = "failed", "La génération a échoué."
                        job.error_message, job.completed_at = str(exc)[:500], now
                        job.expires_at, job.lease_owner, job.lease_until = _retention_deadline(now), None, None
                LOGGER.warning("tool=video_generation provider=veo job_id=%s status=error", job_id)
        except Exception:
            _release(job_id, worker)
            LOGGER.exception("tool=video_generation provider=veo job_id=%s status=unexpected_error", job_id)
        time.sleep(POLL_SECONDS)

def _spawn_monitor(job_id, provider):
    threading.Thread(target=_monitor, args=(job_id, provider), name=f"dashle-video-{job_id[:8]}", daemon=True).start()

def resume_active_jobs(provider):
    if provider is None:
        return 0
    with session_base() as db:
        ids = [x.id for x in db.query(VideoGenerationJob).filter(VideoGenerationJob.status.in_(ACTIVE_STATUSES)).all()]
    for job_id in ids:
        _spawn_monitor(job_id, provider)
    return len(ids)

def _owned(job, user_id, owner_token_hash):
    if job.user_id is not None:
        return user_id == job.user_id
    return bool(owner_token_hash) and secrets.compare_digest(job.owner_token_hash or "", owner_token_hash)

def snapshot_for_owner(job_id, *, user_id, owner_token_hash):
    with session_base() as db:
        job = db.get(VideoGenerationJob, job_id)
        if not job or not _owned(job, user_id, owner_token_hash):
            return None
        return {"id":job.id,"type":"video","status":job.status,"progress":job.progress,"message":job.status_message,
                "error":job.error_message,"conversation_id":job.conversation_id,"filename":job.result_filename,
                "mime_type":job.result_mime_type,"updated_at":job.updated_at.timestamp() if job.updated_at else 0}

def active_for_owner(*, user_id, owner_token_hash):
    with session_base() as db:
        q = db.query(VideoGenerationJob)
        q = q.filter_by(user_id=user_id) if user_id is not None else q.filter_by(owner_token_hash=owner_token_hash)
        return [{"id":j.id,"type":"video","status":j.status,"progress":j.progress,"message":j.status_message,
                 "conversation_id":j.conversation_id,"updated_at":j.updated_at.timestamp() if j.updated_at else 0}
                for j in q.filter(VideoGenerationJob.status.in_(ACTIVE_STATUSES)).order_by(VideoGenerationJob.created_at.asc()).all()]

def result_metadata(job_id, *, user_id, owner_token_hash):
    snap = snapshot_for_owner(job_id, user_id=user_id, owner_token_hash=owner_token_hash)
    if not snap or snap["status"] != "completed":
        return None
    return {"type":"video","mime_type":snap["mime_type"] or "video/mp4","filename":snap["filename"] or "video-dashle.mp4",
            "url":f"/api/outils/jobs/{job_id}/result"}

def read_result(job_id, *, user_id, owner_token_hash):
    with session_base() as db:
        job = db.get(VideoGenerationJob, job_id)
        if not job or job.status != "completed" or not _owned(job, user_id, owner_token_hash):
            return None
        return job.result_data, job.result_mime_type, job.result_filename

def cleanup_expired():
    now = datetime.utcnow()
    with session_base() as db:
        rows = db.query(VideoGenerationJob).filter(VideoGenerationJob.status.in_(TERMINAL_STATUSES), VideoGenerationJob.expires_at < now).all()
        for job in rows:
            db.delete(job)
        return len(rows)
