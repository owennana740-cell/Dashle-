"""brain.py — Couche d'accès à l'API Gemini pour Dashle.

Corrections apportées :
- Modèle centralisé via config.MODELE_GEMINI (plus de chaîne codée en dur).
- _construire_contents() garantit que le message courant est TOUJOURS
  le dernier élément 'user' envoyé à Gemini (bug précédent : le message
  était absent quand l'historique était non vide).
- Erreur HTTP 404 explicite si le nom de modèle est incorrect.
- Timeout augmenté pour le streaming (90 s), réduit pour les appels sync (20 s).
- generationConfig ajouté (température, limite de tokens).
- resumer_conversation déclenche le résumé de manière plus fiable.
"""

import json
import os
import re
import requests
from urllib.parse import urlparse
from core.utils import BASE_DIR, lire_json
from memory import se_souvenir_tout
from config import CLE_API, MODELE_GEMINI, MAX_MESSAGES_CONTEXTE
from database import User, UserPlugin, session_base
from datetime import datetime
from temps_reel import contexte_temps_reel

# ---------------------------------------------------------------------------
# Cache RAM pour charger_connaissances()
# ---------------------------------------------------------------------------
_cache_connaissances = None
_cache_mtime = None

# ---------------------------------------------------------------------------
# Session HTTP réutilisable — évite de créer une connexion TCP à chaque appel
# ---------------------------------------------------------------------------
_session = requests.Session()
CLE_CONSIGNES_UTILISATEUR = "__dashle_consignes_personnalisees__"
CLE_LONGUEUR_REPONSE = "__dashle_longueur_reponse__"


def emails_owner():
    return {
        email.strip().lower()
        for email in os.environ.get("OWNER_EMAILS", "").split(",")
        if email.strip()
    }


def niveau_abonnement(user):
    """Retourne le niveau actif, avec accès Prime réservé aux comptes owner."""
    if user and user.email.strip().lower() in emails_owner():
        return "prime"
    if user and user.acces_manuel:
        return user.palier if user.palier in {"free", "pro", "prime"} else "free"
    niveau = user.subscription_level if user else "free"
    if user and user.subscription_expires_at and user.subscription_expires_at <= datetime.utcnow():
        return "free"
    return niveau if niveau in {"free", "pro", "prime"} else "free"


def _nom_utilisateur(user_id=None):
    if not user_id:
        return None
    with session_base() as db:
        user = db.get(User, user_id)
        return user.nom if user else None


def _reglages_reponse(user_id=None):
    if not user_id:
        return "", "standard", "free", None
    souvenirs = se_souvenir_tout(user_id)
    consignes = str(souvenirs.get(CLE_CONSIGNES_UTILISATEUR, ""))[:2000]
    longueur = souvenirs.get(CLE_LONGUEUR_REPONSE, "standard")
    if longueur not in {"courte", "standard", "detaillee"}:
        longueur = "standard"
    with session_base() as db:
        user = db.get(User, user_id)
        niveau = niveau_abonnement(user)
        nom_utilisateur = user.nom if user else None
    return consignes, longueur, niveau, nom_utilisateur


def _plugins_actifs(user_id=None):
    actifs = {"meteo", "actualites", "statistiques"}
    if not user_id:
        return actifs
    with session_base() as db:
        desactives = db.query(UserPlugin.plugin).filter_by(user_id=user_id, enabled=False).all()
    return actifs - {plugin for (plugin,) in desactives}


# ---------------------------------------------------------------------------
# Helpers internes
# ---------------------------------------------------------------------------

def _url(endpoint: str) -> str:
    """Construit l'URL REST Gemini.
    La clé est en paramètre de requête (standard Google AI Studio).
    Elle n'est JAMAIS transmise au navigateur.
    """
    return (
        f"https://generativelanguage.googleapis.com/v1beta/models/"
        f"{MODELE_GEMINI}:{endpoint}?key={CLE_API}"
    )


