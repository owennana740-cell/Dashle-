from __future__ import annotations
import hashlib,json,secrets
from datetime import datetime,timedelta
from connectors.config import CONNECTOR_MAX_BY_TIER
from connectors.registry import get_connector
from connectors.security import load_credential,sanitize_external_content
from database import ConnectorActionConfirmation,ConnectorAuditLog,ConnectorPermission,User,session_base

def _perm(uid,p):
    with session_base() as db:return db.query(ConnectorPermission).filter_by(user_id=uid,provider=p).one_or_none()
def _allowed(uid,p,a,args):
    with session_base() as db:
        u=db.get(User,uid)
        if not u:return False,"Compte introuvable."
        from brain import niveau_abonnement\n        tier=niveau_abonnement(u);limit=CONNECTOR_MAX_BY_TIER.get(tier,0)
        count=db.query(ConnectorPermission).filter_by(user_id=uid).count()
    if limit==0:return False,"Les connecteurs externes ne sont pas disponibles avec ton forfait."
    if tier!="prime" and count>limit:return False,"Limite de connecteurs atteinte."
    c=get_connector(p);meta=next((x for x in c.spec.actions if x.id==a),None)
    if not meta or meta.risk=="interdite":return False,"Action interdite."
    q=_perm(uid,p)
    if not q:return False,"Connecteur non connecté."
    resources=json.loads(q.resources or "[]");actions=json.loads(q.actions or "[]")
    if args.get("resource_id") and args["resource_id"] not in resources:return False,"Ressource non autorisée."
    if meta.risk=="ecriture":
        if q.access_level!="actions_confirm":return False,"Lecture seule : active les actions avec confirmation."
        if not q.write_until or q.write_until<datetime.utcnow():return False,"Autorisation d'écriture expirée."
        return True,"confirmation"
    return True,"lecture"

def tool_declarations(uid):
    out=[]
    for p in ("github","google","notion","render"):
        try:
            if not load_credential(uid,p):continue
        except Exception:continue
        c=get_connector(p)
        perm=_perm(uid,p)
        for a in c.spec.actions:
            if a.risk=="interdite":continue
            if a.risk=="ecriture" and (not perm or perm.access_level!="actions_confirm" or not perm.write_until or perm.write_until<datetime.utcnow()):continue
            out.append({"name":p+"_"+a.id,"description":a.description,"parameters":{"type":"object","properties":{"resource_id":{"type":"string"},"repo":{"type":"string"},"path":{"type":"string"},"query":{"type":"string"},"number":{"type":"integer"},"title":{"type":"string"},"body":{"type":"string"},"to":{"type":"string"},"subject":{"type":"string"},"service_id":{"type":"string"},"page_id":{"type":"string"},"parent_page_id":{"type":"string"}},"required":[]}})
    return out

def execute_tool(uid,name,args):
    if "_" not in name:raise PermissionError("Outil invalide.")
    provider,action=name.split("_",1);ok,why=_allowed(uid,provider,action,args)
    if not ok:raise PermissionError(why)
    cred=load_credential(uid,provider)
    if why=="confirmation":
        raw=secrets.token_urlsafe(32)
        with session_base() as db:db.add(ConnectorActionConfirmation(user_id=uid,provider=provider,action=action,parameters=json.dumps(args,ensure_ascii=False),token_hash=hashlib.sha256(raw.encode()).hexdigest(),expires_at=datetime.utcnow()+timedelta(seconds=120),consumed=False))
        return {"confirmation":{"token":raw,"expires_in":120,"provider":provider,"action":action,"parameters":args}}
    result=get_connector(provider).perform(cred["secret"],action,args)
    safe=json.loads(sanitize_external_content(result))
    with session_base() as db:db.add(ConnectorAuditLog(user_id=uid,plugin=provider,action=action,target=json.dumps(args,ensure_ascii=False)[:500],result="success"))
    return {"result":safe}
