from __future__ import annotations
import hashlib, json, os, secrets
from datetime import datetime, timedelta
from flask import Blueprint, jsonify, redirect, request, session, render_template
from connectors.config import CONNECTOR_MAX_BY_TIER, WRITE_ACCESS_SECONDS
from connectors.registry import get_connector, public_registry
from connectors.security import consume_oauth_state, delete_credential, issue_oauth_state, load_credential, save_credential
from database import ConnectorActionConfirmation, ConnectorAuditLog, ConnectorCredential, ConnectorPermission, MobileOAuthHandoff, OAuthState, session_base
from brain import niveau_abonnement
from database import User

bp=Blueprint("connectors",__name__)

def _uid(): return session.get("user_id")
def _csrf():
    expected=session.get("csrf_token")
    supplied=request.headers.get("X-CSRF-Token") or request.form.get("csrf_token") or (request.get_json(silent=True) or {}).get("csrf_token")
    return bool(expected and supplied and secrets.compare_digest(str(expected),str(supplied)))
def _base(): return os.environ.get("DASHLE_PUBLIC_URL","https://dashle.onrender.com").rstrip("/")
def _tier_ok(uid,provider):
    with session_base() as db:
        user=db.get(User,uid)
        if not user:return False
        limit=CONNECTOR_MAX_BY_TIER.get(niveau_abonnement(user),0)
        count=db.query(ConnectorPermission).filter_by(user_id=uid).count()
        return limit>=99 or count<limit or db.query(ConnectorPermission).filter_by(user_id=uid,provider=provider).first() is not None
def _perm(uid,provider):
    with session_base() as db:
        p=db.query(ConnectorPermission).filter_by(user_id=uid,provider=provider).one_or_none()
        return {"access_level":p.access_level if p else "read_only","resources":json.loads(p.resources or "[]") if p else [],"actions":json.loads(p.actions or "[]") if p else [],"write_until":p.write_until if p else None}



@bp.get("/plugins")
def plugins_page():
    uid=_uid()
    if not uid:return redirect("/")
    with session_base() as db: connected={x.provider for x in db.query(ConnectorCredential).filter_by(user_id=uid).all()}
    return render_template("connecteurs.html",registry=public_registry(),connected=connected,csrf_token=session.get("csrf_token",""))

@bp.get("/mes-connexions")
def connections_page():
    return plugins_page()

@bp.get("/api/connecteurs")
def registry():
    return jsonify({"connecteurs":public_registry()}) if _uid() else (jsonify({"erreur":"Connexion requise."}),401)

@bp.get("/api/connecteurs/<provider>/oauth/start")
def oauth_start(provider):
    uid=_uid()
    if not uid:return jsonify({"erreur":"Connexion requise."}),401
    c=get_connector(provider)
    if c.spec.auth_type!="oauth2":return jsonify({"erreur":"Ce fournisseur utilise une clé API personnelle."}),400
    if not _tier_ok(uid,provider):return jsonify({"erreur":"Ton forfait ne permet pas ce connecteur."}),403
    state=issue_oauth_state(uid,provider)
    return redirect(c.authorization_url(state,f"{_base()}/oauth/{provider}/callback"))

@bp.get("/oauth/<provider>/callback")
def oauth_callback(provider):
    state,code=request.args.get("state",""),request.args.get("code","")
    if not state or not code:return "OAuth invalide.",400
    with session_base() as db:
        row=db.query(OAuthState).filter_by(provider=provider,state_hash=hashlib.sha256(state.encode()).hexdigest(),used=False).one_or_none()
        uid=row.user_id if row else None
    if not uid or not consume_oauth_state(uid,provider,state):return "État OAuth invalide ou rejoué.",400
    try:
        data=get_connector(provider).exchange_code(code,f"{_base()}/oauth/{provider}/callback")
        secret={k:v for k,v in data.items() if k not in {"scopes","account","expiration"}}
        save_credential(uid,provider,secret,data.get("scopes",[]),data.get("expiration"),data.get("account"))
        handoff=secrets.token_urlsafe(32)
        with session_base() as db:db.add(MobileOAuthHandoff(user_id=uid,provider=provider,token_hash=hashlib.sha256(handoff.encode()).hexdigest(),expires_at=datetime.utcnow()+timedelta(minutes=5),used=False))
        return redirect(f"{_base()}/oauth/mobile-complete?handoff={handoff}")
    except Exception:return "Impossible de terminer la connexion.",502