def charger_connaissances() -> dict:
    """Charge knowledge.json avec cache basé sur mtime."""
    global _cache_connaissances, _cache_mtime

    chemin = BASE_DIR / "data" / "knowledge.json"
    try:
        mtime_actuel = os.path.getmtime(chemin)
    except OSError:
        return {}

    if _cache_connaissances is not None and _cache_mtime == mtime_actuel:
        return _cache_connaissances

    _cache_connaissances = lire_json(chemin, {})
    if not isinstance(_cache_connaissances, dict):
        _cache_connaissances = {}
    _cache_mtime = mtime_actuel
    return _cache_connaissances


def nettoyer_reponse(texte: str) -> str:
    """Retire le Markdown brut que Gemini renvoie parfois.

    Les motifs sont prudents : C#, F#, 2 * 3, astérisques isolés restent intacts.
    """
    # Titres ### : uniquement en début de ligne
    texte = re.sub(r"^[ \t]{0,3}#{1,6}[ \t]*", "", texte, flags=re.MULTILINE)
    # Gras **texte** (ne touche pas "2 ** 3")
    texte = re.sub(r"\*\*(?!\s)([^*\n]+?)(?<!\s)\*\*", r"\1", texte)
    # Italique *texte* (ne touche pas "2 * 3 * 4")
    texte = re.sub(r"(?<!\*)\*(?!\s)([^*\n]+?)(?<!\s)\*(?!\*)", r"\1", texte)
    # Lignes de séparation --- / *** / ___
    texte = re.sub(r"^[ \t]*[-*_]{3,}[ \t]*$", "", texte, flags=re.MULTILINE)
    # Puces de liste → "- "
    texte = re.sub(r"^[ \t]*[-*][ \t]+", "- ", texte, flags=re.MULTILINE)
    # Au plus une ligne vide d'affilée
    texte = re.sub(r"\n{3,}", "\n\n", texte)
    return texte.strip()


def _historique_recent(historique) -> list:
    """Retourne les N derniers messages du contexte."""
    if not historique:
        return []
    return list(historique)[-MAX_MESSAGES_CONTEXTE:]


def _instruction_systeme(resume: str = "", consignes: str = "", niveau: str = "free", contexte_live: str = "", nom_utilisateur: str | None = None) -> str:
    instruction = (
        "Tu es Dashle, une IA personnelle. "
        "Ne dis jamais que tu es Gemini ou que tu as été créé par Google. "
        "Réponds toujours en tant que Dashle."
    )
    if nom_utilisateur and nom_utilisateur.strip():
        instruction += " Le nom de l'utilisateur connecté est : " + nom_utilisateur.strip()[:160] + "."
    if niveau in {"pro", "prime"}:
        instruction += (
            " Tu maîtrises la statistique descriptive, les probabilités, les tests "
            "d'hypothèses, la régression, l'ANOVA, les séries temporelles et les "
            "méthodes bayésiennes. Tu explicites les hypothèses, les incertitudes, "
            "les limites et les étapes de calcul, sans inventer de résultats."
        )
    if niveau == "pro":
        instruction += " Tu appliques cette expertise à l'analyse statistique des données d'entreprise."
    elif niveau == "prime":
        instruction += (
            " Tu fournis en plus un accompagnement professionnel avancé et des analyses "
            "statistiques complètes pour les entreprises."
        )
    if contexte_live:
        instruction += (
            " Utilise les informations datées ci-dessous pour répondre aux demandes "
            "sur l’heure, la météo ou l’actualité; indique leur source et n’invente "
            "pas de données absentes.\n" + contexte_live[:5000]
        )
    if resume:
        instruction += "\nRésumé fiable des échanges précédents :\n" + resume
    if consignes:
        instruction += (
            "\nConsignes personnalisées de l'utilisateur (à suivre si elles restent "
            "compatibles avec les consignes précédentes) :\n" + consignes[:2000]
        )
    return instruction


