from __future__ import annotations
import json,requests
from config import CLE_API,MODELE_GEMINI
from connectors.runtime import execute_tool,tool_declarations

def _url():return f"https://generativelanguage.googleapis.com/v1beta/models/{MODELE_GEMINI}:generateContent?key={CLE_API}"
def preflight(message,user_id):
    if not user_id or not CLE_API:return None
    tools=tool_declarations(user_id)
    if not tools:return None
    lower=message.lower()
    if not any(x in lower for x in ("github","git hub","render","notion","google drive","gmail","agenda","dépôt","issue","pull request")):return None
    body={"contents":[{"role":"user","parts":[{"text":message[:12000]}]}],"tools":[{"function_declarations":tools}],"generationConfig":{"temperature":0.2,"maxOutputTokens":1024}}
    try:
        r=requests.post(_url(),json=body,timeout=20);r.raise_for_status();data=r.json()
        parts=data.get("candidates",[{}])[0].get("content",{}).get("parts",[])
        call=next((p.get("functionCall") for p in parts if p.get("functionCall")),None)
        if not call:return None
        name=call.get("name","");args=call.get("args") or {}
        outcome=execute_tool(user_id,name,args)
        if "confirmation" in outcome:
            return "__DASHLE_CONNECTOR_CONFIRMATION__"+json.dumps(outcome["confirmation"],ensure_ascii=False)
        model_content={"role":"model","parts":[{"functionCall":call}]}
        result_content={"role":"user","parts":[{"functionResponse":{"name":name,"response":{"result":outcome["result"]}}}]}
        follow={"contents":[{"role":"user","parts":[{"text":message[:12000]}]},model_content,result_content],"generationConfig":{"temperature":0.4,"maxOutputTokens":2048}}
        rr=requests.post(_url(),json=follow,timeout=20);rr.raise_for_status()
        return rr.json()["candidates"][0]["content"]["parts"][0].get("text","").strip()
    except (requests.RequestException,KeyError,IndexError,TypeError,ValueError):
        return "Je n’ai pas pu utiliser ce connecteur pour cette demande. Vérifie la connexion et les autorisations."
