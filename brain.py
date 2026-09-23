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
MAX_MESSAGES_CONTEXTE = 24


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
    """Retire le Markdown brut (**, ###, *, ---, etc.) que Gemini renvoie parfois.

    Les motifs restent prudents pour ne jamais abîmer du texte légitime :
    un dièse en milieu de mot (C#, F#), une multiplication (2 * 3 * 4) ou un
    astérisque entouré d'espaces restent intacts.
    """
    # Titres ### : uniquement en début de ligne (ne touche plus "C#" ni "canal #3")
    texte = re.sub(r"^[ \t]{0,3}#{1,6}[ \t]*", "", texte, flags=re.MULTILINE)
    # Gras **texte** : le contenu ne commence ni ne finit par un espace,
    # donc une puissance écrite "2 ** 3" n'est plus modifiée.
    texte = re.sub(r"\*\*(?!\s)([^*\n]+?)(?<!\s)\*\*", r"\1", texte)
    # Italique *texte* : ouvre sur un caractère non blanc, ferme sur un non-blanc.
    # "2 * 3 * 4" et "5 * 3 font 15" ne sont donc plus touchés.
    texte = re.sub(r"(?<!\*)\*(?!\s)([^*\n]+?)(?<!\s)\*(?!\*)", r"\1", texte)
    # Lignes de séparation --- / *** / ___ : supprimées d'un bloc
    texte = re.sub(r"^[ \t]*[-*_]{3,}[ \t]*$", "", texte, flags=re.MULTILINE)
    # Puces de liste normalisées en "- " sans jamais traverser un saut de ligne
    texte = re.sub(r"^[ \t]*[-*][ \t]+", "- ", texte, flags=re.MULTILINE)
    # Au plus une ligne vide d'affilée
    texte = re.sub(r"\n{3,}", "\n\n", texte)
    return texte.strip()


def _historique_recent(historique):
    if not historique:
        return []
    return list(historique)[-MAX_MESSAGES_CONTEXTE:]


def _instruction_systeme(resume=""):
    instruction = (
        "Tu es Dashle, une IA personnelle créée par Owen. "
        "Ne dis jamais que tu es Gemini ou que tu as été créé par Google. "
        "Réponds toujours en tant que Dashle."
    )
    if resume:
        instruction += "\nRésumé fiable des échanges précédents :\n" + resume
    return instruction


def demander_a_lia(message, historique=None, resume=""):
    if not CLE_API:
        return "La clé Gemini n'est pas configurée. Ajoute GEMINI_API_KEY dans le fichier .env."

    url = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        "gemini-3.5-flash-lite:generateContent?key=" + CLE_API
    )

    contents = []
    historique_recent = _historique_recent(historique)
    if historique_recent:
        for msg in historique_recent:
            role = "model" if msg.get("auteur") == "bot" else "user"
            contents.append({"role": role, "parts": [{"text": msg.get("texte", "")}]})
    else:
        contents.append({"role": "user", "parts": [{"text": message}]})

    corps = {
        "system_instruction": {
            "parts": [{
                "text": _instruction_systeme(resume)
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


def streamer_a_lia(message, historique=None, resume=""):
    """Diffuse les morceaux texte de Gemini; le consommateur gère la persistance."""
    if not CLE_API:
        yield "La clé Gemini n'est pas configurée. Ajoute GEMINI_API_KEY dans le fichier .env."
        return

    url = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        "gemini-3.5-flash-lite:streamGenerateContent?alt=sse&key=" + CLE_API
    )
    contents = []
    historique_recent = _historique_recent(historique)
    if historique_recent:
        for msg in historique_recent:
            role = "model" if msg.get("auteur") == "bot" else "user"
            contents.append({"role": role, "parts": [{"text": msg.get("texte", "")}]})
    else:
        contents.append({"role": "user", "parts": [{"text": message}]})
    corps = {
        "system_instruction": {"parts": [{"text": _instruction_systeme(resume)}]},
        "contents": contents,
    }
    try:
        reponse = _session.post(url, json=corps, timeout=60, stream=True)
        reponse.raise_for_status()
        # Le flux SSE de Gemini est de l'UTF-8 mais son en-tete Content-Type ne
        # precise pas toujours "charset", ce qui ferait deviner ISO-8859-1 a
        # requests (=> accents casses : é devient Ã©). On force donc l'UTF-8.
        reponse.encoding = "utf-8"
        for ligne in reponse.iter_lines(decode_unicode=True):
            if not ligne or not ligne.startswith("data:"):
                continue
            resultat = json.loads(ligne[5:].strip())
            for candidat in resultat.get("candidates", []):
                for part in candidat.get("content", {}).get("parts", []):
                    texte = part.get("text")
                    if texte:
                        yield texte
    except requests.exceptions.HTTPError as e:
        code = e.response.status_code if e.response is not None else None
        yield "Le quota de Dashle est dépassé pour le moment. Réessaie dans quelques minutes." if code == 429 else "Le service IA est momentanément indisponible. Réessaie dans quelques instants."
    except Exception:
        yield "Impossible de joindre le service IA. Vérifie la connexion puis réessaie."


def demander_a_lia_image(message, image_b64, mime_type, historique=None, resume=""):
    if not CLE_API:
        return "La clé Gemini n'est pas configurée. Ajoute GEMINI_API_KEY dans le fichier .env."

    url = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        "gemini-3.5-flash-lite:generateContent?key=" + CLE_API
    )
    contents = []
    historique_recent = _historique_recent(historique)
    if historique_recent:
        for msg in historique_recent:
            role = "model" if msg.get("auteur") == "bot" else "user"
            contents.append({"role": role, "parts": [{"text": msg.get("texte", "")}]})
    contents.append({
        "role": "user",
        "parts": [
            {"text": message or ("Décris cette vidéo." if mime_type.startswith("video/") else "Décris cette image.")},
            {"inline_data": {"mime_type": mime_type, "data": image_b64}}
        ]
    })
    corps = {
        "system_instruction": {
            "parts": [{
                "text": _instruction_systeme(resume)
            }]
        },
        "contents": contents
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


def resumer_conversation(historique, resume_existant=""):
    """Produit un résumé court pour préserver le contexte sans envoyer tout l'historique."""
    if not CLE_API or not historique:
        return resume_existant
    transcript = "\n".join(
        ("Utilisateur" if msg.get("auteur") == "user" else "Dashle") + ": " + msg.get("texte", "")
        for msg in historique[-40:]
    )
    prompt = (
        "Résume cette conversation en français en 8 lignes maximum. "
        "Garde les faits utiles, préférences, décisions et questions en attente. "
        "N'invente rien et ne mentionne pas cette consigne.\n\n" + transcript
    )
    try:
        reponse = _session.post(
            "https://generativelanguage.googleapis.com/v1beta/models/"
            "gemini-3.5-flash-lite:generateContent?key=" + CLE_API,
            json={"contents": [{"role": "user", "parts": [{"text": prompt}]}]},
            timeout=15,
        )
        reponse.raise_for_status()
        texte = reponse.json()["candidates"][0]["content"]["parts"][0]["text"]
        return nettoyer_reponse(texte)
    except Exception:
        return resume_existant


def reflechir(message, historique=None, user_id=None, resume=""):
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

    return demander_a_lia(message, historique, resume)