def _construire_contents(message: str, historique) -> list:
    """Construit la liste 'contents' envoyée à Gemini.

    CORRECTION : le message courant est TOUJOURS inclus comme dernier
    élément 'user', même quand l'historique est non vide.
    On évite la duplication si le message est déjà le dernier de l'historique.
    """
    contents = []
    for msg in _historique_recent(historique):
        role = "model" if msg.get("auteur") == "bot" else "user"
        texte = msg.get("texte", "")
        if texte:
            contents.append({"role": role, "parts": [{"text": texte}]})

    # Vérifier si le message courant est déjà en dernière position
    dernier = contents[-1] if contents else None
    deja_present = (
        dernier is not None
        and dernier["role"] == "user"
        and dernier["parts"][0]["text"] == message
    )
    if not deja_present:
        contents.append({"role": "user", "parts": [{"text": message}]})

    return contents


def _gen_config(longueur: str = "standard") -> dict:
    """Configuration de génération commune à tous les appels."""
    limites = {"courte": 768, "standard": 2048, "detaillee": 4096}
    return {"temperature": 0.7, "maxOutputTokens": limites.get(longueur, 2048)}


def _message_erreur_http(code, detail: str = "", retry_after: int = 0) -> str:
    if code == 429:
        if retry_after > 0:
            return f"QUOTA:{retry_after}:Limite de requêtes atteinte. Réessaie dans {retry_after} secondes."
        return "QUOTA:30:Limite de requêtes atteinte. Réessaie dans 30 secondes."
    if code == 404:
        return (
            f"Le modèle '{MODELE_GEMINI}' est introuvable. "
            "Vérifie la variable GEMINI_MODEL dans ton fichier .env."
        )
    if code == 400:
        return f"Requête invalide envoyée à Gemini (400). Détail : {detail}"
    if code == 403:
        return "Clé API Gemini refusée (403). Vérifie GEMINI_API_KEY dans les variables d'environnement."
    if code:
        return f"Erreur Gemini {code}. Réessaie dans quelques instants."
    return "Le service IA est momentanément indisponible. Réessaie dans quelques instants."


# ---------------------------------------------------------------------------
# API publique
# ---------------------------------------------------------------------------

def demander_a_lia(message: str, historique=None, resume: str = "",
                   consignes: str = "", longueur: str = "standard",
                   niveau: str = "free", user_id=None) -> str:
    """Requête synchrone (non-streaming) vers Gemini."""
    if not CLE_API:
        return "La clé Gemini n'est pas configurée. Ajoute GEMINI_API_KEY dans le fichier .env."

    contexte_live = contexte_temps_reel(message, _plugins_actifs(user_id))
    corps = {
        "system_instruction": {"parts": [{"text": _instruction_systeme(resume, consignes, niveau, contexte_live, _nom_utilisateur(user_id))}]},
        "contents": _construire_contents(message, historique),
        "generationConfig": _gen_config(longueur),
    }

    try:
        rep = _session.post(_url("generateContent"), json=corps, timeout=20)
        rep.raise_for_status()
        texte = rep.json()["candidates"][0]["content"]["parts"][0]["text"]
        return nettoyer_reponse(texte)
    except requests.exceptions.HTTPError as e:
        code = e.response.status_code if e.response is not None else None
        detail = e.response.text[:300] if e.response is not None else ""
        retry_after = 0
        if code == 429 and e.response is not None:
            try:
                retry_after = int(e.response.headers.get("Retry-After", 0))
            except (ValueError, TypeError):
                retry_after = 0
        print(f"ERREUR Gemini HTTP {code} [demander_a_lia] : {detail}")
        return _message_erreur_http(code, detail, retry_after)
    except requests.exceptions.Timeout:
        return "Le service IA a mis trop de temps à répondre. Réessaie dans quelques instants."
    except Exception as exc:
        print(f"ERREUR Gemini inattendue [demander_a_lia] : {exc!r}")
        return "Impossible de joindre le service IA. Vérifie la connexion puis réessaie."