@bp.get("/oauth/mobile-complete")
def mobile_complete():
    token=request.args.get("handoff","")
    with session_base() as db:
        row=db.query(MobileOAuthHandoff).filter_by(token_hash=hashlib.sha256(token.encode()).hexdigest(),used=False).one_or_none()
        if not row or row.expires_at<datetime.utcnow():return "Lien expiré.",400
        provider=row.provider
    return redirect(f"dashle://oauth-complete?provider={provider}&handoff={token}")

@bp.get("/api/oauth/mobile/consume")
def mobile_consume():
    uid=_uid();token=request.args.get("handoff","")
    if not uid:return jsonify({"erreur":"Connexion requise."}),401
    with session_base() as db:
        row=db.query(MobileOAuthHandoff).filter_by(user_id=uid,token_hash=hashlib.sha256(token.encode()).hexdigest(),used=False).one_or_none()
        if not row or row.expires_at<datetime.utcnow():return jsonify({"erreur":"Lien expiré ou déjà utilisé."}),400
        row.used=True
    return jsonify({"ok":True})

@bp.post("/api/connecteurs/<provider>/api-key")
def api_key(provider):
    uid=_uid();data=request.get_json(silent=True) or {}
    if not uid or not _csrf():return jsonify({"erreur":"Requête non autorisée."}),403
    if get_connector(provider).spec.auth_type!="personal_api_key":return jsonify({"erreur":"Ce fournisseur utilise OAuth."}),400
    key=str(data.get("api_key","")).strip()
    if not key:return jsonify({"erreur":"Clé API manquante."}),400
    try:account=get_connector(provider).test_connection({"api_key":key})
    except Exception:return jsonify({"erreur":"Clé personnelle refusée ou invalide."}),400
    save_credential(uid,provider,{"api_key":key},[],None,account)
    return jsonify({"ok":True})

@bp.get("/api/connecteurs/<provider>/resources")
def resources(provider):
    uid=_uid();cred=load_credential(uid,provider) if uid else None
    if not cred:return jsonify({"erreur":"Connexion absente."}),404
    try:return jsonify({"resources":get_connector(provider).list_resources(cred["secret"])})
    except Exception:return jsonify({"erreur":"Impossible de lire les ressources."}),502

@bp.post("/api/connecteurs/<provider>/permissions")
def permissions(provider):
    uid=_uid();data=request.get_json(silent=True) or {}
    if not uid or not _csrf():return jsonify({"erreur":"Requête non autorisée."}),403
    level=data.get("access_level","read_only")
    if level not in {"read_only","actions_confirm"}:return jsonify({"erreur":"Niveau invalide."}),400
    valid={a.id for a in get_connector(provider).spec.actions if a.risk!="interdite"}
    actions=[x for x in data.get("actions",[]) if x in valid]
    resources=[str(x)[:200] for x in data.get("resources",[])][:500]
    until=datetime.utcnow()+timedelta(seconds=WRITE_ACCESS_SECONDS) if level=="actions_confirm" else None
    with session_base() as db:
        p=db.query(ConnectorPermission).filter_by(user_id=uid,provider=provider).one_or_none()
        if not p:p=ConnectorPermission(user_id=uid,provider=provider);db.add(p)
        p.access_level=level;p.resources=json.dumps(resources);p.actions=json.dumps(actions);p.write_until=until
    return jsonify({"ok":True})

