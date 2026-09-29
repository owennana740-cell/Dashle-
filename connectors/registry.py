from connectors.github import GitHubConnector
from connectors.google import GoogleConnector
from connectors.notion import NotionConnector
from connectors.render import RenderConnector
CONNECTORS={"github":GitHubConnector(),"google":GoogleConnector(),"notion":NotionConnector(),"render":RenderConnector()}
def get_connector(provider):
    if provider not in CONNECTORS:raise ValueError("Fournisseur inconnu.")
    return CONNECTORS[provider]
def public_registry():
    return [{"id":c.spec.id,"name":c.spec.name,"category":c.spec.category,"description":c.spec.description,"auth_type":c.spec.auth_type,"scopes":list(c.spec.scopes),"enabled_by_default":c.spec.enabled_by_default,"actions":[{"id":a.id,"label":a.label,"risk":a.risk,"description":a.description} for a in c.spec.actions]} for c in CONNECTORS.values()]
