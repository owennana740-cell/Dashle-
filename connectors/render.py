from __future__ import annotations
import requests
from connectors.base import ConnectorAction,ConnectorAdapter,ConnectorSpec
from connectors.config import REQUEST_TIMEOUT_SECONDS
class RenderConnector(ConnectorAdapter):
 spec=ConnectorSpec("render","Render","Développement","Services, déploiements et logs de ton compte Render.","personal_api_key",(),(ConnectorAction("list_services","Lister mes services","lecture","Lister les services."),ConnectorAction("list_deploys","Lire les déploiements","lecture","Lire les déploiements."),ConnectorAction("list_logs","Lire les logs","lecture","Lire les derniers logs."),ConnectorAction("trigger_deploy","Lancer un déploiement","ecriture","Lancer un déploiement après confirmation."),ConnectorAction("restart_service","Redémarrer un service","ecriture","Redémarrer un service après confirmation."),ConnectorAction("read_environment","Lire les variables","interdite","Interdit."),ConnectorAction("delete_service","Supprimer","interdite","Interdit."),ConnectorAction("suspend_service","Suspendre","interdite","Interdit.")),False)
 def _h(self,t):return {"Authorization":f"Bearer {t}","Accept":"application/json"}
 def authorization_url(self,*a,**k):raise RuntimeError("OAuth générique Render API non vérifié ; clé API personnelle uniquement.")
 def exchange_code(self,*a,**k):raise RuntimeError("OAuth Render non disponible.")
 def test_connection(self,s):r=requests.get("https://api.render.com/v1/workspaces",headers=self._h(s["api_key"]),params={"limit":1},timeout=REQUEST_TIMEOUT_SECONDS);r.raise_for_status();return {"ok":True}
 def revoke(self,s):return False
 def list_resources(self,s):
  r=requests.get("https://api.render.com/v1/services",headers=self._h(s["api_key"]),params={"limit":100},timeout=REQUEST_TIMEOUT_SECONDS);r.raise_for_status();return [{"id":"service:"+x["id"],"label":x.get("name",x["id"]),"type":"service"} for x in r.json()]
 def perform(self,s,a,p):
  h=self._h(s["api_key"]);sid=p.get("service_id","").removeprefix("service:")
  if a=="list_services":return {"services":self.list_resources(s)}
  if not sid:raise ValueError("Service Render requis.")
  if a=="list_deploys":r=requests.get(f"https://api.render.com/v1/services/{sid}/deploys",headers=h,params={"limit":20},timeout=REQUEST_TIMEOUT_SECONDS)
  elif a=="list_logs":r=requests.get("https://api.render.com/v1/logs",headers=h,params={"ownerId":p.get("owner_id",""),"resource":sid,"direction":"backward","limit":20},timeout=REQUEST_TIMEOUT_SECONDS)
  elif a=="trigger_deploy":r=requests.post(f"https://api.render.com/v1/services/{sid}/deploys",headers={**h,"Content-Type":"application/json"},json={"deployMode":"build_and_deploy","clearCache":"do_not_clear"},timeout=REQUEST_TIMEOUT_SECONDS)
  elif a=="restart_service":r=requests.post(f"https://api.render.com/v1/services/{sid}/restart",headers=h,timeout=REQUEST_TIMEOUT_SECONDS)
  else:raise PermissionError("Action Render interdite.")
  r.raise_for_status();return r.json() if r.content else {"ok":True}