@bp.post("/api/connecteurs/<provider>/disconnect")
def disconnect(provider):
    uid=_uid()
    if not uid or not _csrf():return jsonify({"erreur":"Requête non autorisée."}),403
    cred=load_credential(uid,provider)
    if cred:
        try:get_connector(provider).revoke(cred["secret"])
        except Exception:pass
    delete_credential(uid,provider)
    with session_base() as db:db.add(ConnectorAuditLog(user_id=uid,plugin=provider,action="disconnect",target=provider,result="success"))
    return jsonify({"ok":True})

@bp.post("/api/connecteurs/<provider>/action")
def action(provider):
    uid=_uid();data=request.get_json(silent=True) or {}
    if not uid or not _csrf():return jsonify({"erreur":"Requête non autorisée."}),403
    a=data.get("action","");params=data.get("parameters") or {};meta=next((x for x in get_connector(provider).spec.actions if x.id==a),None)
    if not meta or meta.risk=="interdite":return jsonify({"erreur":"Cette action est interdite."}),403
    p=_perm(uid,provider)
    if params.get("resource_id") and params["resource_id"] not in p["resources"]:return jsonify({"erreur":"Ressource non autorisée."}),403
    if meta.risk=="ecriture":
        if p["access_level"]!="actions_confirm" or not p["write_until"] or p["write_until"]<datetime.utcnow():return jsonify({"erreur":"Accès en écriture non autorisé ou expiré."}),403
        raw=secrets.token_urlsafe(32)
        with session_base() as db:db.add(ConnectorActionConfirmation(user_id=uid,provider=provider,action=a,parameters=json.dumps(params),token_hash=hashlib.sha256(raw.encode()).hexdigest(),expires_at=datetime.utcnow()+timedelta(seconds=120),consumed=False))
        return jsonify({"confirmation":{"token":raw,"expires_in":120,"action":a,"parameters":params}})
    cred=load_credential(uid,provider)
    try:
        result=get_connector(provider).perform(cred["secret"],a,params)
        with session_base() as db:db.add(ConnectorAuditLog(user_id=uid,plugin=provider,action=a,target=json.dumps(params)[:500],result="success"))
        return jsonify({"resultat":result})
    except Exception:return jsonify({"erreur":"Le fournisseur a renvoyé une erreur."}),502

@bp.post("/api/connecteurs/confirm")
def confirm():
    uid=_uid();data=request.get_json(silent=True) or {}
    if not uid or not _csrf():return jsonify({"erreur":"Requête non autorisée."}),403
    token=data.get("token","")
    with session_base() as db:
        row=db.query(ConnectorActionConfirmation).filter_by(user_id=uid,token_hash=hashlib.sha256(token.encode()).hexdigest(),consumed=False).one_or_none()
        if not row or row.expires_at<datetime.utcnow():return jsonify({"erreur":"La confirmation a expiré."}),403
        row.consumed=True;provider,action,params=row.provider,row.action,json.loads(row.parameters)
    cred=load_credential(uid,provider)
    try:
        result=get_connector(provider).perform(cred["secret"],action,params)
        with session_base() as db:db.add(ConnectorAuditLog(user_id=uid,plugin=provider,action=action,target=json.dumps(params)[:500],result="success"))
        return jsonify({"resultat":result})
    except Exception:return jsonify({"erreur":"L'action externe a échoué."}),502


@bp.get("/.well-known/assetlinks.json")
def assetlinks():
    fingerprint=os.environ.get("ANDROID_RELEASE_CERT_SHA256","").replace(":","").upper()
    if not fingerprint:
        return jsonify([])
    return jsonify([{"relation":["delegate_permission/common.handle_all_urls"],"target":{"namespace":"android_app","package_name":"com.dashle.app","sha256_cert_fingerprints":[fingerprint]}}])
