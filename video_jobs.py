"""Persistance et reprise des générations vidéo longues de DASHLE."""
import hashlib, secrets, threading, time
from datetime import datetime, timedelta, timezone
from uuid import uuid4
from sqlalchemy import or_, and_, update
from database import VideoGenerationJob, session_base
from tool_providers import ProviderError, ProviderTimeout, ProviderUnavailable, ProviderRegistry
VIDEO_PROVIDER_NAME="gemini"; ACTIVE_STATUSES={"queued","processing"}; TERMINAL_STATUSES={"completed","failed","cancelled","expired"}
JOB_TTL=timedelta(hours=2); RETENTION_TTL=timedelta(days=7); LEASE_TTL=timedelta(seconds=75); POLL_INTERVAL_SECONDS=4; MAX_RESULT_BYTES=100*1024*1024
_PROVIDER_REGISTRY=None; _WORKERS=set(); _WORKERS_LOCK=threading.Lock()
def configure(registry: ProviderRegistry):
    global _PROVIDER_REGISTRY; _PROVIDER_REGISTRY=registry
def _now(): return datetime.now(timezone.utc).replace(tzinfo=None)
def visitor_key(raw_key: str|None, secret_key: str)->str|None:
    return hashlib.sha256((secret_key+"|video-job|"+raw_key).encode()).hexdigest() if raw_key else None
def create_job(prompt: str, *, user_id, visitor_key_value, conversation_id):
    provider=_PROVIDER_REGISTRY.get("video_generation") if _PROVIDER_REGISTRY else None
    if provider is None or _PROVIDER_REGISTRY is None or not _PROVIDER_REGISTRY.available("video_generation"): raise ProviderUnavailable("La génération vidéo n'est pas configurée sur DASHLE.")
    remote=provider.create(prompt,timeout_s=30); now=_now()
    job=VideoGenerationJob(id=uuid4().hex,user_id=user_id,visitor_key=visitor_key_value,conversation_id=conversation_id,provider=VIDEO_PROVIDER_NAME,tool_type="video_generation",prompt=prompt[:24000],status="queued",progress=remote.progress,status_message="En attente du fournisseur…",provider_job_id=remote.job_id,expires_at=now+JOB_TTL,retention_until=now+RETENTION_TTL,updated_at=now)
    with session_base() as db: db.add(job); db.flush()
    return job
def _claim(job_id):
    token=secrets.token_hex(24); now=_now()
    with session_base() as db:
        result=db.execute(update(VideoGenerationJob).where(VideoGenerationJob.id==job_id,or_(VideoGenerationJob.status=="queued",and_(VideoGenerationJob.status=="processing",or_(VideoGenerationJob.lease_until.is_(None),VideoGenerationJob.lease_until<now)))).values(status="processing",status_message="Génération en cours…",lease_token=token,lease_until=now+LEASE_TTL,updated_at=now))
        return token if result.rowcount==1 else None
def _snapshot(job_id):
    with session_base() as db:
        job=db.get(VideoGenerationJob,job_id)
        if job is None:return None
        return {c:getattr(job,c) for c in ("id","user_id","visitor_key","conversation_id","provider","tool_type","prompt","status","progress","status_message","provider_job_id","result_mime_type","result_filename","result_size_bytes","error","expires_at","retention_until","created_at","updated_at","completed_at")}
def _update(job_id,token,**values):
    now=_now(); values.update(updated_at=now,lease_until=now+LEASE_TTL)
    with session_base() as db:
        result=db.execute(update(VideoGenerationJob).where(VideoGenerationJob.id==job_id,VideoGenerationJob.lease_token==token).values(**values))
        return result.rowcount==1
def _terminal(job_id,token,status,*,error=None,artifact=None):
    now=_now(); values=dict(status=status,error=error,lease_token=None,lease_until=None,completed_at=now,retention_until=now+RETENTION_TTL,updated_at=now)
    if status=="completed" and artifact is not None: values.update(progress=100.0,status_message="Génération terminée",error=None,result_mime_type=artifact.mime_type,result_filename=artifact.filename or "video-dashle.mp4",result_data=artifact.data,result_size_bytes=len(artifact.data))
    elif status=="expired": values["status_message"]="Le délai de génération est dépassé."
    elif status=="failed": values["status_message"]="La génération vidéo a échoué."
    with session_base() as db:
        result=db.execute(update(VideoGenerationJob).where(VideoGenerationJob.id==job_id,VideoGenerationJob.lease_token==token).values(**values))
        return result.rowcount==1
def _provider():
    if _PROVIDER_REGISTRY is None:return None
    provider=_PROVIDER_REGISTRY.get("video_generation")
    return provider if provider and _PROVIDER_REGISTRY.available("video_generation") else None
