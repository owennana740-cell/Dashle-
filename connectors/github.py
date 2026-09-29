from __future__ import annotations
import os,requests,base64
from urllib.parse import urlencode
from connectors.base import ConnectorAction,ConnectorAdapter,ConnectorSpec
from connectors.config import REQUEST_TIMEOUT_SECONDS
class GitHubConnector(ConnectorAdapter):
 spec=ConnectorSpec("github","GitHub","Développement","Dépôts, commits, issues, pull requests et fichiers de ton compte.","oauth2",("read:user","user:email","repo"),(
 ConnectorAction("list_repositories","Lister mes dépôts","lecture","Lister les dépôts accessibles."),ConnectorAction("list_commits","Lire les commits","lecture","Lire les commits d'un dépôt."),ConnectorAction("list_issues","Lire les issues","lecture","Lire les issues d'un dépôt."),ConnectorAction("list_pull_requests","Lire les pull requests","lecture","Lire les pull requests d'un dépôt."),ConnectorAction("read_file","Lire un fichier","lecture","Lire un fichier texte."),ConnectorAction("create_issue","Créer une issue","ecriture","Créer une issue après confirmation."),ConnectorAction("comment_issue","Commenter une issue","ecriture","Commenter une issue après confirmation."),ConnectorAction("push_code","Pousser du code","interdite","Interdit."),ConnectorAction("merge_pull_request","Fusionner","interdite","Interdit.")))
 def _h(self,t):return {"Accept":"application/vnd.github+json","Authorization":f"Bearer {t}","X-GitHub-Api-Version":"2026-03-10"}
 def authorization_url(self,state,redirect_uri):
  cid=os.environ.get("GITHUB_OAUTH_CLIENT_ID","");
  if not cid:raise RuntimeError("GITHUB_OAUTH_CLIENT_ID manquant.")
  return "https://github.com/login/oauth/authorize?"+urlencode({"client_id":cid,"redirect_uri":redirect_uri,"scope":" ".join(self.spec.scopes),"state":state})
 def exchange_code(self,code,redirect_uri):
  r=requests.post("https://github.com/login/oauth/access_token",data={"client_id":os.environ.get("GITHUB_OAUTH_CLIENT_ID"),"client_secret":os.environ.get("GITHUB_OAUTH_CLIENT_SECRET"),"code":code,"redirect_uri":redirect_uri},headers={"Accept":"application/json"},timeout=REQUEST_TIMEOUT_SECONDS);r.raise_for_status();d=r.json()
  t=d.get("access_token");
  if not t:raise RuntimeError("GitHub n'a pas fourni de jeton.")
  m=requests.get("https://api.github.com/user",headers=self._h(t),timeout=REQUEST_TIMEOUT_SECONDS);m.raise_for_status()
  return {"access_token":t,"scopes":d.get("scope","").split(",") or list(self.spec.scopes),"account":m.json()}
 def test_connection(self,s):r=requests.get("https://api.github.com/user",headers=self._h(s["access_token"]),timeout=REQUEST_TIMEOUT_SECONDS);r.raise_for_status();return {"login":r.json().get("login")}
 def revoke(self,s):
  cid,cs=os.environ.get("GITHUB_OAUTH_CLIENT_ID",""),os.environ.get("GITHUB_OAUTH_CLIENT_SECRET","")
  if not cid or not cs:return False
  r=requests.delete(f"https://api.github.com/applications/{cid}/grant",auth=(cid,cs),json={"access_token":s["access_token"]},timeout=REQUEST_TIMEOUT_SECONDS);return r.status_code in (204,404)
 def list_resources(self,s):
  r=requests.get("https://api.github.com/user/repos",headers=self._h(s["access_token"]),params={"per_page":100,"affiliation":"owner,collaborator,organization_member"},timeout=REQUEST_TIMEOUT_SECONDS);r.raise_for_status();return [{"id":x["full_name"],"label":x["full_name"],"type":"repository"} for x in r.json()]
 def perform(self,s,a,p):
  t=s["access_token"];h=self._h(t);repo=p.get("repo","")
  if a=="list_repositories":return {"repositories":self.list_resources(s)}
  if not repo:raise ValueError("Dépôt requis.")
  if a=="list_commits":url=f"https://api.github.com/repos/{repo}/commits";q={"per_page":30}
  elif a=="list_issues":url=f"https://api.github.com/repos/{repo}/issues";q={"per_page":30,"state":"open"}
  elif a=="list_pull_requests":url=f"https://api.github.com/repos/{repo}/pulls";q={"per_page":30,"state":"open"}
  elif a=="read_file":url=f"https://api.github.com/repos/{repo}/contents/{p.get('path','')}";q={}
  elif a=="create_issue":
   r=requests.post(f"https://api.github.com/repos/{repo}/issues",headers=h,json={"title":p.get("title",""),"body":p.get("body","")},timeout=REQUEST_TIMEOUT_SECONDS);r.raise_for_status();return {"url":r.json().get("html_url"),"number":r.json().get("number")}
  elif a=="comment_issue":
   r=requests.post(f"https://api.github.com/repos/{repo}/issues/{p.get('number')}/comments",headers=h,json={"body":p.get("body","")},timeout=REQUEST_TIMEOUT_SECONDS);r.raise_for_status();return {"url":r.json().get("html_url")}
  else:raise PermissionError("Action GitHub interdite.")
  r=requests.get(url,headers=h,params=q,timeout=REQUEST_TIMEOUT_SECONDS);r.raise_for_status();d=r.json()
  if a=="read_file" and isinstance(d,dict):return {"path":d.get("path"),"content":base64.b64decode(d.get("content","")).decode("utf-8","replace")[:12000]}
  return {"items":d[:30] if isinstance(d,list) else d}
