from __future__ import annotations
import hashlib,json,os,re,secrets
from datetime import datetime,timedelta,timezone
from cryptography.fernet import Fernet,InvalidToken
from database import ConnectorCredential,ConnectorPermission,OAuthState,session_base
from connectors.config import MAX_EXTERNAL_CONTENT_CHARS

def _fernet():
    key=os.environ.get("DASHLE_CONNECTOR_FERNET_KEY","").strip()
    if not key: raise RuntimeError("DASHLE_CONNECTOR_FERNET_KEY n'est pas configurée.")
    return Fernet(key.encode())

def encrypt_secret(v): return _fernet().encrypt(json.dumps(v,separators=(",",":"),ensure_ascii=False).encode()).decode()

def decrypt_secret(v):
    try:return json.loads(_fernet().decrypt(v.encode()).decode())
    except (InvalidToken,ValueError,TypeError,json.JSONDecodeError) as e:raise RuntimeError("Identifiants de connecteur illisibles.") from e

def hash_token(v):return hashlib.sha256(v.encode()).hexdigest()

def sanitize_external_content(v,limit=MAX_EXTERNAL_CONTENT_CHARS):
    s=v if isinstance(v,str) else json.dumps(v,ensure_ascii=False,default=str)
    patterns=[
        (r"(?i)(authorization\s*:\s*bearer\s+)[A-Za-z0-9._~+/=-]+",r"\1[MASQUÉ]"),
        (r"(?i)(api[_ -]?key|password|passwd|secret|token|refresh_token|client_secret)(\s*[:=]\s*)[^\s,;]+",r"\1\2[MASQUÉ]"),
        (r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----","[CLÉ PRIVÉE MASQUÉE]"),
        (r"(?i)\b(ghu|ghr|ghp|github_pat|sk_live|AIza)[A-Za-z0-9_\-]{12,}\b","[SECRET MASQUÉ]"),
    ]
    for p,r in patterns:s=re.sub(p,r,s,flags=re.DOTALL)
    return s[:limit]

def save_credential(user_id,provider,secret,scopes,expiration=None,account=None):
    if account:secret={**secret,"account":account}
    with session_base() as db:
        row=db.query(ConnectorCredential).filter_by(user_id=user_id,provider=provider).one_or_none()
        if row is None:db.add(ConnectorCredential(user_id=user_id,provider=provider,secret_encrypted=encrypt_secret(secret),scopes=json.dumps(scopes),expiration=expiration))
        else:row.secret_encrypted=encrypt_secret(secret);row.scopes=json.dumps(scopes);row.expiration=expiration
        if db.query(ConnectorPermission).filter_by(user_id=user_id,provider=provider).one_or_none() is None:
            db.add(ConnectorPermission(user_id=user_id,provider=provider,access_level="read_only",resources="[]",actions="[]"))

def load_credential(user_id,provider):
    with session_base() as db:
        row=db.query(ConnectorCredential).filter_by(user_id=user_id,provider=provider).one_or_none()
        if not row:return None
        secret=decrypt_secret(row.secret_encrypted);expiration=row.expiration
    if expiration:
        now=datetime.utcnow()
        if expiration.replace(tzinfo=None)<=now:
            try:
                from connectors.registry import get_connector
                connector=get_connector(provider)
                refreshed=connector.refresh(secret)
                if not refreshed:raise RuntimeError("Jeton expiré.")
                new_exp=refreshed.get("expiration")
                if isinstance(new_exp,str):
                    new_exp=datetime.fromisoformat(new_exp.replace("Z","+00:00")).replace(tzinfo=None)
                save_credential(user_id,provider,refreshed,refreshed.get("scopes",[]),new_exp,refreshed.get("account"))
                secret=refreshed;expiration=new_exp
            except Exception as exc:
                raise RuntimeError("Connexion expirée, reconnecte-toi.") from exc
    return {"secret":secret,"scopes":json.loads(row.scopes or "[]"),"expiration":expiration}

def delete_credential(user_id,provider):
    with session_base() as db:
        r=db.query(ConnectorCredential).filter_by(user_id=user_id,provider=provider).one_or_none()
        if r:db.delete(r)
        p=db.query(ConnectorPermission).filter_by(user_id=user_id,provider=provider).one_or_none()
        if p:db.delete(p)

def issue_oauth_state(user_id,provider):
    raw=secrets.token_urlsafe(32)
    with session_base() as db:db.add(OAuthState(user_id=user_id,provider=provider,state_hash=hash_token(raw),expires_at=datetime.utcnow()+timedelta(minutes=10),used=False))
    return raw

def consume_oauth_state(user_id,provider,raw):
    with session_base() as db:
        r=db.query(OAuthState).filter_by(user_id=user_id,provider=provider,state_hash=hash_token(raw),used=False).one_or_none()
        if not r or r.expires_at<datetime.utcnow():return False
        r.used=True;return True
