import json
import os
import re
import requests
from memory import se_souvenir_tout
from config import CLE_API

# --- Cache RAM pour charger_connaissances() ---
_cache_connaissances = None
_cache_mtime = None

# --- Session HTTP réutilisable (plus rapide que urllib à chaque appel) ---
_session = requests.Session()


def charger_connaissances():
    global _cache_connaissances, _cache_mtime

    chemin = os.path.join("data", "knowledge.json")
    mtime_actuel = os.path.getmtime(chemin)

    # Si le fichier n'a pas changé depuis le dernier chargement, on renvoie le cache
    if _cache_connaissances is not None and _cache_mtime == mtime_actuel:
        return _cache_connaissances

    with open(chemin, "r", encoding="utf-8") as fichier:
        _cache_connaissances = json.load(fichier)
        _cache_mtime = mtime_actuel
        return _cache_connaissances


def nettoyer_reponse(texte):
    """Retire le Markdown brut (**, ###, *, ---, etc.) que Gemini renvoie parfois."""
    texte = re.sub(r"#{1,6}\s*", "", texte)          # titres ###
    texte = re.sub(r"\*\*(.*?)\*\*", r"\1", texte)    # gras **texte**
    texte = re.sub(r"\*(.*?)\*", r"\1", texte)        # italique *texte*
    texte = re.sub(r"^-{3,}$", "", texte, flags=re.MULTILINE)  # ---
    texte = re.sub(r"^\s*[-*]\s+", "- ", texte, flags=re.MULTILINE)  # listes
    return texte.strip()


def demander_a_lia(message):
    url = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        "gemini-2.5-flash:generateContent?key=" + CLE_API
    )
    corps = {
        "contents": [{"parts": [{"text": message}]}]
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
        detail = e.response.text if e.response is not None else str(e)
        return "ERREUR HTTP " + str(code) + " : " + detail
    except Exception as e:
        return "ERREUR RÉELLE : " + str(e)


def reflechir(message):
    message_lower = message.lower().strip()
    connaissances = charger_connaissances()

    # Comparaison EXACTE (et non plus "in") pour éviter les faux positifs
    # quand une clé courte comme "nom" est une simple sous-chaîne du message.
    for question, reponse in connaissances.items():
        if question.strip().lower() == message_lower:
            return reponse

    appris = se_souvenir_tout()
    for question, reponse in appris.items():
        if question.strip().lower() == message_lower:
            return reponse

    return demander_a_lia(message)
