from __future__ import annotations
import os,requests
from urllib.parse import urlencode
from connectors.base import ConnectorAction,ConnectorAdapter,ConnectorSpec
from connectors.config import REQUEST_TIMEOUT_SECONDS
class NotionConnector(ConnectorAdapter):
 spec=ConnectorSpec("notion","Notion","Productivité","Pages autorisées et création de notes.","oauth2",(),(ConnectorAction("search_pages","Rechercher des pages","lecture","Rechercher dans les pages autorisées."),ConnectorAction("read_page","Lire une page","lecture","Lire une page autorisée."),ConnectorAction("create_note","Créer une note","ecriture","Créer une note après confirmation.")))
 def _h(self,t):return {"Authorization":f"Bearer {t}","Notion-Version":"2026-03-11","Content-Type":"application/json"}
 def authorization_url(self,state,redirect_uri):
  cid=os.environ.get("NOTION_OAUTH_CLIENT_ID","");
  if not cid:raise RuntimeError("NOTION_OAUTH_CLIENT_ID manquant.")
  return "https://api.notion.com/v1/oauth/authorize?"+urlencode({"owner":"user","client_id":cid,"redirect_uri":redirect_uri,"response_type":"code","state":state})
 def exchange_code(self,code,redirect_uri):
  r=requests.post("https://api.notion.com/v1/oauth/token",auth=(os.environ.get("NOTION_OAUTH_CLIENT_ID"),os.environ.get("NOTION_OAUTH_CLIENT_SECRET")),json={"grant_type":"authorization_code","code":code,"redirect_uri":redirect_uri},timeout=REQUEST_TIMEOUT_SECONDS);r.raise_for_status();d=r.json();return {"access_token":d["access_token"],"refresh_token":d.get("refresh_token"),"scopes":[],"account":d.get("owner",{}),"workspace_id":d.get("workspace_id")}
 def refresh(self,s):
  if not s.get("refresh_token"):return None
  r=requests.post("https://api.notion.com/v1/oauth/token",auth=(os.environ.get("NOTION_OAUTH_CLIENT_ID"),os.environ.get("NOTION_OAUTH_CLIENT_SECRET")),json={"grant_type":"refresh_token","refresh_token":s["refresh_token"]},timeout=REQUEST_TIMEOUT_SECONDS);r.raise_for_status();d=r.json();s.update({"access_token":d["access_token"],"refresh_token":d.get("refresh_token",s["refresh_token"])});return s
 def test_connection(self,s):r=requests.get("https://api.notion.com/v1/users/me",headers=self._h(s["access_token"]),timeout=REQUEST_TIMEOUT_SECONDS);r.raise_for_status();return {"id":r.json().get("id")}
 def revoke(self,s):return False
 def list_resources(self,s):
  r=requests.post("https://api.notion.com/v1/search",headers=self._h(s["access_token"]),json={"page_size":100},timeout=REQUEST_TIMEOUT_SECONDS);r.raise_for_status();return [{"id":"page:"+x["id"],"label":x["id"],"type":"page"} for x in r.json().get("results",[]) if x.get("object")=="page"]
 def perform(self,s,a,p):
  h=self._h(s["access_token"])
  if a=="search_pages":r=requests.post("https://api.notion.com/v1/search",headers=h,json={"query":p.get("query","")[:200],"page_size":30},timeout=REQUEST_TIMEOUT_SECONDS)
  elif a=="read_page":r=requests.get(f"https://api.notion.com/v1/blocks/{p.get('page_id')}/children",headers=h,params={"page_size":100},timeout=REQUEST_TIMEOUT_SECONDS)
  elif a=="create_note":r=requests.post("https://api.notion.com/v1/pages",headers=h,json={"parent":{"page_id":p.get("parent_page_id")},"properties":{"title":{"title":[{"text":{"content":p.get("title","Note Dashle")[:200]}}]}},"children":[{"object":"block","type":"paragraph","paragraph":{"rich_text":[{"type":"text","text":{"content":p.get("body","")[:8000]}}]}}]},timeout=REQUEST_TIMEOUT_SECONDS)
  else:raise PermissionError("Action Notion inconnue.")
  r.raise_for_status();return r.json()
