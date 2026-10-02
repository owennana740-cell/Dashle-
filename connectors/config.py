import os

CONNECTORS_ENABLED=os.environ.get("CONNECTORS_ENABLED","0").strip()=="1"
PROVIDER_ENV={"github":"GITHUB","google":"GOOGLE","notion":"NOTION","render":"RENDER"}
CONNECTOR_MAX_BY_TIER={"free":0,"pro":2,"prime":99}
WRITE_CONFIRMATION_SECONDS=120
WRITE_ACCESS_SECONDS=3600
REQUEST_TIMEOUT_SECONDS=15
MAX_EXTERNAL_CONTENT_CHARS=12000

def provider_enabled(provider):
    if not CONNECTORS_ENABLED:
        return False
    return os.environ.get(f"CONNECTOR_{PROVIDER_ENV[provider]}_ENABLED","0").strip()=="1"

def provider_configured(provider):
    if not provider_enabled(provider):
        return False
    if not os.environ.get("DASHLE_CONNECTOR_FERNET_KEY","").strip():
        return False
    if provider=="github":
        return bool(os.environ.get("GITHUB_APP_CLIENT_ID","").strip() and os.environ.get("GITHUB_APP_CLIENT_SECRET","").strip() and os.environ.get("GITHUB_APP_SLUG","").strip())
    if provider=="google":
        return bool(os.environ.get("GOOGLE_OAUTH_CLIENT_ID","").strip() and os.environ.get("GOOGLE_OAUTH_CLIENT_SECRET","").strip())
    if provider=="notion":
        return False
    if provider=="render":
        return True
    return False
