from __future__ import annotations
import os,requests,base64
from urllib.parse import urlencode
from datetime import datetime,timedelta,timezone
from connectors.base import ConnectorAction,ConnectorAdapter,ConnectorSpec
from connectors.config import REQUEST_TIMEOUT_SECONDS

class GitHubConnector(ConnectorAdapter):
    spec=ConnectorSpec(
        "github","GitHub","Développement",
        "Dépôts, commits, issues, pull requests et fichiers de ton compte.",
        "oauth2",
        (),
        (
            ConnectorAction("list_repositories","Lister mes dépôts","lecture","Lister les dépôts autorisés par l'installation."),
            ConnectorAction("list_commits","Lire les commits","lecture","Lire les commits d'un dépôt autorisé."),
            ConnectorAction("list_issues","Lire les issues","lecture","Lire les issues d'un dépôt autorisé."),
            ConnectorAction("list_pull_requests","Lire les pull requests","lecture","Lire les pull requests d'un dépôt autorisé."),
            ConnectorAction("read_file","Lire un fichier","lecture","Lire un fichier texte."),
            ConnectorAction("create_issue","Créer une issue","ecriture","Créer une issue après confirmation."),
            ConnectorAction("comment_issue","Commenter une issue","ecriture","Commenter une issue après confirmation."),
            ConnectorAction("push_code","Pousser du code","interdite","Interdit."),
            ConnectorAction("merge_pull_request","Fusionner","interdite","Interdit."),
        ),
    )

    def _h(self,t):
        return {"Accept":"application/vnd.github+json","Authorization":f"Bearer {t}","X-GitHub-Api-Version":"2026-03-10"}

    def authorization_url(self,state,redirect_uri):
        cid=os.environ.get("GITHUB_APP_CLIENT_ID","")
        if not cid: raise RuntimeError("GitHub App non configurée.")
        return "https://github.com/login/oauth/authorize?"+urlencode({
            "client_id":cid,"redirect_uri":redirect_uri,"state":state
        })

    def install_url(self,state):
        slug=os.environ.get("GITHUB_APP_SLUG","")
        if not slug: raise RuntimeError("GitHub App non configurée.")
        return "https://github.com/apps/"+slug+"/installations/new?"+urlencode({"state":state})

    def exchange_code(self,code,redirect_uri):
        r=requests.post(
            "https://github.com/login/oauth/access_token",
            data={
                "client_id":os.environ.get("GITHUB_APP_CLIENT_ID"),
                "client_secret":os.environ.get("GITHUB_APP_CLIENT_SECRET"),
                "code":code,"redirect_uri":redirect_uri
            },
            headers={"Accept":"application/json"},
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
        r.raise_for_status();d=r.json()
        t=d.get("access_token")
        if not t: raise RuntimeError("GitHub App n'a pas fourni de jeton utilisateur.")
        exp=d.get("expires_in")
        refresh=d.get("refresh_token")
        expiration=datetime.now(timezone.utc)+timedelta(seconds=int(exp)) if exp else None
        m=requests.get("https://api.github.com/user",headers=self._h(t),timeout=REQUEST_TIMEOUT_SECONDS)
        m.raise_for_status()
        return {
            "access_token":t,"refresh_token":refresh,
            "scopes":d.get("scope","").split(",") if d.get("scope") else [],
            "expiration":expiration,"refresh_token_expires_in":d.get("refresh_token_expires_in"),
            "account":m.json()
        }

    def refresh(self,s):
        rt=s.get("refresh_token")
        if not rt:return None
        r=requests.post(
            "https://github.com/login/oauth/access_token",
            data={
                "client_id":os.environ.get("GITHUB_APP_CLIENT_ID"),
                "client_secret":os.environ.get("GITHUB_APP_CLIENT_SECRET"),
                "grant_type":"refresh_token","refresh_token":rt
            },
            headers={"Accept":"application/json"},
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
        r.raise_for_status();d=r.json()
        s["access_token"]=d["access_token"]
        if d.get("refresh_token"):s["refresh_token"]=d["refresh_token"]
        if d.get("expires_in"):s["expires_at"]=(datetime.now(timezone.utc)+timedelta(seconds=int(d["expires_in"]))).isoformat()
        return s

    def test_connection(self,s):
        r=requests.get("https://api.github.com/user",headers=self._h(s["access_token"]),timeout=REQUEST_TIMEOUT_SECONDS)
        r.raise_for_status();return {"login":r.json().get("login")}

    def revoke(self,s):
        cid,cs=os.environ.get("GITHUB_APP_CLIENT_ID",""),os.environ.get("GITHUB_APP_CLIENT_SECRET","")
        if not cid or not cs:return False
        r=requests.delete(
            f"https://api.github.com/applications/{cid}/token",
            auth=(cid,cs),json={"access_token":s["access_token"]},
            timeout=REQUEST_TIMEOUT_SECONDS
        )
        return r.status_code in (204,404)

    def list_resources(self,s):
        h=self._h(s["access_token"]);out=[]
        r=requests.get("https://api.github.com/user/installations",headers=h,params={"per_page":100},timeout=REQUEST_TIMEOUT_SECONDS)
        r.raise_for_status()
        for inst in r.json().get("installations",[]):
            iid=inst["id"]
            rr=requests.get(
                f"https://api.github.com/user/installations/{iid}/repositories",
                headers=h,params={"per_page":100},timeout=REQUEST_TIMEOUT_SECONDS
            )
            if rr.status_code==200:
                out += [{"id":x["full_name"],"label":x["full_name"],"type":"repository"} for x in rr.json().get("repositories",[])]
        return out

    def perform(self,s,a,p):
        t=s["access_token"];h=self._h(t);repo=p.get("repo","")
        if a=="list_repositories":return {"repositories":self.list_resources(s)}
        if not repo:raise ValueError("Dépôt requis.")
        if a=="list_commits":url=f"https://api.github.com/repos/{repo}/commits";q={"per_page":30}
        elif a=="list_issues":url=f"https://api.github.com/repos/{repo}/issues";q={"per_page":30,"state":"open"}
        elif a=="list_pull_requests":url=f"https://api.github.com/repos/{repo}/pulls";q={"per_page":30,"state":"open"}
        elif a=="read_file":url=f"https://api.github.com/repos/{repo}/contents/{p.get('path','')}";q={}
        elif a=="create_issue":
            r=requests.post(f"https://api.github.com/repos/{repo}/issues",headers=h,json={"title":p.get("title",""),"body":p.get("body","")},timeout=REQUEST_TIMEOUT_SECONDS)
            r.raise_for_status();return {"url":r.json().get("html_url"),"number":r.json().get("number")}
        elif a=="comment_issue":
            r=requests.post(f"https://api.github.com/repos/{repo}/issues/{p.get('number')}/comments",headers=h,json={"body":p.get("body","")},timeout=REQUEST_TIMEOUT_SECONDS)
            r.raise_for_status();return {"url":r.json().get("html_url")}
        else:raise PermissionError("Action GitHub interdite.")
        r=requests.get(url,headers=h,params=q,timeout=REQUEST_TIMEOUT_SECONDS);r.raise_for_status();d=r.json()
        if a=="read_file" and isinstance(d,dict):
            return {"path":d.get("path"),"content":base64.b64decode(d.get("content","")).decode("utf-8","replace")[:12000]}
        return {"items":d[:30] if isinstance(d,list) else d}
