import json
import os
import re
import requests
from core.utils import BASE_DIR, lire_json
from memory import se_souvenir_tout
from config import CLE_API

# --- Cache RAM pour charger_connaissances() ---
_cache_connaissances = None
_cache_mtime = None

# --- Session HTTP réutilisable (plus rapide que urllib à chaque appel) ---
_session = requests.Session()


def charger_connaissances():
    global _cache_connaissances, _cache_mtime

    chemin = BASE_DIR / "data" / "knowledge.json"
    try:
        mtime_actuel = os.path.getmtime(chemin)
    except OSError:
        return {}

    # Si le fichier n'a pas changé depuis le dernier chargement, on renvoie le cache
    if _cache_connaissances is not None and _cache_mtime == mtime_actuel:
        return _cache_connaissances

    _cache_connaissances = lire_json(chemin, {})
    if not isinstance(_cache_connaissances, dict):
        _cache_connaissances = {}
    _cache_mtime = mtime_actuel
    return _cache_connaissances


def nettoyer_reponse(texte):
    """Retire le Markdown brut (**, ###, *, ---, etc.) que Gemini renvoie parfois."""
    texte = re.sub(r"#{1,6}\s*", "", texte)  # titres ###
    texte = re.sub(r"\*\*(.*?)\*\*", r"\1", texte)  # gras **texte**
    texte = re.sub(r"\*(.*?)\*", r"\1", texte)  # italique *texte*
    texte = re.sub(r"^-{3,}$", "", texte, flags=re.MULTILINE)  # ---
    texte = re.sub(r"^\s*[-*]\s+", "- ", texte, flags=re.MULTILINE)  # listes
    return texte.strip()


def demander_a_lia(message, historique=None):
    if not CLE_API:
        return "La clé Gemini n'est pas configurée. Ajoute GEMINI_API_KEY dans le fichier .env."

    url = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        "gemini-3.5-flash-lite:generateContent?key=" + CLE_API
    )

    contents = []
    if historique:
        for msg in historique:
            role = "model" if msg.get("auteur") == "bot" else "user"
            contents.append({"role": role, "parts": [{"text": msg.get("texte", "")}]})
    else:
        contents.append({"role": "user", "parts": [{"text": message}]})

    corps = {
        "system_instruction": {
            "parts": [{
                "text": "Tu es Dashle, une IA personnelle créée par Owen. "
                        "Ne dis jamais que tu es Gemini ou que tu as été créé par Google. "
                        "Réponds toujours en tant que Dashle."
            }]
        },
        "contents": contents
    }

    try:
        reponse = _session.post(url, json=corps, timeout=15)
        reponse.raise_for_status()
        resultat = reponse.json()
        texte = resultat["candidates"][0]["content"]["parts"][0]["text"]
        return nettoyer_reponse(texte)
    except requests.exceptions.HTTPError as e:
        code = e.response.status_code if e.response is not None else None
        if code == 429:
            return "Le quota de Dashle est dépassé pour le moment. Réessaie dans quelques minutes."
        return "Le service IA est momentanément indisponible. Réessaie dans quelques instants."
    except Exception as e:
        return "Impossible de joindre le service IA. Vérifie la connexion puis réessaie."


def demander_a_lia_image(message, image_b64, mime_type):
    if not CLE_API:
        return "La clé Gemini n'est pas configurée. Ajoute GEMINI_API_KEY dans le fichier .env."

    url = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        "gemini-3.5-flash-lite:generateContent?key=" + CLE_API
    )
    corps = {
        "system_instruction": {
            "parts": [{
                "text": "Tu es Dashle, une IA personnelle créée par Owen. "
                        "Ne dis jamais que tu es Gemini ou que tu as été créé par Google. "
                        "Réponds toujours en tant que Dashle."
            }]
        },
        "contents": [{
            "parts": [
                {"text": message or "Décris cette image."},
                {"inline_data": {"mime_type": mime_type, "data": image_b64}}
            ]
        }]
    }
    try:
        reponse = _session.post(url, json=corps, timeout=30)
        reponse.raise_for_status()
        resultat = reponse.json()
        texte = resultat["candidates"][0]["content"]["parts"][0]["text"]
        return nettoyer_reponse(texte)
    except requests.exceptions.HTTPError as e:
        code = e.response.status_code if e.response is not None else None
        if code == 429:
            return "Le quota de Dashle est dépassé pour le moment. Réessaie dans quelques minutes."
        return "Le service IA est momentanément indisponible. Réessaie dans quelques instants."
    except Exception as e:
        return "Impossible de joindre le service IA. Vérifie la connexion puis réessaie."


def reflechir(message, historique=None, user_id=None):
    message_lower = message.lower().strip()
    connaissances = charger_connaissances()

    # Comparaison EXACTE (et non plus "in") pour éviter les faux positifs
    # quand une clé courte comme "nom" est une simple sous-chaîne du message.
    for question, reponse in connaissances.items():
        if question.strip().lower() == message_lower:
            return reponse

    appris = se_souvenir_tout(user_id)
    for question, reponse in appris.items():
        if question.strip().lower() == message_lower:
            return reponse

    return demander_a_lia(message, historique)