def _process(job_id):
    token=_claim(job_id)
    if token is None:return
    provider=_provider()
    if provider is None:_terminal(job_id,token,"failed",error="Fournisseur vidéo indisponible.");return
    while True:
        current=_snapshot(job_id)
        if current is None or current["status"] in TERMINAL_STATUSES:return
        if current["expires_at"]<_now():_terminal(job_id,token,"expired",error="Le job vidéo a expiré.");return
        try:
            state=provider.status(current["provider_job_id"],timeout_s=20)
            if state.status=="succeeded":
                artifact=provider.retrieve(current["provider_job_id"],timeout_s=30)
                if not artifact.data or len(artifact.data)>MAX_RESULT_BYTES:raise ProviderError("Vidéo reçue vide ou trop volumineuse.")
                _terminal(job_id,token,"completed",artifact=artifact);return
            if state.status=="failed":_terminal(job_id,token,"failed",error="Le fournisseur vidéo a échoué.");return
            progress=state.progress if state.progress is not None else None
            _update(job_id,token,status="processing",progress=progress,status_message="Génération en cours…" if progress is None else f"Génération en cours… {progress:g} %")
        except ProviderUnavailable:_terminal(job_id,token,"failed",error="Fournisseur vidéo indisponible.");return
        except ProviderTimeout:_update(job_id,token,status="processing",status_message="En attente du fournisseur…")
        except ProviderError:_terminal(job_id,token,"failed",error="Le fournisseur vidéo a échoué.");return
        except Exception:_terminal(job_id,token,"failed",error="Erreur interne lors du suivi vidéo.");return
        time.sleep(POLL_INTERVAL_SECONDS)
def start(job_id):
    def run():
        try:_process(job_id)
        finally:
            with _WORKERS_LOCK:_WORKERS.discard(threading.current_thread())
    thread=threading.Thread(target=run,name=f"dashle-video-{job_id[:8]}",daemon=True)
    with _WORKERS_LOCK:_WORKERS.add(thread)
    thread.start()
def resume_active_jobs():
    now=_now()
    with session_base() as db: ids=[j.id for j in db.query(VideoGenerationJob).filter(VideoGenerationJob.status.in_(["queued", "processing", "completed"]),VideoGenerationJob.expires_at>now).all()]
    for job_id in ids:start(job_id)
def get_owned(job_id,*,user_id,visitor_key_value):
    with session_base() as db:
        job=db.get(VideoGenerationJob,job_id)
        if job is None:return None
        if user_id is not None:
            if job.user_id!=user_id:return None
        elif job.user_id is not None or job.visitor_key!=visitor_key_value:return None
        return _snapshot(job_id)
def list_owned(*,user_id,visitor_key_value):
    with session_base() as db:
        q=db.query(VideoGenerationJob).filter(VideoGenerationJob.status.in_(list(ACTIVE_STATUSES)))
        q=q.filter(VideoGenerationJob.user_id==user_id) if user_id is not None else q.filter(VideoGenerationJob.user_id.is_(None),VideoGenerationJob.visitor_key==visitor_key_value)
        return [_snapshot(j.id) for j in q.order_by(VideoGenerationJob.created_at.asc()).limit(20).all()]
def result_for_owned(job_id,*,user_id,visitor_key_value):
    with session_base() as db:
        job=db.get(VideoGenerationJob,job_id)
        if job is None or job.status!="completed" or not job.result_data:return None
        if user_id is not None:
            if job.user_id!=user_id:return None
        elif job.user_id is not None or job.visitor_key!=visitor_key_value:return None
        return {"data":job.result_data,"mime_type":job.result_mime_type,"filename":job.result_filename or "video-dashle.mp4"}
def cleanup_expired_jobs():
    with session_base() as db: db.query(VideoGenerationJob).filter(VideoGenerationJob.status.in_(list(TERMINAL_STATUSES)),VideoGenerationJob.retention_until<_now()).delete(synchronize_session=False)
def stream_events(job_id,*,user_id,visitor_key_value):
    last=None; keepalive=time.monotonic()
    while True:
        current=get_owned(job_id,user_id=user_id,visitor_key_value=visitor_key_value)
        if current is None:yield {"event":"action_failed","status":"expired"};return
        snapshot=(current["status"],current["progress"],current["status_message"],current["error"],current["completed_at"])
        if snapshot!=last:
            last=snapshot
            if current["status"]=="completed":
                yield {"event":"action_completed","action":{"id":job_id,"type":"video","step":"termine","message":"Génération vidéo terminée.","result":{"artifact":{"type":"video","mime_type":current["result_mime_type"],"filename":current["result_filename"] or "video-dashle.mp4","url":f"/api/outils/jobs/{job_id}/result"}}}};return
            if current["status"] in {"failed","expired","cancelled"}:
                yield {"event":"action_failed","action":{"id":job_id,"type":"video","step":current["status"],"message":current["error"] or "La génération vidéo a échoué.","status":current["status"]}};return
            yield {"event":"action_progress","action":{"id":job_id,"type":"video","step":current["status"],"message":current["status_message"] or "Génération en cours…","result":{"progress":current["progress"]}}}
        if time.monotonic()-keepalive>=15:keepalive=time.monotonic();yield None
        time.sleep(2)