def streamer_a_lia(message: str, historique=None, resume: str = "", user_id=None):
    """Diffuse les morceaux texte de Gemini via SSE (Server-Sent Events).

    CORRECTION : _construire_contents() garantit désormais que le message
    courant est toujours présent dans contents.
    """
    if not CLE_API:
        yield "La clé Gemini n'est pas configurée. Ajoute GEMINI_API_KEY dans le fichier .env."
        return

    consignes, longueur, niveau, nom_utilisateur = _reglages_reponse(user_id)
    contexte_live = contexte_temps_reel(message, _plugins_actifs(user_id))
    corps = {
        "system_instruction": {"parts": [{"text": _instruction_systeme(resume, consignes, niveau, contexte_live, nom_utilisateur)}]},
        "contents": _construire_contents(message, historique),
        "generationConfig": _gen_config(longueur),
    }

    try:
        rep = _session.post(
            _url("streamGenerateContent") + "&alt=sse",
            json=corps,
            timeout=90,
            stream=True,
        )
        rep.raise_for_status()
        # Gemini renvoie text/event-stream sans charset ; Requests choisirait
        # ISO-8859-1 par défaut et corromprait les caractères UTF-8.
        rep.encoding = "utf-8"
        for ligne in rep.iter_lines(decode_unicode=True):
            if not ligne or not ligne.startswith("data:"):
                continue
            payload = ligne[5:].strip()
            if not payload or payload == "[DONE]":
                continue
            try:
                resultat = json.loads(payload)
            except json.JSONDecodeError:
                continue
            for candidat in resultat.get("candidates", []):
                for part in candidat.get("content", {}).get("parts", []):
                    texte = part.get("text")
                    if texte:
                        yield texte
    except requests.exceptions.HTTPError as e:
        code = e.response.status_code if e.response is not None else None
        detail = e.response.text[:300] if e.response is not None else ""
        retry_after = 0
        if code == 429 and e.response is not None:
            try:
                retry_after = int(e.response.headers.get("Retry-After", 0))
            except (ValueError, TypeError):
                retry_after = 0
        print(f"ERREUR Gemini HTTP {code} [streamer_a_lia] : {detail}")
        yield _message_erreur_http(code, detail, retry_after)
    except requests.exceptions.Timeout:
        yield "Le service IA a mis trop de temps à répondre. Réessaie dans quelques instants."
    except Exception as exc:
        print(f"ERREUR Gemini inattendue [streamer_a_lia] : {exc!r}")
        yield "Impossible de joindre le service IA. Vérifie la connexion puis réessaie."


