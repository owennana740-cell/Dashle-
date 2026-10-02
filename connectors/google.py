from __future__ import annotations
import os,requests,base64
from urllib.parse import urlencode
from datetime import datetime,timedelta,timezone
from connectors.base import ConnectorAction,ConnectorAdapter,ConnectorSpec
from connectors.config import REQUEST_TIMEOUT_SECONDS
class GoogleConnector(ConnectorAdapter):
 spec=ConnectorSpec("google","Google","Productivité","Drive et Agenda en lecture seule ; Gmail en lecture et brouillons.","oauth2",("openid","email","https://www.googleapis.com/auth/drive.metadata.readonly","https://www.googleapis.com/auth/calendar.readonly","https://www.googleapis.com/auth/gmail.readonly","https://www.googleapis.com/auth/gmail.compose"),(
 ConnectorAction("list_drive","Lister Drive","lecture","Lister les fichiers autorisés."),ConnectorAction("list_calendars","Lister les agendas","lecture","Lister les calendriers autorisés."),ConnectorAction("list_gmail","Lire Gmail","lecture","Lire les messages autorisés."),ConnectorAction("create_gmail_draft","Créer un brouillon Gmail","ecriture","Créer un brouillon, jamais l'envoyer."),ConnectorAction("send_gmail","Envoyer un e-mail","interdite","Envoi automatique interdit.")))
 def authorization_url(self,state,redirect_uri):
  cid=os.environ.get("GOOGLE_OAUTH_CLIENT_ID","");
  if not cid:raise RuntimeError("GOOGLE_OAUTH_CLIENT_ID manquant.")
  return "https://accounts.google.com/o/oauth2/v2/auth?"+urlencode({"client_id":cid,"redirect_uri":redirect_uri,"response_type":"code","scope":" ".join(self.spec.scopes),"access_type":"offline","prompt":"consent","include_granted_scopes":"true","state":state})
 def exchange_code(self,code,redirect_uri):
  r=requests.post("https://oauth2.googleapis.com/token",data={"code":code,"client_id":os.environ.get("GOOGLE_OAUTH_CLIENT_ID"),"client_secret":os.environ.get("GOOGLE_OAUTH_CLIENT_SECRET"),"redirect_uri":redirect_uri,"grant_type":"authorization_code"},timeout=REQUEST_TIMEOUT_SECONDS);r.raise_for_status();d=r.json();return {"access_token":d["access_token"],"refresh_token":d.get("refresh_token"),"scopes":d.get("scope","").split(),"expiration":datetime.now(timezone.utc)+timedelta(seconds=int(d.get("expires_in",3600)))}
 def refresh(self,s):
  if not s.get("refresh_token"):return None
  r=requests.post("https://oauth2.googleapis.com/token",data={"client_id":os.environ.get("GOOGLE_OAUTH_CLIENT_ID"),"client_secret":os.environ.get("GOOGLE_OAUTH_CLIENT_SECRET"),"refresh_token":s["refresh_token"],"grant_type":"refresh_token"},timeout=REQUEST_TIMEOUT_SECONDS);r.raise_for_status();d=r.json();s["access_token"]=d["access_token"];s["expiration"]=datetime.now(timezone.utc)+timedelta(seconds=int(d.get("expires_in",3600)));return s
 def _h(self,t):return {"Authorization":f"Bearer {t}","Accept":"application/json"}
 def test_connection(self,s):r=requests.get("https://www.googleapis.com/oauth2/v3/userinfo",headers=self._h(s["access_token"]),timeout=REQUEST_TIMEOUT_SECONDS);r.raise_for_status();return {"email":r.json().get("email")}
 def revoke(self,s):
  t=s.get("refresh_token") or s.get("access_token");r=requests.post("https://oauth2.googleapis.com/revoke",params={"token":t},headers={"content-type":"application/x-www-form-urlencoded"},timeout=REQUEST_TIMEOUT_SECONDS);return r.status_code==200
 def list_resources(self,s):
  h=self._h(s["access_token"]);out=[]
  r=requests.get("https://www.googleapis.com/drive/v3/files",headers=h,params={"pageSize":100,"fields":"files(id,name,mimeType,modifiedTime,webViewLink)"},timeout=REQUEST_TIMEOUT_SECONDS);r.raise_for_status();out += [{"id":"drive:"+x["id"],"label":x.get("name",x["id"]),"type":"drive"} for x in r.json().get("files",[])]
  r=requests.get("https://www.googleapis.com/calendar/v3/users/me/calendarList",headers=h,params={"maxResults":100},timeout=REQUEST_TIMEOUT_SECONDS);r.raise_for_status();out += [{"id":"calendar:"+x["id"],"label":x.get("summary",x["id"]),"type":"calendar"} for x in r.json().get("items",[])]
  return out
 def perform(self,s,a,p):
  h=self._h(s["access_token"])
  if a=="list_drive":r=requests.get("https://www.googleapis.com/drive/v3/files",headers=h,params={"pageSize":50,"fields":"files(id,name,mimeType,modifiedTime,webViewLink)"},timeout=REQUEST_TIMEOUT_SECONDS)
  elif a=="list_calendars":r=requests.get("https://www.googleapis.com/calendar/v3/users/me/calendarList",headers=h,params={"maxResults":50},timeout=REQUEST_TIMEOUT_SECONDS)
  elif a=="list_gmail":r=requests.get("https://gmail.googleapis.com/gmail/v1/users/me/messages",headers=h,params={"maxResults":30},timeout=REQUEST_TIMEOUT_SECONDS)
  elif a=="create_gmail_draft":
   raw=f"To: {p.get('to','')}\\r\\nSubject: {p.get('subject','')}\\r\\nContent-Type: text/plain; charset=utf-8\\r\\n\\r\\n{p.get('body','')}";enc=base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=");r=requests.post("https://gmail.googleapis.com/gmail/v1/users/me/drafts",headers=h,json={"message":{"raw":enc}},timeout=REQUEST_TIMEOUT_SECONDS)
  else:raise PermissionError("Action Google interdite.")
  r.raise_for_status();return r.json()