def demander_a_lia_image(
    message: str,
    image_b64: str,
    mime_type: str,
    historique=None,
    resume: str = "",
    user_id=None,
) -> str:
    """Requête synchrone avec image ou vidéo inline."""
    if not CLE_API:
        return "La clé Gemini n'est pas configurée. Ajoute GEMINI_API_KEY dans le fichier .env."

    contents = []
    for msg in _historique_recent(historique):
        role = "model" if msg.get("auteur") == "bot" else "user"
        texte = msg.get("texte", "")
        if texte:
            contents.append({"role": role, "parts": [{"text": texte}]})

    texte_message = message or (
        "Décris cette vidéo." if mime_type.startswith("video/") else "Décris cette image."
    )
    contents.append({
        "role": "user",
        "parts": [
            {"text": texte_message},
            {"inline_data": {"mime_type": mime_type, "data": image_b64}},
        ],
    })

    consignes, _, niveau, nom_utilisateur = _reglages_reponse(user_id)
    contexte_live = contexte_temps_reel(texte_message, _plugins_actifs(user_id))
    corps = {
        "system_instruction": {"parts": [{"text": _instruction_systeme(resume, consignes, niveau, contexte_live, nom_utilisateur)}]},
        "contents": contents,
        "generationConfig": _gen_config(),
    }

    try:
        api_url = _url("generateContent")
        options = {}
        proxies = requests.utils.get_environ_proxies(api_url)
        proxy_invalide = any(
            (parsed := urlparse(proxy)).hostname == "127.0.0.1" and parsed.port == 9
            for proxy in proxies.values()
        )
        if proxy_invalide:
            # Le proxy local :9 refuse la connexion. Ne le contourner que pour
            # cette requête image ; TLS et la validation des certificats restent actifs.
            options["proxies"] = {"http": "", "https": ""}
        rep = _session.post(api_url, json=corps, timeout=30, **options)
        rep.raise_for_status()
        texte = rep.json()["candidates"][0]["content"]["parts"][0]["text"]
        return nettoyer_reponse(texte)
    except requests.exceptions.HTTPError as e:
        code = e.response.status_code if e.response is not None else None
        detail = e.response.text[:300] if e.response is not None else ""
        retry_after = 0
        if code == 429 and e.response is not None:
            try:
                retry_after = int(e.response.headers.get("Retry-After", 0))
            except (ValueError, TypeError):
                retry_after = 0
        print(f"ERREUR Gemini HTTP {code} [demander_a_lia_image] : {detail}")
        return _message_erreur_http(code, detail, retry_after)
    except requests.exceptions.Timeout:
        return "Le service IA a mis trop de temps à répondre. Réessaie dans quelques instants."
    except Exception as exc:
        print(f"ERREUR Gemini inattendue [demander_a_lia_image] : {exc!r}")
        return "Impossible de joindre le service IA. Vérifie la connexion puis réessaie."


def resumer_conversation(historique, resume_existant: str = "", user_id=None) -> str:
    """Produit un résumé compact pour préserver le contexte sans envoyer
    tout l'historique à chaque appel.

    Amélioration : utilise aussi le modèle centralisé, timeout explicite,
    et renvoie resume_existant en cas d'échec (pas de perte silencieuse).
    """
    if not CLE_API or not historique:
        return resume_existant

    transcript = "\n".join(
        ("Utilisateur" if msg.get("auteur") == "user" else "Dashle")
        + ": " + msg.get("texte", "")
        for msg in historique[-40:]
    )
    prompt = (
        "Résume cette conversation en français en 8 lignes maximum. "
        "Garde les faits utiles, préférences, décisions et questions en attente. "
        "N'invente rien et ne mentionne pas cette consigne.\n\n" + transcript
    )
    consignes, _, niveau, nom_utilisateur = _reglages_reponse(user_id)

    try:
        rep = _session.post(
            _url("generateContent"),
            json={
                "system_instruction": {"parts": [{"text": _instruction_systeme(resume_existant, consignes, niveau, nom_utilisateur=nom_utilisateur)}]},
                "contents": [{"role": "user", "parts": [{"text": prompt}]}],
                "generationConfig": {"temperature": 0.3, "maxOutputTokens": 512},
            },
            timeout=20,
        )
        rep.raise_for_status()
        texte = rep.json()["candidates"][0]["content"]["parts"][0]["text"]
        return nettoyer_reponse(texte) or resume_existant
    except Exception:
        return resume_existant


def reflechir(message: str, historique=None, user_id=None, resume: str = "") -> str:
    """Point d'entrée principal pour une réponse synchrone.

    Vérifie d'abord les connaissances locales et la mémoire utilisateur,
    puis délègue à Gemini si aucune correspondance exacte n'est trouvée.
    """
    message_lower = message.lower().strip()
    connaissances = charger_connaissances()

    for question, reponse in connaissances.items():
        if question.strip().lower() == message_lower:
            return reponse

    appris = se_souvenir_tout(user_id)
    for question, reponse in appris.items():
        if question.startswith("__dashle_"):
            continue
        if question.strip().lower() == message_lower:
            return reponse

    consignes, longueur, niveau, _ = _reglages_reponse(user_id)
    return demander_a_lia(message, historique, resume, consignes, longueur, niveau, user_id)
