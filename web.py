"""web.py — Serveur Flask de Dashle.

Modifications v2 :
- Mode visiteur anonyme : chat disponible sans compte, session temporaire.
- exiger_connexion() assoupli : accueil/chat/flux accessibles à tous.
- /repondre_flux gère visiteur (historique session) et utilisateur connecté (BDD).
- Résumé déclenché dès 20 messages puis tous les 10.
- Mode vocal : séquencement SSE → synthèse → écoute, anti-écho renforcé.
- Interface modernisée : bouton utilisateur dans le header, CSS amélioré.
- Historique visiteur limité à 30 messages dans la session Flask.
"""

import os
import io
import base64
import json
import random
import secrets
import hashlib
import hmac
import re
import requests
import threading
import unicodedata
from time import perf_counter
import time
from datetime import datetime, timedelta, timezone
import calendar
from html import escape as html_escape
from urllib.parse import urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from flask import (
    Flask, Response, request, render_template, render_template_string,
    redirect, stream_with_context, url_for, session, jsonify, send_file, g,
)
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename
from sqlalchemy import func, text
from sqlalchemy.exc import IntegrityError
from app import streamer_message, traiter_message, traiter_message_image
from brain import OFFRES_DASHLE, emails_owner, niveau_abonnement, resumer_conversation
from config import CLE_API, MAX_MESSAGES_CONTEXTE, MODELE_GEMINI
from connectors.web import bp as connectors_bp
from database import (
    AdminAuditLog, Conversation, ImageGenerationUsage, VoiceTranscriptionUsage, LibraryItem, Message, MessageFeedback, ShareLink, SubscriptionPayment, User,
    UserMemory, UserPreference, StatisticalAnalysisUsage, Project, ProjectFile, Reminder, UserPlugin,
    ScheduledTask, ScheduledTaskRun, UserNotification,
    initialiser_base, session_base,
)
from statistiques import analyser_fichier
from temps_reel import actualites_recentes, meteo_du_jour
from artifact_tools import (detecter_demande_pdf, detecter_demande_image,
                            detecter_demande_generation_video,
                            detecter_demande_recherche_web,
                            detecter_demande_modification_image,
                            demande_illustration_pedagogique, extraire_contenu_fourni,
                            structurer_document, rendre_pdf, generer_image, extraire_texte_structure)
from tool_router import detect_tool_intent
from tool_providers import ProviderError, ProviderUnavailable, ProviderTimeout, ProviderRegistry
from video_jobs import (
    active_for_owner, cleanup_expired, create_job as create_persistent_video_job,
    ensure_owner_token, hash_owner_token, read_result, result_metadata,
    resume_active_jobs, snapshot_for_owner,
)

BUILD_COMMIT = os.environ.get("RENDER_GIT_COMMIT") or os.environ.get("DASHLE_BUILD_COMMIT") or "inconnu"
BUILD_COMMIT_SHORT = BUILD_COMMIT[:12] if BUILD_COMMIT != "inconnu" else BUILD_COMMIT
try:
    BUILD_DATE = os.environ.get("DASHLE_BUILD_DATE") or datetime.fromtimestamp(os.path.getmtime(__file__), timezone.utc).isoformat()
except OSError:
    BUILD_DATE = "non déclarée"

try:
    from PIL import Image, ImageOps
    PIL_DISPONIBLE = True
except Exception:
    PIL_DISPONIBLE = False


app = Flask(__name__)


def _observabilite_identite_utilisateur():
    """Retourne un identifiant utilisateur non réversible pour les logs."""
    user_id = session.get("user_id")
    if user_id is None:
        return None
    return hashlib.sha256(str(user_id).encode("utf-8")).hexdigest()[:12]


@app.before_request
def _observabilite_debut():
    """Initialise la corrélation et le chronométrage sans journaliser le contenu."""
    request_id = re.sub(r"[^A-Za-z0-9._:-]", "", (request.headers.get("X-Request-ID") or "").strip())[:64]
    if not request_id:
        request_id = secrets.token_hex(12)
    g.dashle_observabilite = {
        "request_id": request_id,
        "started_at": perf_counter(),
        "user_ref": _observabilite_identite_utilisateur(),
    }


@app.after_request
def _observabilite_fin(response):
    contexte = getattr(g, "dashle_observabilite", None)
    if contexte is None:
        return response
    response.headers["X-Request-ID"] = contexte["request_id"]
    duree_ms = round((perf_counter() - contexte["started_at"]) * 1000, 1)
    if request.endpoint != "static":
        app.logger.info(
            "dashle.request request_id=%s endpoint=%s user_ref=%s provider=gemini model=%s duration_ms=%s status=%s",
            contexte["request_id"], request.endpoint or "unknown", contexte["user_ref"] or "anonymous",
            MODELE_GEMINI, duree_ms, response.status_code,
        )
    return response


def _journaliser_sse(contexte, debut_sse, ttfb_at, resultat):
    if contexte is None:
        return
    maintenant = perf_counter()
    app.logger.info(
        "dashle.sse request_id=%s endpoint=repondre_flux user_ref=%s provider=gemini model=%s duration_ms=%s ttfb_ms=%s result=%s",
        contexte["request_id"], contexte["user_ref"] or "anonymous", MODELE_GEMINI,
        round((maintenant - debut_sse) * 1000, 1),
        round((ttfb_at - debut_sse) * 1000, 1) if ttfb_at is not None else None,
        resultat,
    )


def _journaliser_image(contexte, debut_image, resultat):
    if contexte is None:
        return
    app.logger.info(
        "dashle.image request_id=%s endpoint=repondre_flux user_ref=%s provider=gemini model=%s duration_ms=%s result=%s",
        contexte["request_id"], contexte["user_ref"] or "anonymous", MODELE_GEMINI,
        round((perf_counter() - debut_image) * 1000, 1), resultat,
    )
app.config.update(
    SECRET_KEY=os.environ.get("FLASK_SECRET_KEY") or os.urandom(32),
    MAX_CONTENT_LENGTH=12 * 1024 * 1024,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    # Render définit explicitement 1; le défaut 0 permet les sessions en localhost HTTP.
    SESSION_COOKIE_SECURE=os.environ.get("SESSION_COOKIE_SECURE", "0") != "0",
    PERMANENT_SESSION_LIFETIME=timedelta(days=3650),
)
initialiser_base()
PROVIDER_REGISTRY = ProviderRegistry(lambda: CLE_API, image_generator=lambda prompt, context: generer_image(prompt, context))
cleanup_expired()
_resume_jobs_video = resume_active_jobs(PROVIDER_REGISTRY.get("video_generation"))
app.logger.info("tool=video_generation recovery_active_jobs=%s", _resume_jobs_video)
app.register_blueprint(connectors_bp)


@app.after_request
def definir_charset_json_utf8(response):
    """Annonce explicitement UTF-8 pour les réponses JSON de l'API."""
    if response.mimetype == "application/json":
        response.headers["Content-Type"] = "application/json; charset=utf-8"
    return response

# Nombre maximal de messages conservés en session pour les visiteurs anonymes.
MAX_HISTORIQUE_VISITEUR = 30
IMAGE_UPLOAD_MAX_BYTES = 10 * 1024 * 1024
IMAGE_PREVIEW_MAX_SIDE = 320
_IMAGE_PREVIEW_PREFIX = "[[DASHLE_IMAGE_PREVIEW:"

# Quotas de génération d'images : compteurs exclusivement côté serveur.
IMAGE_DAILY_LIMITS = {
    "visitor": int(os.environ.get("DASHLE_IMAGE_DAILY_LIMIT_VISITOR", "2")),
    "free": int(os.environ.get("DASHLE_IMAGE_DAILY_LIMIT_FREE", "5")),
    "pro": int(os.environ.get("DASHLE_IMAGE_DAILY_LIMIT_PRO", "20")),
    "prime": int(os.environ.get("DASHLE_IMAGE_DAILY_LIMIT_PRIME", "50")),
}

def _debut_jour_suivant_utc():
    maintenant = datetime.now(timezone.utc)
    return maintenant.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)

def _cle_visiteur_image():
    forwarded = request.headers.get("X-Forwarded-For", "")
    ip = (forwarded.split(",")[0].strip() if forwarded else request.remote_addr) or "inconnu"
    secret = app.config.get("SECRET_KEY", "dashle")
    return hashlib.sha256((str(secret) + "|image-quota|" + ip).encode("utf-8")).hexdigest()

def _quota_image_info(user_id=None):
    aujourd_hui = datetime.utcnow().date()
    if user_id:
        with session_base() as db:
            user = db.get(User, user_id)
            if user and user.email.strip().lower() in emails_owner():
                return {"niveau": "prime", "limite": None, "utilise": 0, "illimite": True,
                        "reset_at": _debut_jour_suivant_utc().isoformat()}
            niveau = niveau_abonnement(user) if user else "free"
            limite = IMAGE_DAILY_LIMITS.get(niveau, IMAGE_DAILY_LIMITS["free"])
            usage = db.query(ImageGenerationUsage).filter_by(user_id=user_id, usage_date=aujourd_hui).one_or_none()
            return {"niveau": niveau, "limite": limite, "utilise": usage.count if usage else 0, "illimite": False,
                    "reset_at": _debut_jour_suivant_utc().isoformat(),
                    "message": "Tu as utilisé tes images du jour. Elles reviennent à HH:MM",
                    "action": "voir_forfaits"}
    with session_base() as db:
        usage = db.query(ImageGenerationUsage).filter_by(visitor_key=_cle_visiteur_image(), usage_date=aujourd_hui).one_or_none()
        utilise = usage.count if usage else 0
    return {"niveau": "visitor", "limite": IMAGE_DAILY_LIMITS["visitor"], "utilise": utilise, "illimite": False,
            "reset_at": _debut_jour_suivant_utc().isoformat(),
            "message": "Tu as utilisé tes images du jour. Elles reviennent à HH:MM",
            "action": "creer_compte"}

def _quota_image_bloque(user_id=None):
    info = _quota_image_info(user_id)
    return (not info["illimite"] and info["utilise"] >= info["limite"], info)

def _consommer_quota_image(user_id=None):
    if user_id:
        with session_base() as db:
            user = db.get(User, user_id)
            if user and user.email.strip().lower() in emails_owner():
                return True
            niveau = niveau_abonnement(user) if user else "free"
            limite = IMAGE_DAILY_LIMITS.get(niveau, IMAGE_DAILY_LIMITS["free"])
            aujourd_hui = datetime.utcnow().date()
            usage = db.query(ImageGenerationUsage).filter_by(user_id=user_id, usage_date=aujourd_hui).one_or_none()
            if usage is None:
                usage = ImageGenerationUsage(user_id=user_id, usage_date=aujourd_hui, count=0)
                db.add(usage); db.flush()
            if usage.count >= limite:
                return False
            usage.count += 1
            return True
    with session_base() as db:
        aujourd_hui = datetime.utcnow().date()
        cle = _cle_visiteur_image()
        usage = db.query(ImageGenerationUsage).filter_by(visitor_key=cle, usage_date=aujourd_hui).one_or_none()
        if usage is None:
            usage = ImageGenerationUsage(visitor_key=cle, usage_date=aujourd_hui, count=0)
            db.add(usage); db.flush()
        if usage.count >= IMAGE_DAILY_LIMITS["visitor"]:
            return False
        usage.count += 1
        return True

def _reponse_quota_image(user_id=None):
    info = _quota_image_info(user_id)
    return {"quota": True, "niveau": info["niveau"], "utilise": info["utilise"], "limite": info["limite"],
            "reset_at": info["reset_at"],
            "message": "Tu as utilisé tes images du jour. Elles reviennent à HH:MM",
            "action": "creer_compte" if info["niveau"] == "visitor" else "voir_forfaits"}

# Profil international et règles de paiement. Le pays choisi par l'utilisateur
# est la source de vérité : aucune déduction par adresse IP n'est utilisée.
# Architecture de langue : le français est actif aujourd'hui; l'anglais peut
# être ajouté progressivement sans changer les données ni les routes métier.
LANGUES_INTERFACE = {"fr": "Français", "en": "English"}
LANGUE_INTERFACE_DEFAUT = "fr"

PAYS_PROFIL = [
    ("AF","Afghanistan","93"),("ZA","Afrique du Sud","27"),("AL","Albanie","355"),("DZ","Algérie","213"),
    ("DE","Allemagne","49"),("AD","Andorre","376"),("AO","Angola","244"),("AI","Anguilla","1264"),
    ("AG","Antigua-et-Barbuda","1268"),("SA","Arabie saoudite","966"),("AR","Argentine","54"),
    ("AM","Arménie","374"),("AW","Aruba","297"),("AU","Australie","61"),("AT","Autriche","43"),
    ("AZ","Azerbaïdjan","994"),("BI","Burundi","257"),("BE","Belgique","32"),("BJ","Bénin","229"),
    ("BF","Burkina Faso","226"),("BD","Bangladesh","880"),("BG","Bulgarie","359"),("BH","Bahreïn","973"),
    ("BS","Bahamas","1242"),("BA","Bosnie-Herzégovine","387"),("BY","Biélorussie","375"),("BZ","Belize","501"),
    ("BR","Brésil","55"),("BB","Barbade","1246"),("BN","Brunei","673"),("BT","Bhoutan","975"),
    ("BW","Botswana","267"),("CF","République centrafricaine","236"),("CA","Canada","1"),("CH","Suisse","41"),
    ("CL","Chili","56"),("CN","Chine","86"),("CI","Côte d’Ivoire","225"),("CM","Cameroun","237"),
    ("CD","République démocratique du Congo","243"),("CG","Congo-Brazzaville","242"),("CO","Colombie","57"),
    ("KM","Comores","269"),("CV","Cap-Vert","238"),("CR","Costa Rica","506"),("CU","Cuba","53"),
    ("CW","Curaçao","599"),("CY","Chypre","357"),("CZ","Tchéquie","420"),("DK","Danemark","45"),
    ("DJ","Djibouti","253"),("DM","Dominique","1767"),("DO","République dominicaine","1809"),("DZ","Algérie","213"),
    ("EC","Équateur","593"),("EG","Égypte","20"),("ER","Érythrée","291"),("ES","Espagne","34"),
    ("EE","Estonie","372"),("ET","Éthiopie","251"),("FI","Finlande","358"),("FJ","Fidji","679"),
    ("FR","France","33"),("GA","Gabon","241"),("GM","Gambie","220"),("GE","Géorgie","995"),
    ("GH","Ghana","233"),("GI","Gibraltar","350"),("GR","Grèce","30"),("GD","Grenade","1473"),
    ("GP","Guadeloupe","590"),("GT","Guatemala","502"),("GF","Guyane française","594"),("GN","Guinée","224"),
    ("GW","Guinée-Bissau","245"),("GQ","Guinée équatoriale","240"),("GY","Guyana","592"),("HT","Haïti","509"),
    ("HN","Honduras","504"),("HK","Hong Kong","852"),("HU","Hongrie","36"),("IS","Islande","354"),
    ("IN","Inde","91"),("ID","Indonésie","62"),("IR","Iran","98"),("IQ","Irak","964"),("IE","Irlande","353"),
    ("IL","Israël","972"),("IT","Italie","39"),("JM","Jamaïque","1876"),("JP","Japon","81"),("JO","Jordanie","962"),
    ("KZ","Kazakhstan","76"),("KE","Kenya","254"),("KG","Kirghizistan","996"),("KH","Cambodge","855"),
    ("KR","Corée du Sud","82"),("KW","Koweït","965"),("LA","Laos","856"),("LB","Liban","961"),("LR","Libéria","231"),
    ("LY","Libye","218"),("LI","Liechtenstein","423"),("LK","Sri Lanka","94"),("LS","Lesotho","266"),
    ("LT","Lituanie","370"),("LU","Luxembourg","352"),("LV","Lettonie","371"),("MA","Maroc","212"),
    ("MC","Monaco","377"),("MD","Moldavie","373"),("MG","Madagascar","261"),("ML","Mali","223"),
    ("MT","Malte","356"),("MR","Mauritanie","222"),("MU","Maurice","230"),("MW","Malawi","265"),
    ("MY","Malaisie","60"),("MZ","Mozambique","258"),("NA","Namibie","264"),("NE","Niger","227"),
    ("NG","Nigeria","234"),("NI","Nicaragua","505"),("NL","Pays-Bas","31"),("NO","Norvège","47"),
    ("NP","Népal","977"),("NZ","Nouvelle-Zélande","64"),("OM","Oman","968"),("PK","Pakistan","92"),
    ("PA","Panama","507"),("PE","Pérou","51"),("PH","Philippines","63"),("PL","Pologne","48"),
    ("PT","Portugal","351"),("PY","Paraguay","595"),("PS","Palestine","970"),("QA","Qatar","974"),
    ("RE","La Réunion","262"),("RO","Roumanie","40"),("RU","Russie","7"),("RW","Rwanda","250"),
    ("SA","Arabie saoudite","966"),("SN","Sénégal","221"),("SG","Singapour","65"),("SL","Sierra Leone","232"),
    ("SK","Slovaquie","421"),("SI","Slovénie","386"),("SO","Somalie","252"),("SS","Soudan du Sud","211"),
    ("SD","Soudan","249"),("SE","Suède","46"),("CH","Suisse","41"),("SY","Syrie","963"),("TD","Tchad","235"),
    ("TG","Togo","228"),("TH","Thaïlande","66"),("TN","Tunisie","216"),("TR","Turquie","90"),("UG","Ouganda","256"),
    ("UA","Ukraine","380"),("AE","Émirats arabes unis","971"),("GB","Royaume-Uni","44"),("US","États-Unis","1"),
    ("UY","Uruguay","598"),("UZ","Ouzbékistan","998"),("VE","Venezuela","58"),("VN","Vietnam","84"),
    ("YE","Yémen","967"),("ZM","Zambie","260"),("ZW","Zimbabwe","263"),
]
# Les six marchés Mobile Money PayDunya demandés.
PAYDUNYA_MOBILE_COUNTRIES = {"SN","CI","BJ","BF","TG","ML"}
# CinetPay est proposé séparément lorsque le pays est dans la couverture
# commerciale configurée pour Dashle; la liste peut évoluer sans toucher aux profils.
CINETPAY_COUNTRIES = {"SN","CI","BJ","BF","TG","ML","CM","GN","CD","CG","FR"}
PAYS_CODES = {code for code, _, _ in PAYS_PROFIL}
INDICATIFS = {code: indicatif for code, _, indicatif in PAYS_PROFIL}

def _normaliser_telephone(pays, telephone):
    pays = (pays or "").strip().upper()
    brut = re.sub(r"[^0-9+]", "", str(telephone or ""))
    if pays not in PAYS_CODES or not brut:
        return None, None
    indicatif = INDICATIFS[pays]
    if brut.startswith("+"):
        chiffres = brut[1:]
        if not chiffres.startswith(indicatif):
            return None, None
        national = chiffres[len(indicatif):]
    else:
        national = brut
        # Un numéro saisi avec le code pays sans + est aussi accepté.
        if national.startswith(indicatif):
            national = national[len(indicatif):]
    if not national.isdigit():
        return None, None
    # Les numéros locaux commencent par 0 dans plusieurs pays. On le conserve
    # dans telephone_national, tout en produisant un E.164 sans le 0 pour l'API.
    if national.startswith("0"):
        e164 = "+" + indicatif + national[1:]
    else:
        e164 = "+" + indicatif + national
    longueurs = {"BF": 8, "SN": 9, "CI": 10, "BJ": 10, "TG": 8, "ML": 8}
    longueur_attendue = longueurs.get(pays)
    national_sans_prefixe = national[1:] if national.startswith("0") else national
    if longueur_attendue is not None and len(national_sans_prefixe) != longueur_attendue:
        return None, None
    if not 6 <= len(national_sans_prefixe) <= 14:
        return None, None
    return e164, national

def _moyens_paiement_pays(pays):
    pays = (pays or "").upper()
    if pays in PAYDUNYA_MOBILE_COUNTRIES:
        return ["paydunya", "cinetpay", "stripe"]
    if pays in CINETPAY_COUNTRIES:
        return ["cinetpay", "stripe"]
    return ["stripe"]

def _pays_client(user_id):
    with session_base() as db:
        user = db.get(User, user_id)
        return user.pays if user else None


MESSAGES_ACCUEIL_VISITEUR = (
    "Bonjour, que veux-tu faire aujourd’hui ?",
    "Prêt à commencer ?",
    "Sur quoi je peux t’aider ?",
    "Qu’aimerais-tu explorer aujourd’hui ?",
    "On commence par quoi ?",
    "Quelle idée veux-tu faire avancer ?",
    "Je suis là — qu’est-ce qui t’amène ?",
    "Tu as quelque chose en tête ?",
)
MESSAGES_ACCUEIL_PERSONNALISES = (
    "Bonjour {prenom}, que fais-tu de beau aujourd’hui ?",
    "{prenom}, prêt à avancer sur quelque chose ?",
    "Ravi de te retrouver, {prenom}. Qu’aimerais-tu faire ?",
    "Bonjour {prenom} ! Par quoi veux-tu commencer ?",
    "Qu’est-ce qui te ferait plaisir aujourd’hui, {prenom} ?",
    "On s’y met ensemble, {prenom} ?",
    "Une idée à explorer ensemble, {prenom} ?",
    "Qu’aimerais-tu faire avancer aujourd’hui, {prenom} ?",
)


# ---------------------------------------------------------------------------
# Détection du type de média par signature binaire
# ---------------------------------------------------------------------------

def detecter_type_media(contenu):
    """Détermine le type d'image ou de vidéo depuis sa signature."""
    if contenu.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if contenu.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if contenu.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if contenu.startswith(b"BM"):
        return "image/bmp"
    if len(contenu) >= 12 and contenu.startswith(b"RIFF") and contenu[8:12] == b"WEBP":
        return "image/webp"
    if len(contenu) >= 12 and contenu[4:8] == b"ftyp":
        marque = contenu[8:12]
        if marque in (b"qt  ",):
            return "video/quicktime"
        if marque in (b"isom", b"iso2", b"mp41", b"mp42", b"avc1", b"dash"):
            return "video/mp4"
    if contenu.startswith(b"\x1a\x45\xdf\xa3"):
        return "video/webm"
    if len(contenu) >= 12 and contenu.startswith(b"RIFF") and contenu[8:12] == b"AVI ":
        return "video/x-msvideo"
    if contenu.startswith((b"\x00\x00\x01\xba", b"\x00\x00\x01\xb3")):
        return "video/mpeg"
    return None


# ---------------------------------------------------------------------------
# CSRF
# ---------------------------------------------------------------------------

def jeton_csrf():
    if "csrf_token" not in session:
        session["csrf_token"] = secrets.token_urlsafe(32)
    return session["csrf_token"]


@app.before_request
def verifier_csrf():
    """Vérifie le jeton CSRF sur les POST des utilisateurs connectés."""
    if request.method != "POST":
        return None

    publiques = {"connexion", "inscription"}
    # Les visiteurs non connectés n'ont pas de jeton CSRF obligatoire
    # (leurs données ne sont que temporaires et limitées).
    if request.endpoint not in publiques and "user_id" not in session:
        return None

    token = (
        request.headers.get("X-CSRF-Token")
        or request.form.get("csrf_token")
    )
    if not token and request.is_json:
        donnees = request.get_json(silent=True) or {}
        token = donnees.get("csrf_token")

    attendu = session.get("csrf_token", "")
    if not token or not attendu or not secrets.compare_digest(str(token), str(attendu)):
        return jsonify({"erreur": "Session CSRF expirée. Recharge la page puis réessaie."}), 400
    return None


# ---------------------------------------------------------------------------
# Contrôle d'accès — mode visiteur autorisé sur les routes chat
# ---------------------------------------------------------------------------

# Routes accessibles sans connexion (visiteur ou public)
_ROUTES_PUBLIQUES = {
    "static", "connexion", "inscription", "partage",
    "accueil", "actualites", "repondre_flux", "repondre", "repondre_image",
    "confirmer_message", "nouvelle_conv", "conditions_utilisation",
    "health", "robots_txt", "sitemap_xml", "tarifs", "paiement_retour",
    "cinetpay_notification", "paydunya_callback", "stripe_webhook", "temps_reel", "api_temps_reel",
    "telecharger_pdf_temps_reel", "generer_image_endpoint", "generer_pdf_endpoint", "admin", "executer_taches_cron",
    "flux_job_outil", "liste_jobs_outils", "resultat_job_outil",
}


@app.before_request
def exiger_connexion():
    """Autorise les visiteurs sur les routes publiques et de chat.
    Redirige vers /connexion uniquement pour les routes qui nécessitent
    vraiment un compte (paramètres, sécurité, gestion de conversations, etc.).
    """
    user_id = session.get("user_id")
    if user_id:
        with session_base() as db:
            utilisateur = db.get(User, user_id)
            if utilisateur and utilisateur.is_active:
                return None
        session.clear()
    if request.endpoint in _ROUTES_PUBLIQUES:
        return None
    return redirect(url_for("connexion"))


# ---------------------------------------------------------------------------
# Helpers visiteur — historique anonyme en session Flask
# ---------------------------------------------------------------------------

def _historique_visiteur() -> list:
    """Retourne l'historique anonyme stocké en session (liste de dicts)."""
    hist = session.get("historique_visiteur")
    if not isinstance(hist, list):
        hist = []
        session["historique_visiteur"] = hist
    return hist


def _ajouter_message_visiteur(texte: str, auteur: str):
    """Ajoute un message à l'historique visiteur en respectant la limite."""
    hist = _historique_visiteur()
    hist.append({"auteur": auteur, "texte": texte})
    # Tronquer pour ne garder que les N derniers messages
    if len(hist) > MAX_HISTORIQUE_VISITEUR:
        hist[:] = hist[-MAX_HISTORIQUE_VISITEUR:]
    session["historique_visiteur"] = hist
    session.modified = True


def _historique_recu_temporaire(valeur, limite=MAX_HISTORIQUE_VISITEUR):
    """Valide un contexte fourni par le navigateur sans le conserver côté serveur."""
    if isinstance(valeur, str):
        try:
            valeur = json.loads(valeur)
        except (TypeError, ValueError):
            return []
    if not isinstance(valeur, list):
        return []
    historique = []
    for item in valeur[-limite:]:
        if not isinstance(item, dict) or item.get("auteur") not in {"user", "bot"}:
            continue
        texte = item.get("texte")
        if isinstance(texte, str) and texte.strip():
            historique.append({"auteur": item["auteur"], "texte": texte[:16_000]})
    return historique


def _conversation_selectionnee(user_id):
    """Retourne la conversation sélectionnée si elle appartient au compte."""
    conversation_id = session.get("conversation_id")
    if not conversation_id:
        return None
    with session_base() as db:
        conversation = db.query(Conversation).filter_by(
            id=conversation_id, user_id=user_id
        ).one_or_none()
        return conversation.id if conversation else None


def _contexte_chat_temporaire(user_id, conversation_id, valeur, message):
    historique = _historique_recu_temporaire(valeur)
    if not historique and conversation_id:
        historique = _messages_conversation(
            user_id, conversation_id, limite=MAX_MESSAGES_CONTEXTE
        )
    if not historique or historique[-1].get("auteur") != "user" or historique[-1].get("texte") != message:
        historique.append({"auteur": "user", "texte": message})
    return historique


def _resume_visiteur() -> str:
    return session.get("resume_visiteur", "")


def _maj_resume_visiteur(nouveau_resume: str):
    session["resume_visiteur"] = nouveau_resume
    session.modified = True


# ---------------------------------------------------------------------------
# Helpers utilisateur connecté
# ---------------------------------------------------------------------------

def _conv_courante(user_id):
    """Retourne la conversation sélectionnée ou la dernière, sans en créer."""
    if session.get("nouvelle_conversation_en_attente"):
        return None
    conversation_id = session.get("conversation_id")
    with session_base() as db:
        conversation = None
        if conversation_id:
            conversation = db.query(Conversation).filter_by(
                id=conversation_id, user_id=user_id
            ).one_or_none()
        if conversation is None:
            conversation = (
                db.query(Conversation)
                .filter_by(user_id=user_id, archivee=False)
                .order_by(Conversation.updated_at.desc())
                .first()
            )
        if conversation is None:
            session.pop("conversation_id", None)
            return None
        conversation_id = conversation.id
    session["conversation_id"] = conversation_id
    return conversation_id


def _creer_conversation(user_id):
    with session_base() as db:
        conversation = Conversation(user_id=user_id)
        db.add(conversation)
        db.flush()
        conversation_id = conversation.id
    session["conversation_id"] = conversation_id
    session.pop("nouvelle_conversation_en_attente", None)
    return conversation_id


def _conversation_pour_message(user_id):
    """Crée une conversation uniquement au premier message si nécessaire."""
    if session.pop("nouvelle_conversation_en_attente", False):
        return _creer_conversation(user_id)
    conversation_id = _conv_courante(user_id)
    return conversation_id if conversation_id else _creer_conversation(user_id)


def _conserver_historique(user_id):
    return _preferences(user_id)["conserver_historique"] if user_id else False


def _liste_conversations(user_id):
    with session_base() as db:
        convs = (
            db.query(Conversation)
            .filter_by(user_id=user_id, archivee=False)
            .order_by(Conversation.updated_at.desc())
            .all()
        )
        for conv in convs:
            if conv.title not in {"Nouvelle conversation", "[Image envoyée]", "[Vidéo envoyée]"}:
                continue
            for index, message in enumerate(conv.messages):
                if message.auteur != "user":
                    continue
                titre = _titre_automatique(message.texte)
                if not titre and message.texte in {"[Image envoyée]", "[Vidéo envoyée]"}:
                    for reponse in conv.messages[index + 1:]:
                        if reponse.auteur == "bot" and reponse.texte.strip():
                            titre = _titre_automatique(reponse.texte.splitlines()[0].split(". ", 1)[0])
                            break
                if titre:
                    conv.title = titre
                    break
        return [{"id": c.id, "titre": c.title} for c in convs]


def _messages_conversation(user_id, conversation_id, limite=None):
    with session_base() as db:
        conv = db.query(Conversation).filter_by(
            id=conversation_id, user_id=user_id
        ).one_or_none()
        if conv is None:
            return []
        if limite is not None:
            messages = db.query(Message).filter_by(
                conversation_id=conv.id
            ).order_by(Message.id.desc()).limit(limite).all()
            messages.reverse()
        else:
            messages = conv.messages
        resultat = []
        for m in messages:
            texte, apercu = _extraire_apercu_image_message(m.texte)
            resultat.append({
                "id": m.id,
                "auteur": m.auteur,
                "texte": texte,
                "image_preview": apercu,
                "date": m.created_at.isoformat(),
            })
        return resultat


def _resume_conversation(user_id, conversation_id):
    with session_base() as db:
        conv = db.query(Conversation).filter_by(
            id=conversation_id, user_id=user_id
        ).one_or_none()
        return conv.resume if conv is not None else ""


def _actualiser_resume(user_id, conversation_id):
    """Met à jour le résumé dès 20 messages, puis tous les 10.

    Amélioration : seuil abaissé à 20 (au lieu de 24) et période réduite
    à 10 messages (au lieu de 12) pour une couverture plus régulière.
    """
    with session_base() as db:
        conv = db.query(Conversation).filter_by(
            id=conversation_id, user_id=user_id
        ).one_or_none()
        if conv is None:
            return
        n = db.query(func.count(Message.id)).filter_by(
            conversation_id=conversation_id
        ).scalar() or 0
        if n < 20 or (n - 20) % 10 != 0:
            return
        resume = conv.resume
        nombre_messages = n
        messages = db.query(Message).filter_by(
            conversation_id=conversation_id
        ).order_by(Message.id.desc()).limit(40).all()
        messages.reverse()
        historique = [
            {"id": m.id, "auteur": m.auteur, "texte": m.texte,
             "date": m.created_at.isoformat()}
            for m in messages
        ]
    contexte_projet = _arguments_contexte_projet(user_id, conversation_id)
    nouveau = resumer_conversation(
        historique, resume, user_id=user_id, **contexte_projet
    )
    if nouveau and nouveau != resume:
        with session_base() as db:
            conv = db.query(Conversation).filter_by(
                id=conversation_id, user_id=user_id
            ).one_or_none()
            nombre_actuel = db.query(func.count(Message.id)).filter_by(
                conversation_id=conversation_id
            ).scalar() or 0
            if (conv is not None and conv.resume == resume
                    and nombre_actuel == nombre_messages):
                conv.resume = nouveau


def _actualiser_resume_en_arriere_plan(user_id, conversation_id):
    """Met à jour le résumé hors du flux SSE, sans conserver de contexte temporaire."""
    def actualiser():
        try:
            _actualiser_resume(user_id, conversation_id)
        except Exception as exc:
            app.logger.error(
                "Échec de la mise à jour du résumé (%s)", type(exc).__name__
            )

    try:
        threading.Thread(target=actualiser, daemon=True).start()
    except RuntimeError as exc:
        app.logger.error(
            "Impossible de démarrer la mise à jour du résumé (%s)", type(exc).__name__
        )


def _preferences(user_id):
    with session_base() as db:
        prefs = db.query(UserPreference).filter_by(user_id=user_id).one_or_none()
        if prefs is None:
            prefs = UserPreference(user_id=user_id)
            db.add(prefs)
            db.flush()
        return {
            "theme": prefs.theme,
            "voix_active": prefs.voix_active,
            "lecture_automatique": prefs.lecture_automatique,
            "conserver_historique": prefs.conserver_historique,
            "memoire_active": prefs.memoire_active,
            "voix_nom": prefs.voix_nom,
            "voix_vitesse": prefs.voix_vitesse,
            "voix_tonalite": prefs.voix_tonalite,
            "voix_volume": prefs.voix_volume,
        }


_PREFS_VISITEUR = {
    "theme": "clair",
    "voix_active": True,
    "lecture_automatique": False,
    "conserver_historique": False,
    "memoire_active": False,
    "voix_nom": "",
    "voix_vitesse": 1.0,
    "voix_tonalite": 1.0,
    "voix_volume": 1.0,
}


def _titre_automatique(texte):
    titre = re.sub(r"[*_~#\\x60>]+", "", str(texte or ""))
    titre = re.sub(r"\s+", " ", str(texte or "")).strip()
    if re.fullmatch(r"\[(?:image|vidéo) envoyée\]", titre, flags=re.IGNORECASE):
        return ""
    titre = re.sub(r"^(?:salut|bonjour|bonsoir)(?:\s+dashle)?[\s,!.:;-]*", "", titre, flags=re.IGNORECASE)
    titre = titre.strip(" \t\r\n,.;:!?-–—")
    if not titre:
        return ""
    if len(titre) > 58:
        titre = titre[:58].rsplit(" ", 1)[0].rstrip(" ,.;:-")
    return titre or ""




def _titre_fallback_six_mots(texte):
    nettoye = re.sub(r"[*_~#\\x60>]+", "", str(texte or ""))
    nettoye = nettoye.replace(chr(96), "")
    nettoye = re.sub(r"\\s+", " ", nettoye).strip()
    if not nettoye or nettoye.lower() in {"image", "image envoyée", "photo"}:
        return ""
    return " ".join(nettoye.split()[:6])[:58].rstrip(" ,.;:-")


def _titre_premier_echange(user_id, conversation_id):
    with session_base() as db:
        messages = (
            db.query(Message)
            .filter_by(conversation_id=conversation_id)
            .order_by(Message.id.asc())
            .limit(2)
            .all()
        )
    if len(messages) < 2 or messages[0].auteur != "user" or messages[1].auteur != "bot":
        return ""
    historique = [{"auteur": m.auteur, "texte": m.texte} for m in messages]
    try:
        resume = resumer_conversation(historique, "", user_id=user_id)
    except Exception:
        resume = ""
    titre = _titre_fallback_six_mots(resume)
    if titre:
        return titre
    source = messages[1].texte if not _titre_fallback_six_mots(messages[0].texte) else messages[0].texte
    return _titre_fallback_six_mots(source)

def _extraire_apercu_image_message(texte):
    brut = str(texte or "")
    marqueur = "\n" + _IMAGE_PREVIEW_PREFIX
    if marqueur not in brut:
        return brut, ""
    propre, suffixe = brut.rsplit(marqueur, 1)
    apercu = suffixe[:-2] if suffixe.endswith("]]") else ""
    if apercu.startswith("data:image/jpeg;base64,"):
        return propre.rstrip(), apercu
    return brut, ""


def _texte_persistant_message(texte, image_preview=""):
    propre = str(texte or "").strip()
    apercu = str(image_preview or "")
    if apercu.startswith("data:image/jpeg;base64,"):
        return propre + "\n" + _IMAGE_PREVIEW_PREFIX + apercu + "]]"
    return propre


def _miniature_image_data_uri(image_bytes):
    if not PIL_DISPONIBLE:
        return ""
    try:
        with Image.open(io.BytesIO(image_bytes)) as image:
            image = ImageOps.exif_transpose(image).convert("RGB")
            image.thumbnail((IMAGE_PREVIEW_MAX_SIDE, IMAGE_PREVIEW_MAX_SIDE), Image.Resampling.LANCZOS)
            sortie = io.BytesIO()
            image.save(sortie, format="JPEG", quality=72, optimize=True)
        return "data:image/jpeg;base64," + base64.b64encode(sortie.getvalue()).decode("ascii")
    except Exception:
        return ""


def ajouter_message(user_id, conversation_id, texte, auteur, image_preview=""):
    with session_base() as db:
        conv = db.query(Conversation).filter_by(
            id=conversation_id, user_id=user_id
        ).one_or_none()
        if conv is None:
            raise LookupError("Conversation introuvable")
        texte_propre = str(texte or "").strip()
        msg = Message(
            conversation_id=conv.id,
            auteur=auteur,
            texte=_texte_persistant_message(texte_propre, image_preview),
        )
        db.add(msg)
        db.flush()
        conv.updated_at = datetime.utcnow()
        message_id = msg.id
        doit_titrer = auteur == "bot" and conv.title == "Nouvelle conversation"
    if doit_titrer:
        titre = _titre_premier_echange(user_id, conversation_id)
        if titre:
            with session_base() as db:
                conv = db.query(Conversation).filter_by(
                    id=conversation_id, user_id=user_id
                ).one_or_none()
                if conv is not None and conv.title == "Nouvelle conversation":
                    conv.title = titre
    return message_id


# ---------------------------------------------------------------------------
# CSS — interface modernisée (identité visuelle Dashle conservée)
# ---------------------------------------------------------------------------

_CSS = """
* { box-sizing: border-box; }

button, a, [role="button"] { touch-action: manipulation; }

:root {
  --accent-vert: #22C55E;
  --accent-bleu: #3B82F6;
  --accent-gradient: linear-gradient(110deg, var(--accent-vert) 0%, var(--accent-bleu) 100%);
  --vert: var(--accent-vert);
  --vert-fonce: #2563eb;
  --vert-clair: #eaf8ef;
  --texte: #17251f;
  --fond: #ffffff;
  --fond-secondaire: #f7fbf9;
  --bordure: #dce7e2;
  --msg-user: linear-gradient(110deg, #dcfce7 0%, #dbeafe 100%);
  --msg-bot: #f0f0f0;
  --sidebar-bg: #ffffff;
  --header-bg: var(--accent-gradient);
  --radius: 16px;
  --transition: 0.18s ease;
  /* Taille de texte des messages — modifiable via JS depuis les paramètres */
  --taille-msg: 15px;
  --largeur-conversation: 760px;
}

body.theme-sombre {
  --texte: #e8f5ef;
  --fond: #101816;
  --fond-secondaire: #17231f;
  --bordure: #294238;
  --msg-bot: #1e2e29;
  --sidebar-bg: #17231f;
  --vert-clair: rgba(59,130,246,.16);
  --msg-user: linear-gradient(110deg, rgba(34,197,94,.2), rgba(59,130,246,.22));
}

body {
  font-family: 'Segoe UI', system-ui, -apple-system, sans-serif;
  margin: 0;
  background: var(--fond);
  color: var(--texte);
  height: var(--dashle-viewport-height, 100vh);
  height: var(--dashle-viewport-height, 100dvh);
  display: flex;
  flex-direction: column;
  overflow: hidden;
}

/* ---- Header ---- */
header {
  background: var(--header-bg);
  color: white;
  padding: 10px 16px;
  display: flex;
  align-items: center;
  gap: 8px;
  flex-shrink: 0;
  box-shadow: 0 1px 4px rgba(0,0,0,0.15);
  z-index: 3;
}

header .logo-wrap {
  display: flex;
  align-items: center;
  gap: 8px;
  flex: 1;
}

header img.logo { height: 30px; width: auto; border-radius: 0; object-fit: contain; }

header .titre {
  font-weight: 700;
  font-size: 17px;
  letter-spacing: 0.01em;
}

header button.icon-btn {
  background: none;
  border: none;
  color: white;
  font-size: 20px;
  cursor: pointer;
  width: 36px;
  height: 36px;
  border-radius: 50%;
  display: flex;
  align-items: center;
  justify-content: center;
  transition: background var(--transition);
  flex-shrink: 0;
}

header button.icon-btn:hover { background: rgba(255,255,255,0.18); }

/* ---- Bouton utilisateur ---- */
.user-menu-wrap {
  position: relative;
  flex-shrink: 0;
}

.user-badge {
  display: flex;
  align-items: center;
  gap: 6px;
  background: rgba(255,255,255,0.15);
  border: 1px solid rgba(255,255,255,0.3);
  border-radius: 20px;
  padding: 5px 10px 5px 6px;
  cursor: pointer;
  color: white;
  font-size: 13px;
  font-weight: 500;
  transition: background var(--transition);
}

.user-badge:hover { background: rgba(255,255,255,0.25); }

.user-avatar {
  width: 24px;
  height: 24px;
  border-radius: 50%;
  background: rgba(255,255,255,0.35);
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 12px;
  font-weight: 700;
  flex-shrink: 0;
}

.user-dropdown {
  display: none;
  position: absolute;
  right: 0;
  top: calc(100% + 8px);
  background: white;
  border: 1px solid var(--bordure);
  border-radius: 12px;
  box-shadow: 0 4px 20px rgba(0,0,0,0.12);
  min-width: 180px;
  z-index: 100;
  overflow: hidden;
}

.user-dropdown.ouvert { display: block; }

.user-dropdown a,
.user-dropdown button {
  display: block;
  width: 100%;
  padding: 11px 16px;
  text-align: left;
  border: none;
  background: none;
  color: #17251f;
  font: inherit;
  font-size: 14px;
  cursor: pointer;
  text-decoration: none;
  transition: background var(--transition);
}

.user-dropdown a:hover,
.user-dropdown button:hover { background: var(--vert-clair); color: var(--vert-fonce); }

.user-dropdown .separateur {
  height: 1px;
  background: var(--bordure);
  margin: 4px 0;
}

.user-dropdown .email-info {
  padding: 10px 16px 6px;
  font-size: 12px;
  color: #71837b;
  border-bottom: 1px solid var(--bordure);
}

/* ---- Bannière visiteur ---- */
#banniere-visiteur {
  display: none;
  background: linear-gradient(135deg, #f0faf6 0%, #e8f5ef 100%);
  border-bottom: 1px solid #c8e8db;
  padding: 8px 16px;
  font-size: 13px;
  color: #2d5a4a;
  align-items: center;
  gap: 10px;
  flex-shrink: 0;
}

#banniere-visiteur.visible { display: flex; }
#banniere-historique-temporaire { display:flex; align-items:center; gap:10px; padding:9px 18px; background:#eef7f3; color:#245f4a; font-size:13px; }

#banniere-visiteur a {
  color: var(--vert-fonce);
  font-weight: 600;
  text-decoration: none;
  white-space: nowrap;
}

#banniere-visiteur a:hover { text-decoration: underline; }

#banniere-visiteur .spacer { flex: 1; }
#banniere-visiteur .visitor-actions { display:flex; align-items:center; gap:8px; margin-left:auto; }

/* ---- Sidebar ---- */
#voile {
  display: none;
  position: fixed;
  inset: 0;
  background: rgba(0,0,0,0.3);
  z-index: 5;
}

#sidebar {
  display: none;
  position: fixed;
  top: 0;
  left: 0;
  width: 82%;
  max-width: 320px;
  height: var(--dashle-viewport-height, 100%);
  height: var(--dashle-viewport-height, 100dvh);
  background: var(--sidebar-bg);
  z-index: 6;
  overflow-y: auto;
  box-shadow: 2px 0 16px rgba(0,0,0,0.15);
  transition: transform var(--transition);
}

#sidebar h2 {
  padding: 20px 18px 10px;
  margin: 0;
  color: var(--vert);
}

#sidebar a,
#sidebar button.nouvelle {
  display: block;
  width: 100%;
  padding: 12px 18px;
  text-decoration: none;
  color: var(--texte);
  border: 0;
  border-bottom: 1px solid var(--bordure);
  background: var(--sidebar-bg);
  font: inherit;
  cursor: pointer;
  transition: background var(--transition);
}

#sidebar a:hover,
#sidebar button.nouvelle:hover { background: var(--vert-clair); }

#sidebar a.nouvelle,
#sidebar button.nouvelle { color: var(--vert); font-weight: bold; }

.recherche-conversations {
  margin: 0 14px 10px;
  padding: 9px 11px;
  width: calc(100% - 28px);
  border: 1px solid var(--bordure);
  border-radius: 9px;
  background: var(--fond-secondaire);
  color: var(--texte);
  font: inherit;
  font-size: 14px;
}

.menu-section {
  padding: 12px 18px 5px;
  color: #71837b;
  font-size: 11px;
  text-transform: uppercase;
  letter-spacing: 0.08em;
}

/* ---- Zone de chat ---- */
#chat {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
  padding: 16px;
  width: min(900px, 100%);
  margin: 0 auto;
  scroll-behavior: smooth;
}

.message-wrap {
  max-width: 82%;
  margin-bottom: 16px;
  animation: apparaitre 0.2s ease;
}

@keyframes apparaitre {
  from { opacity: 0; transform: translateY(6px); }
  to   { opacity: 1; transform: translateY(0); }
}

.message-wrap.user { margin-left: auto; }
.message-wrap.bot  { margin-right: auto; }

.msg {
  padding: 11px 15px;
  border-radius: var(--radius);
  white-space: pre-wrap;
  line-height: 1.5;
  font-size: var(--taille-msg);
  word-break: break-word;
}

.msg.user {
  background: var(--msg-user);
  border-bottom-right-radius: 4px;
}

.msg.bot {
  background: var(--msg-bot);
  border-bottom-left-radius: 4px;
}

body[data-densite="compacte"] .message-wrap { margin-bottom: 8px; }
body[data-densite="compacte"] .msg { padding: 7px 11px; }
body[data-animations="reduites"] .message-wrap,
body[data-animations="desactivees"] .message-wrap,
body[data-animations="desactivees"] .dot { animation: none; }
body[data-animations="reduites"] .suggestion { transition-duration: 0.01ms; }
body[data-animations="desactivees"] .suggestion { transition: none; transform: none; }

body.theme-sombre .msg.bot { color: var(--texte); }
body, #sidebar, #chat, form.bas, .msg { transition:background-color .2s ease,color .2s ease,border-color .2s ease; }
body.theme-sombre .user-dropdown { background:var(--fond-secondaire); border-color:var(--bordure); }
body.theme-sombre .user-dropdown a,body.theme-sombre .user-dropdown button { color:var(--texte); }
body.theme-sombre .user-dropdown .email-info { color:#a8bdb4; }
body.theme-sombre .suggestion { background:var(--fond-secondaire); color:var(--texte); }
body.theme-sombre .msg.bot pre { border:1px solid #31483e; }

/* ---- Actions réponse ---- */
.actions-reponse {
  display: flex;
  gap: 2px;
  padding: 3px 4px;
  flex-wrap: wrap;
}

.actions-reponse button {
  border: 0;
  background: transparent;
  color: #278a70;
  border-radius: 7px;
  padding: 4px 7px;
  cursor: pointer;
  font-size: 13px;
  transition: background var(--transition), color var(--transition), transform var(--transition), box-shadow var(--transition);
}

.actions-reponse button svg { display:block; width:18px; height:18px; fill:none; stroke:currentColor; stroke-width:1.65; stroke-linecap:round; stroke-linejoin:round; }
.actions-reponse button:hover { transform:translateY(-1px); box-shadow:0 2px 8px rgba(34,160,125,.18); }

.actions-reponse button:hover,
.actions-reponse button.actif {
  background: var(--vert-clair);
  color: var(--vert-fonce);
}

.actions-reponse .lecture-etat {
  font-size: 12px;
  color: var(--vert);
  min-width: 48px;
  align-self: center;
}

/* ---- Indicateur de réflexion ---- */
.suivi-action {
  margin: 6px 12px;
  padding: 10px 12px;
  border: 1px solid var(--bordure);
  border-radius: 12px;
  background: var(--fond-secondaire);
  max-width: min(520px, 92%);
}
.suivi-action-entete { display:flex; align-items:center; gap:8px; }
.suivi-action-titre { flex:1; font-size:13px; color:var(--texte); }
.suivi-action-indicateur { width:8px; height:8px; border-radius:50%; background:var(--vert); animation:pulse .9s infinite ease-in-out; }
.suivi-action-indicateur.termine { animation:none; }
.suivi-action-indicateur.echec { animation:none; background:#c0392b; }
.suivi-action-indicateur.annule { animation:none; background:#888; }
.suivi-action-annuler { border:0; background:transparent; color:var(--vert-fonce); cursor:pointer; font-size:12px; }
.suivi-action-etapes { margin-top:7px; display:grid; gap:3px; font-size:12px; color:var(--texte-secondaire); }
.suivi-action-etape.termine { color:var(--vert-fonce); }
.suivi-action-etape.echec { color:#c0392b; }
.suivi-action-etape.annule { color:var(--texte-secondaire); }
.suivi-action-points { display:inline-block; letter-spacing:2px; animation:pulse .9s infinite ease-in-out; }
.suivi-action-resultat { margin-top:8px; }
.reflexion {
  display: flex;
  align-items: center;
  gap: 5px;
  padding: 10px 14px;
}

.dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: var(--vert);
  animation: pulse 0.9s infinite ease-in-out;
}

.dot:nth-child(2) { animation-delay: 0.15s; }
.dot:nth-child(3) { animation-delay: 0.3s; }

@keyframes pulse {
  0%, 100% { transform: scale(0.65); opacity: 0.4; }
  50%       { transform: scale(1);    opacity: 1;   }
}

/* ---- Barre de saisie ---- */
form.bas {
  display: flex;
  gap: 6px;
  padding: 10px 12px calc(10px + env(safe-area-inset-bottom));
  border-top: 1px solid var(--bordure);
  align-items: flex-end;
  background: var(--fond);
  flex-shrink: 0;
}

body.theme-sombre form.bas { background: var(--fond); border-color: var(--bordure); }

form.bas textarea {
  flex: 1;
  resize: none;
  border: 1px solid var(--bordure);
  border-radius: 18px;
  padding: 10px 14px;
  font-size: 15px;
  max-height: 120px;
  min-height: 42px;
  background: var(--fond-secondaire);
  color: var(--texte);
  font-family: inherit;
  line-height: 1.4;
  transition: border-color var(--transition);
}

form.bas textarea:focus {
  outline: none;
  border-color: transparent;
  background: linear-gradient(var(--fond-secondaire),var(--fond-secondaire)) padding-box,
              var(--accent-gradient) border-box;
  box-shadow: 0 0 0 2px rgba(59,130,246,.12);
}

.recherche-conversations:focus {
  outline:none;
  border-color:transparent;
  background:linear-gradient(var(--fond-secondaire),var(--fond-secondaire)) padding-box,
             var(--accent-gradient) border-box;
  box-shadow:0 0 0 2px rgba(59,130,246,.12);
}

.groupe-actions {
  display: flex;
  align-items: center;
  gap: 3px;
  flex-shrink: 0;
  padding-bottom: 2px;
}

button.micro {
  background: none;
  border: none;
  cursor: pointer;
  width: 34px;
  height: 34px;
  border-radius: 50%;
  font-size: 16px;
  display: flex;
  align-items: center;
  justify-content: center;
  color: #6b6b6b;
  transition: background var(--transition), color var(--transition);
}

button.micro:hover { background: var(--vert-clair); color: var(--vert); }
button.micro.actif { color: #fff; background: var(--accent-gradient); }

button.vocal {
  background: var(--accent-gradient);
  border: none;
  cursor: pointer;
  width: 34px;
  height: 34px;
  border-radius: 50%;
  display: flex;
  align-items: center;
  justify-content: center;
  color: white;
  box-shadow: 0 1px 4px rgba(0,0,0,0.18);
  transition: filter var(--transition), box-shadow var(--transition);
  flex-shrink: 0;
}

button.vocal:hover   { filter: brightness(.95); }
button.vocal.vocal-on { box-shadow: 0 0 0 2px var(--vert); }

button.vocal.ecoute {
  animation: pulse-vocal 1.1s infinite ease-in-out;
}

button.vocal.parle { background: var(--accent-gradient); }

@keyframes pulse-vocal {
  0%, 100% { box-shadow: 0 0 0 0   rgba(59,130,246,0.55); }
  50%       { box-shadow: 0 0 0 8px rgba(59,130,246,0);    }
}

button.envoyer {
  background: var(--accent-gradient);
  color: white;
  border: none;
  border-radius: 50%;
  width: 38px;
  height: 38px;
  font-size: 16px;
  flex-shrink: 0;
  display: flex;
  align-items: center;
  justify-content: center;
  cursor: pointer;
  transition: filter var(--transition), opacity var(--transition), transform var(--transition);
}

button.envoyer:hover:not(:disabled) {
  filter: brightness(.94);
  transform: scale(1.05);
}

button.envoyer:disabled { opacity: 0.45; cursor: default; }

/* ---- Bouton arrêter la génération ---- */
button.arreter {
  background: #ef4444;
  color: white;
  border: none;
  border-radius: 50%;
  width: 38px;
  height: 38px;
  font-size: 14px;
  flex-shrink: 0;
  display: none;
  align-items: center;
  justify-content: center;
  cursor: pointer;
  transition: background var(--transition), transform var(--transition);
}

button.arreter.visible { display: flex; }
button.arreter:hover { background: #dc2626; transform: scale(1.05); }

/* ---- Aperçu fichier ---- */
#apercu-fichier {
  display: none;
  align-items: center;
  gap: 10px;
  margin: 0 12px 8px;
  padding: 8px 10px;
  border: 1px solid var(--bordure);
  border-radius: 12px;
  background: var(--fond-secondaire);
  flex-shrink: 0;
}

#apercu-fichier.visible { display: flex; }
#apercu-fichier-media   { width:58px; height:58px; flex:0 0 58px; border-radius:9px; object-fit:cover; }

video#apercu-fichier-media { object-fit: contain; }

#apercu-fichier-info    { min-width:0; flex:1; font-size:12px; }
#apercu-fichier-nom     { display:block; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; font-weight:600; }
#apercu-fichier-type    { display:block; margin-top:3px; color:#688078; }

#retirer-fichier {
  width:30px; height:30px; flex:0 0 30px;
  border:0; border-radius:50%; background:transparent;
  color:#6b7c76; cursor:pointer; font-size:20px;
}
#retirer-fichier:hover { background: #ffe0e0; color: #b00020; }

/* ---- Bouton rouvrir overlay vocal (vue réduite) ---- */
.btn-rouvrir-vocal {
  display: none; /* géré par JS */
  align-items: center;
  justify-content: center;
  gap: 6px;
  margin: 0 12px 6px;
  padding: 7px 14px;
  background: var(--accent-gradient);
  color: white;
  border: none;
  border-radius: 20px;
  font-size: 13px;
  font-weight: 600;
  cursor: pointer;
  flex-shrink: 0;
  transition: filter var(--transition);
}
.btn-rouvrir-vocal:hover { filter: brightness(.94); }
.btn-rouvrir-vocal.actif { display: flex; }

/* ---- Statut vocal (texte) ---- */
#statut-vocal {
  text-align: center;
  font-size: 12px;
  color: var(--vert);
  padding: 0 10px 6px;
  display: none;
  flex-shrink: 0;
}
#statut-vocal.visible { display: block; }

/* ---- Mode vocal plein écran ---- */
#mode-vocal {
  display: none;
  position: fixed;
  inset: 0;
  z-index: 4;
  overflow: hidden;
  color: #effff8;
  background: radial-gradient(
    circle at 50% 42%,
    #1b8d67 0%, #07543f 36%, #032d25 72%, #011b18 100%
  );
}

#mode-vocal.visible { display: flex; flex-direction: column; }

#mode-vocal::before {
  content: "";
  position: absolute;
  inset: -30%;
  opacity: 0.45;
  pointer-events: none;
  background:
    radial-gradient(ellipse at 30% 20%, rgba(87,255,190,.22), transparent 35%),
    radial-gradient(ellipse at 75% 78%, rgba(16,163,127,.28), transparent 38%);
  animation: fond-vocal 14s ease-in-out infinite alternate;
}

@keyframes fond-vocal {
  from { transform: translate3d(-2%,-1%,0) scale(1);    }
  to   { transform: translate3d( 2%, 1%,0) scale(1.08); }
}

.vocal-entete {
  position: relative;
  z-index: 1;
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 18px 20px;
}

.vocal-entete strong { font-size: 16px; letter-spacing: 0.02em; }

.vocal-commandes { display: flex; gap: 8px; }

.vocal-commandes button {
  border: 1px solid rgba(255,255,255,.25);
  border-radius: 20px;
  padding: 8px 14px;
  color: #effff8;
  background: rgba(0,0,0,.16);
  cursor: pointer;
  font-size: 13px;
  transition: background var(--transition);
}

.vocal-commandes button:hover { background: rgba(255,255,255,.14); }

.scene-vocale {
  position: relative;
  z-index: 1;
  display: grid;
  place-items: center;
  flex: 1;
  min-height: 0;
}

.systeme-solaire {
  position: relative;
  width: min(78vw, 430px);
  aspect-ratio: 1;
}

.orbite {
  position: absolute;
  left: 50%; top: 50%;
  width: var(--taille); height: var(--taille);
  border: 1px solid rgba(169,255,221,.24);
  border-radius: 50%;
  transform: translate(-50%,-50%);
  animation: rotation-orbite var(--vitesse) linear infinite;
}

.orbite:nth-child(2) { animation-direction: reverse; }
.orbite:nth-child(3) { animation-delay: -4s; }

@keyframes rotation-orbite { to { transform: translate(-50%,-50%) rotate(360deg); } }

.planete {
  position: absolute;
  left: 50%; top: 50%;
  width: var(--diametre); height: var(--diametre);
  margin: calc(var(--diametre) / -2);
  border-radius: 50%;
  background: var(--couleur);
  box-shadow: 0 0 12px var(--couleur);
  transform: translateX(calc(var(--taille) / 2));
}

.orbe-dashle {
  position: absolute;
  left: 50%; top: 50%;
  width: clamp(104px, 25vw, 150px);
  aspect-ratio: 1;
  transform: translate(-50%,-50%);
  border-radius: 50%;
  background: radial-gradient(
    circle at 34% 28%,
    #d0e8ff 0%, #5aaaf5 13%, #2563eb 43%, #1d4ed8 72%, #0a1a5c 100%
  );
  box-shadow:
    0 0 22px rgba(59,130,246,.9),
    0 0 72px rgba(37,99,235,.65),
    inset -16px -18px 28px rgba(0,10,60,.48);
  animation: respiration-orbe 3.8s ease-in-out infinite;
}

.orbe-dashle::after {
  content: "";
  position: absolute;
  inset: -14%;
  border: 1px solid rgba(147,197,253,.48);
  border-radius: 50%;
  animation: halo-orbe 2.8s ease-in-out infinite;
}

@keyframes respiration-orbe { 0%,100% { transform:translate(-50%,-50%) scale(.96); } 50% { transform:translate(-50%,-50%) scale(1.04); } }
@keyframes halo-orbe         { 0%,100% { transform:scale(.92); opacity:.3; } 50% { transform:scale(1.08); opacity:.8; } }

.etat-vocal {
  position: absolute;
  left: 50%;
  bottom: 8%;
  transform: translateX(-50%);
  min-width: 180px;
  text-align: center;
  color: #c9ffeb;
  font-size: 14px;
}

/* ---- Responsive ---- */
@media (max-width: 600px) {
  .vocal-entete   { padding: 14px; }
  .systeme-solaire { width: min(86vw, 360px); }
  .etat-vocal      { bottom: 5%; }
  .user-badge span.email-label { display: none; }
  header .titre    { font-size: 15px; }
}

@media (prefers-reduced-motion: reduce) {
  #mode-vocal::before, .orbite, .orbe-dashle, .orbe-dashle::after { animation-play-state: paused; }
  .message-wrap { animation: none; }
}

/* ---- Interface de conversation desktop et mobile ---- */
#sidebar { display:flex; flex-direction:column; width:272px; max-width:none; padding-bottom:12px; }
.sidebar-brand { display:flex; align-items:center; gap:10px; padding:18px 18px 14px; color:var(--texte); font-size:18px; font-weight:700; }
.sidebar-brand img { width:auto; height:30px; border-radius:0; object-fit:contain; }
.sidebar-conversations { min-height:0; overflow-y:auto; }
.sidebar-account { margin-top:auto; padding:12px 16px 0; border-top:1px solid var(--bordure); }
.sidebar-account .user-badge { width:100%; justify-content:flex-start; color:var(--texte); background:transparent; border-color:var(--bordure); }
.sidebar-account .user-avatar { color:#fff; background:var(--accent-gradient); }
.sidebar-account a, .sidebar-account button { color:var(--texte); }
.sidebar-account .sidebar-account-links { display:flex; gap:8px; margin-top:8px; }
.sidebar-account-links a { flex:1; padding:8px 6px; border-radius:8px; text-align:center; text-decoration:none; font-size:13px; }
.sidebar-account-links a:hover { background:var(--vert-clair); }
.ligne-conversation { min-height:44px; padding:0 8px 0 12px; }
.ligne-conversation > a { overflow:hidden; text-overflow:ellipsis; white-space:nowrap; border:0 !important; background:transparent !important; }
.conversation-icon-button { display:inline-flex; align-items:center; justify-content:center; flex:0 0 40px; width:40px; height:40px; padding:0; border:0; border-radius:10px; background:transparent; color:#7d8d86; cursor:pointer; transition:color var(--transition),background var(--transition),transform var(--transition); }
.conversation-icon-button svg { width:18px; height:18px; fill:none; stroke:currentColor; stroke-width:1.8; stroke-linecap:round; stroke-linejoin:round; }
.conversation-icon-button:hover,.conversation-icon-button:focus-visible { color:#168c65; background:var(--vert-clair); }
.conversation-icon-button:focus-visible { outline:2px solid #3b82f6; outline-offset:1px; }
.ligne-conversation .epingle-conversation { margin-right:2px; }
.ligne-conversation .epingle-conversation[aria-pressed="true"] { color:#fff; background:var(--accent-gradient); }
.ligne-conversation .epingle-conversation[aria-pressed="true"] svg { fill:rgba(255,255,255,.18); }
.conversation-action-list { display:flex; align-items:center; gap:2px; }
.conversation-action-list[hidden] { display:none; }
.conversation-action-label { display:none; }
.conversation-menu-toggle { display:none; }
.conversation-action-form { margin:0; }
.ligne-conversation > a.actif { border-radius:8px; background:var(--accent-gradient) !important; color:#fff !important; }
#sidebar button.nouvelle { border-color:transparent !important; background:var(--accent-gradient); color:#fff; }
#sidebar button.nouvelle:hover { filter:brightness(.94); }
.actions-reponse button:hover,.actions-reponse button.actif { background:var(--accent-gradient); color:#fff; }
.suggestion:hover { border-color:transparent; background:linear-gradient(var(--fond),var(--fond)) padding-box,var(--accent-gradient) border-box; }
.sidebar-vide { padding:4px 18px 12px; color:#71837b; font-size:13px; }
.accueil-vide { min-height:100%; display:flex; flex-direction:column; justify-content:center; align-items:center; gap:20px; text-align:center; padding:36px 16px; }
.accueil-vide img { width:auto; height:92px; max-width:min(220px,70vw); object-fit:contain; border-radius:0; }
.accueil-vide h1 { margin:0; font-size:clamp(24px,4vw,34px); }
.accueil-vide p { margin:0; color:#71837b; }
.suggestions { width:min(720px,100%); display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:12px; }
.suggestion { padding:14px 16px; border:1px solid var(--bordure); border-radius:14px; background:var(--fond); color:var(--texte); text-align:left; font:inherit; cursor:pointer; transition:border-color var(--transition),background var(--transition),transform var(--transition); }
.suggestion:hover { border-color:var(--vert); background:var(--fond-secondaire); transform:translateY(-2px); }
.msg.bot h1,.msg.bot h2,.msg.bot h3 { margin:.7em 0 .35em; line-height:1.3; }
.msg.bot pre { max-width:100%; overflow:auto; padding:12px; border-radius:10px; background:#101816; color:#e8f5ef; }
.msg.bot code { padding:2px 5px; border-radius:5px; background:rgba(16,163,127,.12); font-family:Consolas,monospace; font-size:.92em; }
.msg.bot pre code { padding:0; background:transparent; }
.bloc-code { position:relative; padding-top:28px; }
.bloc-code .copier-code { position:absolute; top:4px; right:8px; padding:4px 8px; border:1px solid #496157; border-radius:6px; background:#1e2e29; color:#e8f5ef; cursor:pointer; font-size:12px; }
.bloc-code pre { margin:0; }
.msg.bot ul,.msg.bot ol { padding-left:1.5em; }
.msg.bot a { color:var(--vert-fonce); }
.msg.bot .pdf-telechargement-chat { display:inline-flex;align-items:center;margin-top:10px;padding:9px 14px;border-radius:10px;background:var(--accent-gradient);color:#fff;text-decoration:none;font-weight:650;box-shadow:0 3px 10px rgba(34,160,125,.18); }
.msg.bot .pdf-telechargement-chat:hover { filter:brightness(.96); }
.message-wrap { max-width:min(95%,var(--largeur-conversation)); }
.message-wrap { margin-bottom:22px; }
.msg { padding:14px 17px; border:1px solid var(--bordure); box-shadow:0 3px 12px rgba(17,51,39,.045); }
.message-image-persistante { margin:0 0 8px; max-width:min(320px,100%); }
.msg table { display:block; max-width:100%; overflow-x:auto; border-collapse:collapse; }
.msg th, .msg td { padding:6px 8px; border:1px solid rgba(127,127,127,.25); text-align:left; }
.msg thead th { background:rgba(127,127,127,.08); }
.msg ul, .msg ol { padding-left:1.4rem; }

.message-image-persistante img { display:block; width:auto; max-width:100%; max-height:260px; border-radius:14px; object-fit:contain; cursor:zoom-in; }
.msg.user { border-color:rgba(34,197,94,.18); }
.msg.bot { background:var(--fond-secondaire); }
.actions-reponse { gap:4px; padding:6px 4px; }
.actions-reponse button { width:36px; height:36px; display:inline-grid; place-items:center; border:1px solid transparent; }
.actions-reponse button:focus-visible { outline:2px solid #3b82f6; outline-offset:1px; }
.actions-reponse button:hover { border-color:var(--bordure); }
.image-message-lien { display:block; margin-top:4px; }
.image-message { display:block; max-width:min(280px,70vw); max-height:320px; object-fit:contain; border-radius:12px; cursor:zoom-in; }
.suivi-action.image-generation { width:min(100%,560px); }
.suivi-action-image-progress { position:relative; overflow:hidden; margin:12px 0 4px; padding:15px 16px; border:1px solid rgba(16,163,127,.18); border-radius:16px; background:linear-gradient(135deg,rgba(34,197,94,.08),rgba(59,130,246,.09)); }
.suivi-action-image-progress::before { content:""; position:absolute; inset:0 auto 0 0; width:42%; background:linear-gradient(90deg,rgba(34,197,94,.0),rgba(34,197,94,.20),rgba(59,130,246,.22),rgba(59,130,246,0)); transform:translateX(-120%); animation:dashleImageSweep 2.1s ease-in-out infinite; }
.suivi-action-image-progress .titre { position:relative; font-weight:700; color:var(--texte); }
.suivi-action-image-progress .sous-titre { position:relative; margin-top:4px; color:var(--muted); font-size:13px; }
.suivi-action-image-progress .barre { position:relative; height:5px; margin-top:13px; overflow:hidden; border-radius:99px; background:rgba(16,163,127,.10); }
.suivi-action-image-progress .barre::after { content:""; display:block; width:38%; height:100%; border-radius:inherit; background:linear-gradient(90deg,#22c55e,#3b82f6); animation:dashleImageBar 1.8s ease-in-out infinite; }
.suivi-action-image-progress .barre.indeterminee { width:100%; }
.suivi-action-image-progress .barre.indeterminee::after { width:35%; animation:dashleImageBar 1.6s ease-in-out infinite; }
.suivi-action-image-progress .barre { transition:width .25s ease; }
.suivi-action-web-resultat { margin-top:12px; padding:13px 14px; border:1px solid var(--bordure); border-radius:14px; background:var(--fond); }
.suivi-action-web-answer { white-space:pre-wrap; line-height:1.55; }
.suivi-action-sources { margin:8px 0 0; padding-left:20px; }
.suivi-action-sources a { color:var(--vert-fonce); text-decoration:none; }
.suivi-action-sources a:hover { text-decoration:underline; }
.dashle-video-resultat { display:block; width:min(100%,720px); max-height:480px; border-radius:14px; margin-top:4px; background:#000; }

.suivi-action-image-resultat { margin-top:12px; }
.suivi-action-image-actions { display:flex; flex-wrap:wrap; gap:8px; margin-top:10px; }
.suivi-action-image-actions button,.suivi-action-image-actions a { display:inline-flex; align-items:center; justify-content:center; min-height:38px; padding:8px 12px; border:1px solid var(--bordure); border-radius:10px; background:var(--fond); color:var(--texte); text-decoration:none; font:600 13px/1.2 inherit; cursor:pointer; }
.suivi-action-image-actions button.primaire,.suivi-action-image-actions a.primaire { border-color:transparent; color:#fff; background:linear-gradient(110deg,#22c55e,#3b82f6); }
.image-viewer { position:fixed; inset:0; z-index:3000; display:flex; flex-direction:column; align-items:center; justify-content:center; padding:12px; background:rgba(5,16,13,.94); }
.image-viewer[hidden] { display:none; }
.image-viewer img { width:auto; max-width:100%; max-height:calc(100vh - 132px); object-fit:contain; border-radius:12px; }
.image-viewer-bar { width:min(100%,720px); display:flex; flex-wrap:wrap; gap:8px; justify-content:center; margin-top:12px; }
.image-viewer-bar button { min-height:42px; padding:9px 13px; border:1px solid rgba(255,255,255,.18); border-radius:11px; background:rgba(255,255,255,.09); color:#fff; font:600 13px/1.2 inherit; cursor:pointer; }
.image-viewer-bar button.primaire { background:linear-gradient(110deg,#22c55e,#3b82f6); border-color:transparent; }
.image-viewer-fermer { position:absolute; top:max(12px,env(safe-area-inset-top)); right:12px; min-width:42px; min-height:42px; }
@keyframes dashleImageSweep { to { transform:translateX(330%); } }
@keyframes dashleImageBar { 0% { transform:translateX(-150%); } 50% { transform:translateX(120%); } 100% { transform:translateX(300%); } }
@media (max-width:420px) { .image-viewer { padding:8px; } .image-viewer img { max-height:calc(100vh - 150px); } .image-viewer-bar { display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); width:100%; } .image-viewer-bar button { width:100%; } }
.bas { gap:10px !important; padding:12px 14px max(12px,env(safe-area-inset-bottom)) !important; border:1px solid var(--bordure) !important; border-radius:24px; margin-top:8px; margin-bottom:14px; box-shadow:0 8px 28px rgba(16,55,41,.08); }
.bas textarea { min-height:46px !important; padding:12px 15px !important; border-radius:16px !important; }
.btn-attach { border:1px solid var(--bordure); background:var(--fond-secondaire); }
.groupe-actions { gap:6px; }
button.envoyer,button.arreter { width:42px; height:42px; }
.btn-attach { display:grid; place-items:center; flex-shrink:0; width:40px; height:40px; padding:0; border:0; border-radius:50%; background:transparent; color:var(--texte); font-size:30px; font-weight:300; line-height:1; cursor:pointer; }
.btn-attach:hover { background:var(--fond-secondaire); }
.feuille-fichiers-voile { position:fixed; inset:0; z-index:1200; display:flex; align-items:flex-end; justify-content:center; padding:16px; background:rgba(15,23,42,.42); }
.feuille-fichiers-voile[hidden] { display:none; }
.feuille-fichiers { width:min(100%,480px); padding:24px 20px max(24px,env(safe-area-inset-bottom)); border-radius:24px 24px 16px 16px; background:var(--fond); color:var(--texte); box-shadow:0 -12px 40px rgba(0,0,0,.16); }
.feuille-fichiers h2 { margin:0 0 22px; font-size:18px; text-align:center; }
.feuille-fichiers-options { display:grid; grid-template-columns:repeat(3,minmax(0,1fr)); gap:12px; }
.option-fichier { display:flex; flex-direction:column; align-items:center; gap:9px; padding:4px; border:0; background:transparent; color:inherit; font:inherit; cursor:pointer; }
.option-fichier-icone { display:grid; place-items:center; width:58px; height:58px; border-radius:50%; background:#f1f3f5; color:#334155; }
.option-fichier-icone svg { width:25px; height:25px; fill:none; stroke:currentColor; stroke-width:1.8; stroke-linecap:round; stroke-linejoin:round; }
.option-fichier:hover .option-fichier-icone { background:#e7ebef; }

/* L'orbe conserve sa base verte dans les trois états. */
.orbe-dashle { background:radial-gradient(circle at 34% 28%,#d5fff0 0%,#62dcb0 18%,#10a37f 53%,#087355 78%,#043d31 100%); box-shadow:0 0 22px rgba(16,163,127,.55),0 0 72px rgba(8,115,85,.35),inset -16px -18px 28px rgba(0,40,28,.35); transition:filter .45s ease,box-shadow .45s ease; }
.orbe-dashle::after { border-color:rgba(169,255,221,.48); }
#mode-vocal[data-etat="ecoute"] .orbe-dashle,
#mode-vocal[data-etat="parle"] .orbe-dashle { box-shadow:0 0 30px rgba(16,163,127,.7),0 0 100px rgba(8,115,85,.45),inset -16px -18px 28px rgba(0,40,28,.35); }
#mode-vocal[data-etat="reflexion"] .orbe-dashle { filter:hue-rotate(26deg) brightness(1.16) saturate(1.12); box-shadow:0 0 36px rgba(34,211,238,.78),0 0 100px rgba(16,163,127,.62),inset -16px -18px 28px rgba(0,40,28,.35); }
#mode-vocal[data-etat="attente"] .orbe-dashle { filter:saturate(.78) brightness(.92); }
#mode-vocal[data-etat="ecoute"] .orbe-dashle { animation-duration:1.8s; }
#mode-vocal[data-etat="parle"] .orbe-dashle { animation-duration:1.25s; }
#mode-vocal[data-etat="erreur"] .orbe-dashle { filter:hue-rotate(105deg) saturate(.9); box-shadow:0 0 28px rgba(248,113,113,.58),0 0 76px rgba(239,68,68,.28),inset -16px -18px 28px rgba(0,40,28,.35); }
#mode-vocal[data-etat="erreur"] .etat-vocal { color:#fecaca; }

@media (min-width: 851px) {
  #sidebar { display:flex !important; }
  #voile, header > .icon-btn:first-child { display:none !important; }
  header, #banniere-visiteur, #banniere-historique-temporaire { margin-left:272px; }
  #chat { width:min(900px,calc(100% - 304px)); margin-left:calc(272px + max(16px,(100vw - 272px - 900px)/2)); margin-right:16px; }
  form.bas, #apercu-fichier, #statut-vocal, .btn-rouvrir-vocal { width:min(900px,calc(100% - 304px)); margin-left:calc(272px + max(16px,(100vw - 272px - 900px)/2)); margin-right:16px; }
  #sidebar .user-menu-wrap { display:none; }
}
@media (max-width: 850px) {
  #sidebar { display:none; width:82%; max-width:320px; }
  #sidebar[style*="display: block"] { display:flex !important; }
  .sidebar-brand { padding-top:20px; }
  .sidebar-account { margin-top:16px; }
  .ligne-conversation { position:relative; }
  .ligne-conversation .epingle-conversation { flex-basis:44px; width:44px; height:44px; }
  .conversation-menu-toggle { display:inline-flex; flex:0 0 44px; width:44px; height:44px; }
  .conversation-action-list { position:absolute; z-index:20; top:calc(100% - 4px); right:8px; display:flex; flex-direction:column; align-items:stretch; gap:3px; width:190px; padding:6px; border:1px solid var(--bordure); border-radius:13px; background:var(--fond); box-shadow:0 12px 32px rgba(15,35,28,.2); }
  .conversation-action-list[hidden] { display:none; }
  .conversation-action-list .conversation-icon-button { justify-content:flex-start; gap:10px; flex:0 0 44px; width:100%; height:44px; padding:0 12px; border-radius:9px; color:var(--texte); text-align:left; }
  .conversation-action-list .conversation-icon-button:hover,.conversation-action-list .conversation-icon-button:focus-visible { color:#168c65; background:var(--vert-clair); }
  .conversation-action-label { display:inline; font:500 14px/1.2 'Segoe UI',system-ui,sans-serif; }
}
@media (max-width: 600px) {
  .suggestions { grid-template-columns:1fr; }
  .message-wrap { max-width:92%; }
  #banniere-visiteur { flex-wrap:wrap; align-items:center; }
  #banniere-visiteur > span:first-child { flex:1 0 100%; min-width:0; }
  #banniere-visiteur .visitor-actions { margin-left:0; }
  #banniere-visiteur .spacer { display:none; }
}
"""


# ---------------------------------------------------------------------------
# Templates HTML
# ---------------------------------------------------------------------------

# Macro Jinja2 pour le header utilisateur (connecté ou visiteur)
_HEADER_USER_MACRO = """
{% macro header_user() %}
<div class="user-menu-wrap" id="user-menu-wrap">
  {% if utilisateur %}
    <button class="user-badge" id="user-badge-btn" type="button" aria-haspopup="true" aria-expanded="false" title="Menu utilisateur">
      <span class="user-avatar">{{ utilisateur.email[0].upper() }}</span>
      <span class="email-label">{{ utilisateur.email }}</span>
      <svg width="12" height="12" viewBox="0 0 12 12" fill="currentColor"><path d="M6 8L1 3h10z"/></svg>
    </button>
    <div class="user-dropdown" id="user-dropdown" role="menu">
      <div class="email-info">{{ utilisateur.email }}</div>
      <a href="{{ url_for('parametres') }}" role="menuitem">⚙ Paramètres</a>
      <a href="{{ url_for('securite') }}" role="menuitem">🔒 Sécurité</a>
      <div class="separateur"></div>
      <form action="{{ url_for('deconnexion') }}" method="post" style="margin:0;">
        <input type="hidden" name="csrf_token" value="{{ csrf_token }}">
        <button type="submit" role="menuitem">⏻ Se déconnecter</button>
      </form>
    </div>
  {% else %}
    <button class="user-badge" id="user-badge-btn" type="button" aria-haspopup="true" aria-expanded="false" title="Se connecter ou créer un compte">
      <span class="user-avatar">?</span>
      <span class="email-label">Visiteur</span>
      <svg width="12" height="12" viewBox="0 0 12 12" fill="currentColor"><path d="M6 8L1 3h10z"/></svg>
    </button>
    <div class="user-dropdown" id="user-dropdown" role="menu">
      <div class="email-info">Mode visiteur</div>
      <a href="{{ url_for('connexion') }}" role="menuitem">→ Se connecter</a>
      <a href="{{ url_for('inscription') }}" role="menuitem">✚ Créer un compte</a>
    </div>
  {% endif %}
</div>
{% endmacro %}
"""

PAGE = _HEADER_USER_MACRO + """
<!DOCTYPE html>
<html lang="fr">
<head>
<meta charset="UTF-8">
<meta name="google-site-verification" content="Thfhw3_kxuum7bWPLLuLgOrubxOw298KvLDBChfO3Tg" />
<title>Dashle - Votre intelligence artificielle personnelle</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="description" content="Dashle est votre intelligence artificielle personnelle pour échanger, explorer vos idées et demander l’analyse d’images ou de vidéos depuis votre navigateur.">
<link rel="canonical" href="https://dashle.onrender.com/">
<meta property="og:title" content="Dashle — Votre intelligence artificielle personnelle">
<meta property="og:description" content="Échangez avec votre intelligence artificielle personnelle, explorez vos idées et demandez l’analyse d’images ou de vidéos.">
<meta property="og:type" content="website">
<meta property="og:url" content="https://dashle.onrender.com/">
<meta property="og:image" content="https://dashle.onrender.com/static/icons/dashle-icon-1024.png">
<meta property="og:image:alt" content="Icône de Dashle avec couronne">
<meta property="og:locale" content="fr_FR">
<meta name="twitter:card" content="summary_large_image">
<meta name="twitter:title" content="Dashle — Votre intelligence artificielle personnelle">
<meta name="twitter:description" content="Échangez avec votre intelligence artificielle personnelle, explorez vos idées et demandez l’analyse d’images ou de vidéos.">
<meta name="twitter:image" content="https://dashle.onrender.com/static/icons/dashle-icon-1024.png">
<meta name="twitter:url" content="https://dashle.onrender.com/">
<script type="application/ld+json">
{"@context":"https://schema.org","@type":"WebApplication","name":"Dashle","url":"https://dashle.onrender.com/","description":"Dashle est une intelligence artificielle personnelle accessible depuis un navigateur pour échanger par écrit et demander l’analyse d’images ou de vidéos."}
</script>
<script defer src="{{ url_for('static', filename='vendor/marked.min.js') }}"></script>
<script defer src="{{ url_for('static', filename='vendor/purify.min.js') }}"></script>
<link rel="manifest" href="/static/manifest.json">
<link rel="icon" type="image/png" sizes="1024x1024" href="/static/icons/dashle-icon-1024.png">
<link rel="icon" type="image/png" sizes="512x512" href="/static/icons/dashle-icon-512.png">
<link rel="icon" type="image/png" sizes="192x192" href="/static/icons/dashle-icon-192.png">
<link rel="icon" type="image/png" sizes="48x48" href="/static/icons/dashle-icon-48.png">
<link rel="apple-touch-icon" sizes="192x192" href="/static/icons/dashle-icon-192.png">
<meta name="theme-color" content="#22C55E">
<script>
if ('serviceWorker' in navigator) {
  navigator.serviceWorker.register('/static/service-worker.js');
}
</script>
<style>{{ css }}</style>
</head>
<body class="theme-{{ preferences.theme }}">

<script>
(function(){
  if (!/DASHLE-Android/i.test(navigator.userAgent) || !window.visualViewport) return;
  const root = document.documentElement;
  const updateViewport = function(){
    if (window.visualViewport.scale !== 1) return;
    root.style.setProperty('--dashle-viewport-height', Math.round(window.visualViewport.height) + 'px');
  };
  window.visualViewport.addEventListener('resize', updateViewport, {passive:true});
  window.addEventListener('resize', updateViewport, {passive:true});
  updateViewport();
})();
</script>

<header>
  <button class="icon-btn" onclick="document.getElementById('sidebar').style.display='block';document.getElementById('voile').style.display='block';" aria-label="Menu" title="Menu">&#9776;</button>
  <div class="logo-wrap">
    <img class="logo" src="{{ url_for('static', filename='icons/dashle-logo-header.png') }}" alt="Dashle">
    <span class="titre">Dashle</span>
  </div>
  {% if utilisateur %}
    <form action="{{ url_for('nouvelle_conv') }}" method="post" style="margin:0;">
      <input type="hidden" name="csrf_token" value="{{ csrf_token }}">
      <button class="icon-btn" type="submit" aria-label="Nouvelle conversation" title="Nouvelle conversation">+</button>
    </form>
  {% endif %}
  {{ header_user() }}
</header>

{% if not utilisateur %}
<div id="banniere-visiteur" class="visible" role="complementary" aria-label="Mode visiteur">
  <span>💬 Tu discutes en mode visiteur. Ta conversation est temporaire.</span>
  <span class="spacer"></span>
  <span class="visitor-actions"><a href="{{ url_for('connexion') }}">Se connecter</a>
  <span style="margin:0 4px;">·</span>
  <a href="{{ url_for('inscription') }}">Créer un compte</a>
</span>
</div>
{% elif not preferences.conserver_historique %}
<div id="banniere-historique-temporaire" class="visible" role="status">
  La conservation est d&eacute;sactiv&eacute;e : les nouveaux &eacute;changes restent dans cette page jusqu'au rechargement. Les conversations d&eacute;j&agrave; enregistr&eacute;es restent intactes.
</div>
{% endif %}

<div id="voile" onclick="document.getElementById('sidebar').style.display='none';this.style.display='none';"></div>

<div id="sidebar" data-compte="{{ utilisateur.email if utilisateur else 'visiteur' }}">
  <div class="sidebar-brand">
    <img src="{{ url_for('static', filename='icons/dashle-logo-header.png') }}" alt="">
    <span>DASHLE</span>
  </div>
  <form action="{{ url_for('nouvelle_conv') }}" method="post" style="margin:0 12px 10px;">
    {% if utilisateur %}<input type="hidden" name="csrf_token" value="{{ csrf_token }}">{% endif %}
    <button class="nouvelle" type="submit" style="width:100%;text-align:left;border:1px solid var(--bordure);border-radius:10px;">&#43; Nouvelle conversation</button>
  </form>
  {% if utilisateur %}
    <input class="recherche-conversations" id="recherche-conversations" type="search" placeholder="Rechercher dans l'historique..." aria-label="Rechercher dans l'historique">
    <div class="sidebar-conversations">
      <div class="menu-section">&Eacute;pingl&eacute;es</div>
      <div id="conversations-epinglees"></div>
      <div class="menu-section">R&eacute;centes</div>
      <div id="conversations-recentes">
        {% for conv in conversations %}
          <div class="ligne-conversation" data-conv-id="{{ conv.id }}" data-titre="{{ conv.titre|lower }}" style="display:flex;align-items:center;">
            <button type="button" class="conversation-icon-button epingle-conversation" aria-pressed="false" title="&Eacute;pingler" aria-label="&Eacute;pingler cette conversation"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="m12 3 2.8 5.7 6.2.9-4.5 4.4 1.1 6.2-5.6-3-5.6 3 1.1-6.2L3 9.6l6.2-.9L12 3z"/></svg></button>
            <a href="{{ url_for('charger_conv', i=conv.id) }}" class="{{ 'actif' if conv.id == conversation_id else '' }}" style="flex:1;">{{ conv.titre }}</a>
            <button type="button" class="conversation-icon-button conversation-menu-toggle" aria-label="Actions de la conversation" aria-expanded="false" aria-controls="actions-conv-{{ conv.id }}" title="Actions"><svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="5" r="1"/><circle cx="12" cy="12" r="1"/><circle cx="12" cy="19" r="1"/></svg></button>
            <div class="conversation-action-list" id="actions-conv-{{ conv.id }}" aria-label="Actions de la conversation" hidden>
              <button type="button" class="conversation-icon-button" title="Partager" aria-label="Partager" onclick="partagerConversation({{ conv.id }})"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M10 13a5 5 0 0 0 7.1 0l3-3A5 5 0 0 0 13 2.9l-1.7 1.7"/><path d="M14 11a5 5 0 0 0-7.1 0l-3 3A5 5 0 0 0 11 21.1l1.7-1.7"/></svg><span class="conversation-action-label">Partager</span></button>
              <form class="conversation-action-form" action="{{ url_for('archiver_conv', i=conv.id) }}" method="post"><input type="hidden" name="csrf_token" value="{{ csrf_token }}"><button type="submit" class="conversation-icon-button" title="Archiver — masquer des récentes" aria-label="Archiver cette conversation (la masquer des récentes)"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M3 4h18v4H3z"/><path d="M5 8v12h14V8M10 12h4"/></svg><span class="conversation-action-label">Archiver des récentes</span></button></form>
              <form class="conversation-action-form" action="{{ url_for('supprimer_conv', i=conv.id) }}" method="post"><input type="hidden" name="csrf_token" value="{{ csrf_token }}"><button type="submit" class="conversation-icon-button" onclick="return confirm('Supprimer cette conversation ?');" title="Supprimer" aria-label="Supprimer cette conversation"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M3 6h18M8 6V4h8v2m3 0-1 14H6L5 6m4 4v6m6-6v6"/></svg><span class="conversation-action-label">Supprimer</span></button></form>
            </div>
          </div>
        {% else %}
          <div class="sidebar-vide">Tes conversations appara&icirc;tront ici.</div>
        {% endfor %}
      </div>
    </div>
  {% else %}
    <div class="sidebar-vide">Mode visiteur : cette conversation est temporaire.</div>
  {% endif %}
  <div class="menu-section">Navigation</div>
  <a href="{{ url_for('actualites') }}">&#128240; Nouveaut&eacute;s DASHLE</a>
  <a href="{{ url_for('tarifs') }}">&#9733; Tarifs</a>
  <a href="{{ url_for('temps_reel') }}">&#127780; Temps r&eacute;el</a>
  {% if est_admin %}<a href="{{ url_for('admin') }}">Administration</a>{% endif %}
  {% if utilisateur %}
    <a href="{{ url_for('parametres') }}">&#9881; Param&egrave;tres</a>
    <a href="{{ url_for('statistiques') }}">&#128202; Statistiques</a>
  {% endif %}
  {% if utilisateur %}
    <a href="{{ url_for('planification') }}">&#128197; Planification</a>
    <a href="{{ url_for('taches_planifiees') }}">&#9200; Tâches planifiées</a>
    <a href="{{ url_for('plugins') }}">&#128268; Plugins</a>
    <a href="{{ url_for('projets') }}">&#128193; Projets</a>
    <a href="{{ url_for('bibliotheque') }}">&#128218; Biblioth&egrave;que</a>
  {% else %}
    <a href="{{ url_for('connexion') }}">&#128197; Planification</a>
    <a href="{{ url_for('connexion') }}">&#9200; Tâches planifiées</a>
    <a href="{{ url_for('connexion') }}">&#128268; Plugins</a>
    <a href="{{ url_for('connexion') }}">&#128193; Projets</a>
  {% endif %}
  <div class="sidebar-account">
    {% if utilisateur %}
      <div class="user-badge" title="{{ utilisateur.email }}"><span class="user-avatar">{{ utilisateur.email[0].upper() }}</span><span class="email-label">{{ utilisateur.email }}</span></div>
      <div class="sidebar-account-links">
        <a href="{{ url_for('parametres') }}">&#9881; Param&egrave;tres</a>
        <form action="{{ url_for('deconnexion') }}" method="post" style="margin:0;flex:1;">
          <input type="hidden" name="csrf_token" value="{{ csrf_token }}">
          <button type="submit" style="width:100%;padding:8px 6px;border:0;border-radius:8px;background:transparent;font:inherit;font-size:13px;cursor:pointer;">&D&eacute;connexion</button>
        </form>
      </div>
    {% else %}
      <div class="sidebar-account-links">
        <a href="{{ url_for('connexion') }}">Se connecter</a>
        <a href="{{ url_for('inscription') }}">Cr&eacute;er un compte</a>
      </div>
    {% endif %}
  </div>
</div>

<div id="chat">
  {% if not messages %}
    <section class="accueil-vide" aria-label="Accueil DASHLE">
      <img src="{{ url_for('static', filename='icons/dashle-logo-header.png') }}" alt="Logo DASHLE">
      <h1>{{ message_accueil }}</h1>
      <p>Votre IA personnelle pour échanger, explorer vos idées et demander l’analyse d’images ou de vidéos.</p>
      <div class="suggestions">
        <button type="button" class="suggestion">Aide-moi à organiser ma journée</button>
        <button type="button" class="suggestion">Explique-moi un sujet simplement</button>
        <button type="button" class="suggestion">Aide-moi à écrire un message</button>
        <button type="button" class="suggestion">Donne-moi des idées de repas</button>
      </div>
    </section>
  {% endif %}
  {% for m in messages %}
    <div class="message-wrap {{ 'user' if m.auteur == 'user' else 'bot' }}">
      {% if m.get('image_preview') %}
      <div class="message-image-persistante">
        <img src="{{ m.get('image_preview') }}" alt="Image envoyée" loading="lazy">
      </div>
      {% endif %}
      <div class="msg {{ 'user' if m.auteur == 'user' else 'bot' }}" data-message-id="{{ m.get('id','') }}">{{ m.texte }}</div>
      {% if m.auteur == 'bot' %}
      <div class="actions-reponse">
        <button type="button" class="action-copier" title="Copier" aria-label="Copier"><svg viewBox="0 0 24 24" aria-hidden="true"><rect x="8" y="8" width="12" height="13" rx="2"/><path d="M16 8V5a2 2 0 0 0-2-2H5a2 2 0 0 0-2 2v11a2 2 0 0 0 2 2h3"/></svg></button>
        {% if utilisateur and preferences.conserver_historique %}
          <button type="button" class="action-feedback" data-valeur="positif" title="J'aime" aria-label="J'aime"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M7 10v11H4a2 2 0 0 1-2-2v-7a2 2 0 0 1 2-2h3Zm0 0 5-7a3 3 0 0 1 2 3v4h5a2 2 0 0 1 2 2l-2 7a2 2 0 0 1-2 2H7"/></svg></button>
          <button type="button" class="action-feedback" data-valeur="negatif" title="Je n'aime pas" aria-label="Je n'aime pas"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M7 14V3H4a2 2 0 0 0-2 2v7a2 2 0 0 0 2 2h3Zm0 0 5 7a3 3 0 0 0 2-3v-4h5a2 2 0 0 0 2-2l-2-7a2 2 0 0 0-2-2H7"/></svg></button>
          <button type="button" class="action-partager" title="Partager" aria-label="Partager"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 16V4m-5 5 5-5 5 5M5 12v7h14v-7"/></svg></button>
          <button type="button" class="action-regenerer" title="Régénérer" aria-label="Régénérer"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M20 7v5h-5M20 12a8 8 0 1 0 2 5"/></svg></button>
        {% endif %}
        <button type="button" class="action-repondre" title="Répondre à ce message" aria-label="Répondre à ce message"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="m9 14-5-5 5-5M4 9h10a6 6 0 0 1 0 12h-1"/></svg></button>
        <button type="button" class="action-lire" title="Lecture / pause" aria-label="Lecture / pause"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="m8 5 12 7-12 7z"/></svg></button>
        <button type="button" class="action-stop" title="Arrêter" aria-label="Arrêter"><svg viewBox="0 0 24 24" aria-hidden="true"><rect x="5" y="5" width="14" height="14" rx="2"/></svg></button>
        <span class="lecture-etat"></span>
      </div>
      {% endif %}
    </div>
  {% endfor %}
</div>

<section id="mode-vocal" data-etat="attente" aria-label="Conversation vocale" inert>
  <div class="vocal-entete">
    <strong>Conversation vocale</strong>
    <div class="vocal-commandes">
      <button type="button" id="reduire-vocal" title="Réduire">Réduire</button>
      <button type="button" id="fermer-vocal" title="Quitter le mode vocal">Fermer</button>
    </div>
  </div>
  <div class="scene-vocale">
    <div class="systeme-solaire" aria-hidden="true">
      <div class="orbite" style="--taille:58%;--vitesse:11s"><span class="planete" style="--diametre:9px;--couleur:#b8ffe5"></span></div>
      <div class="orbite" style="--taille:78%;--vitesse:17s"><span class="planete" style="--diametre:13px;--couleur:#62dcb0"></span></div>
      <div class="orbite" style="--taille:98%;--vitesse:25s"><span class="planete" style="--diametre:7px;--couleur:#d5fff0"></span></div>
      <div class="orbe-dashle" id="orbe-dashle" role="button" tabindex="0" aria-label="Appuie pour interrompre Dashle"></div>
    </div>
    <div class="etat-vocal" id="etat-vocal">En attente</div>
  </div>
</section>
<form class="bas" id="form-message" autocomplete="off" method="post" action="">
  <input type="file" id="image-input" accept="image/*,video/*" style="display:none;">
  <button type="button" id="btn-attach" class="btn-attach" title="Ajouter une image ou vidéo" aria-label="Ajouter une image ou vidéo" aria-haspopup="dialog" aria-controls="feuille-fichiers">+
  </button>
  <textarea id="message" name="message" rows="1" placeholder="Écris à Dashle..." aria-label="Message"></textarea>
  <div class="groupe-actions">
    <button type="button" class="arreter" id="btn-arreter" title="Arrêter la génération" aria-label="Arrêter">&#9632;</button>
    <button type="button" class="vocal" id="btn-vocal" title="Conversation vocale">
      <svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round">
        <line x1="2"  y1="9"  x2="2"  y2="15"></line>
        <line x1="7"  y1="6"  x2="7"  y2="18"></line>
        <line x1="12" y1="3"  x2="12" y2="21"></line>
        <line x1="17" y1="6"  x2="17" y2="18"></line>
        <line x1="22" y1="9"  x2="22" y2="15"></line>
      </svg>
    </button>
    <button type="button" class="micro" id="btn-micro" title="Dicter un message" aria-label="Dicter un message">🎙️</button>
    <button class="envoyer" type="submit" id="btn-envoyer" aria-label="Envoyer">&#10148;</button>
  </div>
</form>

<div id="feuille-fichiers" class="feuille-fichiers-voile" role="presentation" hidden>
  <section class="feuille-fichiers" role="dialog" aria-modal="true" aria-labelledby="titre-feuille-fichiers">
    <h2 id="titre-feuille-fichiers">Ajouter un fichier</h2>
    <div class="feuille-fichiers-options">
      <button type="button" class="option-fichier" data-source-fichier="camera">
        <span class="option-fichier-icone" aria-hidden="true"><svg viewBox="0 0 24 24"><path d="M4 7h3l1.5-2h7L17 7h3v12H4z"></path><circle cx="12" cy="13" r="3.5"></circle></svg></span>
        <span>Caméra</span>
      </button>
      <button type="button" class="option-fichier" data-source-fichier="photos">
        <span class="option-fichier-icone" aria-hidden="true"><svg viewBox="0 0 24 24"><rect x="4" y="4" width="16" height="16" rx="3"></rect><circle cx="9" cy="9" r="1.5"></circle><path d="m5 17 5-5 3 3 2-2 4 4"></path></svg></span>
        <span>Photos</span>
      </button>
      <button type="button" class="option-fichier" data-source-fichier="fichiers">
        <span class="option-fichier-icone" aria-hidden="true"><svg viewBox="0 0 24 24"><path d="M5 3h9l5 5v13H5z"></path><path d="M14 3v6h5M8 14h8M8 17h8"></path></svg></span>
        <span>Fichiers</span>
      </button>
    </div>
  </section>
</div>

<div id="apercu-fichier" aria-live="polite">
  <img id="apercu-fichier-media" alt="Aperçu du fichier sélectionné">
  <div id="apercu-fichier-info">
    <span id="apercu-fichier-nom"></span>
    <span id="apercu-fichier-type"></span>
  </div>
  <button type="button" id="retirer-fichier" aria-label="Retirer le fichier" title="Retirer">&times;</button>
</div>

<div id="statut-vocal" aria-live="polite"></div>
<button type="button" id="btn-rouvrir-vocal" class="btn-rouvrir-vocal" title="Rouvrir la conversation vocale" aria-label="Rouvrir le mode vocal">
  🎙 Vue vocale
</button>
"""

# Javascript principal — injecté dans PAGE
_JS = r"""
<script>
// =====================================================================
// Variables globales
// =====================================================================
const chat        = document.getElementById('chat');
const form        = document.getElementById('form-message');
const champ       = document.getElementById('message');
const btnEnvoyer  = document.getElementById('btn-envoyer');
const orbeDashle = document.getElementById('orbe-dashle');
const btnMicro    = document.getElementById('btn-micro');
const btnVocal    = document.getElementById('btn-vocal');
const statutVocal = document.getElementById('statut-vocal');
const modeVocalEl = document.getElementById('mode-vocal');
const etatVocalEl = document.getElementById('etat-vocal');
const apercuFichierEl = document.getElementById('apercu-fichier');
let   apercuMedia     = document.getElementById('apercu-fichier-media');
const apercuNom       = document.getElementById('apercu-fichier-nom');
const apercuType      = document.getElementById('apercu-fichier-type');
const inputImage      = document.getElementById('image-input');
const btnRouvrirVocal = document.getElementById('btn-rouvrir-vocal');

// CSRF token injecté côté serveur
document.querySelectorAll('a[href*="/parametres"]').forEach(function(lien) {
  let appuis = 0, dernierAppui = 0;
  lien.addEventListener('click', function() {
    const maintenant = Date.now();
    appuis = maintenant - dernierAppui < 1400 ? appuis + 1 : 1;
    dernierAppui = maintenant;
    if (appuis >= 5) {
      try { localStorage.setItem('dashle_vocal_diag_open', '1'); } catch(e) {}
      appuis = 0;
    }
  });
});
const csrfToken        = __CSRF_TOKEN__;
const preferencesVocales = __PREFS_VOCALES__;
if (preferencesVocales.voix_active === false) {
  btnVocal.disabled = true;
  btnMicro.disabled = true;
  btnVocal.title = 'Mode vocal désactivé dans les paramètres';
  btnMicro.title = 'Mode vocal désactivé dans les paramètres';
}
const estConnecte      = __EST_CONNECTE__;
const urlFlux          = __URL_FLUX__;
const urlImage         = __URL_IMAGE__;
const conversationId   = __CONV_ID__;
let reco = null;
let recoEnCours = false;
let recoResultatsAutorises = false;
let recoDebutEcouteMs = 0;
let dernierTranscriptDictee = '';

// États vocaux
let vocalActif          = false;
let modeActuel          = 'texte';   // 'texte' | 'dictee' | 'vocal'
let requeteActiveController = null;
let reponseActiveElement = null;
let reponseEnCours      = false;
let interruptionDemandee = false;
let texteGenerationEnCours = '';
let generationInterrompueParReseau = false;

// VAD (Voice Activity Detection) — interruption pendant que Dashle parle
let vadStream       = null;
let vadAudioContext = null;
let vadAnalyser     = null;
let vadSource       = null;
let vadAnimation    = null;
let vadDerniereDetection = 0;
let vadDebutParole  = 0;
let vadPret         = false;
let vadBruitBase = 0.01;
let vadSeuilCourant = 0.06;

// Flag : vrai pendant toute la durée d'une synthèse vocale pour éviter
// que le VAD ne déclenche une interruption sur la voix de Dashle lui-même.
// Timestamp du début de la dernière synthèse. Utilisé uniquement pour
// le délai anti-écho post-fin de synthèse (laisser l'écho s'estomper).
let vadDebutSynthese = 0;

// Verrou d'état binaire : true pendant TOUTE la durée réelle de la synthèse
// vocale, de utteranceActuelle.onstart jusqu'à onend/onerror.
// Le VAD conserve la détection de barge-in pendant le TTS; la reconnaissance, elle, est mutée.
let syntheseEnCours = false;

// true quand c'est le TTS qui a forcé l'arrêt de reco (pas un silence naturel).
// Empêche reco.onend de relancer reco automatiquement pendant la synthèse.
let recoMutePendantTTS = false;

const VAD_SEUIL       = 0.06;  // RMS plancher pour "parole humaine"
const VAD_DUREE_MIN   = 350;   // ms continus avant interruption (anti-plosive)
const VAD_MARGE        = 0.025; // marge au-dessus du bruit ambiant estimé
const VAD_FACTEUR      = 3.0;   // seuil adaptatif = bruit x facteur + marge
const VAD_COOLDOWN    = 1200;  // ms minimum entre deux interruptions
const VAD_DELAI_POST  = 350;   // ms de délai anti-écho après fin réelle de synthèse
const AUDIO_SECOURS_MAX_MS = 20000;
const AUDIO_SECOURS_SILENCE_MS = 1200;
let audioSecoursActif = false;
let audioSecoursRecorder = null;
let audioSecoursStream = null;
let audioSecoursContext = null;
let audioSecoursAnimation = null;
let audioSecoursDebut = 0;
let audioSecoursParoleDepuis = 0;

function arreterSecoursAudio() {
  if (audioSecoursAnimation) cancelAnimationFrame(audioSecoursAnimation);
  audioSecoursAnimation = null;
  if (audioSecoursRecorder && audioSecoursRecorder.state !== 'inactive') {
    try { audioSecoursRecorder.stop(); } catch(e) {}
  }
  if (audioSecoursStream) audioSecoursStream.getTracks().forEach(function(t) { t.stop(); });
  audioSecoursStream = null;
  if (audioSecoursContext) { try { audioSecoursContext.close(); } catch(e) {} }
  audioSecoursContext = null;
  audioSecoursRecorder = null;
  audioSecoursActif = false;
}

async function demarrerSecoursAudio() {
  if (audioSecoursActif || !navigator.mediaDevices || !navigator.mediaDevices.getUserMedia
      || typeof window.MediaRecorder !== 'function') {
    return false;
  }
  try {
    audioSecoursStream = await navigator.mediaDevices.getUserMedia({
      audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true, channelCount: 1 }
    });
    let mime = 'audio/webm';
    if (MediaRecorder.isTypeSupported && MediaRecorder.isTypeSupported('audio/webm;codecs=opus')) {
      mime = 'audio/webm;codecs=opus';
    } else if (MediaRecorder.isTypeSupported && MediaRecorder.isTypeSupported('audio/ogg;codecs=opus')) {
      mime = 'audio/ogg;codecs=opus';
    }
    const morceaux = [];
    audioSecoursRecorder = new MediaRecorder(audioSecoursStream, { mimeType: mime });
    audioSecoursActif = true;
    audioSecoursDebut = performance.now();
    audioSecoursParoleDepuis = 0;
    afficherEtatVocal('ecoute', 'Enregistrement de secours…');
    afficherStatutVocal("🎙️ Ton audio sera envoyé à Dashle pour transcription.");

    audioSecoursRecorder.ondataavailable = function(e) {
      if (e.data && e.data.size) morceaux.push(e.data);
    };
    audioSecoursRecorder.onstop = async function() {
      const dureeMs = Math.min(AUDIO_SECOURS_MAX_MS, Math.round(performance.now() - audioSecoursDebut));
      const blob = new Blob(morceaux, { type: mime });
      arreterSecoursAudio();
      if (!blob.size) {
        afficherEtatVocal('erreur', "Je n'arrive pas à t'entendre, réessaie");
        afficherStatutVocal("Je n'arrive pas à t'entendre, réessaie");
        return;
      }
      const formAudio = new FormData();
      formAudio.append('audio', blob, 'dashle-vocal.' + (mime.indexOf('ogg') >= 0 ? 'ogg' : 'webm'));
      try {
        const rep = await fetch('/api/transcrire', {
          method: 'POST',
          headers: { 'X-CSRF-Token': csrfToken, 'X-Dashle-Audio-Duration-Ms': String(dureeMs) },
          body: formAudio,
          credentials: 'same-origin'
        });
        const data = await rep.json();
        if (!rep.ok || !data.texte) throw new Error(data.erreur || 'Transcription indisponible');
        champ.value = data.texte;
        champ.style.height = 'auto';
        if (vocalActif) {
          afficherEtatVocal('reflexion', 'Dashle réfléchit…');
          afficherStatutVocal('');
          try { form.requestSubmit(); } catch(e) { form.dispatchEvent(new Event('submit', {bubbles:true,cancelable:true})); }
        }
      } catch(e) {
        console.warn('[DASHLE][AudioFallback] échec transcription', e && e.message);
        afficherEtatVocal('erreur', "Je n'arrive pas à t'entendre, réessaie");
        afficherStatutVocal(e && e.message ? e.message : "Je n'arrive pas à t'entendre, réessaie");
      }
    };

    const AudioCtx = window.AudioContext || window.webkitAudioContext;
    if (AudioCtx) {
      audioSecoursContext = new AudioCtx();
      const source = audioSecoursContext.createMediaStreamSource(audioSecoursStream);
      const analyser = audioSecoursContext.createAnalyser();
      analyser.fftSize = 1024;
      source.connect(analyser);
      const donnees = new Uint8Array(analyser.fftSize);
      const verifier = function() {
        if (!audioSecoursActif) return;
        const elapsed = performance.now() - audioSecoursDebut;
        analyser.getByteTimeDomainData(donnees);
        let somme = 0;
        for (let i = 0; i < donnees.length; i++) {
          const x = (donnees[i] - 128) / 128;
          somme += x * x;
        }
        const rms = Math.sqrt(somme / donnees.length);
        if (rms > 0.035) {
          audioSecoursParoleDepuis = performance.now();
        } else if (audioSecoursParoleDepuis && performance.now() - audioSecoursParoleDepuis >= AUDIO_SECOURS_SILENCE_MS) {
          try { audioSecoursRecorder.stop(); } catch(e) {}
          return;
        }
        if (elapsed >= AUDIO_SECOURS_MAX_MS) {
          try { audioSecoursRecorder.stop(); } catch(e) {}
          return;
        }
        audioSecoursAnimation = requestAnimationFrame(verifier);
      };
      audioSecoursAnimation = requestAnimationFrame(verifier);
    } else {
      setTimeout(function() {
        if (audioSecoursActif) { try { audioSecoursRecorder.stop(); } catch(e) {} }
      }, AUDIO_SECOURS_MAX_MS);
    }
    audioSecoursRecorder.start(250);
    return true;
  } catch(e) {
    arreterSecoursAudio();
    console.warn('[DASHLE][AudioFallback] micro indisponible', e);
    return false;
  }
}



// Compteur de backoff pour les relances SpeechRecognition sans résultat.
let nbRelancesVocal = 0;
let minuteurRelanceReco = null;
const DELAI_RELANCE_RECO_INITIAL = 300;
const DELAI_RELANCE_RECO_MAX = 2000;
const MAX_PALIERS_RELANCE_RECO = 6;
let nbFinsSansTranscriptionVocal = 0;
const DUREE_FIN_IMMEDIATE_VOCAL_MS = 1200;
const MAX_FINS_SANS_TRANSCRIPTION_VOCAL = 3;

// Attendre que les résultats finaux se stabilisent avant d'envoyer le tour vocal.
let transcriptionFinaleVocale = '';
const VOCAL_DIAG_STORAGE_KEY = 'dashle_vocal_diagnostic_v1';
function journaliserDiagnosticVocal(type, detail) {
  try {
    const courant = JSON.parse(localStorage.getItem(VOCAL_DIAG_STORAGE_KEY) || '[]');
    courant.push({ ts: new Date().toISOString(), type: String(type || ''), detail: detail || null });
    while (courant.length > 120) courant.shift();
    localStorage.setItem(VOCAL_DIAG_STORAGE_KEY, JSON.stringify(courant));
  } catch (e) {}
}
let dernierIndexFinalVocal = 0;
let minuteurFinPhraseVocale = null;
const DELAI_FIN_PHRASE_VOCALE = 1600;

function annulerFinPhraseVocale() {
  if (minuteurFinPhraseVocale !== null) {
    clearTimeout(minuteurFinPhraseVocale);
    minuteurFinPhraseVocale = null;
  }
}

function reinitialiserTranscriptionVocale() {
  annulerFinPhraseVocale();
  transcriptionFinaleVocale = '';
  dernierIndexFinalVocal = 0;
}

function ajouterTexteFinalVocalUnique(texte) {
  const candidat = String(texte || '').trim().replace(/\s+/g, ' ');
  if (!candidat) return;
  const courant = transcriptionFinaleVocale.trim().replace(/\s+/g, ' ');
  if (!courant) {
    transcriptionFinaleVocale = candidat;
    return;
  }
  if (courant === candidat || courant.endsWith(' ' + candidat)) return;
  if (candidat.startsWith(courant + ' ')) {
    transcriptionFinaleVocale = candidat;
    return;
  }
  const motsCourant = courant.split(' ');
  const motsCandidat = candidat.split(' ');
  let chevauchement = 0;
  const maximum = Math.min(motsCourant.length, motsCandidat.length);
  for (let taille = maximum; taille > 0; taille -= 1) {
    if (motsCourant.slice(-taille).join(' ') === motsCandidat.slice(0, taille).join(' ')) {
      chevauchement = taille;
      break;
    }
  }
  const suffixe = motsCandidat.slice(chevauchement).join(' ');
  if (suffixe) transcriptionFinaleVocale = courant + ' ' + suffixe;
}

function planifierEnvoiFinPhraseVocale() {
  annulerFinPhraseVocale();
  minuteurFinPhraseVocale = setTimeout(function() {
    minuteurFinPhraseVocale = null;
    if (!vocalActif || reponseEnCours || syntheseEnCours || recoMutePendantTTS) return;
    const texteComplet = transcriptionFinaleVocale.trim()  // resultIndex repart à zéro ; ne pas dupliquer les finals;
    if (!texteComplet) return;

    transcriptionFinaleVocale = '';
    dernierIndexFinalVocal = 0;
    recoResultatsAutorises = false;
    champ.value = texteComplet;
    champ.style.height = 'auto';
    afficherEtatVocal('reflexion', 'Dashle réfléchit...');
    afficherStatutVocal('');
    // Le gestionnaire d'envoi coupe l'écoute avant de lancer la requête SSE.
    try { form.requestSubmit(); }
    catch(errSub) { form.dispatchEvent(new Event('submit', { bubbles: true, cancelable: true })); }
  }, DELAI_FIN_PHRASE_VOCALE);
}

// Identifiant du watchdog d'écoute (clearTimeout pour l'annuler).
let watchdogEcoute = null;

// Arme le watchdog : si reco ne produit pas de résultat dans WATCHDOG_MS,
// on force un abort + relance. Annulé dès onresult, onend ou onerror.
const WATCHDOG_MS = 9000;

function armerWatchdog() {
  desarmerWatchdog();
  watchdogEcoute = setTimeout(function() {
    if (!vocalActif || reponseEnCours || syntheseEnCours || transcriptionFinaleVocale.trim()) return;
    console.warn('[DASHLE] Watchdog écoute déclenché — relance reco');
    recoEnCours = false;
    try { reco && reco.abort(); } catch(e) {}
    setTimeout(function() {
      if (vocalActif && !recoEnCours && !reponseEnCours && !syntheseEnCours) {
        demarrerEcouteVocale();
      }
    }, 300);
  }, WATCHDOG_MS);
}

function desarmerWatchdog() {
  if (watchdogEcoute) { clearTimeout(watchdogEcoute); watchdogEcoute = null; }
}

// Réinitialise TOUS les flags et stoppe tout — utilisé avant un redémarrage
// complet du mode vocal (P1-B).
function reinitialiserEtatVocal() {
  desarmerWatchdog();
  syntheseEnCours    = false;
  recoMutePendantTTS = false;
  recoResultatsAutorises = false;
  vadDebutSynthese   = 0;
  interruptionDemandee = false;
  recoEnCours        = false;
  nbRelancesVocal    = 0;
  if ('speechSynthesis' in window) window.speechSynthesis.cancel();
  arreterGeneration();
  try { reco && reco.abort(); } catch(e) {}
  arreterVAD();
  if (lectureActuelle && lectureActuelle.isConnected) {
    lectureActuelle.classList.remove('actif');
    lectureActuelle.textContent = '▶';
  }
  lectureActuelle  = null;
  utteranceActuelle = null;
}

// =====================================================================
// Helpers UI
// =====================================================================
function journaliserEtatAudioReconnaissance() {
  let gum = 'indisponible';
  try {
    const tracks = vadStream && typeof vadStream.getAudioTracks === 'function'
      ? vadStream.getAudioTracks() : [];
    gum = tracks.length ? tracks.map(function(t) { return t.readyState; }) : 'aucun flux actif';
  } catch (e) { gum = 'erreur'; }
  let audioContext = vadAudioContext ? vadAudioContext.state : 'aucun';
  let mediaRecorder = typeof window.MediaRecorder === 'function';
  let tts = 'indisponible';
  let audioElement = null;
  try {
    tts = ('speechSynthesis' in window)
      ? { speaking: window.speechSynthesis.speaking, pending: window.speechSynthesis.pending }
      : 'indisponible';
    audioElement = document.querySelector('audio');
    audioElement = audioElement ? {
      paused: audioElement.paused, readyState: audioElement.readyState,
      currentTime: audioElement.currentTime
    } : 'aucun élément audio';
  } catch (e) { audioElement = 'erreur'; }
  console.log('[DASHLE][AudioState]', {
    getUserMedia: gum, AudioContext: audioContext, MediaRecorder: mediaRecorder,
    speechSynthesis: tts, audioTTS: audioElement
  });
}

function afficherEtatVocal(etat, libelle) {
  modeVocalEl.dataset.etat = etat;
  etatVocalEl.textContent  = libelle;
}

function synchroniserVueVocale() {
  if (vocalActif) return true;
  if (modeVocalEl.contains(document.activeElement)) {
    try { document.activeElement.blur(); } catch(e) {}
  }
  modeVocalEl.setAttribute('inert', '');
  modeVocalEl.classList.remove('visible');
  if (btnRouvrirVocal) btnRouvrirVocal.classList.remove('actif');
  return false;
}

function ouvrirModeVocal() {
  if (!synchroniserVueVocale()) return false;
  modeVocalEl.classList.add('visible');
  modeVocalEl.removeAttribute('inert');
  if (btnRouvrirVocal) btnRouvrirVocal.classList.remove('actif');
  return true;
}

function fermerModeVocal(autoriserRouvrir = true) {
  if (modeVocalEl.contains(document.activeElement)) {
    try { document.activeElement.blur(); } catch(e) {}
  }
  modeVocalEl.setAttribute('inert', '');
  modeVocalEl.classList.remove('visible');
  if (btnRouvrirVocal) {
    btnRouvrirVocal.classList.toggle('actif', Boolean(vocalActif && autoriserRouvrir));
  }
}

function desactiverModeVocal() {
  vocalActif = false;
  modeActuel = 'texte';
  interruptionDemandee = true;
  reinitialiserTranscriptionVocale();
  annulerFinPhraseVocale();
  if (minuteurRelanceReco !== null) {
    clearTimeout(minuteurRelanceReco);
    minuteurRelanceReco = null;
  }
  reinitialiserEtatVocal();
  arreterSecoursAudio();
  btnVocal.classList.remove('vocal-on', 'ecoute', 'parle');
  btnMicro.classList.remove('actif');
  afficherStatutVocal('');
  fermerModeVocal(false);
  afficherEtatVocal('attente', 'En attente');
  interruptionDemandee = false;
}


// Source de vérité : au chargement, le mode vocal désactivé ne peut jamais
// laisser une vue ou un bouton de réouverture visibles.
synchroniserVueVocale();
function afficherStatutVocal(texte) {
  statutVocal.textContent = texte;
  statutVocal.classList.toggle('visible', !!texte);
}

function echapperHtml(texte) {
  return String(texte).replace(/[&<>"']/g, function(c) {
    return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c];
  });
}

function texteSansMarqueursMarkdown(texte) {
  return String(texte || '')
    .replace(/!\[([^\]]*)\]\([^)]*\)/g, '$1')
    .replace(/\[([^\]]+)\]\([^)]*\)/g, '$1')
    .replace(/\x60\x60\x60[\w+-]*\n?([\s\S]*?)\x60\x60\x60/g, '$1')
    .replace(/\x60([^\x60\n]+)\x60/g, '$1')
    .replace(/[*_~#>]/g, '')
    .replace(/^[-+*]\s+/gm, '')
    .replace(/^\d+[.)]\s+/gm, '')
    .replace(/\n{3,}/g, '\n\n')
    .replace(/[ \t]{2,}/g, ' ')
    .trim();
}
function rendreMarkdown(texte) {
  const source = String(texte || '');
  if (!window.marked || !window.DOMPurify) return echapperHtml(source).replace(/\\n/g, '<br>');
  const brut = window.marked.parse(source, { gfm: true, breaks: true, headerIds: false, mangle: false, sanitize: false });
  const propre = window.DOMPurify.sanitize(brut, { USE_PROFILES: { html: true }, FORBID_TAGS: ['script', 'style', 'iframe', 'object', 'embed', 'form', 'input', 'button'] });
  const conteneur = document.createElement('div'); conteneur.innerHTML = propre;
  conteneur.querySelectorAll('a').forEach(function(lien) { const href = lien.getAttribute('href') || ''; if (!/^(?:https?:|mailto:|tel:)/i.test(href)) lien.removeAttribute('href'); lien.setAttribute('target', '_blank'); lien.setAttribute('rel', 'noopener noreferrer'); });
  conteneur.querySelectorAll('pre').forEach(function(pre) { if (pre.parentElement && pre.parentElement.classList.contains('bloc-code')) return; const bloc = document.createElement('div'); bloc.className = 'bloc-code'; const bouton = document.createElement('button'); bouton.type = 'button'; bouton.className = 'copier-code'; bouton.textContent = 'Copier le code'; bouton.setAttribute('aria-label', 'Copier le code'); pre.parentNode.insertBefore(bloc, pre); bloc.appendChild(bouton); bloc.appendChild(pre); });
  return conteneur.innerHTML;
}

function afficherMarkdown(message, texte) { message.dataset.markdownSource = String(texte); message.innerHTML = rendreMarkdown(texte); }
function afficherMarkdownStreaming(message, texte) { message.dataset.markdownSource = String(texte); message.innerHTML = rendreMarkdown(texte); }

function afficherMarkdownInitial() { document.querySelectorAll('#chat .msg.bot').forEach(function(message) { afficherMarkdown(message, message.textContent); }); }
document.addEventListener('DOMContentLoaded', afficherMarkdownInitial);
document.addEventListener('DOMContentLoaded', function(){ restaurerJobsOutils(); });
function ajouterMessage(texte, classe) {
  const accueil = document.querySelector('.accueil-vide');
  if (accueil) accueil.remove();
  const enveloppe = document.createElement('div');
  enveloppe.className = 'message-wrap ' + classe;
  const div = document.createElement('div');
  div.className = 'msg ' + classe;
  div.textContent = texte;
  enveloppe.appendChild(div);
  chat.appendChild(enveloppe);
  chat.scrollTop = chat.scrollHeight;
  return div;
}

function ajouterMessageImage(texte, fichier, miniature) {
  if (!fichier || !fichier.type.startsWith('image/')) {
    return ajouterMessage(texte || '📎 Fichier envoyé', 'user');
  }
  const message = ajouterMessage(texte, 'user');
  const lien = document.createElement('a');
  lien.className = 'image-message-lien';
  lien.href = miniature || '#';
  lien.target = '_blank';
  lien.rel = 'noopener noreferrer';
  lien.setAttribute('aria-label', 'Ouvrir l’image envoyée');
  const image = document.createElement('img');
  image.className = 'image-message';
  image.src = miniature || '';
  image.alt = fichier.name ? 'Image envoyée : ' + fichier.name : 'Image envoyée';
  image.addEventListener('error', function(){ lien.replaceWith(document.createTextNode('Image envoyée : ' + (fichier.name || 'fichier image'))); });
  lien.appendChild(image);
  message.appendChild(lien);
  return message;
}

function historiqueChatTemporaire(exclureDernierUtilisateur) {
  const elements = Array.from(chat.querySelectorAll('.message-wrap .msg'));
  if (exclureDernierUtilisateur) {
    for (let i = elements.length - 1; i >= 0; i--) {
      if (elements[i].classList.contains('user')) { elements.splice(i, 1); break; }
    }
  }
  const messages = elements.map(function(element) {
    return {
      auteur: element.classList.contains('bot') ? 'bot' : 'user',
      texte: element.dataset.markdownSource || element.textContent || ''
    };
  }).filter(function(message) { return message.texte.trim().length > 0; });
  return messages.slice(-30);
}

function iconeAction(nom) {
  const chemins = {
    copier: '<rect x="8" y="8" width="12" height="13" rx="2"/><path d="M16 8V5a2 2 0 0 0-2-2H5a2 2 0 0 0-2 2v11a2 2 0 0 0 2 2h3"/>',
    positif: '<path d="M7 10v11H4a2 2 0 0 1-2-2v-7a2 2 0 0 1 2-2h3Zm0 0 5-7a3 3 0 0 1 2 3v4h5a2 2 0 0 1 2 2l-2 7a2 2 0 0 1-2 2H7"/>',
    negatif: '<path d="M7 14V3H4a2 2 0 0 0-2 2v7a2 2 0 0 0 2 2h3Zm0 0 5 7a3 3 0 0 0 2-3v-4h5a2 2 0 0 0 2-2l-2-7a2 2 0 0 0-2-2H7"/>',
    partager: '<path d="M12 16V4m-5 5 5-5 5 5M5 12v7h14v-7"/>',
    regenerer: '<path d="M20 7v5h-5M20 12a8 8 0 1 0 2 5"/>',
    repondre: '<path d="m9 14-5-5 5-5M4 9h10a6 6 0 0 1 0 12h-1"/>',
    lire: '<path d="m8 5 12 7-12 7z"/>',
    stop: '<rect x="5" y="5" width="14" height="14" rx="2"/>'
  };
  return '<svg viewBox="0 0 24 24" aria-hidden="true">' + (chemins[nom] || '') + '</svg>';
}

function ajouterReponse(texte, messageId) {
  const accueil = document.querySelector('.accueil-vide');
  if (accueil) accueil.remove();
  const enveloppe = document.createElement('div');
  enveloppe.className = 'message-wrap bot';
  const message = document.createElement('div');
  message.className = 'msg bot';
  message.dataset.messageId = messageId || '';
  afficherMarkdown(message, texte);

  let actionsHtml = '<div class="actions-reponse">'
    + '<button type="button" class="action-copier" title="Copier" aria-label="Copier">' + iconeAction('copier') + '</button>'
    + '';
  if (estConnecte && preferencesVocales.conserver_historique) {
    actionsHtml += '<button type="button" class="action-feedback" data-valeur="positif" title="J\'aime" aria-label="J\'aime">' + iconeAction('positif') + '</button>'
      + '<button type="button" class="action-feedback" data-valeur="negatif" title="Je n\'aime pas" aria-label="Je n\'aime pas">' + iconeAction('negatif') + '</button>'
      + '<button type="button" class="action-partager" title="Partager" aria-label="Partager">' + iconeAction('partager') + '</button>'
      + '<button type="button" class="action-regenerer" title="Régénérer" aria-label="Régénérer">' + iconeAction('regenerer') + '</button>';
  }
  actionsHtml += '<button type="button" class="action-repondre" title="Répondre à ce message" aria-label="Répondre à ce message">' + iconeAction('repondre') + '</button>'
    + '<button type="button" class="action-lire" title="Lecture / pause" aria-label="Lecture / pause">' + iconeAction('lire') + '</button>'
    + '<button type="button" class="action-stop" title="Arrêter" aria-label="Arrêter">' + iconeAction('stop') + '</button>'
    + '<span class="lecture-etat"></span></div>';

  enveloppe.innerHTML = actionsHtml;
  enveloppe.insertBefore(message, enveloppe.firstChild);
  chat.appendChild(enveloppe);
  chat.scrollTop = chat.scrollHeight;
  return enveloppe;
}

function creerSuiviAction(action) {
  const bloc = document.createElement('div');
  bloc.className = 'suivi-action' + (action.type === 'image' ? ' image-generation' : '');
  bloc.dataset.actionId = action.id || '';
  bloc.innerHTML = '<div class="suivi-action-entete"><span class="suivi-action-indicateur"></span><strong class="suivi-action-titre"></strong><button type="button" class="suivi-action-annuler" title="Annuler" aria-label="Annuler">Annuler</button></div><div class="suivi-action-etapes"></div>';
  const titre = bloc.querySelector('.suivi-action-titre');
  const titres = { image:'Génération d’image', pdf:'Génération de PDF', video:'Génération de vidéo', recherche_web:'Recherche Web' };
  titre.textContent = titres[action.type] || 'Action Dashle';
  const annuler = bloc.querySelector('.suivi-action-annuler');
  annuler.style.display = action.cancelable ? '' : 'none';
  annuler.addEventListener('click', function() {
    if (requeteActiveController) {
      try { requeteActiveController.abort(); } catch(e) {}
      requeteActiveController = null;
    }
    reponseEnCours = false;
    mettreAJourSuiviAction(bloc, {
      id: bloc.dataset.actionId, type: action.type, step: 'annule',
      message: 'Suivi interrompu', event: 'action_cancelled'
    });
  });
  chat.appendChild(bloc);
  chat.scrollTop = chat.scrollHeight;
  return bloc;
}

async function suivreJobOutil(bloc, jobId) {
  let delai = 1000;
  while (true) {
    try {
      const response = await fetch('/api/outils/jobs/' + encodeURIComponent(jobId) + '/flux', {
        headers: { 'Accept': 'text/event-stream' }, cache: 'no-store'
      });
      if (!response.ok || !response.body) throw new Error('Suivi du job indisponible');
      const lecteur = response.body.getReader();
      const decodeur = new TextDecoder();
      let tampon = '';
      let terminal = false;
      while (true) {
        const {done, value} = await lecteur.read();
        if (done) break;
        tampon += decodeur.decode(value, {stream:true});
        const lignes = tampon.split('\n'); tampon = lignes.pop();
        for (const ligne of lignes) {
          if (!ligne.startsWith('data:')) continue;
          let ev; try { ev = JSON.parse(ligne.slice(5).trim()); } catch(e) { continue; }
          if (!ev || !ev.event) continue;
          if (ev.action) {
            mettreAJourSuiviAction(bloc, ev.action);
            if (ev.event === 'action_completed') {
              await finaliserSuiviAction(bloc, ev.action.result || {});
              terminal = true;
              return;
            }
            if (ev.event === 'action_failed' || ev.event === 'action_cancelled') {
              terminal = true;
              return;
            }
          }
        }
      }
      if (terminal) return;
      throw new Error('Flux terminé avant la fin du job');
    } catch (e) {
      delai = Math.min(delai * 2, 10000);
      mettreAJourSuiviAction(bloc, {
        id: jobId, type:'video', step:'generation',
        message:'Connexion au suivi interrompue. Reconnexion…'
      });
      await new Promise(function(resolve){ setTimeout(resolve, delai); });
    }
  }
}

async function restaurerJobsOutils() {
  try {
    const response = await fetch('/api/outils/jobs/actifs', {cache:'no-store'});
    if (!response.ok) return;
    const payload = await response.json();
    (payload.jobs || []).forEach(function(job) {
      const bloc = creerSuiviAction({
        id: job.id, type: 'video', step: job.status,
        message: job.message || 'Génération en cours…', cancelable: false
      });
      bloc.dataset.toolJobFollowed = 'true';
      mettreAJourSuiviAction(bloc, {
        id: job.id, type: 'video', step: job.status,
        message: job.message, result: {progress: job.progress}
      });
      suivreJobOutil(bloc, job.id);
    });
  } catch (e) {
    console.warn('[DASHLE] restauration des jobs vidéo impossible', e);
  }
}

function mettreAJourSuiviAction(bloc, action) {
  if (!bloc) return;
  const etapes = bloc.querySelector('.suivi-action-etapes');
  const indicateur = bloc.querySelector('.suivi-action-indicateur');
  const messages = { preparation: 'Préparation…', generation: 'Génération en cours…', finalisation: 'Finalisation…', en_attente: 'En attente du fournisseur…' };
  const libelle = action.message || messages[action.step] || 'Action en cours…';
  if (action.result && action.result.job_id && !bloc.dataset.toolJobFollowed) {
    bloc.dataset.toolJobFollowed = 'true';
    suivreJobOutil(bloc, action.result.job_id);
  }
  if (action.event === 'action_failed') {
    indicateur.className = 'suivi-action-indicateur echec';
    const ligne = document.createElement('div'); ligne.className = 'suivi-action-etape echec';
    ligne.textContent = action.message || '✕ L’opération a échoué'; etapes.appendChild(ligne);
    bloc.dataset.generationState = action.status || 'failed';
    bloc.dataset.generationActive = 'false';
    return;
  }
  if (action.event === 'action_cancelled' || action.step === 'annule') {
    indicateur.className = 'suivi-action-indicateur annule';
    const ligne = document.createElement('div'); ligne.className = 'suivi-action-etape annule';
    ligne.textContent = '— Suivi interrompu'; etapes.appendChild(ligne);
    bloc.dataset.generationState = 'cancelled';
    bloc.dataset.generationActive = 'false';
    return;
  }
  if (action.event === 'action_completed' || action.step === 'termine') {
    indicateur.className = 'suivi-action-indicateur termine';
    const labels = { image:'✓ Génération terminée', pdf:'✓ PDF terminé', video:'✓ Vidéo terminée', recherche_web:'✓ Recherche Web effectuée' };
    const ligne = document.createElement('div'); ligne.className = 'suivi-action-etape termine';
    ligne.textContent = labels[action.type] || '✓ Action terminée'; etapes.appendChild(ligne);
    bloc.dataset.generationState = 'completed';
    bloc.dataset.generationActive = 'false';
    return;
  }
  indicateur.className = 'suivi-action-indicateur actif';
  if (action.type === 'image' || action.type === 'video') {
    bloc.dataset.generationState = action.step || 'generation';
    bloc.dataset.generationActive = 'true';
    let carte = bloc.querySelector('.suivi-action-image-progress');
    if (!carte) { carte = document.createElement('div'); carte.className = 'suivi-action-image-progress'; etapes.appendChild(carte); }
    const progress = action.result && typeof action.result.progress === 'number' ? action.result.progress : null;
    const pct = progress !== null ? Math.max(0, Math.min(100, progress)) : null;
    carte.innerHTML = '<div class="titre"></div><div class="sous-titre"></div><div class="barre" aria-hidden="true"></div>';
    carte.querySelector('.titre').textContent = libelle;
    carte.querySelector('.sous-titre').textContent = pct === null ? 'Traitement en cours…' : ('Traitement en cours… ' + pct + ' %');
    const barre = carte.querySelector('.barre');
    if (pct === null) { barre.classList.add('indeterminee'); barre.style.width = ''; }
    else { barre.classList.remove('indeterminee'); barre.style.width = pct + '%'; }
  } else {
    const ligne = document.createElement('div'); ligne.className = 'suivi-action-etape actif';
    ligne.innerHTML = '<span class="suivi-action-points">•••</span> <span></span>';
    ligne.lastElementChild.textContent = libelle; etapes.appendChild(ligne);
  }
  chat.scrollTop = chat.scrollHeight;
}

function ouvrirVisionneuseImage(url, alt, prompt, filename) {
  let viewer = document.getElementById('dashle-image-viewer');
  if (!viewer) {
    viewer = document.createElement('div'); viewer.id = 'dashle-image-viewer'; viewer.className = 'image-viewer'; viewer.hidden = true;
    viewer.innerHTML = '<button type="button" class="image-viewer-fermer" aria-label="Fermer">×</button><img alt=""><div class="image-viewer-bar"><button type="button" class="primaire" data-action="download">Télécharger</button><button type="button" data-action="share">Partager</button><button type="button" data-action="retry">Régénérer</button><button type="button" data-action="close">Fermer</button></div>';
    document.body.appendChild(viewer);
    viewer.querySelector('[data-action="close"]').addEventListener('click', function(){ viewer.hidden = true; });
    viewer.querySelector('.image-viewer-fermer').addEventListener('click', function(){ viewer.hidden = true; });
    viewer.addEventListener('click', function(e){ if(e.target === viewer) viewer.hidden = true; });
  }
  const image = viewer.querySelector('img'); image.src = url; image.alt = alt || 'Image générée par DASHLE';
  viewer.querySelector('[data-action="download"]').onclick = function(){ const a=document.createElement('a'); a.href=url; a.download=filename || 'image-dashle.png'; document.body.appendChild(a); a.click(); a.remove(); };
  viewer.querySelector('[data-action="share"]').onclick = async function(){
    try {
      if (navigator.share) { await navigator.share({title:'Image générée par DASHLE', text: alt || 'Image générée par DASHLE', url:url}); return; }
    } catch(e) { if (e && e.name === 'AbortError') return; }
    try { await navigator.clipboard.writeText(url); this.textContent='Lien copié'; setTimeout(()=>{this.textContent='Partager';},1600); }
    catch(e) { window.prompt('Copie ce lien :', url); }
  };
  viewer.querySelector('[data-action="retry"]').onclick = function(){ viewer.hidden = true; if(prompt) genererArtifactDansChat(prompt, 'image'); };
  viewer.hidden = false;
}

function afficherEchecImage(bloc, prompt, message) {
  if (!bloc) return;
  const zone=document.createElement('div'); zone.className='suivi-action-image-resultat';
  const texte=document.createElement('div'); texte.textContent=message || 'La génération a échoué. Réessaie.'; zone.appendChild(texte);
  const actions=document.createElement('div'); actions.className='suivi-action-image-actions';
  const bouton=document.createElement('button'); bouton.className='primaire'; bouton.textContent='Réessayer';
  bouton.onclick=function(){ genererArtifactDansChat(prompt || '', 'image'); };
  actions.appendChild(bouton); zone.appendChild(actions); bloc.appendChild(zone);
}

function afficherQuotaImage(bloc, quota, prompt) {
  if (!bloc) return;
  const zone = document.createElement('div'); zone.className = 'suivi-action-image-resultat';
  const reset = quota && quota.reset_at ? new Date(quota.reset_at) : null;
  const heure = reset && !isNaN(reset.getTime()) ? reset.toLocaleTimeString([], {hour:'2-digit', minute:'2-digit'}) : '00:00';
  const texte = (quota && quota.message ? quota.message : 'Tu as utilisé tes images du jour. Elles reviennent à HH:MM').replace('HH:MM', heure);
  const p = document.createElement('div'); p.textContent = texte; zone.appendChild(p);
  const actions = document.createElement('div'); actions.className = 'suivi-action-image-actions';
  const bouton = document.createElement('button'); bouton.className='primaire';
  const visiteur = quota && quota.niveau === 'visitor'; bouton.textContent = visiteur ? 'Créer un compte' : 'Voir les forfaits';
  bouton.onclick = function(){ window.location.href = visiteur ? '/inscription' : '/tarifs'; };
  actions.appendChild(bouton); zone.appendChild(actions); bloc.appendChild(zone);
}

async function finaliserSuiviAction(bloc, result) {
  if (!bloc || !result) return;
  if (result.web_search) {
    const zone = document.createElement('div'); zone.className = 'suivi-action-web-resultat';
    const answer = document.createElement('div'); answer.className = 'suivi-action-web-answer';
    answer.textContent = result.web_search.answer || '';
    zone.appendChild(answer);
    const sources = result.web_search.sources || [];
    if (sources.length) {
      const titre = document.createElement('strong'); titre.textContent = 'Sources Web'; zone.appendChild(titre);
      const liste = document.createElement('ul'); liste.className = 'suivi-action-sources';
      sources.forEach(function(source) {
        if (!source.url) return;
        const li=document.createElement('li'); const a=document.createElement('a');
        a.href=source.url; a.target='_blank'; a.rel='noopener noreferrer'; a.textContent=source.title || source.url;
        li.appendChild(a); liste.appendChild(li);
      });
      zone.appendChild(liste);
    }
    bloc.appendChild(zone); chat.scrollTop=chat.scrollHeight; return;
  }
  if (!result.artifact || !result.artifact.data) return;
  const artifact = result.artifact;
  try {
    let blob;
    if (artifact.url && !artifact.data) {
      const response = await fetch(artifact.url, {cache:'no-store'});
      if (!response.ok) throw new Error('Résultat vidéo indisponible');
      blob = await response.blob();
    } else {
      const bytes = Uint8Array.from(atob(String(artifact.data)), function(c){ return c.charCodeAt(0); });
      blob = new Blob([bytes], { type: String(artifact.mime_type || 'application/octet-stream') });
    }
    const mime = String(artifact.mime_type || blob.type || 'application/octet-stream').toLowerCase();
    const url = URL.createObjectURL(blob);
    const contenu = document.createElement('div'); contenu.className = 'suivi-action-resultat suivi-action-image-resultat';
    if (artifact.type === 'image') {
      if (!mime.startsWith('image/')) throw new Error('MIME image invalide');
      const lien = document.createElement('a'); lien.className = 'image-message-lien'; lien.href = url; lien.setAttribute('aria-label','Ouvrir l’image générée');
      const image = document.createElement('img'); image.className = 'image-message'; image.src = url; image.alt = 'Image générée par DASHLE';
      image.onclick = function(e){ e.preventDefault(); ouvrirVisionneuseImage(url, image.alt, bloc.dataset.prompt || '', artifact.filename || 'image-dashle.png'); };
      image.onerror = function(){ contenu.dataset.imageError='true'; image.alt='Image générée indisponible'; };
      lien.appendChild(image); contenu.appendChild(lien);
    } else if (artifact.type === 'video') {
      if (!mime.startsWith('video/')) throw new Error('MIME vidéo invalide');
      const video = document.createElement('video'); video.controls = true; video.preload = 'metadata';
      video.playsInline = true; video.className = 'dashle-video-resultat'; video.src = url;
      contenu.appendChild(video);
      const lien = document.createElement('a'); lien.href=url; lien.download=artifact.filename || 'video-dashle.mp4'; lien.className='pdf-telechargement-chat'; lien.textContent='Ouvrir / télécharger la vidéo'; contenu.appendChild(lien);
    } else {
      const lien = document.createElement('a'); lien.href=url; lien.download=artifact.filename || 'dashle-document.pdf'; lien.className='pdf-telechargement-chat'; lien.textContent='Ouvrir / télécharger le PDF'; contenu.appendChild(lien);
    }
    bloc.appendChild(contenu); chat.scrollTop=chat.scrollHeight;
  } catch(erreur) {
    console.error('[DASHLE] Artefact reçu mais rendu impossible', erreur);
    const erreurEl=document.createElement('div'); erreurEl.className='suivi-action-etape echec'; erreurEl.textContent='✕ Le résultat n’a pas pu être affiché.'; bloc.appendChild(erreurEl);
  }
}

function afficherReflexion() {
  const div = document.createElement('div');
  div.className = 'reflexion';
  div.id = 'reflexion-active';
  div.innerHTML = '<div class="dot"></div><div class="dot"></div><div class="dot"></div>';
  chat.appendChild(div);
  chat.scrollTop = chat.scrollHeight;
}

function retirerReflexion() {
  const el = document.getElementById('reflexion-active');
  if (el) el.remove();
}

function bloquerEnvoi(secondes) {
  champ.disabled = true;
  btnEnvoyer.disabled = true;
  const placeholder = champ.placeholder;
  champ.placeholder = 'Patiente ' + secondes + 's...';
  let n = secondes;
  const t = setInterval(function() {
    n--;
    if (n <= 0) {
      clearInterval(t);
      champ.disabled = false;
      btnEnvoyer.disabled = false;
      champ.placeholder = placeholder;
    } else {
      champ.placeholder = 'Patiente ' + n + 's...';
    }
  }, 1000);
}

function estDemandeImage(texte) {
  const normalise = String(texte || '').toLocaleLowerCase().normalize('NFD').replace(/[\\u0300-\\u036f]/g, '');
  return /\\b(?:gener(?:e|es|ez|er|ee|ees|es)|cre(?:e|es|ez|er|ee|ees|es)|fais|faire|dessin(?:e|es|ez|er)?|illustr(?:e|es|ez|er)?|montre|represent(?:e|es|ez|er)?)\\b/.test(normalise)
    && /\\b(?:image|illustration|logo|affiche|schema|diagramme|infographie|visuel|dessin)\\b/.test(normalise);
}

function estDemandePdf(texte) {
  const normalise = String(texte || '').toLocaleLowerCase().normalize('NFD').replace(/[\u0300-\u036f]/g, '');
  return /\bpdf\b/.test(normalise)
    && /\b(genere|generer|creer|cree|fais|faire|fabrique|fabriquer|telecharger|telecharge|produis|produire|exporte|exporter|exportez|imprime|imprimer|transforme|transformer|prepare|preparer)\b/.test(normalise);
}

function extraireSujetPdf(texte) {
  const normalise = String(texte || '').toLocaleLowerCase().normalize('NFD').replace(/[\u0300-\u036f]/g, '');
  const correspondance = normalise.match(/\bpdf\b([\s\S]*)$/);
  if (!correspondance) return '';
  return correspondance[1]
    .replace(/^\s*(?:(?:au|en)\s+format\s+)?(?:(?:a\s+propos\s+(?:de|du|des|d')|concernant|sur|de|du|des|d'|le\s+sujet|sujet)\s*)?/, '')
    .replace(/[?!.,;:\s]+$/g, '')
    .replace(/\b(?:s'il\s+te\s+plait|svp|stp|merci)\b\s*$/g, '')
    .trim();
}

function demandePdfSansSujet(texte) {
  const normalise = String(texte || '').toLocaleLowerCase().normalize('NFD').replace(/[\u0300-\u036f]/g, '');
  return /^(peux[- ]tu|pourrais[- ]tu|est[- ]ce que tu peux|tu peux|peut[- ]tu)\s+(me\s+)?(genere|generer|creer|cree|fais|faire|fabriquer|produire|produis)\s+(un|une)?\s*pdf(?:\s+(s'il te plait|stp))?\s*\??$/.test(normalise);
}

function demandeIllustrationPedagogique(texte) {
  const t = String(texte || '').toLocaleLowerCase();
  return !/\b(image|illustration|logo|affiche|schéma|diagramme|infographie)\b/.test(t)
    && /\b(explique|comment fonctionne|fonctionnement|processus|architecture|système|concept|comparaison|notion)\b/.test(t)
    && t.length >= 35;
}

function estPdfTempsReel(texte) {
  const normalise = String(texte || '').toLocaleLowerCase().normalize('NFD').replace(/[\u0300-\u036f]/g, '');
  return /\b(meteo|actualites?|nouvelles recentes|temps reel|date et heure|heure locale)\b/.test(normalise);
}

function dashleActionRequestId() {
  if (window.crypto && typeof window.crypto.randomUUID === 'function') return window.crypto.randomUUID();
  return 'dashle-' + Date.now().toString(36) + '-' + Math.random().toString(36).slice(2);
}

async function genererArtifactDansChat(texte, type) {
  ajouterMessage(texte, 'user'); champ.value = ''; champ.style.height = 'auto';
  const headers = { 'Content-Type': 'application/x-www-form-urlencoded;charset=UTF-8', 'Accept': 'text/event-stream',
    'X-Dashle-Action-ID': dashleActionRequestId() };
  if (estConnecte) headers['X-CSRF-Token'] = csrfToken;
  let suivi = null;
  try {
    const body = new URLSearchParams(); body.set('message', texte);
    const controller = new AbortController();
    requeteActiveController = controller; reponseEnCours = true;
    const res = await fetch('/repondre_flux', { method: 'POST', headers, body: body.toString(), cache: 'no-store', signal: controller.signal });
    if (!res.ok || !res.body) throw new Error('Flux indisponible (' + res.status + ')');
    const lecteur = res.body.getReader(); const decodeur = new TextDecoder(); let tampon = '';
    while (true) {
      const {done, value} = await lecteur.read();
      if (done) break;
      tampon += decodeur.decode(value, {stream:true});
      const lignes = tampon.split('\n'); tampon = lignes.pop();
      for (const ligne of lignes) {
        if (!ligne.startsWith('data:')) continue;
        let ev; try { ev = JSON.parse(ligne.slice(5).trim()); } catch(e) { continue; }
        if (ev.event === 'action_started') {
          suivi = creerSuiviAction(ev.action);
          suivi.dataset.prompt = texte;
          mettreAJourSuiviAction(suivi, ev.action);
        } else if (ev.event === 'action_progress') {
          if (!suivi) suivi = creerSuiviAction(ev.action);
          mettreAJourSuiviAction(suivi, ev.action);
        } else if (ev.event === 'action_completed') {
          if (!suivi) suivi = creerSuiviAction(ev.action);
          mettreAJourSuiviAction(suivi, ev.action);
          finaliserSuiviAction(suivi, ev.action.result || {});
        } else if (ev.event === 'action_failed') {
          if (!suivi) suivi = creerSuiviAction(ev.action);
          mettreAJourSuiviAction(suivi, ev.action);
          if (ev.action.result && ev.action.result.quota) afficherQuotaImage(suivi, ev.action.result.quota, texte);
          else if (ev.action.type === 'image') afficherEchecImage(suivi, texte, ev.action.error);
        } else if (ev.event === 'action_cancelled') {
          if (!suivi) suivi = creerSuiviAction(ev.action);
          mettreAJourSuiviAction(suivi, ev.action);
        }
        if (ev.termine && ev.reponse && !estConnecte) {
          try {
            await fetch('/confirmer_message', {
              method: 'POST',
              headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({ reponse: ev.reponse }),
              cache: 'no-store'
            });
          } catch (confirmationErreur) {
            console.warn('[DASHLE] Résultat action non confirmé en session visiteur', confirmationErreur);
          }
        }
        if (ev.erreur) throw new Error(ev.erreur);
      }
    }
  } catch (erreur) {
    if (erreur.name !== 'AbortError') ajouterMessage(erreur.message || 'La génération a échoué. Réessaie.', 'bot');
  } finally {
    if (requeteActiveController === controller) {
      requeteActiveController = null;
      reponseEnCours = false;
      texteGenerationEnCours = '';
    }
  }
}

async function genererPdfTempsReelDansChat(texte) {
  const sujet = extraireSujetPdf(texte);
  const pdfTempsReel = estPdfTempsReel(sujet);
  ajouterMessage(texte, 'user');
  champ.value = '';
  champ.style.height = 'auto';
  if (!pdfTempsReel && !sujet) {
    ajouterMessage('Oui, je peux générer un PDF. Sur quel sujet veux-tu que je le prépare ?', 'bot');
    return;
  }
  afficherReflexion();

  const donnees = new URLSearchParams({
    fuseau: Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC',
    ville: '',
  });
  if (!pdfTempsReel) donnees.set('sujet', sujet);
  const headers = { 'Content-Type': 'application/x-www-form-urlencoded;charset=UTF-8',
      'X-Dashle-Action-ID': dashleActionRequestId() };
  if (estConnecte) headers['X-CSRF-Token'] = csrfToken;

  try {
    const reponse = await fetch('/telecharger-pdf-temps-reel', {
      method: 'POST', headers, body: donnees.toString(), cache: 'no-store',
    });
    const type = (reponse.headers.get('Content-Type') || '').toLowerCase();
    if (!reponse.ok || !type.includes('application/pdf')) {
      throw new Error('Le PDF n’a pas pu être généré. Réessaie.');
    }
    const fichier = await reponse.blob();
    const signature = new Uint8Array(await fichier.slice(0, 5).arrayBuffer());
    if (signature.length !== 5 || String.fromCharCode.apply(null, signature) !== '%PDF-') {
      throw new Error('Le fichier reçu n’est pas un PDF valide. Réessaie.');
    }
    const url = URL.createObjectURL(fichier);
    const enveloppe = ajouterReponse(pdfTempsReel
      ? 'Voici ton PDF avec les informations du Temps réel.'
      : 'Voici le PDF demandé sur ton sujet.', '');
    const message = enveloppe.querySelector('.msg');
    const lien = document.createElement('a');
    lien.className = 'pdf-telechargement-chat';
    lien.href = url;
    lien.download = pdfTempsReel ? 'dashle-temps-maintenant.pdf' : 'dashle-document.pdf';
    lien.textContent = 'Télécharger le PDF';
    message.appendChild(document.createElement('br'));
    message.appendChild(lien);
  } catch (erreur) {
    ajouterMessage(erreur.message || 'Le PDF n’a pas pu être généré. Réessaie.', 'bot');
  } finally {
    retirerReflexion();
  }
}

// =====================================================================
// Gestion des générations SSE
// =====================================================================
function arreterGeneration() {
  if ('speechSynthesis' in window) window.speechSynthesis.cancel();
  if (reponseActiveElement && reponseActiveElement.isConnected) {
    reponseActiveElement.remove();
  }
  reponseActiveElement = null;
  const messagesActifs = Array.from(document.querySelectorAll('.message-wrap.bot .msg'))
    .filter(function(message) { return !message.dataset.messageId; });
  const dernierMessageActif = messagesActifs[messagesActifs.length - 1];
  if (dernierMessageActif && dernierMessageActif.parentElement) {
    dernierMessageActif.parentElement.remove();
  }
  if (requeteActiveController) {
    try { requeteActiveController.abort(); } catch(e) {}
    requeteActiveController = null;
  }
  reponseEnCours = false;
}

function gererPerteReseau() {
  const texteAReprendre = texteGenerationEnCours;
  const generationActive = reponseEnCours || Boolean(requeteActiveController);
  if (!generationActive) return;

  generationInterrompueParReseau = true;
  arreterGeneration();

  if (texteAReprendre && !champ.value.trim()) {
    champ.value = texteAReprendre;
    champ.style.height = 'auto';
    champ.style.height = Math.min(champ.scrollHeight, 120) + 'px';
  }
  champ.placeholder = 'Connexion perdue — réessaie quand le réseau revient';
  if (vocalActif) {
    afficherEtatVocal('erreur', 'Connexion perdue. Réessaie quand le réseau revient.');
    afficherStatutVocal('Connexion perdue. Aucun renvoi automatique n’a été effectué.');
  }
}

function gererRetourReseau() {
  if (!generationInterrompueParReseau) return;
  generationInterrompueParReseau = false;
  const placeholder = champ.dataset.placeholderReseauInitial || '';
  champ.placeholder = placeholder;
  if (vocalActif) {
    afficherStatutVocal('Connexion rétablie. Tu peux renvoyer ton message.');
  }
}

window.addEventListener('offline', gererPerteReseau);
window.addEventListener('online', gererRetourReseau);

// =====================================================================
// VAD — détection de voix pendant que Dashle parle
// =====================================================================
async function demarrerVAD() {
  if (vadPret || !navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) return vadPret;
  try {
    const AudioCtx = window.AudioContext || window.webkitAudioContext;
    if (!AudioCtx) return false;
    vadAudioContext = new AudioCtx();
    if (vadAudioContext.state === 'suspended') await vadAudioContext.resume();
    vadStream = await navigator.mediaDevices.getUserMedia({
      audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true, channelCount: 1 }
    });
    vadSource   = vadAudioContext.createMediaStreamSource(vadStream);
    vadAnalyser = vadAudioContext.createAnalyser();
    vadAnalyser.fftSize = 1024;
    vadAnalyser.smoothingTimeConstant = 0.2;
    vadSource.connect(vadAnalyser);
    vadPret = true;
    surveillerParole();
    return true;
  } catch(e) {
    console.warn('VAD indisponible :', e);
    return false;
  }
}

function arreterVAD() {
  if (vadAnimation) cancelAnimationFrame(vadAnimation);
  vadAnimation = null;
  if (vadStream) vadStream.getTracks().forEach(function(t) { t.stop(); });
  vadStream = null;
  if (vadSource)   { try { vadSource.disconnect();   } catch(e) {} }
  if (vadAnalyser) { try { vadAnalyser.disconnect(); } catch(e) {} }
  vadSource = vadAnalyser = null;
  if (vadAudioContext) { try { vadAudioContext.close(); } catch(e) {} }
  vadAudioContext = null;
  vadPret = false;
  vadDebutParole = 0;
  vadBruitBase = 0.01;
  vadSeuilCourant = VAD_SEUIL;
  // Libérer les verrous de synthèse : on quitte le mode vocal,
  // plus aucune protection n'est nécessaire.
  syntheseEnCours = false;
  recoMutePendantTTS = false;
  vadDebutSynthese = 0;
}

function surveillerParole() {
  if (!vadPret || !vadAnalyser || !vocalActif) return;
  const donnees = new Uint8Array(vadAnalyser.fftSize);

  const verifier = function() {
    if (!vadPret || !vadAnalyser || !vocalActif) return;
    const now = performance.now();
    vadAnalyser.getByteTimeDomainData(donnees);
    let somme = 0;
    for (let i = 0; i < donnees.length; i++) {
      const x = (donnees[i] - 128) / 128;
      somme += x * x;
    }
    const rms = Math.sqrt(somme / donnees.length);
    if (rms < vadSeuilCourant) {
      vadBruitBase = (vadBruitBase * 0.92) + (rms * 0.08);
    }
    vadSeuilCourant = Math.max(
      VAD_SEUIL,
      Math.min(0.18, (vadBruitBase * VAD_FACTEUR) + VAD_MARGE)
    );
    window.__dashleVocalDiagnostic = {
      vadActif: Boolean(vadPret && vadStream),
      rms: Number(rms.toFixed(4)),
      bruitBase: Number(vadBruitBase.toFixed(4)),
      seuil: Number(vadSeuilCourant.toFixed(4)),
      paroleDepuisMs: vadDebutParole ? Math.round(now - vadDebutParole) : 0,
      dureeMinMs: VAD_DUREE_MIN,
      echoCancellation: true,
      noiseSuppression: true,
      autoGainControl: true,
      speechRecognitionBloqueePendantTTS: Boolean(recoMutePendantTTS || syntheseEnCours)
    };
    // Dashle est occupé si SSE en cours OU synthèse en cours.
    const dashleOccupe = reponseEnCours
      || syntheseEnCours
      || (window.speechSynthesis && window.speechSynthesis.speaking);

    // Le VAD reste actif pendant le TTS pour détecter une vraie prise de parole.
    // echoCancellation, le seuil et la durée minimale filtrent la voix de Dashle.
    // Après le TTS, attendre VAD_DELAI_POST pour laisser l'écho résiduel retomber.
    const enPeriodeProtegee = vadDebutSynthese > 0
      && (now - vadDebutSynthese) < VAD_DELAI_POST;

    if (dashleOccupe && !enPeriodeProtegee && rms >= vadSeuilCourant) {
      if (!vadDebutParole) vadDebutParole = now;
      if (now - vadDebutParole >= VAD_DUREE_MIN
          && now - vadDerniereDetection >= VAD_COOLDOWN) {
        vadDerniereDetection = now;
        vadDebutParole = 0;
        interrompreDashle();
      }
    } else {
      vadDebutParole = 0;
    }
    vadAnimation = requestAnimationFrame(verifier);
  };
  verifier();
}

// =====================================================================
// Mode vocal — interruption et séquencement
// =====================================================================
if (orbeDashle) {
  orbeDashle.addEventListener('click', function() {
    interrompreDashle();
  });
  orbeDashle.addEventListener('keydown', function(e) {
    if (e.key === 'Enter' || e.key === ' ') {
      e.preventDefault();
      interrompreDashle();
    }
  });
}

function interrompreDashle() {
  if (!vocalActif) return;
  interruptionDemandee = true;
  reinitialiserTranscriptionVocale();
  const reconnaissanceEnCoursAvantInterruption = recoEnCours;
  const ttsEtaitActif = ('speechSynthesis' in window)
    && (syntheseEnCours || window.speechSynthesis.speaking);
  const utteranceInterrompue = utteranceActuelle;

  // 1. Stopper immédiatement la synthèse vocale.
  // syntheseEnCours et vadDebutSynthese sont remis à 0 : la période
  // protégée est levée car c'est une interruption explicite de l'utilisateur.
  syntheseEnCours = false;
  recoMutePendantTTS = false;
  vadDebutSynthese = 0;
  if ('speechSynthesis' in window) window.speechSynthesis.cancel();
  vadDebutSynthese = ttsEtaitActif ? performance.now() : 0;

  // 2. Stopper la génération SSE
  arreterGeneration();

  // 3. Réinitialiser l'UI lecture
  if (lectureActuelle) {
    if (lectureActuelle.isConnected) {
      lectureActuelle.classList.remove('actif');
      lectureActuelle.textContent = '▶';
    }
  }
  lectureActuelle = null;
  utteranceActuelle = null;

  // 4. Mettre à jour l'UI
  afficherEtatVocal('ecoute', "Je t'écoute...");
  afficherStatutVocal("🎙️ Vas-y, je t'écoute.");
  btnVocal.classList.add('ecoute');
  btnVocal.classList.remove('parle');

  // 5. Redémarrer reco pour capter la nouvelle phrase.
  recoResultatsAutorises = false;
  arreterVAD();
  if (reconnaissanceEnCoursAvantInterruption) {
    recoEnCours = true;
    try { reco && reco.abort(); } catch(e) {}
  }
  function relancerRecoApresInterruption(tentative) {
    if (!vocalActif || !reco) { recoEnCours = false; return; }
    interruptionDemandee = false;
    recoEnCours = false;
    if (demarrerEcouteVocale()) {
      return;
    }
    // abort() peut mettre plus de 120 ms à libérer SpeechRecognition.
    // Réessayer brièvement évite que son InvalidStateError laisse le vocal bloqué.
    if (tentative < 12) {
      setTimeout(function() { relancerRecoApresInterruption(tentative + 1); }, 150);
    }
  }
  // Si reco était déjà arrêtée (cas normal pendant le TTS), ne pas ajouter
  // de délai après un abort inutile : ouvrir immédiatement la fenêtre d'écoute.
  setTimeout(function() { relancerRecoApresInterruption(0); }, reconnaissanceEnCoursAvantInterruption ? 120 : 0);
}

// =====================================================================
// SpeechRecognition
// =====================================================================
if ('SpeechRecognition' in window || 'webkitSpeechRecognition' in window) {
  const Reco = window.SpeechRecognition || window.webkitSpeechRecognition;
  reco = new Reco();
  reco.lang = 'fr-FR';
  reco.interimResults = false;
  reco.continuous = false;
  reco.maxAlternatives = 1;

  function demarrerEcouteVocale() {
    if (!vocalActif || recoMutePendantTTS || syntheseEnCours || reponseEnCours
        || (('speechSynthesis' in window) && window.speechSynthesis.speaking)) {
      return false;
    }
    // Ne pas démarrer si un démarrage est déjà en vol (guard anti-doublon).
    if (recoEnCours) {
      return false;
    }
    journaliserEtatAudioReconnaissance();
    arreterVAD();
    reco.interimResults = false;
    reco.continuous = false;
    modeActuel = 'vocal';
    ouvrirModeVocal();
    afficherEtatVocal('ecoute', 'Dashle écoute...');
    btnVocal.classList.add('ecoute');
    btnVocal.classList.remove('parle');
    afficherStatutVocal("🎧 Je t'écoute...");
    recoEnCours = true;
    recoResultatsAutorises = false;
    try {
      recoDebutEcouteMs = performance.now();
      recoDebutEcouteMs = performance.now();
      reco.start();
      return true;
    } catch(e) {
      recoEnCours = false;
      console.warn('[DASHLE] Échec du démarrage de la reconnaissance vocale :', e);
      return false;
    }
  }

  function planifierRelanceReco() {
    if (minuteurRelanceReco !== null || minuteurFinPhraseVocale !== null) return;
    const parleEncore = ('speechSynthesis' in window) && window.speechSynthesis.speaking;
    if (!vocalActif || interruptionDemandee || recoMutePendantTTS || syntheseEnCours
        || reponseEnCours || parleEncore || transcriptionFinaleVocale.trim()) {
      return;
    }

    const delai = Math.min(
      DELAI_RELANCE_RECO_INITIAL * (2 ** nbRelancesVocal),
      DELAI_RELANCE_RECO_MAX
    );
    nbRelancesVocal = Math.min(nbRelancesVocal + 1, MAX_PALIERS_RELANCE_RECO);
    minuteurRelanceReco = setTimeout(function() {
      minuteurRelanceReco = null;
      const ttsActif = ('speechSynthesis' in window) && window.speechSynthesis.speaking;
      if (!vocalActif || interruptionDemandee || recoEnCours || recoMutePendantTTS
          || syntheseEnCours || reponseEnCours || ttsActif) {
        return;
      }
      if (!demarrerEcouteVocale()) planifierRelanceReco();
    }, delai);
  }

  btnMicro.onclick = function() {
    if (vocalActif) return;
    modeActuel = 'dictee';
    reco.interimResults = false;
    reco.continuous = false;
    btnMicro.classList.add('actif');
    journaliserEtatAudioReconnaissance();
    try { reco.start(); } catch(e) {}
  };

  btnVocal.onclick = async function() {
    if (vocalActif) {
      desactiverModeVocal();
      return;
    }
    vocalActif = true;
    reinitialiserTranscriptionVocale();
    btnVocal.classList.add('vocal-on');
    ouvrirModeVocal();
    interruptionDemandee = false;
    // Aucun getUserMedia/AnalyserNode en parallèle de SpeechRecognition.
    recoResultatsAutorises = false;
    try { reco.stop(); } catch(e) {}
    setTimeout(function() {
      if (vocalActif) demarrerEcouteVocale();
    }, 80);
  };

  reco.onstart = function() {
    journaliserDiagnosticVocal('onstart', { mode: modeActuel, vocalActif: vocalActif });
    recoDebutEcouteMs = performance.now();
    if (modeActuel === 'vocal') journaliserEtatAudioReconnaissance();
    console.log('[DASHLE][SpeechRecognition] onstart', { mode: modeActuel, vocalActif: vocalActif });
    if (!transcriptionFinaleVocale.trim()) dernierIndexFinalVocal = 0;
    recoResultatsAutorises = !recoMutePendantTTS && !syntheseEnCours && !reponseEnCours;
    if (vocalActif && modeActuel === 'vocal') {
      afficherEtatVocal('ecoute', 'Dashle écoute...');
      afficherStatutVocal("🎧 Je t'écoute...");
      btnVocal.classList.add('ecoute');
      btnVocal.classList.remove('parle');
    }
  };

  reco.onresult = function(e) {
    journaliserDiagnosticVocal('onresult', { resultIndex: e && e.resultIndex, count: (e && e.results && e.results.length) || 0 });
    const resultatsJournal = Array.from(e.results || []).map(function(r, index) {
      const transcript = r && r[0] ? String(r[0].transcript || '').trim() : '';
      const confidence = r && r[0] && typeof r[0].confidence === 'number' ? r[0].confidence : null;
      console.log('[DASHLE][SpeechRecognition][resultat]', {
        index: index, transcript: transcript, confidence: confidence, isFinal: Boolean(r && r.isFinal)
      });
      return { transcript: transcript, confidence: confidence, isFinal: Boolean(r && r.isFinal) };
    });
    console.log('[DASHLE][SpeechRecognition] onresult', {
      mode: modeActuel, resultIndex: e.resultIndex, results: resultatsJournal,
      resultatsAutorises: recoResultatsAutorises
    });
    if (!recoResultatsAutorises || recoMutePendantTTS || syntheseEnCours
        || (vadDebutSynthese > 0 && performance.now() - vadDebutSynthese < VAD_DELAI_POST)) {
      return;
    }
    const resultats = e.results || [];
    if (modeActuel !== 'vocal') {
      const resultat = resultats[e.resultIndex || 0];
      const transcript = (resultat && resultat[0] && resultat[0].transcript || '').trim();
      if (!transcript) return;
      dernierTranscriptDictee = transcript;
      champ.value = transcript;
      champ.style.height = 'auto';
      return;
    }

    let resultatNonVide = false;
    for (let i = 0; i < resultats.length; i++) {
      const texte = resultats[i] && resultats[i][0] && resultats[i][0].transcript;
      if (texte && texte.trim()) resultatNonVide = true;
    }
    const debut = Math.max(Number.isInteger(e.resultIndex) ? e.resultIndex : 0, dernierIndexFinalVocal);
    for (let i = debut; i < resultats.length; i++) {
      const resultat = resultats[i];
      const texte = (resultat && resultat[0] && resultat[0].transcript || '').trim();
      if (!resultat || !resultat.isFinal || !texte) continue;
      ajouterTexteFinalVocalUnique(texte);
      dernierIndexFinalVocal = i + 1;
    }

    if (resultatNonVide) {
      nbRelancesVocal = 0;
      nbFinsSansTranscriptionVocal = 0;
      if (minuteurRelanceReco !== null) {
        clearTimeout(minuteurRelanceReco);
        minuteurRelanceReco = null;
      }
    }
    if (!transcriptionFinaleVocale) return;
    interruptionDemandee = false;
    planifierEnvoiFinPhraseVocale();
  };

  reco.onend = function() {
    journaliserDiagnosticVocal('onend', { mode: modeActuel, vocalActif: vocalActif });
    const dureeEcouteMs = recoDebutEcouteMs ? Math.max(0, Math.round(performance.now() - recoDebutEcouteMs)) : null;
    const transcriptionLog = modeActuel === 'dictee' ? dernierTranscriptDictee : transcriptionFinaleVocale;
    console.log('[DASHLE][SpeechRecognition] onend', {
      mode: modeActuel, vocalActif: vocalActif, dureeEcouteMs: dureeEcouteMs,
      hasTranscription: Boolean(String(transcriptionLog || '').trim()),
      transcriptionLength: String(transcriptionLog || '').trim().length
    });
    btnMicro.classList.remove('actif');
    recoEnCours = false;  // reco s'est arrêté, le guard est libéré
    recoResultatsAutorises = false;
    if (!vocalActif) { btnVocal.classList.remove('ecoute'); return; }
    // Ne pas redémarrer si : TTS en cours a muté reco (relance gérée par
    // utteranceActuelle.onend), interruption en cours, SSE en cours,
    // ou synthèse encore active.
    if (recoMutePendantTTS) {
      return;  // TTS prend la main, onend le relancera
    }
    if (transcriptionFinaleVocale.trim()) {
      planifierEnvoiFinPhraseVocale();
      return;
    }
    const synthActive = ('speechSynthesis' in window) && window.speechSynthesis.speaking;
    if (modeActuel === 'vocal' && !transcriptionFinaleVocale.trim()) {
      nbFinsSansTranscriptionVocal += 1;
      console.warn('[DASHLE][SpeechRecognition] fin sans transcription', {
        compteur: nbFinsSansTranscriptionVocal, dureeEcouteMs: dureeEcouteMs
      });
      if (nbFinsSansTranscriptionVocal >= MAX_FINS_SANS_TRANSCRIPTION_VOCAL) {
        nbFinsSansTranscriptionVocal = 0;
        if (demarrerSecoursAudio()) return;
        vocalActif = false;
        btnVocal.classList.remove('vocal-on', 'ecoute', 'parle');
        afficherEtatVocal('erreur', "Je n'arrive pas à t'entendre, réessaie");
        afficherStatutVocal("Je n'arrive pas à t'entendre, réessaie");
        return;
      }
    }
    if (!interruptionDemandee && !reponseEnCours && !syntheseEnCours && !synthActive) {
      planifierRelanceReco();
    }
  };

  reco.onerror = function(e) {
    journaliserDiagnosticVocal('onerror', { error: e && e.error, message: e && e.message });
    console.log('[DASHLE][SpeechRecognition] onerror', {
      error: e && e.error, message: e && e.message, mode: modeActuel, vocalActif: vocalActif
    });
    btnMicro.classList.remove('actif');
    recoResultatsAutorises = false;
    if (vocalActif && (e.error === 'not-allowed' || e.error === 'service-not-allowed')) {
      afficherEtatVocal('erreur', 'Micro refusé');
      afficherStatutVocal('Autorise le micro pour Dashle dans les réglages du navigateur, puis relance le mode vocal.');
      return;
    }
    if (vocalActif && e.error !== 'aborted') {
      if (transcriptionFinaleVocale.trim()) {
        planifierEnvoiFinPhraseVocale();
        return;
      }
      if (!recoMutePendantTTS && !syntheseEnCours && !reponseEnCours) {
        afficherEtatVocal('attente', 'En attente du micro...');
        afficherStatutVocal(e.error === 'network'
          ? 'La reconnaissance vocale a perdu sa connexion. Je réessaie…'
          : "🎧 Petit souci, je réessaie...");
      }
      planifierRelanceReco();
    }
  };

  window._dashleVocal = {
    estActif:       function() { return vocalActif; },
    reprendreEcoute: demarrerEcouteVocale,
    marquerParle:   function() {
      ouvrirModeVocal();
      afficherEtatVocal('parle', 'Dashle parle...');
      btnVocal.classList.add('parle');
      btnVocal.classList.remove('ecoute');
      afficherStatutVocal('🗣️ Dashle répond...');
    },
    interrompre: interrompreDashle,
  };
} else {
  btnMicro.style.display = 'none';
  btnVocal.title = 'Utiliser la transcription audio de secours';
  btnVocal.addEventListener('click', async function() {
    vocalActif = true;
    modeActuel = 'vocal';
    btnVocal.classList.add('vocal-on');
    ouvrirModeVocal();
    if (!await demarrerSecoursAudio()) {
      vocalActif = false;
      btnVocal.classList.remove('vocal-on', 'ecoute', 'parle');
      afficherEtatVocal('erreur', "Je n'arrive pas à t'entendre, réessaie");
      afficherStatutVocal("Je n'arrive pas à t'entendre, réessaie");
    }
  });
}

// =====================================================================
// SpeechSynthesis
// =====================================================================
let lectureActuelle   = null;
let utteranceActuelle = null;
let voixDisponibles   = [];

function chargerVoix() {
  if ('speechSynthesis' in window) voixDisponibles = window.speechSynthesis.getVoices();
}
chargerVoix();
if ('speechSynthesis' in window) window.speechSynthesis.onvoiceschanged = chargerVoix;

function choisirVoixFrancaise() {
  const fr = voixDisponibles.filter(function(v) {
    return v.lang && v.lang.toLowerCase().startsWith('fr');
  });
  const fmRegex = /female|femme|woman|amelie|audrey|claire|julie|marie|sophie|hortense|celine|victoria|eloquence/i;
  return fr.find(function(v) { return v.name === preferencesVocales.voix_nom; })
    || fr.find(function(v) { return fmRegex.test(v.name); })
    || fr[0] || voixDisponibles[0] || null;
}

function arreterLecture() {
  if ('speechSynthesis' in window) window.speechSynthesis.cancel();
  if (lectureActuelle) {
    // Vérifier que le bouton est encore dans le DOM (peut avoir été retiré
    // lors d'une interruption qui supprime le message en cours de lecture).
    if (lectureActuelle.isConnected) {
      lectureActuelle.classList.remove('actif');
      lectureActuelle.textContent = '▶';
      const act = lectureActuelle.closest('.actions-reponse');
      if (act) act.querySelector('.lecture-etat').textContent = '';
    }
  }
  lectureActuelle   = null;
  utteranceActuelle = null;
}

function nettoyerPourLecture(texte) {
  // Retire les marqueurs Markdown courants (ne modifie PAS le textContent affiché)
  var propre = texteSansMarqueursMarkdown(texte)
    .replace(/#{1,6}\s*/g, '')
    .replace(/\*{1,3}([^*]*)\*{1,3}/g, '$1')
    .replace(/_{1,3}([^_]*)_{1,3}/g, '$1')
    .replace(/~~([^~]*)~~/g, '$1')
    .replace(/`{1,3}[^`]*`{1,3}/g, '')
    .replace(/\[([^\]]*)\]\([^)]*\)/g, '$1')
    .replace(/!\[[^\]]*\]\([^)]*\)/g, '')
    .replace(/^[-*+]\s+/gm, '')
    .replace(/^\d+\.\s+/gm, '')
    .replace(/^>\s*/gm, '')
    .replace(/[-]{2,}/g, ' ')
  ;
  // Retire les émojis et symboles Unicode hors texte
  propre = propre.replace(
    /[\u{1F300}-\u{1FAFF}\u{2600}-\u{27BF}\u{2190}-\u{21FF}\u{2B00}-\u{2BFF}\u{FE00}-\u{FE0F}\u{1F000}-\u{1F02F}\u{1F0A0}-\u{1F0FF}]/gu,
    ''
  );
  // Normalise les espaces multiples issus des suppressions
  propre = propre.replace(/[ \t]{2,}/g, ' ').replace(/\n{3,}/g, '\n\n').trim();
  return propre;
}

const lecturesAutomatiquesEffectuees = new Set();

function lireReponse(bouton, texteForce, lectureAutomatique) {
  if (!('speechSynthesis' in window)) {
    bouton.closest('.actions-reponse').querySelector('.lecture-etat').textContent = 'Voix indisponible';
    return;
  }
  const messageWrap = bouton.closest('.message-wrap');
  const messageId = messageWrap ? messageWrap.dataset.messageId : '';
  if (lectureAutomatique && messageId && lecturesAutomatiquesEffectuees.has(messageId)) return;
  const texte = typeof texteForce === 'string'
    ? texteForce
    : (messageWrap.querySelector('.msg').dataset.markdownSource
        || messageWrap.querySelector('.msg').textContent);
  if (lectureActuelle === bouton && window.speechSynthesis.speaking) {
    if (window.speechSynthesis.paused) {
      window.speechSynthesis.resume();
      bouton.textContent = '⏸';
      bouton.closest('.actions-reponse').querySelector('.lecture-etat').textContent = 'Lecture';
    } else {
      window.speechSynthesis.pause();
      bouton.textContent = '▶';
      bouton.closest('.actions-reponse').querySelector('.lecture-etat').textContent = 'Pause';
    }
    return;
  }
  arreterLecture();
  const etat = bouton.closest('.actions-reponse').querySelector('.lecture-etat');
  const texteVocal = nettoyerPourLecture(texte);
  utteranceActuelle = new SpeechSynthesisUtterance(texteVocal);
  if (utteranceActuelle.text.toLowerCase().includes('copyright')) {
    console.warn('[DASHLE TTS] Texte transmis contenant Copyright :', utteranceActuelle.text);
  }
  utteranceActuelle.lang   = 'fr-FR';
  utteranceActuelle.voice  = choisirVoixFrancaise();
  utteranceActuelle.rate   = Number(preferencesVocales.voix_vitesse) || 1;
  utteranceActuelle.pitch  = Number(preferencesVocales.voix_tonalite) || 1;
  utteranceActuelle.volume = Number(preferencesVocales.voix_volume) || 1;
  lectureActuelle = bouton;
  bouton.classList.add('actif');
  bouton.textContent = '⏸';
  etat.textContent = 'Lecture';

  utteranceActuelle.onstart = function() {
    // L'audio démarre réellement : on arme le verrou d'état.
    // C'est ici, pas dans speak(), que le son commence vraiment.
    syntheseEnCours = true;
    vadDebutSynthese = 0;
    demarrerVAD().then(function(ok) {
      if (!ok) {
        console.warn('[DASHLE][VAD] micro VAD indisponible pendant TTS');
        window.__dashleVocalDiagnostic = Object.assign({}, window.__dashleVocalDiagnostic || {}, {
          vadActif: false,
          vadErreur: 'getUserMedia indisponible/refusé'
        });
      }
    });
  };

  utteranceActuelle.onend = function() {
    // Synthèse terminée normalement.
    syntheseEnCours = false;
    recoMutePendantTTS = false;
    // Armer le délai anti-écho : le VAD attend encore VAD_DELAI_POST ms
    // avant d'autoriser une interruption, le temps que l'écho s'estompe.
    vadDebutSynthese = performance.now();
    arreterVAD();
    arreterLecture();
    // En mode vocal : relancer reco maintenant que le TTS est terminé.
    // On attend VAD_DELAI_POST ms (délai anti-écho) avant d'écouter.
    if (vocalActif && window._dashleVocal && !interruptionDemandee && !recoEnCours) {
      setTimeout(function() {
        if (vocalActif && !recoEnCours && !interruptionDemandee && !recoMutePendantTTS
            && !reponseEnCours && !syntheseEnCours) {
          demarrerEcouteVocale();
        }
      }, VAD_DELAI_POST);
    }
  };

  utteranceActuelle.onerror = function(ev) {
    // Synthèse interrompue ou en erreur : libérer le verrou dans tous les cas.
    syntheseEnCours = false;
    recoMutePendantTTS = false;
    vadDebutSynthese = 0;
    arreterVAD();
    // Ne pas afficher d'erreur si l'interruption est volontaire (cancel).
    if (ev && ev.error !== 'interrupted' && ev.error !== 'canceled') {
      etat.textContent = 'Erreur audio';
    }
    arreterLecture();
    // Sur erreur non volontaire, relancer reco manuellement.
    if (window._dashleVocal && window._dashleVocal.estActif() && !recoEnCours
        && ev && ev.error !== 'interrupted' && ev.error !== 'canceled') {
      recoEnCours = true;
      setTimeout(function() { recoEnCours = false; window._dashleVocal.reprendreEcoute(); }, 400);
    }
  };

  // Stopper reco avant de lancer le TTS en mode vocal.
  // Cela garantit que la voix de Dashle n'est jamais capturée par la
  // reconnaissance vocale. reco.onend ne relancera pas grâce à recoMutePendantTTS.
  // La reprise est assurée par utteranceActuelle.onend ci-dessus.
  if (vocalActif && reco) {
    recoMutePendantTTS = true;
    recoResultatsAutorises = false;
    if (recoEnCours) {
      // Ne laisser recoEnCours vrai que si une session active doit encore
      // produire onend pour libérer ce verrou.
      try { reco.abort(); } catch(e) {}
    }
    // recoMutePendantTTS empêche reco.onend de relancer l'écoute pendant le TTS.
  }

  // On n'arme PAS syntheseEnCours ici : speak() met l'utterance en file
  // d'attente mais l'audio peut démarrer avec un délai. C'est onstart
  // qui marque le vrai début du son.
  window.speechSynthesis.speak(utteranceActuelle);
  if (lectureAutomatique && messageId) lecturesAutomatiquesEffectuees.add(messageId);
}

// =====================================================================
// Aperçu fichier
// =====================================================================
function effacerApercuFichier() {
  if (apercuMedia.dataset.url) URL.revokeObjectURL(apercuMedia.dataset.url);
  apercuMedia.removeAttribute('src');
  delete apercuMedia.dataset.url;
  apercuFichierEl.classList.remove('visible');
  fichierImage = null;
  inputImage.value = '';
}

function afficherApercuFichier(fichier) {
  if (!fichier) { effacerApercuFichier(); return; }
  if (apercuMedia.dataset.url) URL.revokeObjectURL(apercuMedia.dataset.url);
  const url = URL.createObjectURL(fichier);
  if (fichier.type.startsWith('video/')) {
    const vid = document.createElement('video');
    vid.id = 'apercu-fichier-media';
    vid.controls = true; vid.muted = true; vid.playsInline = true;
    apercuMedia.replaceWith(vid);
    apercuMedia = vid;
  } else if (apercuMedia.tagName !== 'IMG') {
    const img = document.createElement('img');
    img.id = 'apercu-fichier-media';
    img.alt = 'Aperçu';
    apercuMedia.replaceWith(img);
    apercuMedia = img;
  }
  apercuMedia.dataset.url = url;
  apercuMedia.src = url;
  apercuMedia.alt = 'Aperçu de ' + fichier.name;
  apercuNom.textContent  = fichier.name;
  apercuType.textContent = (fichier.type || 'Type inconnu') + ' · ' + Math.ceil(fichier.size / 1024) + ' Ko';
  apercuFichierEl.classList.add('visible');
}

let fichierImage = null;
const IMAGE_UPLOAD_MAX_BYTES = 10 * 1024 * 1024;

async function _rasteriserImage(fichier, coteMax, qualite) {
  const bitmap = await createImageBitmap(fichier, { imageOrientation: 'from-image' });
  const echelle = Math.min(1, coteMax / Math.max(bitmap.width, bitmap.height));
  const canvas = document.createElement('canvas');
  canvas.width = Math.max(1, Math.round(bitmap.width * echelle));
  canvas.height = Math.max(1, Math.round(bitmap.height * echelle));
  const contexte = canvas.getContext('2d', { alpha: false });
  contexte.fillStyle = '#fff';
  contexte.fillRect(0, 0, canvas.width, canvas.height);
  contexte.drawImage(bitmap, 0, 0, canvas.width, canvas.height);
  bitmap.close();
  return new Promise(function(resolve, reject) {
    canvas.toBlob(function(blob) {
      if (!blob) reject(new Error('Compression de l’image indisponible.'));
      else resolve(blob);
    }, 'image/jpeg', qualite);
  });
}

async function preparerImagePourEnvoi(fichier) {
  if (!fichier || !fichier.type.startsWith('image/')) return fichier;
  if (fichier.size > IMAGE_UPLOAD_MAX_BYTES) {
    throw new Error('L’image dépasse 10 Mo. Réduis-la puis réessaie.');
  }
  try {
    const blob = await _rasteriserImage(fichier, 1600, 0.8);
    const nom = (fichier.name || 'image').replace(/\.[^.]+$/, '') + '.jpg';
    return new File([blob], nom, { type: 'image/jpeg', lastModified: Date.now() });
  } catch (erreur) {
    throw new Error('Je n’ai pas pu préparer cette image. Réessaie.');
  }
}

async function creerMiniatureImage(fichier) {
  if (!fichier || !fichier.type.startsWith('image/')) return '';
  try {
    const blob = await _rasteriserImage(fichier, 320, 0.72);
    return await new Promise(function(resolve, reject) {
      const lecteur = new FileReader();
      lecteur.onload = function(){ resolve(String(lecteur.result || '')); };
      lecteur.onerror = reject;
      lecteur.readAsDataURL(blob);
    });
  } catch (erreur) {
    return '';
  }
}
inputImage.addEventListener('change', function(e) {
  fichierImage = e.target.files[0] || null;
  afficherApercuFichier(fichierImage);
});
function envoyerImageAvecProgression(fd, headers, onProgress) {
  return new Promise(function(resolve, reject) {
    const xhr = new XMLHttpRequest();
    xhr.open('POST', urlImage, true);
    xhr.responseType = 'json';
    Object.keys(headers || {}).forEach(function(cle) { xhr.setRequestHeader(cle, headers[cle]); });
    xhr.upload.onprogress = function(e) {
      if (e.lengthComputable && typeof onProgress === 'function') {
        onProgress(Math.round((e.loaded / e.total) * 100));
      }
    };
    xhr.onload = function() {
      let data = xhr.response;
      if (!data) {
        try { data = JSON.parse(xhr.responseText || '{}'); } catch(e) { data = {}; }
      }
      resolve({ ok: xhr.status >= 200 && xhr.status < 300, status: xhr.status, message: data.reponse || data.erreur || '', ...data });
    };
    xhr.onerror = function() {
      reject(new Error('Je n’ai pas pu envoyer l’image. Réessaie.'));
    };
    xhr.ontimeout = function() {
      reject(new Error('Je n’ai pas pu envoyer l’image. Réessaie.'));
    };
    xhr.send(fd);
  });
}

function afficherEchecEnvoiImage(texte, fichier, message) {
  const zone = ajouterMessage(message || 'Je n’ai pas pu envoyer l’image. Réessaie.', 'bot');
  const bouton = document.createElement('button');
  bouton.type = 'button';
  bouton.className = 'primaire';
  bouton.textContent = 'Réessayer';
  bouton.addEventListener('click', function() {
    fichierImage = fichier;
    champ.value = texte || '';
    afficherApercuFichier(fichier);
    zone.appendChild(bouton);
    form.requestSubmit();
  });
  zone.appendChild(bouton);
}

document.getElementById('retirer-fichier').addEventListener('click', effacerApercuFichier);

const feuilleFichiers = document.getElementById('feuille-fichiers');
document.getElementById('btn-attach').addEventListener('click', function() {
  feuilleFichiers.hidden = false;
});
feuilleFichiers.addEventListener('click', function(e) {
  if (e.target === feuilleFichiers) feuilleFichiers.hidden = true;
});
feuilleFichiers.querySelectorAll('[data-source-fichier]').forEach(function(option) {
  option.addEventListener('click', function() {
    const source = option.dataset.sourceFichier;
    inputImage.accept = source === 'photos' ? 'image/*' : 'image/*,video/*';
    if (source === 'camera') inputImage.setAttribute('capture', 'environment');
    else inputImage.removeAttribute('capture');
    feuilleFichiers.hidden = true;
    inputImage.click();
  });
});
document.addEventListener('keydown', function(e) {
  if (e.key === 'Escape' && !feuilleFichiers.hidden) feuilleFichiers.hidden = true;
});

// =====================================================================
// Contrôles mode vocal plein écran
// =====================================================================
document.getElementById('reduire-vocal').addEventListener('click', function() {
  fermerModeVocal();
  // Afficher le bouton de réouverture si le mode vocal reste actif.
  var btnRouvrir = document.getElementById('btn-rouvrir-vocal');
  if (btnRouvrir) btnRouvrir.classList.toggle('actif', vocalActif);
});

document.getElementById('fermer-vocal').addEventListener('click', function() {
  desactiverModeVocal();
});


// Bouton rouvrir : ramène l'overlay sans relancer quoi que ce soit —
// le VAD et la reconnaissance continuent de tourner en arrière-plan.
if (btnRouvrirVocal) {
  btnRouvrirVocal.addEventListener('click', function() {
    if (!vocalActif) return;
    ouvrirModeVocal();
  });
}

// =====================================================================
// Menu utilisateur dropdown
// =====================================================================
const badgeBtn = document.getElementById('user-badge-btn');
const dropdown = document.getElementById('user-dropdown');
if (badgeBtn && dropdown) {
  badgeBtn.addEventListener('click', function(e) {
    e.stopPropagation();
    const ouvert = dropdown.classList.toggle('ouvert');
    badgeBtn.setAttribute('aria-expanded', ouvert ? 'true' : 'false');
  });
  document.addEventListener('click', function() {
    dropdown.classList.remove('ouvert');
    badgeBtn.setAttribute('aria-expanded', 'false');
  });
  dropdown.addEventListener('click', function(e) { e.stopPropagation(); });
}

// =====================================================================
// Recherche et épinglage des conversations dans le sidebar.
// =====================================================================
const rechercheEl = document.getElementById('recherche-conversations');
const sidebarEl = document.getElementById('sidebar');
const epingleesEl = document.getElementById('conversations-epinglees');
const recentesEl = document.getElementById('conversations-recentes');
const breakpointMenuConversations = window.matchMedia('(max-width: 850px)');
function adapterMenusConversations(mobile) {
  document.querySelectorAll('.conversation-action-list').forEach(function(menu) {
    menu.hidden = mobile;
    const bouton = menu.parentElement.querySelector('.conversation-menu-toggle');
    if (bouton) bouton.setAttribute('aria-expanded', 'false');
  });
}
adapterMenusConversations(breakpointMenuConversations.matches);
if (breakpointMenuConversations.addEventListener) {
  breakpointMenuConversations.addEventListener('change', function(e) { adapterMenusConversations(e.matches); });
} else if (breakpointMenuConversations.addListener) {
  breakpointMenuConversations.addListener(function(e) { adapterMenusConversations(e.matches); });
}
const cleEpinglees = 'dashle:conversations:epinglees:' + (sidebarEl.dataset.compte || 'visiteur');
let conversationsEpinglees = [];
if (epingleesEl && recentesEl) {
  try {
    const stockees = JSON.parse(localStorage.getItem(cleEpinglees) || '[]');
    conversationsEpinglees = Array.isArray(stockees) ? stockees.map(String) : [];
  } catch(err) {
    console.warn('Lecture des conversations épinglées impossible :', err);
  }
  document.querySelectorAll('.ligne-conversation').forEach(function(ligne) {
    const estEpinglee = conversationsEpinglees.includes(ligne.dataset.convId);
    const bouton = ligne.querySelector('.epingle-conversation');
    bouton.setAttribute('aria-pressed', estEpinglee ? 'true' : 'false');
    bouton.title = estEpinglee ? 'Désépingler' : 'Épingler';
    (estEpinglee ? epingleesEl : recentesEl).appendChild(ligne);
  });
  sidebarEl.addEventListener('click', function(e) {
    const menuToggle = e.target.closest('.conversation-menu-toggle');
    if (menuToggle) {
      const menu = document.getElementById(menuToggle.getAttribute('aria-controls'));
      document.querySelectorAll('.conversation-menu-toggle').forEach(function(otherToggle) {
        if (otherToggle !== menuToggle) {
          otherToggle.setAttribute('aria-expanded', 'false');
          const otherMenu = document.getElementById(otherToggle.getAttribute('aria-controls'));
          if (otherMenu) otherMenu.hidden = true;
        }
      });
      const ouvert = menuToggle.getAttribute('aria-expanded') !== 'true';
      menuToggle.setAttribute('aria-expanded', ouvert ? 'true' : 'false');
      if (menu) menu.hidden = !ouvert;
      return;
    }
    if (e.target.closest('.conversation-action-list')) {
      const toggle = e.target.closest('.ligne-conversation').querySelector('.conversation-menu-toggle');
      if (toggle && e.target.closest('button')) {
        toggle.setAttribute('aria-expanded', 'false');
        e.target.closest('.ligne-conversation').querySelector('.conversation-action-list').hidden = true;
      }
    }
    const bouton = e.target.closest('.epingle-conversation');
    if (!bouton) return;
    const ligne = bouton.closest('.ligne-conversation');
    const id = ligne.dataset.convId;
    const epinglee = conversationsEpinglees.includes(id);
    conversationsEpinglees = epinglee
      ? conversationsEpinglees.filter(function(item) { return item !== id; })
      : conversationsEpinglees.concat(id);
    try {
      localStorage.setItem(cleEpinglees, JSON.stringify(conversationsEpinglees));
    } catch(err) {
      console.warn('Enregistrement des conversations épinglées impossible :', err);
    }
    bouton.setAttribute('aria-pressed', epinglee ? 'false' : 'true');
    bouton.title = epinglee ? 'Épingler' : 'Désépingler';
    (epinglee ? recentesEl : epingleesEl).appendChild(ligne);
  });
  document.addEventListener('click', function(e) {
    if (e.target.closest('.ligne-conversation')) return;
    document.querySelectorAll('.conversation-menu-toggle').forEach(function(toggle) {
      toggle.setAttribute('aria-expanded', 'false');
      const menu = document.getElementById(toggle.getAttribute('aria-controls'));
      if (menu) menu.hidden = true;
    });
  });
}
if (rechercheEl) {
  let minuteurRecherche = null;
  rechercheEl.addEventListener('input', function() {
    const terme = this.value.trim();
    clearTimeout(minuteurRecherche);
    if (!terme) {
      document.querySelectorAll('.ligne-conversation').forEach(function(ligne) { ligne.style.display = 'flex'; });
      return;
    }
    minuteurRecherche = setTimeout(async function() {
      try {
        const res = await fetch('/rechercher?q=' + encodeURIComponent(terme), { headers: { 'Accept': 'application/json' } });
        if (!res.ok) throw new Error('Recherche indisponible (' + res.status + ')');
        const ids = new Set((await res.json()).resultats.map(function(item) { return String(item.id); }));
        document.querySelectorAll('.ligne-conversation').forEach(function(ligne) {
          ligne.style.display = ids.has(ligne.dataset.convId) ? 'flex' : 'none';
        });
      } catch(err) {
        console.warn('Recherche de conversations impossible :', err);
      }
    }, 250);
  });
}

document.querySelectorAll('.suggestion').forEach(function(bouton) {
  bouton.addEventListener('click', function() {
    champ.value = bouton.textContent.trim();
    form.requestSubmit();
  });
});

// =====================================================================
// Partage
// =====================================================================
async function partagerConversation(convId) {
  if (!estConnecte) return;
  const res  = await fetch('/partager/' + convId, { method: 'POST', headers: { 'X-CSRF-Token': csrfToken } });
  const data = await res.json();
  if (data.url) {
    try { await navigator.clipboard.writeText(data.url); } catch(e) {}
    alert('Lien de partage copié : ' + data.url);
  }
}

// =====================================================================
// Actions sur les messages (copier, feedback, régénérer, lire…)
// =====================================================================
chat.addEventListener('click', async function(e) {
  const bouton = e.target.closest('button');
  if (!bouton) return;
  const enveloppe = bouton.closest('.message-wrap');
  const message   = enveloppe && enveloppe.querySelector('.msg');
  if (!message) return;

  if (bouton.classList.contains('action-copier')) {
    await navigator.clipboard.writeText(texteSansMarqueursMarkdown(message.dataset.markdownSource || message.innerText || message.textContent));
    bouton.classList.add('actif');
    setTimeout(function() { bouton.classList.remove('actif'); }, 1200);

  } else if (bouton.classList.contains('copier-code')) {
    const code = bouton.parentElement.querySelector('code');
    if (!code) return;
    await navigator.clipboard.writeText(code.textContent);
    bouton.textContent = 'Copié';
    setTimeout(function() { bouton.textContent = 'Copier le code'; }, 1200);

  } else if (bouton.classList.contains('action-pdf')) {
    const textePdf = (message.dataset.markdownSource || message.innerText || message.textContent || '').trim();
    if (textePdf) await genererArtifactDansChat('Transforme ce contenu en PDF.\n\n' + textePdf, 'pdf');

  } else if (bouton.classList.contains('action-repondre')) {
    const texteCite = (message.dataset.markdownSource || message.innerText || message.textContent || '').trim();
    if (!texteCite) return;
    const citation = texteCite.split('\n').map(function(ligne) { return '> ' + ligne; }).join('\n');
    champ.value = (champ.value.trim() ? champ.value.trimEnd() + '\n\n' : '') + citation + '\n\n';
    champ.focus();
    champ.style.height = 'auto';
    champ.style.height = Math.min(champ.scrollHeight, 120) + 'px';

  } else if (bouton.classList.contains('action-lire')) {
    lireReponse(bouton);

  } else if (bouton.classList.contains('action-stop')) {
    arreterLecture();

  } else if (bouton.classList.contains('action-feedback') && estConnecte) {
    const corps = 'message_id=' + encodeURIComponent(message.dataset.messageId)
      + '&valeur=' + bouton.dataset.valeur;
    await fetch('/feedback', {
      method: 'POST',
      headers: { 'Content-Type': 'application/x-www-form-urlencoded', 'X-CSRF-Token': csrfToken },
      body: corps,
    });
    bouton.classList.add('actif');

  } else if (bouton.classList.contains('action-regenerer') && estConnecte) {
    const res  = await fetch('/regenerer/' + message.dataset.messageId, { method: 'POST', headers: { 'X-CSRF-Token': csrfToken } });
    const data = await res.json();
    if (data.reponse) ajouterReponse(data.reponse, data.message_id);

  } else if (bouton.classList.contains('action-partager') && estConnecte) {
    partagerConversation(conversationId);
  }
});

// =====================================================================
// Envoi du formulaire (texte + SSE)
// =====================================================================
chat.scrollTop = chat.scrollHeight;

champ.addEventListener('keydown', function(e) {
  if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); form.requestSubmit(); }
});

champ.addEventListener('input', function() {
  this.style.height = 'auto';
  this.style.height = Math.min(this.scrollHeight, 120) + 'px';
  if (generationInterrompueParReseau) {
    generationInterrompueParReseau = false;
    this.placeholder = this.dataset.placeholderReseauInitial || '';
  }
});

form.addEventListener('submit', async function(e) {
  e.preventDefault();
  const texte = champ.value.trim();
  if (!texte && !fichierImage) return;
  if (!champ.dataset.placeholderReseauInitial) {
    champ.dataset.placeholderReseauInitial = champ.placeholder || '';
  }

  // Un nouvel envoi prend la main : annuler immédiatement le SSE et le TTS précédents.
  if (reponseEnCours || requeteActiveController) {
    arreterGeneration();
    if ('speechSynthesis' in window) window.speechSynthesis.cancel();
  }

  // Couper toute lecture en cours si l'utilisateur envoie manuellement
  if ('speechSynthesis' in window && window.speechSynthesis.speaking) arreterLecture();

  // --- Envoi image ---
  if (fichierImage) {
    const imageOriginale = fichierImage;
    champ.value = '';
    champ.style.height = 'auto';
    afficherReflexion();

    try {
      const imageEnvoyee = await preparerImagePourEnvoi(imageOriginale);
      const miniature = await creerMiniatureImage(imageEnvoyee);
      ajouterMessageImage(texte, imageEnvoyee, miniature);
      const fd = new FormData();
      fd.append('message', texte);
      fd.append('image', imageEnvoyee, imageEnvoyee.name);
      if (estConnecte && !preferencesVocales.conserver_historique) {
        fd.append('historique', JSON.stringify(historiqueChatTemporaire(true)));
      }
      const headers = {};
      if (estConnecte) headers['X-CSRF-Token'] = csrfToken;
      const data = await envoyerImageAvecProgression(fd, headers, function(pourcentage) {
        const reflexion = document.getElementById('reflexion-active');
        if (reflexion) reflexion.setAttribute('aria-label', 'Envoi de l’image : ' + pourcentage + '%');
      });
      retirerReflexion();
      if (!data.ok) throw new Error(data.message || 'Je n’ai pas pu envoyer l’image. Réessaie.');
      ajouterReponse(data.reponse, data.message_id);
    } catch(err) {
      retirerReflexion();
      console.warn('[DASHLE] Échec envoi image (%s)', err && err.name ? err.name : 'erreur');
      afficherEchecEnvoiImage(texte, imageOriginale, err && err.message);
    }
    fichierImage = null;
    effacerApercuFichier();
    return;
  }

  if (!texte) return;

  if (!vocalActif && estDemandeImage(texte)) {
    await genererArtifactDansChat(texte, 'image');
    return;
  }

  if (!vocalActif && estDemandePdf(texte)) {
    if (estPdfTempsReel(texte)) {
      await genererPdfTempsReelDansChat(texte);
      return;
    }
    if (demandePdfSansSujet(texte)) {
      ajouterMessage('Oui. Je peux générer un PDF. Sur quel sujet veux-tu que je le prépare ?', 'bot');
      return;
    }
    await genererArtifactDansChat(texte, 'pdf');
    return;
  }

  if (vocalActif && modeActuel === 'vocal') {
    reinitialiserTranscriptionVocale();
    recoResultatsAutorises = false;
    try { if (recoEnCours && reco) reco.stop(); } catch(e) {}
  }

  ajouterMessage(texte, 'user');
  champ.value = '';
  champ.style.height = 'auto';
  afficherReflexion();
  // Passer immédiatement en état réflexion (orbe bleue) dès l'envoi,
  // avant même la réponse du serveur.
  if (window._dashleVocal && window._dashleVocal.estActif()) {
    afficherEtatVocal('reflexion', 'Dashle réfléchit...');
  }

  const controller = new AbortController();
  requeteActiveController = controller;
  reponseEnCours  = true;
  interruptionDemandee = false;
  generationInterrompueParReseau = false;
  texteGenerationEnCours = texte;

  let reponseElement = null;
  let messageElement = null;
  const headers = {
    'Content-Type': 'application/x-www-form-urlencoded;charset=UTF-8',
    'Accept': 'text/event-stream',
  };
  if (estConnecte) headers['X-CSRF-Token'] = csrfToken;

  try {
    const corps = new URLSearchParams();
    corps.set('message', texte);
    if (estConnecte && !preferencesVocales.conserver_historique) {
      corps.set('historique', JSON.stringify(historiqueChatTemporaire(false)));
    }
    const res = await fetch(urlFlux, {
      method:  'POST',
      headers,
      body:    corps.toString(),
      signal:  controller.signal,
      cache:   'no-store',
    });

    if (controller.signal.aborted || requeteActiveController !== controller) {
      if (!controller.signal.aborted) controller.abort();
      if (!reponseEnCours && !requeteActiveController) retirerReflexion();
      return;
    }

    retirerReflexion();

    if (!res.ok || !res.body) {
      let detail = '';
      try { detail = (await res.json()).erreur || ''; } catch(ex) {}
      throw new Error(detail || 'Flux indisponible (' + res.status + ')');
    }

    reponseElement = ajouterReponse('', '');
    reponseActiveElement = reponseElement;
    messageElement = reponseElement.querySelector('.msg');

    const lecteur  = res.body.getReader();
    const decodeur = new TextDecoder();
    let tampon      = '';
    let reponseTexte = '';
    let messageId   = null;

    let suiviActionSse = null;
    let actionArtifactSse = false;
    function traiterEvenementAction(ev) {
      if (!ev || !ev.event || !ev.action) return false;
      if (ev.event === 'action_started') {
        actionArtifactSse = true;
        if (reponseElement) { reponseElement.remove(); reponseElement = null; messageElement = null; }
        reponseActiveElement = null;
        retirerReflexion();
        suiviActionSse = creerSuiviAction(ev.action);
        suiviActionSse.dataset.prompt = texte;
        mettreAJourSuiviAction(suiviActionSse, ev.action);
        return true;
      }
      if (ev.event === 'action_progress') {
        actionArtifactSse = true;
        if (!suiviActionSse) { retirerReflexion(); suiviActionSse = creerSuiviAction(ev.action); }
        mettreAJourSuiviAction(suiviActionSse, ev.action);
        return true;
      }
      if (ev.event === 'action_completed') {
        actionArtifactSse = true;
        if (!suiviActionSse) { retirerReflexion(); suiviActionSse = creerSuiviAction(ev.action); }
        mettreAJourSuiviAction(suiviActionSse, ev.action);
        finaliserSuiviAction(suiviActionSse, ev.action.result || {});
        return true;
      }
      if (ev.event === 'action_failed' || ev.event === 'action_cancelled') {
        actionArtifactSse = true;
        if (!suiviActionSse) { retirerReflexion(); suiviActionSse = creerSuiviAction(ev.action); }
        mettreAJourSuiviAction(suiviActionSse, ev.action);
        if (ev.event === 'action_failed') {
          if (ev.action.result && ev.action.result.quota) afficherQuotaImage(suiviActionSse, ev.action.result.quota, texte);
          else if (ev.action.type === 'image') afficherEchecImage(suiviActionSse, texte, ev.action.error);
        }
        return true;
      }
      return false;
    }

    while (true) {
      const { done, value } = await lecteur.read();
      if (controller.signal.aborted || requeteActiveController !== controller) {
        if (!controller.signal.aborted) controller.abort();
        if (reponseElement) reponseElement.remove();
        if (!reponseEnCours && !requeteActiveController) retirerReflexion();
        return;
      }
      if (done) {
        break;
      }
      tampon += decodeur.decode(value, { stream: true });
      const lignes = tampon.split('\n');
      tampon = lignes.pop();

      for (const ligne of lignes) {
        if (!ligne.startsWith('data:')) continue;
        let ev;
        try { ev = JSON.parse(ligne.slice(5).trim()); } catch(ex) { continue; }
        if (traiterEvenementAction(ev)) {
          if (ev.termine) messageId = ev.message_id;
          continue;
        }
        if (ev.erreur) {
          throw new Error(ev.erreur);
        }
        if (ev.morceau) {
          reponseTexte += ev.morceau;
          afficherMarkdownStreaming(messageElement, reponseTexte);
          chat.scrollTop = chat.scrollHeight;
        }
        if (ev.termine) {
          messageId = ev.message_id;
          if (demandeIllustrationPedagogique(texte)) {
            try {
              const h = { 'Content-Type': 'application/x-www-form-urlencoded;charset=UTF-8' };
              if (estConnecte) h['X-CSRF-Token'] = csrfToken;
              const b = new URLSearchParams(); b.set('prompt', 'Illustration pédagogique fidèle à cette explication : ' + reponseTexte.slice(0, 8000));
              const ir = await fetch('/generer-image', { method: 'POST', headers: h, body: b.toString(), cache: 'no-store' });
              const idata = await ir.json().catch(function(){ return {}; });
              if (ir.ok && idata.data) {
                const raw = Uint8Array.from(atob(idata.data), function(c){ return c.charCodeAt(0); });
                const blob = new Blob([raw], { type: idata.mime_type || 'image/png' });
                const u = URL.createObjectURL(blob);
                const lien = document.createElement('a'); lien.href = u; lien.target = '_blank'; lien.rel = 'noopener noreferrer';
                const img = document.createElement('img'); img.className = 'image-message'; img.src = u; img.alt = 'Illustration pédagogique générée par DASHLE';
                lien.appendChild(img); messageElement.appendChild(document.createElement('br')); messageElement.appendChild(lien);
              }
            } catch (_) {}
          }
          // Visiteur : sauvegarder la réponse en session via une requête séparée.
          // Impossible de le faire dans le générateur SSE (headers déjà envoyés).
          if (!estConnecte && ev.reponse) {
            fetch('/confirmer_message', {
              method: 'POST',
              headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({ reponse: ev.reponse }),
            }).catch(function(err) {
              console.warn('confirmer_message échoué :', err);
            });
          }
        }
      }
    }

    if (!actionArtifactSse && messageElement) {
      const quotaVisuel = reponseTexte.replace(/^QUOTA:\d+:/, '');
      afficherMarkdown(messageElement, quotaVisuel);
      messageElement.dataset.messageId = messageId || '';
    }
    reponseEnCours = false;
    requeteActiveController = null;
    reponseActiveElement = null;

    // Lecture vocale si le mode vocal est actif
    const vocal = window._dashleVocal;
    if (!actionArtifactSse && vocal && vocal.estActif() && reponseTexte) {
      vocal.marquerParle();
      lireReponse(reponseElement.querySelector('.action-lire'), undefined, true);
      // L'écoute reprendra via utteranceActuelle.onend (après la synthèse)
    } else if (preferencesVocales.voix_active && preferencesVocales.lecture_automatique
        && reponseTexte && reponseElement) {
      lireReponse(reponseElement.querySelector('.action-lire'), undefined, true);
    }

    if (reponseTexte) {
      // Format QUOTA:N: émis par brain.py quand l'API retourne 429.
      // On extrait le délai réel (Retry-After ou défaut 30s) pour bloquer
      // l'envoi exactement le bon nombre de secondes.
      const quotaMatch = reponseTexte.match(/^QUOTA:(\d+):/);
      if (quotaMatch) {
        const delai = parseInt(quotaMatch[1], 10) || 30;
        // Remplacer le préfixe technique par un message lisible avant affichage.
        afficherMarkdown(messageElement, reponseTexte.replace(/^QUOTA:\d+:/, ''));
        bloquerEnvoi(delai);
      } else if (reponseTexte.toLowerCase().includes('quota')) {
        bloquerEnvoi(30);
      }
    }

  } catch(err) {
    const requeteObsolete = controller.signal.aborted
      && requeteActiveController !== controller;
    if (requeteObsolete) {
      if (reponseElement) reponseElement.remove();
      if (!reponseEnCours && !requeteActiveController) retirerReflexion();
      if (vocalActif && !recoEnCours && !reponseEnCours && !interruptionDemandee
          && window._dashleVocal) {
        setTimeout(function() {
          if (vocalActif && !recoEnCours && !reponseEnCours && !interruptionDemandee) {
            window._dashleVocal.reprendreEcoute();
          }
        }, 120);
      }
      return;
    }

    retirerReflexion();
    reponseEnCours = false;
    if (requeteActiveController === controller) requeteActiveController = null;
    reponseActiveElement = null;

    // En cas d'erreur, remettre l'orbe en état écoute (pas bloquée en réflexion).
    if (window._dashleVocal && window._dashleVocal.estActif()) {
      afficherEtatVocal('ecoute', "Je t'écoute...");
      btnVocal.classList.add('ecoute');
      btnVocal.classList.remove('parle');
    }

    if (err && err.name === 'AbortError') {
      if (reponseElement) reponseElement.remove();
      if (vocalActif && !recoEnCours && !interruptionDemandee && window._dashleVocal) {
        setTimeout(function() {
          if (vocalActif && !recoEnCours && !reponseEnCours && !interruptionDemandee) {
            window._dashleVocal.reprendreEcoute();
          }
        }, 120);
      }
      return;
    }

    if (reponseElement && !reponseElement.querySelector('.msg').textContent.trim()) {
      reponseElement.remove();
    }
    ajouterMessage("Erreur de connexion. Réessaie.", 'bot');
    if (window._dashleVocal && window._dashleVocal.estActif() && !recoEnCours) {
      setTimeout(window._dashleVocal.reprendreEcoute, 700);
    }
  }
});

// =====================================================================
// Bouton "Arrêter la génération"
// =====================================================================
const btnArreter = document.getElementById('btn-arreter');

// Affiche le bouton arrêter pendant la génération, cache le bouton envoyer.
function montrerBtnArreter() {
  if (btnArreter) btnArreter.classList.add('visible');
  btnEnvoyer.style.display = 'none';
}

function cacherBtnArreter() {
  if (btnArreter) btnArreter.classList.remove('visible');
  btnEnvoyer.style.display = '';
}

if (btnArreter) {
  btnArreter.addEventListener('click', function() {
    arreterGeneration();
    if ('speechSynthesis' in window) window.speechSynthesis.cancel();
    syntheseEnCours = false;
    vadDebutSynthese = 0;
    retirerReflexion();
    cacherBtnArreter();
    champ.disabled = false;
    btnEnvoyer.disabled = false;
    if (window._dashleVocal && window._dashleVocal.estActif()) {
      afficherEtatVocal('ecoute', "Je t'écoute...");
    }
  });
}

// Synchroniser l'affichage du bouton arrêter avec reponseEnCours.
// On wrappe form.addEventListener('submit') pour ajouter le show/hide.
(function() {
  var origSubmit = form.onsubmit;
  // Patch : on observe les changements de reponseEnCours via polling léger.
  var dernierEtat = false;
  setInterval(function() {
    if (reponseEnCours !== dernierEtat) {
      dernierEtat = reponseEnCours;
      if (reponseEnCours) {
        montrerBtnArreter();
      } else {
        cacherBtnArreter();
      }
    }
  }, 120);
})();

// =====================================================================
// Thème en temps réel et taille du texte
// =====================================================================

// Applique le thème sans rechargement de page.
function appliquerTheme(theme) {
  var systemeSombre = window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches;
  var sombre = theme === 'sombre' || (theme === 'systeme' && systemeSombre);
  document.body.classList.toggle('theme-sombre', sombre);
  document.body.classList.toggle('theme-clair', !sombre);
  try { localStorage.setItem('dashle_theme', theme); } catch(e) {}
}

if (window.matchMedia) {
  var preferenceSysteme = window.matchMedia('(prefers-color-scheme: dark)');
  var suivreThemeSysteme = function() {
    try { if (localStorage.getItem('dashle_theme') === 'systeme') appliquerTheme('systeme'); } catch(e) {}
  };
  if (preferenceSysteme.addEventListener) preferenceSysteme.addEventListener('change', suivreThemeSysteme);
  else if (preferenceSysteme.addListener) preferenceSysteme.addListener(suivreThemeSysteme);
}

function appliquerAccent(accent) {
  var palettes = {
    'vert-bleu': ['#22C55E', '#3B82F6'],
    'bleu': ['#2563EB', '#06B6D4'],
    'violet': ['#7C3AED', '#DB2777'],
    'ambre': ['#D97706', '#DC2626'],
  };
  var couleurs = palettes[accent] || palettes['vert-bleu'];
  document.documentElement.style.setProperty('--accent-vert', couleurs[0]);
  document.documentElement.style.setProperty('--accent-bleu', couleurs[1]);
  try { localStorage.setItem('dashle_accent', accent); } catch(e) {}
}

function appliquerDensite(densite) {
  document.body.dataset.densite = densite === 'compacte' ? 'compacte' : 'confortable';
  try { localStorage.setItem('dashle_densite', document.body.dataset.densite); } catch(e) {}
}

function appliquerLargeurConversation(largeur) {
  var largeurs = { 'etroite': '620px', 'standard': '760px', 'large': '980px' };
  var valeur = largeurs[largeur] ? largeur : 'standard';
  document.documentElement.style.setProperty('--largeur-conversation', largeurs[valeur]);
  try { localStorage.setItem('dashle_largeur_conversation', valeur); } catch(e) {}
}

function appliquerAnimations(mode) {
  var modes = ['normales', 'reduites', 'desactivees'];
  document.body.dataset.animations = modes.includes(mode) ? mode : 'normales';
  try { localStorage.setItem('dashle_animations', document.body.dataset.animations); } catch(e) {}
}

// Applique la taille de texte des messages sans rechargement.
function appliquerTailleMsg(taille) {
  document.documentElement.style.setProperty('--taille-msg', taille);
  try { localStorage.setItem('dashle_taille_msg', taille); } catch(e) {}
}

// Restaurer les préférences sauvegardées localement (si l'utilisateur
// n'est pas connecté ou si la page vient de charger).
(function() {
  try {
    var t = localStorage.getItem('dashle_theme') || preferencesVocales.theme;
    if (t === 'sombre' || t === 'clair' || t === 'systeme') appliquerTheme(t);
    var accent = localStorage.getItem('dashle_accent');
    if (accent) appliquerAccent(accent);
    appliquerDensite(localStorage.getItem('dashle_densite') || 'confortable');
    var tm = localStorage.getItem('dashle_taille_msg');
    if (tm) appliquerTailleMsg(tm);
    appliquerLargeurConversation(localStorage.getItem('dashle_largeur_conversation') || 'standard');
    appliquerAnimations(localStorage.getItem('dashle_animations') || 'normales');
  } catch(e) {}
})();
if (window.matchMedia) {
  window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change', function() {
    try { if (localStorage.getItem('dashle_theme') === 'systeme') appliquerTheme('systeme'); } catch(e) {}
  });
}
</script>
"""

# On injecte le JS dans la chaîne PAGE après la fermeture de </div id="statut-vocal">
PAGE = PAGE + _JS + "\n</body>\n</html>\n"


# ---------------------------------------------------------------------------
# Pages secondaires (partage, paramètres, sécurité, auth)
# ---------------------------------------------------------------------------

SHARE_PAGE = """
<!doctype html><html lang="fr"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Dashle - {{ titre }}</title>
<style>
body{font-family:Segoe UI,sans-serif;background:#f4f8f6;color:#14251f;margin:0}
.partage{max-width:760px;margin:0 auto;padding:28px 18px}
.marque{color:#22C55E;font-weight:700;font-size:18px}
h1{margin:4px 0 20px;font-size:20px}
.message{padding:12px 16px;margin:12px 0;border-radius:14px;white-space:pre-wrap;line-height:1.5;background:#fff;box-shadow:0 2px 8px rgba(0,0,0,0.06)}
.user{margin-left:15%;background:linear-gradient(110deg,#dcfce7,#dbeafe)}
.bot{margin-right:15%}
</style></head>
<body><main class="partage">
<div class="marque">Dashle</div>
<h1>{{ titre }}</h1>
{% for m in messages %}
<div class="message {{ m.auteur }}">{{ m.texte }}</div>
{% endfor %}
</main></body></html>
"""

SETTINGS_PAGE = """
<!doctype html><html lang="fr"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Dashle - Paramètres</title>
<style>
:root{font-family:Segoe UI,sans-serif;color:#17251f;background:#f4f8f6;--param-texte:#17251f;--param-fond:#f4f8f6;--param-carte:#fff;--param-bordure:#dceae4;--param-muted:#71837b}
*{box-sizing:border-box}body{margin:0}
.page{max-width:820px;margin:auto;padding:30px 20px 56px}
.bar{display:flex;justify-content:space-between;align-items:center;margin-bottom:22px}
.bar a{color:#22C55E;text-decoration:none;font-weight:600;margin-left:12px}
.carte{background:var(--param-carte);border:1px solid var(--param-bordure);border-radius:18px;padding:20px;margin:14px 0;box-shadow:0 8px 24px rgba(20,55,42,.045)}
.carte h2{font-size:15px;margin:0 0 14px;color:#168c65;letter-spacing:.02em}
label{display:flex;justify-content:space-between;gap:14px;align-items:center;padding:12px 0;border-top:1px solid #edf2f0}
label:first-of-type{border-top:0}
select,input[type=checkbox],input[type=range]{accent-color:#22C55E}
select,textarea{max-width:100%;padding:9px 11px;border:1px solid var(--param-bordure);border-radius:9px;background:var(--param-carte);color:var(--param-texte);font:inherit}
select:focus,textarea:focus{outline:2px solid rgba(59,130,246,.28);border-color:#3b82f6}
input[type=range]{width:160px}
button{border:0;border-radius:9px;background:linear-gradient(110deg,#22C55E,#3B82F6);color:#fff;padding:10px 14px;cursor:pointer}
.secondaire{background:#eef6ff;color:#2563eb}
.note{color:var(--param-muted);font-size:13px}
body.theme-sombre{color:var(--param-texte);background:var(--param-fond);--param-texte:#e8f5ef;--param-fond:#101816;--param-carte:#17231f;--param-bordure:#294238;--param-muted:#a8bdb4}
body.theme-sombre label{border-color:#294238}
@media(max-width:600px){.page{padding:20px 13px 40px}.bar{align-items:flex-start;gap:12px}.bar>div:last-child{display:grid;gap:8px}.bar a{margin:0}.carte{padding:16px;border-radius:15px}label{align-items:flex-start;flex-direction:column;gap:7px}label select,label input[type=range],label textarea{width:100%;max-width:100%}}
</style></head>
<body class="theme-{{ preferences.theme }}"><main class="page">
<div class="bar">
  <div><strong>Dashle</strong><h1>Paramètres</h1></div>
  <div>
    <a href="{{ url_for('accueil') }}">Retour au chat</a>
    <a href="{{ url_for('securite') }}">Sécurité</a>
  </div>
</div>
{% if erreur %}<p class="note">{{ erreur }}</p>{% endif %}
{% if succes %}<p class="note">{{ succes }}</p>{% endif %}
<form method="post">
  <input type="hidden" name="csrf_token" value="{{ csrf_token }}">
  <section class="carte"><h2>Compte</h2><p>{{ utilisateur }}</p>
    <label>Pays
      <select name="pays" required autocomplete="country">
        {% for code, nom_pays, indicatif in pays_profil %}<option value="{{ code }}" {% if pays_utilisateur == code %}selected{% endif %}>{{ nom_pays }} (+{{ indicatif }})</option>{% endfor %}
      </select>
    </label>
    <label>Numéro de téléphone
      <input name="telephone" type="tel" required inputmode="tel" autocomplete="tel-national" value="{{ telephone_utilisateur }}">
    </label>
    <p class="note">Le numéro est enregistré avec son indicatif international. Le 0 initial reste conservé dans ton profil.</p>
    <p class="note">La modification de l'adresse e-mail et la récupération de compte ne sont pas encore disponibles.</p>
  </section>
  <section class="carte"><h2>Apparence</h2>
    <label>Thème
      <select name="theme">
        <option value="clair"  {% if preferences.theme == 'clair'  %}selected{% endif %}>Clair</option>
        <option value="sombre" {% if preferences.theme == 'sombre' %}selected{% endif %}>Sombre</option>
        <option value="systeme" {% if preferences.theme == 'systeme' %}selected{% endif %}>Syst&egrave;me</option>
      </select>
    </label>
    <label>Couleur d'accent<select id="accent-select"><option value="vert-bleu">Vert - bleu</option><option value="bleu">Bleu</option><option value="violet">Violet</option><option value="ambre">Ambre</option></select></label>
    <label>Densit&eacute;<select id="densite-select"><option value="confortable">Confortable</option><option value="compacte">Compacte</option></select></label>
    <label>Largeur de conversation<select id="largeur-conversation-select"><option value="etroite">&Eacute;troite</option><option value="standard">Standard</option><option value="large">Large</option></select></label>
    <label>Animations<select id="animations-select"><option value="normales">Normales</option><option value="reduites">R&eacute;duites</option><option value="desactivees">D&eacute;sactiv&eacute;es</option></select></label>
  </section>
  <section class="carte"><h2>Voix</h2>
    <label>Lecture automatique des r&eacute;ponses<input type="checkbox" name="lecture_automatique" {% if preferences.lecture_automatique %}checked{% endif %}></label>
    <p class="note">L'autorisation du microphone se g&egrave;re dans les permissions du navigateur.</p>
    <label>Mode vocal et microphone activés<input type="checkbox" name="voix_active" {% if preferences.voix_active %}checked{% endif %}></label>
    <label>Voix française<select id="voix-select" name="voix_nom" data-selection="{{ preferences.voix_nom }}"><option value="">Automatique</option></select></label>
    <label>Vitesse<input type="range" name="voix_vitesse" min="0.6" max="1.4" step="0.05" value="{{ preferences.voix_vitesse }}"><output id="vitesse-valeur">{{ preferences.voix_vitesse }}</output></label>
    <label>Tonalité<input type="range" name="voix_tonalite" min="0.7" max="1.3" step="0.05" value="{{ preferences.voix_tonalite }}"><output id="tonalite-valeur">{{ preferences.voix_tonalite }}</output></label>
    <label>Volume<input type="range" name="voix_volume" min="0.2" max="1" step="0.05" value="{{ preferences.voix_volume }}"><output id="volume-valeur">{{ preferences.voix_volume }}</output></label>
    <button type="button" class="secondaire" id="tester-voix">▶ Tester la voix</button>
    <p class="note">Dashle privilégie automatiquement une voix féminine française. La lecture automatique reste désactivée par défaut.</p>
  </section>
  <section class="carte"><h2>Conversations et confidentialité</h2>
    <label>Longueur des r&eacute;ponses<select name="longueur_reponse"><option value="courte" {% if longueur_reponse == 'courte' %}selected{% endif %}>Courte</option><option value="standard" {% if longueur_reponse == 'standard' %}selected{% endif %}>Normale</option><option value="detaillee" {% if longueur_reponse == 'detaillee' %}selected{% endif %}>D&eacute;taill&eacute;e</option></select></label>
    <label for="consignes-personnalisees">Consignes personnalis&eacute;es</label>
    <textarea id="consignes-personnalisees" name="consignes_personnalisees" maxlength="2000" rows="4" style="width:100%;resize:vertical">{{ consignes_personnalisees }}</textarea>
    <label>Activer ma mémoire personnelle<input type="checkbox" name="memoire_active" {% if preferences.memoire_active %}checked{% endif %}></label>
    <p class="note">Désactive cette option pour que Dashle ne lise ni n’enregistre tes souvenirs personnels. Les consignes et réglages du compte restent disponibles.</p>
    <label>Conserver l'historique<input type="checkbox" name="conserver_historique" {% if preferences.conserver_historique %}checked{% endif %}></label>
    <p class="note">D&eacute;sactiv&eacute;e, cette option garde les nouveaux &eacute;changes dans la page courante jusqu'au rechargement. Les conversations d&eacute;j&agrave; enregistr&eacute;es restent intactes; aucun nouveau message n'est ajout&eacute; &agrave; l'historique.</p>
    <p class="note">Les conversations partagées utilisent un lien révocable et ne montrent pas les informations du compte.</p>
  </section>
  <section class="carte"><h2>Sécurité</h2>
    <p class="note">Les mots de passe sont hachés. <a href="{{ url_for('securite') }}">Gérer la sécurité du compte →</a></p>
  </section>
  <section class="carte"><h2>Données et confidentialité</h2>
    <p><a href="{{ url_for('gestion_donnees') }}">Exporter les données et gérer la mémoire</a></p>
    <p class="note">L'export et la gestion de la m&eacute;moire sont disponibles sur la page <a href="{{ url_for('gestion_donnees') }}">Donn&eacute;es et m&eacute;moire</a>.</p>
    <p class="note">Les conversations partagées utilisent un lien révocable et ne montrent pas les informations du compte.</p>
  </section>
  <section class="carte"><h2>Apparence avancée</h2>
    <label>Taille du texte des messages
      <select name="taille_msg" id="taille-msg-select">
        <option value="13px" {% if preferences.get('taille_msg','15px')=='13px' %}selected{% endif %}>Petite</option>
        <option value="15px" {% if preferences.get('taille_msg','15px')=='15px' or not preferences.get('taille_msg') %}selected{% endif %}>Normale</option>
        <option value="17px" {% if preferences.get('taille_msg','15px')=='17px' %}selected{% endif %}>Grande</option>
        <option value="19px" {% if preferences.get('taille_msg','15px')=='19px' %}selected{% endif %}>Très grande</option>
      </select>
    </label>
  </section>
  <section class="carte" id="diagnostic-vocal" hidden>
  <h2>Diagnostic vocal</h2>
  <p class="note">Ce panneau est local au navigateur et n'apparaît qu'après 5 appuis rapides sur « Paramètres ».</p>
  <pre id="diagnostic-vocal-contenu" style="max-height:280px;overflow:auto;white-space:pre-wrap"></pre>
  <button type="button" class="secondaire" id="diagnostic-vocal-copier">Copier</button>
  <button type="button" class="secondaire" id="diagnostic-vocal-effacer">Effacer</button>
</section>
<section class="carte"><h2>À propos de Dashle</h2>
    <p class="note"><strong>Build :</strong> {{ build_commit_short }}</p>
    <p class="note"><strong>Commit :</strong> {{ build_commit }}</p>
    <p class="note"><strong>Date :</strong> {{ build_date }}</p>
    <p class="note"><strong>Modèle IA :</strong> {{ modele_gemini }} (Google AI)</p>
    <p class="note">Dashle est un assistant personnel conçu par Owen. Il mémorise le contexte de tes conversations et s'améliore avec le temps.</p>
    <p><a href="{{ url_for('conditions_utilisation') }}">Conditions d'utilisation</a></p>
  </section>
  <button type="submit">Enregistrer</button>
</form>
<form method="post" action="{{ url_for('nouvelle_conv') }}">
  <input type="hidden" name="csrf_token" value="{{ csrf_token }}">
  <button type="submit" class="secondaire">Commencer une nouvelle conversation</button>
</form>
<script>
const diagSection = document.getElementById('diagnostic-vocal');
const diagContent = document.getElementById('diagnostic-vocal-contenu');
function afficherDiagnosticVocal() {
  if (!diagSection || !diagContent) return;
  diagSection.hidden = false;
  try { diagContent.textContent = JSON.stringify(JSON.parse(localStorage.getItem('dashle_vocal_diagnostic_v1') || '[]'), null, 2); } catch(e) { diagContent.textContent = 'Diagnostic indisponible.'; }
}
try {
  if (localStorage.getItem('dashle_vocal_diag_open') === '1') {
    localStorage.removeItem('dashle_vocal_diag_open');
    afficherDiagnosticVocal();
  }
} catch(e) {}
if (diagSection) {
  document.getElementById('diagnostic-vocal-copier').addEventListener('click', async function() {
    try { await navigator.clipboard.writeText(diagContent.textContent); } catch(e) {}
  });
  document.getElementById('diagnostic-vocal-effacer').addEventListener('click', function() {
    try { localStorage.removeItem('dashle_vocal_diagnostic_v1'); } catch(e) {}
    diagContent.textContent = '';
  });
}
const sv = document.getElementById('voix-select');
let vp = [];
function remplirVoix() {
  vp = ('speechSynthesis' in window) ? speechSynthesis.getVoices().filter(v => v.lang && v.lang.toLowerCase().startsWith('fr')) : [];
  sv.innerHTML = '<option value="">Automatique</option>';
  vp.forEach(v => { const o = document.createElement('option'); o.value = v.name; o.textContent = v.name + ' (' + v.lang + ')'; o.selected = v.name === sv.dataset.selection; sv.appendChild(o); });
}
remplirVoix();
if ('speechSynthesis' in window) speechSynthesis.onvoiceschanged = remplirVoix;
function sync() {
  document.getElementById('vitesse-valeur').value  = document.querySelector('[name=voix_vitesse]').value;
  document.getElementById('tonalite-valeur').value = document.querySelector('[name=voix_tonalite]').value;
  document.getElementById('volume-valeur').value   = document.querySelector('[name=voix_volume]').value;
}
document.querySelectorAll('input[type=range]').forEach(i => i.addEventListener('input', sync));
document.getElementById('tester-voix').addEventListener('click', () => {
  if (!('speechSynthesis' in window)) return;
  speechSynthesis.cancel();
  const u = new SpeechSynthesisUtterance('Bonjour, je suis Dashle.');
  u.lang = 'fr-FR';
  u.voice = vp.find(v => v.name === sv.value) || vp[0] || null;
  u.rate   = Number(document.querySelector('[name=voix_vitesse]').value);
  u.pitch  = Number(document.querySelector('[name=voix_tonalite]').value);
  u.volume = Number(document.querySelector('[name=voix_volume]').value);
  speechSynthesis.speak(u);
});

// Thème en temps réel : appliqué immédiatement quand l'utilisateur change
// la valeur, sans attendre l'enregistrement. Utilise localStorage pour
// synchroniser avec le chat (la page principale lit localStorage au chargement).
var themeSelect = document.querySelector('[name=theme]');
if (themeSelect) {
  try {
    var savedTheme = localStorage.getItem('dashle_theme');
    if (savedTheme && ['clair', 'sombre', 'systeme'].includes(savedTheme)) themeSelect.value = savedTheme;
  } catch(e) {}
  themeSelect.addEventListener('change', function() {
    try { localStorage.setItem('dashle_theme', this.value); } catch(e) {}
    document.body.classList.toggle('theme-sombre', this.value === 'sombre' || (this.value === 'systeme' && window.matchMedia('(prefers-color-scheme: dark)').matches));
    document.body.classList.toggle('theme-clair', !document.body.classList.contains('theme-sombre'));
  });
  if (themeSelect.value === 'systeme' && window.matchMedia) {
    window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change', function(e) {
      if (themeSelect.value === 'systeme') {
        document.body.classList.toggle('theme-sombre', e.matches);
        document.body.classList.toggle('theme-clair', !e.matches);
      }
    });
  }
}

// Taille de texte en temps réel.
var tailleSelect = document.getElementById('taille-msg-select');
if (tailleSelect) {
  try { tailleSelect.value = localStorage.getItem('dashle_taille_msg') || '15px'; } catch(e) {}
  tailleSelect.addEventListener('change', function() {
    try { localStorage.setItem('dashle_taille_msg', this.value); } catch(e) {}
  });
}
var accentSelect = document.getElementById('accent-select');
var densiteSelect = document.getElementById('densite-select');
var largeurSelect = document.getElementById('largeur-conversation-select');
var animationsSelect = document.getElementById('animations-select');
try {
  if (accentSelect) accentSelect.value = localStorage.getItem('dashle_accent') || 'vert-bleu';
  if (densiteSelect) densiteSelect.value = localStorage.getItem('dashle_densite') || 'confortable';
  if (largeurSelect) largeurSelect.value = localStorage.getItem('dashle_largeur_conversation') || 'standard';
  if (animationsSelect) animationsSelect.value = localStorage.getItem('dashle_animations') || 'normales';
} catch(e) {}
if (accentSelect) accentSelect.addEventListener('change', function() {
  try { localStorage.setItem('dashle_accent', this.value); } catch(e) {}
});
if (densiteSelect) densiteSelect.addEventListener('change', function() {
  try { localStorage.setItem('dashle_densite', this.value); } catch(e) {}
});
if (largeurSelect) largeurSelect.addEventListener('change', function() {
  try { localStorage.setItem('dashle_largeur_conversation', this.value); } catch(e) {}
});
if (animationsSelect) animationsSelect.addEventListener('change', function() {
  try { localStorage.setItem('dashle_animations', this.value); } catch(e) {}
});
</script>
</main></body></html>
"""

DATA_PAGE = """
<!doctype html><html lang="fr"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Dashle - Donn&eacute;es</title>
<style>:root{font-family:Segoe UI,sans-serif;color:#17251f;background:#f4f8f6}*{box-sizing:border-box}body{margin:0}.page{max-width:760px;margin:auto;padding:24px 18px 50px}.carte{background:#fff;border:1px solid #dceae4;border-radius:14px;padding:18px;margin:12px 0}.carte h2{font-size:16px;color:#22C55E}.note{color:#71837b;font-size:13px}a{color:#22C55E}button{border:0;border-radius:9px;background:linear-gradient(110deg,#22C55E,#3B82F6);color:#fff;padding:10px 14px;cursor:pointer}.danger{background:#b42318}li{margin:10px 0}.valeur{white-space:pre-wrap;overflow-wrap:anywhere}</style></head>
<body><main class="page"><a href="{{ url_for('parametres') }}">&larr; Param&egrave;tres</a><h1>Donn&eacute;es et m&eacute;moire</h1>
{% if erreur %}<p class="note">{{ erreur }}</p>{% endif %}{% if succes %}<p class="note">{{ succes }}</p>{% endif %}
<section class="carte"><h2>Exporter</h2><p>Une copie JSON comprend tes conversations et les souvenirs enregistr&eacute;s dans Dashle.</p><a href="{{ url_for('exporter_donnees') }}">T&eacute;l&eacute;charger mes donn&eacute;es</a></section>
<section class="carte"><h2>M&eacute;moire</h2>{% if memoires %}<ul>{% for souvenir in memoires %}<li><strong>{{ souvenir.cle }}</strong><div class="valeur">{{ souvenir.valeur }}</div><form method="post" action="{{ url_for('supprimer_souvenir', cle=souvenir.cle) }}"><input type="hidden" name="csrf_token" value="{{ csrf_token }}"><button type="submit">Retirer ce souvenir</button></form></li>{% endfor %}</ul>{% else %}<p class="note">Aucun souvenir enregistr&eacute;.</p>{% endif %}
<form method="post" action="{{ url_for('effacer_memoire') }}"><input type="hidden" name="csrf_token" value="{{ csrf_token }}"><label>Pour tout effacer, saisis EFFACER <input name="confirmation" required></label><button class="danger" type="submit">Effacer toute la m&eacute;moire</button></form></section>
<section class="carte"><h2>Historique</h2><p>Cette action supprime les conversations de ton compte ainsi que leurs liens de partage.</p><form method="post" action="{{ url_for('effacer_historique') }}"><input type="hidden" name="csrf_token" value="{{ csrf_token }}"><label>Pour confirmer, saisis EFFACER <input name="confirmation" required></label><button class="danger" type="submit">Effacer tout l'historique</button></form></section>
</main></body></html>
"""

CONDITIONS_PAGE = """
<!doctype html><html lang="fr"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Conditions d'utilisation - Dashle</title>
<style>body{font:16px/1.6 'Segoe UI',sans-serif;color:#17251f;background:#f4f8f6;margin:0}.page{max-width:760px;margin:auto;padding:28px 18px}main{background:#fff;border:1px solid #dceae4;border-radius:14px;padding:22px}a{color:#22C55E}</style></head>
<body><main class="page"><a href="{{ url_for('accueil') }}">&larr; Dashle</a><h1>Conditions d'utilisation</h1>
<p>Dashle est un assistant personnel. Les r&eacute;ponses peuvent contenir des erreurs : v&eacute;rifie les informations importantes.</p>
<p>Les messages envoy&eacute;s au service peuvent &ecirc;tre trait&eacute;s par Google Gemini pour g&eacute;n&eacute;rer une r&eacute;ponse. Ne partage pas d'informations que tu ne souhaites pas transmettre &agrave; ce service.</p>
<p>Les utilisateurs connect&eacute;s peuvent exporter ou supprimer leurs conversations et souvenirs depuis les param&egrave;tres. Les visiteurs utilisent une conversation temporaire dans leur session.</p>
<p>En utilisant Dashle, tu acceptes ces modalit&eacute;s d'utilisation du service.</p></main></body></html>
"""

NEWS_PAGE = """
<!doctype html><html lang="fr"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Nouveaut&eacute;s DASHLE</title>
<style>
body{font:16px/1.6 'Segoe UI',sans-serif;color:#17251f;background:#f4f8f6;margin:0}
.page{max-width:760px;margin:auto;padding:28px 18px 48px}
.bar{display:flex;align-items:center;justify-content:space-between;gap:16px;margin-bottom:22px}
.bar a,.carte a{color:#16803d;text-decoration:none}
.carte{background:#fff;border:1px solid #dceae4;border-radius:14px;padding:18px;margin:12px 0}
.carte h2{margin:0 0 8px;color:#16803d;font-size:18px}
.categorie{color:#52645b;font-size:12px;font-weight:700;letter-spacing:.06em;text-transform:uppercase}
.note{color:#71837b;font-size:14px}
</style></head><body><main class="page">
<div class="bar"><h1>Nouveaut&eacute;s DASHLE</h1><a href="{{ url_for('accueil') }}">&larr; Retour au chat</a></div>
<p class="note">Annonces et informations du projet, publi&eacute;es directement dans DASHLE.</p>
<article class="carte"><span class="categorie">Fonctionnalit&eacute;s</span><h2>Personnalise ton interface</h2>
<p>Les param&egrave;tres permettent de choisir le th&egrave;me, l'accent, la taille du texte, la largeur de conversation et le niveau d'animation. Ces pr&eacute;f&eacute;rences d'interface sont conserv&eacute;es dans ce navigateur.</p>
<a href="{{ url_for('parametres') }}">Ouvrir les Param&egrave;tres</a></article>
<article class="carte"><span class="categorie">Fonctionnalit&eacute;s</span><h2>Images et vid&eacute;os dans la conversation</h2>
<p>Tu peux joindre une image ou une vid&eacute;o au chat pour demander &agrave; DASHLE de l'examiner.</p></article>
<article class="carte"><span class="categorie">Projet</span><h2>Un flux interne</h2>
<p>Cette page regroupe les annonces et les informations DASHLE. Son contenu est g&eacute;r&eacute; dans l'application et ne d&eacute;pend pas d'une API d'actualit&eacute;s externe.</p></article>
</main></body></html>
"""

SECURITY_PAGE = """
<!doctype html><html lang="fr"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Dashle - Sécurité</title>
<style>
:root{font-family:Segoe UI,sans-serif;color:#17251f;background:#f4f8f6}*{box-sizing:border-box}body{margin:0}
.page{max-width:620px;margin:auto;padding:24px 18px 50px}
.bar{display:flex;justify-content:space-between;align-items:center;margin-bottom:22px}
.bar a{color:#22C55E;text-decoration:none;font-weight:600}
.carte{background:#fff;border:1px solid #dceae4;border-radius:14px;padding:18px;margin:12px 0}
.carte h2{font-size:15px;margin:0 0 14px;color:#22C55E}
label{display:block;margin-top:12px;font-size:14px}
input{display:block;width:100%;margin-top:5px;padding:10px;border:1px solid #dceae4;border-radius:8px}
button{border:0;border-radius:9px;background:linear-gradient(110deg,#22C55E,#3B82F6);color:#fff;padding:10px 14px;margin-top:16px;cursor:pointer}
.danger{background:#b42318}
.note{color:#71837b;font-size:13px}
.message{padding:10px;border-radius:8px;background:#eef6ff;color:#2563eb}
</style></head>
<body><main class="page">
<div class="bar"><div><strong>Dashle</strong><h1>Sécurité</h1></div><a href="{{ url_for('parametres') }}">← Paramètres</a></div>
{% if erreur %}<p class="note">{{ erreur }}</p>{% endif %}
{% if succes %}<p class="message">{{ succes }}</p>{% endif %}
<section class="carte"><h2>Session active</h2>
  <p class="note">Cette session est active dans le navigateur courant. Les sessions ne sont pas enregistr&eacute;es individuellement par Dashle.</p>
  <form method="post" action="{{ url_for('deconnexion') }}"><input type="hidden" name="csrf_token" value="{{ csrf_token }}"><button type="submit">Fermer cette session</button></form>
</section>
<section class="carte"><h2>Modifier le mot de passe</h2>
  <form method="post" action="{{ url_for('changer_mot_de_passe') }}">
    <input type="hidden" name="csrf_token" value="{{ csrf_token }}">
    <label>Ancien mot de passe<input type="password" name="ancien_password" required autocomplete="current-password"></label>
    <label>Nouveau mot de passe<input type="password" name="nouveau_password" minlength="8" required autocomplete="new-password"></label>
    <label>Confirmation<input type="password" name="confirmation_password" minlength="8" required autocomplete="new-password"></label>
    <button type="submit">Modifier le mot de passe</button>
  </form>
</section>
<section class="carte"><h2>Supprimer le compte</h2>
  <p class="note">Cette action supprime définitivement le compte, les conversations, les préférences et la mémoire associée.</p>
  <form method="post" action="{{ url_for('supprimer_compte') }}">
    <input type="hidden" name="csrf_token" value="{{ csrf_token }}">
    <label>Écris <strong>supprimer</strong> pour confirmer<input type="text" name="confirmation" required></label>
    <button class="danger" type="submit">Supprimer définitivement</button>
  </form>
</section>
</main></body></html>
"""

TEMPS_REEL_PAGE = """
<!doctype html><html lang="fr"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Temps réel — DASHLE</title>
<style>:root{font-family:Inter,Segoe UI,sans-serif;color:#18352c;background:#f3f8f6}*{box-sizing:border-box}body{margin:0;padding:26px 16px}.wrap{max-width:900px;margin:auto}a{color:#16765b;text-decoration:none;font-weight:600}h1{font-size:clamp(28px,5vw,40px);margin:28px 0 8px}.intro{color:#627970}.card{background:#fff;border:1px solid #dce9e4;border-radius:16px;padding:20px;margin:16px 0;box-shadow:0 10px 28px #173a2b0c}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(250px,1fr));gap:14px}.field{display:flex;gap:8px;margin-top:14px}input{flex:1;min-width:0;padding:10px;border:1px solid #d5e3dd;border-radius:9px;font:inherit}button{padding:10px 14px;border:0;border-radius:9px;background:linear-gradient(110deg,#25bd80,#3b82f6);color:white;font:600 14px Inter,Segoe UI,sans-serif;cursor:pointer}#clock{font-size:24px;font-weight:700;color:#19765d}.subtle{font-size:13px;color:#6b7e76}.weather{line-height:1.65}.weather-dashboard{position:relative;overflow:hidden;margin-top:14px;padding:22px;border-radius:14px;background:linear-gradient(120deg,#e2f8e9,#e6f1ff);border:1px solid #cfe9df}.weather-top{display:flex;align-items:center;gap:16px}.weather-icon{font-size:54px;line-height:1}.weather-place{font-weight:700;font-size:18px}.weather-description{color:#526d64;text-transform:capitalize;margin-top:4px}.weather-temperature{font-size:clamp(42px,10vw,60px);font-weight:750;letter-spacing:-2px;color:#176b54;line-height:1.15;margin:14px 0}.weather-metrics{display:grid;grid-template-columns:repeat(auto-fit,minmax(110px,1fr));gap:10px;margin-top:18px}.weather-metric{background:#ffffffb8;border:1px solid #ffffff;border-radius:11px;padding:11px 13px}.weather-metric strong{display:block;font-size:18px;margin-top:4px}.humidity-track{height:6px;background:#d9e6e1;border-radius:9px;margin-top:8px;overflow:hidden}.humidity-track span{display:block;height:100%;border-radius:9px;background:linear-gradient(90deg,#25bd80,#3b82f6)}.news{padding-left:20px;line-height:1.6}.news li{margin:9px 0}.error{color:#9b3828}.source{font-size:12px;color:#6b7e76}</style></head>
<body><main class="wrap"><a href="{{ url_for('accueil') }}">← Retour à DASHLE</a><h1>Le temps, maintenant</h1><p class="intro">Date et heure locales, météo du jour et titres récents de sources identifiées.</p>
<div class="grid"><section class="card"><h2>Date et heure locales</h2><div id="clock">—</div><p class="subtle">Affichées selon le fuseau horaire de ton appareil.</p></section>
<section class="card"><h2>Météo du jour</h2><label for="ville">Ville</label><div class="field"><input id="ville" maxlength="80" value="{{ ville_defaut }}" placeholder="Ex. Dakar"><button id="charger-meteo" type="button">Afficher</button></div><div id="weather" class="weather" aria-live="polite">Saisis une ville pour consulter la météo.</div><p class="source">Données météo : <a href="https://openweathermap.org/" target="_blank" rel="noopener">OpenWeather</a>.</p></section></div>
<section class="card"><h2>Actualités récentes</h2><ul id="news" class="news"><li>Chargement des titres…</li></ul><p class="source">Titres fournis par le flux RSS officiel de <a href="https://www.lemonde.fr/" target="_blank" rel="noopener">Le Monde</a>. Ouvre les liens pour lire les articles à la source.</p></section>
<section class="card"><h2>T\u00e9l\u00e9charger un PDF</h2><p class="subtle">G\u00e9n\u00e8re un document dat\u00e9 avec les informations m\u00e9t\u00e9o et les actualit\u00e9s disponibles au moment du t\u00e9l\u00e9chargement.</p><form method="post" action="{{ url_for('telecharger_pdf_temps_reel') }}" id="pdf-form"><input type="hidden" name="csrf_token" value="{{ csrf_token }}"><input type="hidden" name="fuseau" id="pdf-fuseau"><input type="hidden" name="ville" id="pdf-ville"><button type="submit">T\u00e9l\u00e9charger le PDF</button></form></section>
</main><script>
const horloge=document.getElementById('clock');function mettreAJourHorloge(){horloge.textContent=new Intl.DateTimeFormat('fr-FR',{dateStyle:'full',timeStyle:'medium'}).format(new Date());}mettreAJourHorloge();setInterval(mettreAJourHorloge,1000);
function ajouterNouvelles(items){const liste=document.getElementById('news');liste.replaceChildren();if(!items.length){const li=document.createElement('li');li.textContent='Le flux d’actualités est momentanément indisponible.';liste.appendChild(li);return;}items.forEach(function(item){const li=document.createElement('li'),lien=document.createElement('a');lien.href=item.url;lien.target='_blank';lien.rel='noopener';lien.textContent=item.titre;li.appendChild(lien);if(item.date){const date=document.createElement('span');date.className='subtle';date.textContent=' · '+item.date;li.appendChild(date);}liste.appendChild(li);});}
function afficherCarteMeteo(zone,m){const description=String(m.description||'Conditions indisponibles').toLowerCase();const icone=/orage|tonnerre/.test(description)?'⛈️':/pluie|bruine|averse/.test(description)?'🌧️':/neige|grésil/.test(description)?'❄️':/nuage|couvert|brume|brouillard/.test(description)?'☁️':'☀️';const humidite=Number.isFinite(Number(m.humidite))?Math.max(0,Math.min(100,Number(m.humidite))):null;const ville=[m.ville,m.pays].filter(Boolean).join(', ');zone.replaceChildren();const carte=document.createElement('div');carte.className='weather-dashboard';const haut=document.createElement('div');haut.className='weather-top';const symbole=document.createElement('div');symbole.className='weather-icon';symbole.setAttribute('aria-hidden','true');symbole.textContent=icone;const lieu=document.createElement('div');const nom=document.createElement('div');nom.className='weather-place';nom.textContent=ville;const etat=document.createElement('div');etat.className='weather-description';etat.textContent=m.description||'Conditions indisponibles';lieu.append(nom,etat);haut.append(symbole,lieu);carte.appendChild(haut);const temperature=document.createElement('div');temperature.className='weather-temperature';temperature.textContent=(m.temperature??'—')+' °C';carte.appendChild(temperature);const mesures=document.createElement('div');mesures.className='weather-metrics';[['Aujourd’hui',m.minimum==null||m.maximum==null?'—':m.minimum+'° / '+m.maximum+'°'],['Ressenti',m.ressenti==null?'—':m.ressenti+' °C'],['Humidité',humidite===null?'—':humidite+' %']].forEach(function(item){const bloc=document.createElement('div');bloc.className='weather-metric';const label=document.createElement('span');label.className='subtle';label.textContent=item[0];const valeur=document.createElement('strong');valeur.textContent=item[1];bloc.append(label,valeur);if(item[0]==='Humidité'&&humidite!==null){const jauge=document.createElement('div');jauge.className='humidity-track';const niveau=document.createElement('span');niveau.style.width=humidite+'%';jauge.appendChild(niveau);bloc.appendChild(jauge);}mesures.appendChild(bloc);});carte.appendChild(mesures);if(m.probabilite_pluie!=null){const pluie=document.createElement('p');pluie.className='subtle';pluie.textContent='Probabilité de pluie : '+m.probabilite_pluie+' %';carte.appendChild(pluie);}zone.appendChild(carte);}function chargerTemps(ville){const args=ville?'?ville='+encodeURIComponent(ville):'';fetch('/api/temps-reel'+args,{cache:'no-store'}).then(function(r){if(!r.ok)throw new Error('Temps r\u00e9el indisponible');return r.json();}).then(function(data){ajouterNouvelles(data.actualites||[]);const zone=document.getElementById('weather');if(data.meteo&&data.meteo.erreur){zone.textContent=data.meteo.erreur;return;}const m=data.meteo;if(!m){zone.textContent='Saisis une ville pour consulter la météo.';return;}var plages=(m.minimum===null||m.minimum===undefined||m.maximum===null||m.maximum===undefined)?' \u00b7 extr\u00eames du jour indisponibles':' \u00b7 minimum '+m.minimum+' \u00b0C, maximum '+m.maximum+' \u00b0C';var pluie=m.probabilite_pluie===null||m.probabilite_pluie===undefined?'':' \u00b7 pluie '+m.probabilite_pluie+' %';afficherCarteMeteo(zone,m);}).catch(function(){document.getElementById('weather').textContent='Le service m\u00e9t\u00e9o est momentan\u00e9ment indisponible.';document.getElementById('news').textContent='Le flux d\u2019actualit\u00e9s est momentan\u00e9ment indisponible.';});}
document.getElementById('pdf-form').addEventListener('submit',function(){document.getElementById('pdf-fuseau').value=Intl.DateTimeFormat().resolvedOptions().timeZone||'UTC';document.getElementById('pdf-ville').value=document.getElementById('ville').value.trim();});document.getElementById('charger-meteo').addEventListener('click',function(){chargerTemps(document.getElementById('ville').value.trim());});document.getElementById('ville').addEventListener('keydown',function(e){if(e.key==='Enter')chargerTemps(this.value.trim());});chargerTemps(document.getElementById('ville').value.trim());
</script></body></html>
"""


ESPACE_PAGE = """
<!doctype html><html lang="fr"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{{ titre }} — DASHLE</title><style>
:root{font-family:Inter,Segoe UI,sans-serif;color:#18352c;background:#f3f8f6}*{box-sizing:border-box}body{margin:0;padding:26px 16px}.wrap{max-width:900px;margin:auto}a{color:#16765b;text-decoration:none;font-weight:600}h1{font-size:clamp(28px,5vw,40px);margin:28px 0 8px}.intro,.muted{color:#627970;line-height:1.5}.card{background:white;border:1px solid #dce9e4;border-radius:15px;padding:20px;margin:15px 0;box-shadow:0 10px 28px #173a2b0c}input,select{width:100%;padding:10px;border:1px solid #d5e3dd;border-radius:9px;font:inherit;margin:6px 0 12px}button{padding:10px 14px;border:0;border-radius:9px;background:linear-gradient(110deg,#25bd80,#3b82f6);color:white;font:600 14px inherit;cursor:pointer}form.inline{display:inline}ul{padding-left:20px}li{margin:10px 0}.row{display:flex;align-items:center;gap:12px;justify-content:space-between}.error{color:#9b3828}.check{width:auto;margin:0 8px 0 0}
</style></head><body><main class="wrap"><a href="{{ url_for('accueil') }}">← Retour à DASHLE</a><h1>{{ titre }}</h1><p class="intro">{{ intro }}</p>{% if erreur %}<p class="error">{{ erreur }}</p>{% endif %}
{% if mode == 'rappels' %}
<form class="card" method="post" action="{{ url_for('planification') }}"><input type="hidden" name="csrf_token" value="{{ csrf_token }}"><label>Tâche ou rappel<input name="title" maxlength="200" required placeholder="Ex. Appeler le fournisseur"></label><label>Date et heure locale<input id="due-local" type="datetime-local" required><input id="due-utc" name="due_at" type="hidden"></label><button>Ajouter</button></form>
<section class="card"><h2>À venir et terminés</h2>{% if rappels %}<ul>{% for rappel in rappels %}<li class="row"><span><strong>{{ rappel.title }}</strong><br><span class="muted">{{ rappel.due_at.strftime('%d/%m/%Y à %H:%M UTC') }} · {{ 'Terminée' if rappel.completed else 'À faire' }}</span></span><form class="inline" method="post" action="{{ url_for('modifier_rappel', reminder_id=rappel.id) }}"><input type="hidden" name="csrf_token" value="{{ csrf_token }}"><button>{{ 'Rouvrir' if rappel.completed else 'Terminer' }}</button></form><form class="inline" method="post" action="{{ url_for('supprimer_rappel', reminder_id=rappel.id) }}"><input type="hidden" name="csrf_token" value="{{ csrf_token }}"><button aria-label="Supprimer">Supprimer</button></form></li>{% endfor %}</ul>{% else %}<p class="muted">Aucune tâche pour le moment.</p>{% endif %}</section>
<script>document.querySelector('form.card').addEventListener('submit',function(e){const local=document.getElementById('due-local');if(local.value)document.getElementById('due-utc').value=new Date(local.value).toISOString();else e.preventDefault();});</script>
{% elif mode == 'projets' %}
{% include 'projets.html' %}
{% elif mode == 'plugins' %}
<form method="post" class="card"><input type="hidden" name="csrf_token" value="{{ csrf_token }}"><p class="muted">Ces réglages contrôlent les fonctions utilisées par Dashle dans tes conversations. Les analyses restent soumises à l’offre Pro ou Prime.</p>{% for key, label, helptext in options %}<label class="row"><span><strong>{{ label }}</strong><br><span class="muted">{{ helptext }}</span></span><input class="check" type="checkbox" name="plugin_{{ key }}" value="1" {% if etat[key] %}checked{% endif %}></label>{% endfor %}<button>Enregistrer les préférences</button></form>
{% endif %}</main></body></html>
"""


STATISTIQUES_PAGE = """
<!doctype html><html lang="fr"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Analyses statistiques — DASHLE</title><style>
:root{font-family:Inter,Segoe UI,sans-serif;color:#18352c;background:#f3f8f6}*{box-sizing:border-box}body{margin:0;padding:28px 16px}.wrap{max-width:850px;margin:auto}a{color:#16765b;text-decoration:none;font-weight:600}h1{font-size:clamp(28px,5vw,40px);margin:30px 0 8px}.intro{color:#627970;line-height:1.55}.card{background:#fff;border:1px solid #dce9e4;border-radius:16px;padding:22px;margin:18px 0;box-shadow:0 10px 28px #173a2b0c}label{display:block;font-weight:600;margin:14px 0 6px}input,textarea{display:block;width:100%;padding:11px;border:1px solid #d5e3dd;border-radius:9px;font:inherit}textarea{min-height:100px;resize:vertical}button,.cta{display:inline-block;padding:11px 16px;border:0;border-radius:9px;background:linear-gradient(110deg,#25bd80,#3b82f6);color:white;font-family:inherit;font-size:14px;font-weight:600;text-decoration:none;cursor:pointer;margin-top:14px}.result{white-space:pre-wrap;overflow-wrap:anywhere;line-height:1.5;background:#f7faf8;padding:14px;border-radius:10px;max-height:55vh;overflow:auto}.error{background:#fff0ed;color:#9b3828;padding:12px;border-radius:9px}.meta{font-size:13px;color:#627970}.notice{background:#e8f5ef;padding:14px;border-radius:10px}
</style></head><body><main class="wrap"><a href="{{ url_for('accueil') }}">← Retour à DASHLE</a><h1>Analyse de données</h1>
<p class="intro">Importe un CSV ou un classeur Excel (8 Mo maximum). DASHLE calcule des statistiques descriptives et, selon ta question, des analyses avec SciPy. Le fichier est traité en mémoire et n’est pas conservé.</p>
{% if erreur %}<p class="error">{{ erreur }}</p>{% endif %}
{% if not plugin_active %}<section class="card"><h2>Mode statistique désactivé</h2><p>Active-le depuis la page Plugins pour analyser tes fichiers.</p><a class="cta" href="{{ url_for('plugins') }}">Gérer les plugins</a></section>
{% elif not autorise %}<section class="card"><h2>Fonction réservée à Dashle Pro et Prime</h2><p>Débloque les analyses statistiques avancées et jusqu’à 25 analyses par jour.</p><a class="cta" href="{{ url_for('tarifs') }}">Voir les offres</a></section>
{% else %}<p class="meta">Offre {{ niveau|capitalize }} · {{ restant }} analyse(s) restante(s) aujourd’hui (limite : 25).</p>
<form class="card" method="post" enctype="multipart/form-data"><input type="hidden" name="csrf_token" value="{{ csrf_token }}"><label for="fichier">Fichier CSV ou Excel</label><input id="fichier" name="fichier" type="file" accept=".csv,.xls,.xlsx,.xlsm" required><label for="question">Que veux-tu analyser ?</label><textarea id="question" name="question" maxlength="1000" required placeholder="Ex. : Compare les ventes selon la région, teste la différence entre les groupes, ou calcule la probabilité que ventes > 100."></textarea><button type="submit">Analyser</button></form>
{% endif %}
{% if metriques %}<section class="card"><h2>Calculs Python</h2><p class="meta">{{ metriques.lignes }} lignes · {{ metriques.colonnes }} colonnes · {{ restant }} analyse(s) restante(s) aujourd’hui</p>{% if metriques.graphique %}<figure class="graphique"><img src="{{ metriques.graphique }}" alt="Histogramme de distribution de la première colonne numérique"><figcaption>Distribution de la première colonne numérique · survole les barres pour voir les effectifs.</figcaption></figure>{% endif %}<pre class="result">{{ calculs }}</pre><h2>Interprétation DASHLE</h2><pre class="result">{{ interpretation }}</pre></section>{% endif %}
</main></body></html>
"""


TARIFS_PAGE = """
<!doctype html><html lang="fr"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Tarifs — DASHLE</title><style>
:root{font-family:Inter,Segoe UI,sans-serif;color:#18352c;background:#f3f8f6}*{box-sizing:border-box}body{margin:0;padding:28px 16px 48px}
header{max-width:1100px;margin:0 auto 28px;display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap}header a{color:#187a60;text-decoration:none;font-weight:600}
h1{text-align:center;font-size:clamp(30px,5vw,44px);margin:18px 0 8px}.intro{text-align:center;color:#657b73;margin:0 auto 10px;max-width:680px}
.indicatif{text-align:center;color:#71837b;font-size:13px;margin:0 auto 28px}.plans{max-width:1100px;margin:auto;display:grid;grid-template-columns:repeat(auto-fit,minmax(250px,1fr));gap:16px}
.plan{background:white;border:1px solid #dce9e4;border-radius:18px;padding:24px;box-shadow:0 10px 28px #173a2b0c;display:flex;flex-direction:column}.plan.featured{border:2px solid #35a982}
.plan h2{margin:4px 0 10px}.price{font-size:27px;font-weight:750;color:#137d61}.price small{font-size:14px;color:#70847c;font-weight:500}.indicatifs-prix{font-size:13px;color:#657b73;margin-top:5px}.features{padding-left:20px;line-height:1.65;flex:1;color:#526a61}
form{margin-top:20px}select{width:100%;padding:10px;border:1px solid #d5e3dd;border-radius:9px;background:#fff;font:inherit}
button,.button{display:block;width:100%;margin-top:9px;padding:11px;border:0;border-radius:10px;font-family:inherit;font-size:14px;font-weight:600;text-align:center;text-decoration:none;cursor:pointer;background:linear-gradient(110deg,#25bd80,#3b82f6);color:white}.button.secondary{background:#e8f4ef;color:#17654f}
.notice,.error{max-width:740px;margin:14px auto;padding:12px 15px;border-radius:10px;background:#e7f6ef;color:#27634e}.error{background:#fff0ed;color:#9b3828}.current{text-align:center;color:#61796f;margin:20px auto;max-width:760px}
.sr-only{position:absolute;left:-10000px}
</style></head><body>
<header><a href="{{ url_for('accueil') }}">← Retour à DASHLE</a>{% if utilisateur %}<span>{{ utilisateur }} · offre {{ niveau|capitalize }} · <a href="{{ url_for('factures') }}">Factures</a></span>{% else %}<a href="{{ url_for('connexion') }}">Connexion</a>{% endif %}</header>
<h1>Tarifs</h1><p class="intro">Les abonnements sont facturés en FCFA (XOF), devise de règlement.</p>
<p class="indicatif">Pour les clients hors zone FCFA : équivalent EUR et USD affiché à titre indicatif, selon un taux mis à jour régulièrement. <strong>Prix indicatif</strong>.</p>
{% if erreur %}<p class="error">{{ erreur }}</p>{% endif %}{% if request.args.get('retour') %}<p class="notice">Le paiement a été transmis. Ton offre sera activée après confirmation du prestataire.</p>{% endif %}
<div class="plans">
<article class="plan"><h2>{{ offre_free.nom }}</h2><div class="price">{{ '{:,}'.format(offre_free.mensuel).replace(',', ' ') }} FCFA <small>/ toujours</small></div><ul class="features">{% for avantage in offre_free.avantages %}<li>{{ avantage }}</li>{% endfor %}</ul><a class="button secondary" href="{{ url_for('accueil') }}">Commencer gratuitement</a></article>
{% for code, nom, mensuel, annuel, avantages in offres %}
<article class="plan {{ 'featured' if code == 'prime' else '' }}"><h2>{{ nom }}</h2>
<div class="price"><span data-month="{{ mensuel }}" data-year="{{ annuel }}">{{ '{:,}'.format(mensuel).replace(',', ' ') }}</span> FCFA <small class="period">/ mois</small></div>
<div class="indicatifs-prix" data-xof="{{ mensuel }}">≈ {{ '%.2f'|format(mensuel / taux_eur) }} € · ≈ {{ '%.2f'|format(mensuel / taux_eur * taux_usd) }} $ — prix indicatif</div>
<ul class="features">{% for avantage in avantages %}<li>{{ avantage }}</li>{% endfor %}</ul>
{% if utilisateur %}
<form method="post" action="{{ url_for('initier_paiement') }}"><input type="hidden" name="csrf_token" value="{{ csrf_token }}"><input type="hidden" name="tier" value="{{ code }}">
<label class="sr-only" for="cadence-{{ code }}">Périodicité</label><select id="cadence-{{ code }}" name="cadence" class="cadence"><option value="monthly">Mensuel</option><option value="annual">Annuel — 2 mois offerts</option></select>
{% if 'paydunya' in moyens_paiement %}<button name="provider" value="paydunya">Mobile Money + carte · PayDunya</button>{% endif %}
{% if 'cinetpay' in moyens_paiement %}<button name="provider" value="cinetpay">CinetPay</button>{% endif %}
{% if 'stripe' in moyens_paiement %}<button class="button secondary" name="provider" value="stripe">Carte bancaire</button>{% endif %}
</form>
{% else %}<a class="button" href="{{ url_for('connexion', next=url_for('tarifs')) }}">Connecte-toi pour choisir</a>{% endif %}
</article>{% endfor %}
</div>
<p class="current">La facturation et le règlement restent en XOF. Les équivalents EUR/USD ne sont jamais le montant débité.</p>
<script>
document.querySelectorAll('.plan').forEach(function(plan){var select=plan.querySelector('.cadence');if(!select)return;
var price=plan.querySelector('.price span'),period=plan.querySelector('.period'),indicatif=plan.querySelector('.indicatifs-prix');
function render(){var annuel=select.value==='annual';var xof=Number(price.dataset[annuel?'year':'month']);price.textContent=xof.toLocaleString('fr-FR');period.textContent=annuel?'/ an':'/ mois';var eur=Number(xof/{{ taux_eur }}),usd=eur*Number({{ taux_usd }});indicatif.textContent='≈ '+eur.toFixed(2)+' € · ≈ '+usd.toFixed(2)+' $ — prix indicatif';}
select.addEventListener('change',render);render();});
</script></body></html>
"""


AUTH_PAGE = """
<!doctype html><html lang="fr"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Dashle — {{ titre }}</title>
<style>
body{font-family:Segoe UI,sans-serif;background:#f5f7f6;margin:0;display:grid;place-items:center;min-height:100vh}
.carte{width:min(420px,92vw);padding:32px 28px;background:#fff;border-radius:16px;box-shadow:0 4px 24px rgba(0,0,0,.09)}
.logo-titre{display:flex;align-items:center;gap:10px;margin-bottom:4px}.logo{height:36px;border-radius:50%}
h1{color:#22C55E;margin:0;font-size:22px}h2{color:#333;margin:0 0 20px;font-size:16px;font-weight:500}
label,input,select,button{display:block;width:100%;box-sizing:border-box}label{margin-top:14px;font-size:14px;color:#444}
input,select{padding:10px 12px;margin-top:5px;border:1px solid #ddd;border-radius:8px;font:inherit;font-size:15px;background:#fff}
input:focus,select:focus{outline:none;border-color:#3B82F6;box-shadow:0 0 0 2px rgba(34,197,94,.16)}
button{margin-top:22px;padding:12px;border:0;border-radius:10px;background:linear-gradient(110deg,#22C55E,#3B82F6);color:#fff;cursor:pointer;font-size:15px;font-weight:600}
.erreur{color:#b00020;font-size:13px;margin-top:8px}.note{font-size:12px;color:#71837b;margin:7px 0 0}
p{font-size:14px;color:#555;margin-top:16px}p a{color:#22C55E;font-weight:600;text-decoration:none}
.visiteur{display:block;text-align:center;margin-top:12px;font-size:13px;color:#71837b}.visiteur a{color:#22C55E}
.phone{display:grid;grid-template-columns:auto minmax(0,1fr);gap:8px;align-items:end}
.phone-prefix{min-width:3.5rem;max-width:5rem;box-sizing:border-box;margin-top:5px;padding:12px 8px;border:1px solid #d9e1dd;border-radius:10px;background:#f5f7f6;text-align:center;font-weight:600;line-height:1.2;white-space:nowrap;overflow:hidden}
.phone-prefix:empty{visibility:hidden}
</style></head><body><main class="carte">
<div class="logo-titre"><img class="logo" src="/static/icons/dashle-logo-header.png" alt="Dashle"><h1>Dashle</h1></div>
<h2>{{ titre }}</h2>
{% if erreur %}<p class="erreur">{{ erreur }}</p>{% endif %}
<form method="post">
<input type="hidden" name="csrf_token" value="{{ csrf_token }}">
<label>E-mail<input name="email" type="email" required maxlength="254" autocomplete="email" value="{{ inscription_email|default('') }}"></label>
{% if afficher_nom %}<label>Nom<input name="nom" type="text" required maxlength="160" autocomplete="name" value="{{ inscription_nom|default('') }}"></label>
<label>Pays
<select name="pays" id="pays" required autocomplete="country">
<option value="">Sélectionner un pays</option>
{% for code, nom_pays, indicatif in pays_profil %}<option value="{{ code }}" {% if inscription_pays|default('') == code %}selected{% endif %}>{{ nom_pays }} (+{{ indicatif }})</option>{% endfor %}
</select></label>
<label>Numéro de téléphone
<div class="phone"><span id="indicatif-prefix" class="phone-prefix" aria-label="Indicatif international"></span><input type="hidden" id="indicatif-envoye" name="indicatif" value="{{ inscription_indicatif|default('') }}"><input id="telephone" name="telephone" type="tel" required autocomplete="tel-national" inputmode="tel" placeholder="Numéro national" value="{{ inscription_telephone|default('') }}"></div>
<p class="note">Le numéro est enregistré avec son indicatif international. Le 0 initial est conservé dans ton profil.</p>
</label>{% endif %}
<label>Mot de passe<input name="password" type="password" required minlength="8" autocomplete="{{ autocomplete }}"></label>
<button type="submit">{{ action }}</button>
</form>
<p>{{ texte_lien }} <a href="{{ url_for(lien) }}">{{ libelle_lien }}</a></p>
<span class="visiteur">Pas encore prêt ? <a href="{{ url_for('accueil') }}">Continuer sans compte →</a></span>
{% if afficher_pays %}<script type="application/json" id="donnees-indicatifs">{{ pays_profil|tojson }}</script>
<script>
const pays=document.getElementById('pays'), indicatif=document.getElementById('indicatif');
const indicatifEnvoye=document.getElementById('indicatif-envoye');
const repliIndicatifs={"BF":"+226"};
let donneesIndicatifs={};
try {
  const donnees=document.getElementById('donnees-indicatifs');
  const lignes=JSON.parse(donnees ? donnees.textContent : "[]");
  for (const ligne of lignes) {
    if (Array.isArray(ligne) && ligne.length >= 3) donneesIndicatifs[String(ligne[0]).toUpperCase()]="+"+String(ligne[2]);
  }
} catch (_erreur) {
  donneesIndicatifs={};
}
if (!Object.keys(donneesIndicatifs).length) donneesIndicatifs=repliIndicatifs;
for (const [code, valeur] of Object.entries(repliIndicatifs)) {
  if (!donneesIndicatifs[code]) donneesIndicatifs[code]=valeur;
}
function syncIndicatif(){
  const valeur=donneesIndicatifs[pays.value]||'';
  document.getElementById('indicatif-prefix').textContent=valeur;
  indicatifEnvoye.value=valeur;
}
pays.addEventListener('change',syncIndicatif);
pays.addEventListener('input',syncIndicatif);
syncIndicatif();
</script>{% endif %}
</main></body></html>
"""


# ---------------------------------------------------------------------------
# Helper : rendu de PAGE avec toutes les variables communes
# ---------------------------------------------------------------------------

def _prenom_accueil(user_id):
    if not user_id:
        return None
    with session_base() as db:
        user = db.get(User, user_id)
        valeur = user.nom if user else None
    if not isinstance(valeur, str):
        return None
    prenom = valeur.strip().split(maxsplit=1)[0] if valeur.strip() else ""
    if (not prenom or len(prenom) > 40
            or not all(caractere.isalpha() or caractere in "-'" for caractere in prenom)):
        return None
    return prenom[:1].upper() + prenom[1:]


def _rendre_page(messages, utilisateur=None, conversations=None, conversation_id=None,
                 preferences=None, prenom=None):
    """Rend le template PAGE en injectant CSS, données JSON et tokens."""
    prefs = preferences or _PREFS_VISITEUR
    est_connecte = utilisateur is not None

    # Résoudre les URLs ici (côté serveur) pour ne pas exposer de logique
    # Flask dans le JS
    url_flux  = url_for("repondre_flux")
    url_image = url_for("repondre_image")

    # Remplacer les placeholders JS par des valeurs JSON sérialisées.
    # La clé CSRF n'est jamais exposée aux visiteurs non connectés via JS ;
    # elle est remplacée par null pour que le JS sache ne pas l'envoyer.

    message_accueil = (
        random.choice(MESSAGES_ACCUEIL_PERSONNALISES).format(prenom=prenom)
        if prenom else random.choice(MESSAGES_ACCUEIL_VISITEUR)
    )
    html = render_template_string(
        PAGE,
        css=_CSS,
        messages=messages,
        message_accueil=message_accueil,
        utilisateur={"email": utilisateur} if utilisateur else None,
        conversations=conversations or [],
        conversation_id=conversation_id or 0,
        preferences=prefs,
        csrf_token=jeton_csrf(),
        est_admin=bool(utilisateur and utilisateur.strip().lower() in emails_owner()),
    )

    # Injection des constantes JS
    html = html.replace("__CSRF_TOKEN__",   json.dumps(jeton_csrf() if est_connecte else None))
    html = html.replace("__PREFS_VOCALES__", json.dumps({
        "voix_nom":      prefs["voix_nom"] or "",
        "voix_vitesse":  float(prefs["voix_vitesse"] or 1.0),
        "voix_tonalite": float(prefs["voix_tonalite"] or 1.0),
        "voix_volume":   float(prefs["voix_volume"] or 1.0),
        "voix_active":   bool(prefs["voix_active"]),
        "lecture_automatique": bool(prefs["lecture_automatique"]),
        "conserver_historique": bool(prefs["conserver_historique"]),
        "theme": prefs["theme"],
    }))
    html = html.replace("__EST_CONNECTE__",  "true" if est_connecte else "false")
    html = html.replace("__URL_FLUX__",      json.dumps(url_flux))
    html = html.replace("__URL_IMAGE__",     json.dumps(url_image))
    html = html.replace("__CONV_ID__",       str(conversation_id or 0))
    return html


# ---------------------------------------------------------------------------
# Routes — chat principal
# ---------------------------------------------------------------------------

VOICE_TRANSCRIPTION_LIMITS = {
    "visitor": int(os.environ.get("DASHLE_VOICE_TRANSCRIPTION_DAILY_LIMIT_VISITOR", "3")),
    "free": int(os.environ.get("DASHLE_VOICE_TRANSCRIPTION_DAILY_LIMIT_FREE", "10")),
    "pro": int(os.environ.get("DASHLE_VOICE_TRANSCRIPTION_DAILY_LIMIT_PRO", "30")),
    "prime": int(os.environ.get("DASHLE_VOICE_TRANSCRIPTION_DAILY_LIMIT_PRIME", "60")),
}
VOICE_TRANSCRIPTION_MAX_BYTES = 5 * 1024 * 1024
VOICE_TRANSCRIPTION_MAX_SECONDS = 20
VOICE_TRANSCRIPTION_MIMES = {"audio/webm", "audio/ogg", "audio/wav", "audio/x-wav", "audio/mpeg", "audio/mp4", "audio/aac"}

def _cle_visiteur_voix():
    forwarded = request.headers.get("X-Forwarded-For", "")
    ip = (forwarded.split(",")[0].strip() if forwarded else request.remote_addr) or "inconnu"
    return hashlib.sha256((str(app.config.get("SECRET_KEY", "dashle")) + "|voice-quota|" + ip).encode("utf-8")).hexdigest()

def _quota_transcription_voix(user_id=None):
    today = datetime.utcnow().date()
    if user_id:
        with session_base() as db:
            user = db.get(User, user_id)
            niveau = niveau_abonnement(user) if user else "free"
            limite = VOICE_TRANSCRIPTION_LIMITS.get(niveau, VOICE_TRANSCRIPTION_LIMITS["free"])
            usage = db.query(VoiceTranscriptionUsage).filter_by(user_id=user_id, usage_date=today).one_or_none()
            return niveau, limite, usage.count if usage else 0
    with session_base() as db:
        usage = db.query(VoiceTranscriptionUsage).filter_by(visitor_key=_cle_visiteur_voix(), usage_date=today).one_or_none()
        return "visitor", VOICE_TRANSCRIPTION_LIMITS["visitor"], usage.count if usage else 0

def _consommer_quota_transcription_voix(user_id=None):
    today = datetime.utcnow().date()
    with session_base() as db:
        if user_id:
            user = db.get(User, user_id)
            niveau = niveau_abonnement(user) if user else "free"
            limite = VOICE_TRANSCRIPTION_LIMITS.get(niveau, VOICE_TRANSCRIPTION_LIMITS["free"])
            usage = db.query(VoiceTranscriptionUsage).filter_by(user_id=user_id, usage_date=today).one_or_none()
            if usage is None:
                usage = VoiceTranscriptionUsage(user_id=user_id, usage_date=today, count=0)
                db.add(usage); db.flush()
        else:
            cle = _cle_visiteur_voix()
            limite = VOICE_TRANSCRIPTION_LIMITS["visitor"]
            usage = db.query(VoiceTranscriptionUsage).filter_by(visitor_key=cle, usage_date=today).one_or_none()
            if usage is None:
                usage = VoiceTranscriptionUsage(visitor_key=cle, usage_date=today, count=0)
                db.add(usage); db.flush()
        if usage.count >= limite:
            return False
        usage.count += 1
        return True

@app.post("/api/transcrire")
def transcrire_audio():
    fichier = request.files.get("audio")
    if not fichier:
        return jsonify({"erreur": "Aucun enregistrement audio reçu."}), 400
    mime = (fichier.mimetype or "").lower().split(";")[0]
    if mime not in VOICE_TRANSCRIPTION_MIMES:
        return jsonify({"erreur": "Format audio non pris en charge."}), 415
    audio = fichier.read(VOICE_TRANSCRIPTION_MAX_BYTES + 1)
    if len(audio) > VOICE_TRANSCRIPTION_MAX_BYTES:
        return jsonify({"erreur": "L'enregistrement est trop volumineux (5 Mo maximum)."}), 413
    try:
        duree_ms = int(request.headers.get("X-Dashle-Audio-Duration-Ms", "0") or 0)
    except (TypeError, ValueError):
        duree_ms = 0
    if duree_ms and duree_ms > VOICE_TRANSCRIPTION_MAX_SECONDS * 1000:
        return jsonify({"erreur": "L'enregistrement est trop long (20 secondes maximum)."}), 413

    user_id = session.get("user_id")
    niveau, limite, utilise = _quota_transcription_voix(user_id)
    if utilise >= limite:
        return jsonify({"erreur": "Le quota de transcription vocale du jour est atteint.", "niveau": niveau}), 429
    if not CLE_API:
        return jsonify({"erreur": "La transcription vocale de secours est momentanément indisponible."}), 503

    payload = {
        "contents": [{"parts": [
            {"text": "Transcris cet audio en français. Retourne uniquement le texte prononcé, sans commentaire ni balise."},
            {"inline_data": {"mime_type": mime, "data": base64.b64encode(audio).decode("ascii")}},
        ]}],
        "generationConfig": {"temperature": 0, "maxOutputTokens": 512},
    }
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{MODELE_GEMINI}:generateContent?key={CLE_API}"
    try:
        rep = requests.post(url, json=payload, timeout=20)
        if rep.status_code >= 400:
            return jsonify({"erreur": "Le service de transcription a refusé l'enregistrement."}), 502
        data = rep.json()
        texte = ""
        for candidat in data.get("candidates", []):
            for part in candidat.get("content", {}).get("parts", []):
                if isinstance(part.get("text"), str):
                    texte += part["text"]
        texte = texte.strip()[:4000]
        if not texte:
            return jsonify({"erreur": "Aucune parole détectée dans l'enregistrement."}), 422
        if not _consommer_quota_transcription_voix(user_id):
            return jsonify({"erreur": "Le quota de transcription vocale du jour est atteint."}), 429
        return jsonify({"texte": texte, "quota": {"niveau": niveau, "utilise": utilise + 1, "limite": limite}})
    except requests.RequestException:
        return jsonify({"erreur": "Le service de transcription est temporairement indisponible."}), 502

@app.route("/")
def accueil():
    user_id = session.get("user_id")

    if user_id:
        preferences = _preferences(user_id)
        if not preferences["conserver_historique"]:
            conversation_id = _conversation_selectionnee(user_id)
            return _rendre_page(
                messages=_messages_conversation(user_id, conversation_id) if conversation_id else [],
                utilisateur=session["user_email"],
                prenom=_prenom_accueil(user_id),
                conversations=_liste_conversations(user_id),
                conversation_id=conversation_id,
                preferences=preferences,
            )
        # Utilisateur connecté : l'ouverture ne crée aucune conversation.
        conversation_id = _conv_courante(user_id)
        return _rendre_page(
            messages=_messages_conversation(user_id, conversation_id),
            utilisateur=session["user_email"],
            prenom=_prenom_accueil(user_id),
            conversations=_liste_conversations(user_id),
            conversation_id=conversation_id,
            preferences=preferences,
        )
    else:
        # Visiteur anonyme — historique temporaire en session
        hist = _historique_visiteur()
        # Proposer un transfert si le visiteur vient de se connecter
        # (géré via session["transfert_propose"] dans /connexion)
        return _rendre_page(
            messages=hist,
            utilisateur=None,
            conversations=[],
            conversation_id=None,
            preferences=_PREFS_VISITEUR,
        )


@app.route("/nouvelle", methods=["POST"])
def nouvelle_conv():
    user_id = session.get("user_id")
    session.pop("projet_temporaire_id", None)
    if not user_id:
        # Visiteur : effacer la conversation temporaire
        session.pop("historique_visiteur", None)
        session.pop("resume_visiteur", None)
        return redirect(url_for("accueil"))
    if not _conserver_historique(user_id):
        session.pop("conversation_id", None)
        session.pop("nouvelle_conversation_en_attente", None)
        return redirect(url_for("accueil"))
    conversation_id = session.get("conversation_id")
    if conversation_id:
        with session_base() as db:
            conv = db.query(Conversation).filter_by(
                id=conversation_id, user_id=user_id
            ).one_or_none()
            if conv is not None and not conv.messages:
                return redirect(url_for("accueil"))
    session.pop("conversation_id", None)
    session["nouvelle_conversation_en_attente"] = True
    return redirect(url_for("accueil"))


@app.route("/conv/<int:i>")
def charger_conv(i):
    user_id = session.get("user_id")
    if not user_id:
        return redirect(url_for("accueil"))
    session.pop("projet_temporaire_id", None)
    with session_base() as db:
        if db.query(Conversation).filter_by(id=i, user_id=user_id).one_or_none():
            session["conversation_id"] = i
    return redirect(url_for("accueil"))


@app.route("/supprimer_conv/<int:i>", methods=["POST"])
def supprimer_conv(i):
    user_id = session.get("user_id")
    if not user_id:
        return redirect(url_for("accueil"))
    with session_base() as db:
        conv = db.query(Conversation).filter_by(id=i, user_id=user_id).one_or_none()
        if conv:
            db.delete(conv)
    if session.get("conversation_id") == i:
        session.pop("conversation_id", None)
    return redirect(url_for("accueil"))


@app.route("/archiver_conv/<int:i>", methods=["POST"])
def archiver_conv(i):
    user_id = session.get("user_id")
    if not user_id:
        return redirect(url_for("accueil"))
    with session_base() as db:
        conv = db.query(Conversation).filter_by(id=i, user_id=user_id).one_or_none()
        if conv is None:
            return jsonify({"erreur": "Conversation introuvable."}), 404
        conv.archivee = True
    if session.get("conversation_id") == i:
        session.pop("conversation_id", None)
    return redirect(url_for("accueil"))


@app.route("/renommer/<int:i>", methods=["POST"])
def renommer_conv(i):
    user_id = session.get("user_id")
    if not user_id:
        return jsonify({"erreur": "Non connecté."}), 401
    titre = request.form.get("titre", "").strip()[:120]
    if not titre:
        return jsonify({"erreur": "Le titre est vide."}), 400
    with session_base() as db:
        conv = db.query(Conversation).filter_by(id=i, user_id=user_id).one_or_none()
        if conv is None:
            return jsonify({"erreur": "Conversation introuvable."}), 404
        conv.title = titre
    return redirect(url_for("accueil"))


@app.route("/rechercher")
def rechercher():
    user_id = session.get("user_id")
    if not user_id:
        return jsonify({"resultats": []})
    terme = request.args.get("q", "").strip().lower()
    with session_base() as db:
        convs = (
            db.query(Conversation)
            .filter(Conversation.user_id == user_id)
            .order_by(Conversation.updated_at.desc())
            .all()
        )
        resultats = []
        for c in convs:
            if not terme or terme in c.title.lower() or any(
                terme in m.texte.lower() for m in c.messages
            ):
                resultats.append({"id": c.id, "titre": c.title})
    return jsonify({"resultats": resultats})


@app.route("/feedback", methods=["POST"])
def feedback():
    user_id = session.get("user_id")
    if not user_id:
        return jsonify({"erreur": "Non connecté."}), 401
    message_id = request.form.get("message_id", type=int)
    valeur = request.form.get("valeur", "").strip().lower()
    if valeur not in {"positif", "negatif"} or not message_id:
        return jsonify({"erreur": "Retour invalide."}), 400
    with session_base() as db:
        msg = db.query(Message).join(Conversation).filter(
            Message.id == message_id, Conversation.user_id == user_id
        ).one_or_none()
        if msg is None or msg.auteur != "bot":
            return jsonify({"erreur": "Réponse introuvable."}), 404
        retour = db.query(MessageFeedback).filter_by(
            user_id=user_id, message_id=message_id
        ).one_or_none()
        if retour is None:
            db.add(MessageFeedback(user_id=user_id, message_id=message_id, valeur=valeur))
        else:
            retour.valeur = valeur
    return jsonify({"ok": True, "valeur": valeur})


@app.route("/regenerer/<int:message_id>", methods=["POST"])
def regenerer(message_id):
    user_id = session.get("user_id")
    if not user_id:
        return jsonify({"erreur": "Non connecté."}), 401
    conserver = _conserver_historique(user_id)
    with session_base() as db:
        msg = db.query(Message).join(Conversation).filter(
            Message.id == message_id, Message.auteur == "bot",
            Conversation.user_id == user_id,
        ).one_or_none()
        if msg is None:
            return jsonify({"erreur": "Réponse introuvable."}), 404
        conv_id = msg.conversation_id
        historique = [
            {"auteur": m.auteur, "texte": m.texte}
            for m in db.query(Message).filter(
                Message.conversation_id == conv_id, Message.id < message_id
            ).order_by(Message.id).all()
        ]
        dernier_user = next(
            (h["texte"] for h in reversed(historique) if h["auteur"] == "user"), ""
        )
    if not dernier_user:
        return jsonify({"erreur": "Aucun message utilisateur à régénérer."}), 400
    resume = _resume_conversation(user_id, conv_id)
    reponse = traiter_message(
        dernier_user, historique, user_id, resume,
        **_arguments_contexte_projet(user_id, conv_id),
    )
    mid = ajouter_message(user_id, conv_id, reponse, "bot") if conserver else None
    return jsonify({"reponse": reponse, "message_id": mid})


@app.route("/partager/<int:i>", methods=["POST"])
def creer_partage(i):
    user_id = session.get("user_id")
    if not user_id:
        return jsonify({"erreur": "Non connecté."}), 401
    with session_base() as db:
        conv = db.query(Conversation).filter_by(id=i, user_id=user_id).one_or_none()
        if conv is None:
            return jsonify({"erreur": "Conversation introuvable."}), 404
        lien = db.query(ShareLink).filter_by(conversation_id=i, actif=True).one_or_none()
        if lien is None:
            lien = ShareLink(conversation_id=i, token=secrets.token_urlsafe(32))
            db.add(lien)
            db.flush()
        return jsonify({"url": url_for("partage", token=lien.token, _external=True)})


@app.route("/partager/<int:i>/desactiver", methods=["POST"])
def desactiver_partage(i):
    user_id = session.get("user_id")
    if not user_id:
        return jsonify({"erreur": "Non connecté."}), 401
    with session_base() as db:
        lien = db.query(ShareLink).join(Conversation).filter(
            ShareLink.conversation_id == i,
            Conversation.user_id == user_id,
            ShareLink.actif.is_(True),
        ).one_or_none()
        if lien is not None:
            lien.actif = False
    return jsonify({"ok": True})


@app.route("/partage/<token>")
def partage(token):
    with session_base() as db:
        lien = db.query(ShareLink).filter_by(token=token, actif=True).one_or_none()
        if lien is None:
            return "Lien de partage invalide ou désactivé.", 404
        conv = db.query(Conversation).filter_by(id=lien.conversation_id).one_or_none()
        if conv is None:
            return "Conversation introuvable.", 404
        messages = [{"auteur": m.auteur, "texte": m.texte} for m in conv.messages]
        titre = conv.title
    return render_template_string(SHARE_PAGE, titre=titre, messages=messages)


# ---------------------------------------------------------------------------
# Routes — paramètres et sécurité
# ---------------------------------------------------------------------------

@app.route("/conditions")
def conditions_utilisation():
    return render_template_string(CONDITIONS_PAGE)


@app.route("/actualites")
def actualites():
    return render_template_string(NEWS_PAGE)


@app.route("/robots.txt")
def robots_txt():
    contenu = """User-agent: *
Allow: /
Disallow: /connexion
Disallow: /inscription
Disallow: /parametres
Disallow: /securite
Disallow: /conv/
Disallow: /rechercher
Disallow: /supprimer_conv/
Disallow: /archiver_conv/
Disallow: /renommer/
Disallow: /feedback
Disallow: /regenerer/
Disallow: /partager/
Disallow: /partage/
Disallow: /nouvelle
Disallow: /repondre
Disallow: /repondre_flux
Disallow: /repondre_image
Disallow: /confirmer_message
Disallow: /health
Disallow: /admin
Disallow: /api/
Disallow: /telecharger-pdf-temps-reel
Disallow: /paiement/
Disallow: /abonnement/
Disallow: /planification
Disallow: /projets
Disallow: /plugins
Disallow: /statistiques
Sitemap: https://dashle.onrender.com/sitemap.xml
"""
    return Response(contenu, mimetype="text/plain")


@app.route("/sitemap.xml")
def sitemap_xml():
    contenu = """<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>https://dashle.onrender.com/</loc></url>
  <url><loc>https://dashle.onrender.com/actualites</loc></url>
  <url><loc>https://dashle.onrender.com/conditions</loc></url>
  <url><loc>https://dashle.onrender.com/temps-reel</loc></url>
  <url><loc>https://dashle.onrender.com/tarifs</loc></url>
</urlset>
"""
    return Response(contenu, mimetype="application/xml")


@app.route("/parametres", methods=["GET", "POST"])
def parametres():
    user_id = session.get("user_id")
    if not user_id:
        return redirect(url_for("connexion"))

    if request.method == "POST":
        pays = request.form.get("pays", "").strip().upper()
        telephone_saisi = request.form.get("telephone", "").strip()
        telephone, telephone_national = _normaliser_telephone(pays, telephone_saisi)
        if pays not in PAYS_CODES:
            return redirect(url_for("parametres", erreur="Sélectionne un pays."))
        if not telephone:
            return redirect(url_for("parametres", erreur="Indique un numéro de téléphone valide pour le pays sélectionné."))
        def num(nom, lo, hi, defaut):
            try:
                return max(lo, min(hi, float(request.form.get(nom, defaut))))
            except (TypeError, ValueError):
                return defaut

        with session_base() as db:
            user = db.get(User, user_id)
            if user is None:
                return redirect(url_for("connexion"))
            user.pays = pays
            user.telephone = telephone
            user.telephone_national = telephone_national
            prefs = db.query(UserPreference).filter_by(user_id=user_id).one_or_none()
            if prefs is None:
                prefs = UserPreference(user_id=user_id)
                db.add(prefs)
            theme = request.form.get("theme")
            prefs.theme = theme if theme in {"clair", "sombre", "systeme"} else "clair"
            prefs.voix_active = request.form.get("voix_active") == "on"
            prefs.lecture_automatique = request.form.get("lecture_automatique") == "on"
            prefs.conserver_historique = request.form.get("conserver_historique") == "on"
            prefs.memoire_active = request.form.get("memoire_active") == "on"
            prefs.voix_nom     = request.form.get("voix_nom", "")[:160]
            prefs.voix_vitesse = num("voix_vitesse", 0.6, 1.4, 1.0)
            prefs.voix_tonalite = num("voix_tonalite", 0.7, 1.3, 1.0)
            prefs.voix_volume  = num("voix_volume", 0.2, 1.0, 1.0)
            consignes = request.form.get("consignes_personnalisees", "").strip()[:2000]
            longueur = request.form.get("longueur_reponse", "standard")
            if longueur not in {"courte", "standard", "detaillee"}:
                longueur = "standard"
            for cle, valeur in (
                ("__dashle_consignes_personnalisees__", consignes),
                ("__dashle_longueur_reponse__", longueur),
            ):
                souvenir = db.query(UserMemory).filter_by(user_id=user_id, cle=cle).one_or_none()
                if souvenir is None:
                    db.add(UserMemory(user_id=user_id, cle=cle, valeur=valeur))
                else:
                    souvenir.valeur = valeur
        return redirect(url_for("parametres"))

    with session_base() as db:
        user = db.get(User, user_id)
        souvenirs = db.query(UserMemory).filter_by(user_id=user_id).all()
    reglages = {souvenir.cle: souvenir.valeur for souvenir in souvenirs}
    cle_consignes = "__dashle_consignes_personnalisees__"
    cle_longueur = "__dashle_longueur_reponse__"
    memoires = [
        {"cle": s.cle, "valeur": s.valeur}
        for s in souvenirs
        if s.cle not in {cle_consignes, cle_longueur}
    ]
    return render_template_string(
        SETTINGS_PAGE,
        utilisateur=session["user_email"],
        pays_profil=PAYS_PROFIL,
        pays_utilisateur=(user.pays if user else ""),
        telephone_utilisateur=(user.telephone_national if user else ""),
        preferences=_preferences(user_id),
        modele_gemini=MODELE_GEMINI,
        build_commit=BUILD_COMMIT,
        build_commit_short=BUILD_COMMIT_SHORT,
        build_date=BUILD_DATE,
        consignes_personnalisees=reglages.get(cle_consignes, ""),
        longueur_reponse=reglages.get(cle_longueur, "standard"),
        memoires=memoires,
        csrf_token=jeton_csrf(),
        erreur=request.args.get("erreur"),
        succes=request.args.get("succes"),
    )


@app.route("/parametres/donnees")
def gestion_donnees():
    user_id = session.get("user_id")
    if not user_id:
        return redirect(url_for("connexion"))
    with session_base() as db:
        souvenirs = db.query(UserMemory).filter_by(user_id=user_id).all()
    memoires = [
        {"cle": item.cle, "valeur": item.valeur}
        for item in souvenirs
        if not item.cle.startswith("__dashle_")
    ]
    return render_template_string(
        DATA_PAGE,
        memoires=memoires,
        csrf_token=jeton_csrf(),
        erreur=request.args.get("erreur"),
        succes=request.args.get("succes"),
    )


@app.route("/parametres/exporter")
def exporter_donnees():
    user_id = session.get("user_id")
    if not user_id:
        return redirect(url_for("connexion"))
    with session_base() as db:
        user = db.query(User).filter_by(id=user_id).one_or_none()
        if user is None:
            return redirect(url_for("connexion"))
        conversations = db.query(Conversation).filter_by(user_id=user_id).all()
        export = {
            "email": user.email,
            "conversations": [
                {
                    "titre": conv.title,
                    "resume": conv.resume,
                    "messages": [
                        {"auteur": msg.auteur, "texte": msg.texte,
                         "date": msg.created_at.isoformat() if msg.created_at else None}
                        for msg in conv.messages
                    ],
                }
                for conv in conversations
            ],
            "memoire": [
                {"cle": item.cle, "valeur": item.valeur}
                for item in db.query(UserMemory).filter_by(user_id=user_id).all()
            ],
        }
    response = Response(
        json.dumps(export, ensure_ascii=False, indent=2),
        mimetype="application/json; charset=utf-8",
    )
    response.headers["Content-Disposition"] = 'attachment; filename="dashle-export.json"'
    return response


@app.route("/parametres/memoire/supprimer/<path:cle>", methods=["POST"])
def supprimer_souvenir(cle):
    user_id = session.get("user_id")
    if not user_id:
        return redirect(url_for("connexion"))
    if cle.startswith("__dashle_"):
        return redirect(url_for("gestion_donnees", erreur="Ce réglage ne peut pas être supprimé ici."))
    with session_base() as db:
        db.query(UserMemory).filter_by(user_id=user_id, cle=cle).delete()
    return redirect(url_for("gestion_donnees", succes="Souvenir supprimé."))


@app.route("/parametres/memoire/effacer", methods=["POST"])
def effacer_memoire():
    user_id = session.get("user_id")
    if not user_id:
        return redirect(url_for("connexion"))
    if request.form.get("confirmation", "").strip().upper() != "EFFACER":
        return redirect(url_for("gestion_donnees", erreur="Confirmation incorrecte."))
    with session_base() as db:
        db.query(UserMemory).filter_by(user_id=user_id).delete()
    return redirect(url_for("gestion_donnees", succes="Mémoire effacée."))


@app.route("/parametres/historique/effacer", methods=["POST"])
def effacer_historique():
    user_id = session.get("user_id")
    if not user_id:
        return redirect(url_for("connexion"))
    if request.form.get("confirmation", "").strip().upper() != "EFFACER":
        return redirect(url_for("gestion_donnees", erreur="Confirmation incorrecte."))
    with session_base() as db:
        conversations = db.query(Conversation).filter_by(user_id=user_id).all()
        conversation_ids = [conversation.id for conversation in conversations]
        if conversation_ids:
            db.query(ShareLink).filter(ShareLink.conversation_id.in_(conversation_ids)).delete(
                synchronize_session=False
            )
        for conversation in conversations:
            db.delete(conversation)
    session.pop("conversation_id", None)
    return redirect(url_for("gestion_donnees", succes="Historique effacé."))


@app.route("/securite")
def securite():
    if not session.get("user_id"):
        return redirect(url_for("connexion"))
    return render_template_string(
        SECURITY_PAGE,
        csrf_token=jeton_csrf(),
        erreur=request.args.get("erreur"),
        succes=request.args.get("succes"),
    )


# ---------------------------------------------------------------------------
# Routes — endpoints IA (visiteur + connecté)
# ---------------------------------------------------------------------------

def _executer_recherche_web(message):
    provider = PROVIDER_REGISTRY.get("web_search")
    if provider is None or not PROVIDER_REGISTRY.available("web_search"):
        raise ProviderUnavailable("aucun fournisseur de navigation Web n'est configuré sur DASHLE.")
    resultats = provider.search(message, timeout_s=35)
    if not resultats:
        raise ProviderError("Aucun résultat Web exploitable n'a été retourné.")
    answer = next((r.answer for r in resultats if r.answer), "").strip()
    sources = []
    vus = set()
    for item in resultats:
        if item.url and item.url not in vus:
            vus.add(item.url)
            sources.append({
                "title": item.title,
                "url": item.url,
                "source": item.source,
                "snippet": item.snippet,
            })
    return answer, sources


@app.route("/repondre", methods=["POST"])
def repondre():
    """Endpoint JSON synchrone, avec génération de fichiers et d'images."""
    user_id = session.get("user_id")
    message = request.form.get("message", "").strip()
    if not message:
        return jsonify({"reponse": ""})
    intention = detect_tool_intent(message)
    if intention.name == "video_generation":
        try:
            if not user_id:
                ensure_owner_token(session)
            request_key = re.sub(r"[^A-Za-z0-9._:-]", "", request.headers.get("X-Dashle-Action-ID", "")).strip()[:128]
            request_key = request_key or g.dashle_observabilite["request_id"]
            job_id = _lancer_job_video(message, user_id, session.get("conversation_id"), request_key)
            return jsonify({"reponse": "Génération vidéo lancée.", "status": "queued", "job_id": job_id, "progress": None}), 202
        except ProviderUnavailable as exc:
            return jsonify({"reponse": str(exc), "status": "provider_unavailable"}), 501
        except ProviderTimeout:
            return jsonify({"reponse": "Le fournisseur vidéo n'a pas répondu à temps.", "status": "timeout"}), 504
        except ProviderError:
            app.logger.warning("tool=video_generation provider=veo status=error type=ProviderError")
            return jsonify({"reponse": "La génération vidéo a échoué.", "status": "error"}), 502
    if intention.name == "web_search":
        try:
            answer, sources = _executer_recherche_web(message)
            return jsonify({"reponse": answer, "status": "web_search", "sources": sources})
        except ProviderUnavailable as exc:
            return jsonify({"reponse": str(exc), "status": "provider_unavailable"}), 501
        except ProviderTimeout:
            return jsonify({"reponse": "La recherche Web a dépassé le délai autorisé.", "status": "timeout"}), 504
        except ProviderError as exc:
            app.logger.warning("tool=web_search provider=gemini status=error type=%s", type(exc).__name__)
            return jsonify({"reponse": "La recherche Web a échoué.", "status": "error"}), 502
    if intention.name == "image_generation" or detecter_demande_image(message):
        bloque, quota = _quota_image_bloque(user_id)
        if bloque:
            return jsonify({"reponse": quota["message"], "quota": quota}), 429
        try:
            conversation_id = session.get("conversation_id")
            historique = _messages_conversation(user_id, conversation_id, limite=MAX_MESSAGES_CONTEXTE) if conversation_id else []
            contexte = "\n".join(str(x.get("texte", "")) for x in historique[-12:])
            image_provider = PROVIDER_REGISTRY.get("image_generation")
            if image_provider is None or not PROVIDER_REGISTRY.available("image_generation"):
                raise ProviderUnavailable("La génération d'image n'est pas configurée sur DASHLE.")
            edited = image_provider.generate(message, contexte, timeout_s=90)
            raw, mime = edited.data, edited.mime_type
            if not _consommer_quota_image(user_id):
                bloque, quota = _quota_image_bloque(user_id)
                return jsonify({"reponse": quota["message"], "quota": quota}), 429
            saved = _enregistrer_element_bibliotheque(user_id, "image", "image-dashle", mime, raw, conversation_id) if user_id else False
            return jsonify({"reponse": "Image générée par DASHLE.", "artifact": {"type": "image", "mime_type": mime, "data": base64.b64encode(raw).decode("ascii"), "saved": saved}})
        except ProviderUnavailable as exc:
            return jsonify({"reponse": str(exc), "status": "provider_unavailable"}), 501
        except Exception as exc:
            app.logger.exception("Échec de génération d'image")
            return jsonify({"reponse": "Je n’ai pas pu générer l’image pour le moment.", "artifact_error": type(exc).__name__}), 502
    if detecter_demande_pdf(message):
        try:
            conversation_id = session.get("conversation_id")
            historique = _messages_conversation(user_id, conversation_id, limite=MAX_MESSAGES_CONTEXTE) if conversation_id else []
            contexte = "\n".join(str(x.get("texte", "")) for x in historique[-12:])
            structure = structurer_document(message, contexte, extraire_contenu_fourni(message))
            raw = rendre_pdf(structure)
            titre = structure["title"] or "dashle-document"
            saved = _enregistrer_element_bibliotheque(user_id, "pdf", titre, "application/pdf", raw, conversation_id) if user_id else False
            return jsonify({"reponse": "Voici le document PDF demandé.", "artifact": {"type": "pdf", "mime_type": "application/pdf", "filename": secure_filename(titre)[:120] + ".pdf", "data": base64.b64encode(raw).decode("ascii"), "saved": saved}})
        except Exception as exc:
            app.logger.exception("Échec de génération de PDF")
            return jsonify({"reponse": "Je n’ai pas pu générer le PDF pour le moment.", "artifact_error": type(exc).__name__}), 502

    if user_id:
        if not _conserver_historique(user_id):
            conversation_id = _conversation_selectionnee(user_id)
            historique = _contexte_chat_temporaire(
                user_id, conversation_id, request.form.get("historique"), message
            )
            resume = _resume_conversation(user_id, conversation_id) if conversation_id else ""
            reponse = traiter_message(
                message, historique, user_id, resume,
                **_arguments_contexte_projet(user_id, conversation_id),
            )
            return jsonify({"reponse": reponse, "message_id": None})
        conversation_id = _conv_courante(user_id)
        historique = _messages_conversation(
            user_id, conversation_id, limite=MAX_MESSAGES_CONTEXTE
        )
        resume = _resume_conversation(user_id, conversation_id)
        ajouter_message(user_id, conversation_id, message, "user")
        reponse = traiter_message(
            message, historique + [{"auteur": "user", "texte": message}], user_id, resume,
            **_arguments_contexte_projet(user_id, conversation_id),
        )
        mid = ajouter_message(user_id, conversation_id, reponse, "bot")
        _actualiser_resume(user_id, conversation_id)
        return jsonify({"reponse": reponse, "message_id": mid})

    historique = _historique_visiteur()
    resume = _resume_visiteur()
    _ajouter_message_visiteur(message, "user")
    reponse = traiter_message(
        message, historique + [{"auteur": "user", "texte": message}], None, resume
    )
    _ajouter_message_visiteur(reponse, "bot")
    return jsonify({"reponse": reponse, "message_id": None})


def _evenement_action(nom_evenement, action_id, action_type, etape, message,
                         resultats=None, erreur=None, statut=None):
    """Construit un événement SSE générique de suivi d'action."""
    payload = {
        "event": nom_evenement,
        "action": {
            "id": action_id,
            "type": action_type,
            "step": etape,
            "message": message,
            "cancelable": False,
        },
    }
    if resultats is not None:
        payload["action"]["result"] = resultats
    if erreur is not None:
        payload["action"]["error"] = erreur
    if statut is not None:
        payload["action"]["status"] = statut
    return "data: " + json.dumps(payload, ensure_ascii=False) + "\n\n"


def _video_owner_token_hash(user_id):
    if user_id is not None:
        return None
    return hash_owner_token(ensure_owner_token(session))


def _lancer_job_video(message, user_id, conversation_id, idempotency_key):
    provider = PROVIDER_REGISTRY.get("video_generation")
    if provider is None or not PROVIDER_REGISTRY.available("video_generation"):
        raise ProviderUnavailable("aucun fournisseur vidéo n'est configuré sur DASHLE.")
    return create_persistent_video_job(
        provider,
        prompt=message,
        user_id=user_id,
        owner_token_hash=_video_owner_token_hash(user_id),
        conversation_id=conversation_id,
        idempotency_key=idempotency_key[:128],
    )


def _demande_action_longue(message):
    """Retourne l'outil choisi par le routeur naturel, avec compatibilité historique."""
    intention = detect_tool_intent(message)
    mapping = {
        "video_generation": "video",
        "web_search": "recherche_web",
        "image_generation": "image",
        "pdf_generation": "pdf",
    }
    if intention.name in mapping:
        return mapping[intention.name]
    if detecter_demande_generation_video(message):
        return "video"
    if detecter_demande_recherche_web(message):
        return "recherche_web"
    if detecter_demande_image(message):
        return "image"
    if detecter_demande_pdf(message):
        return "pdf"
    return None


def _video_owner_hash_for_request(user_id):
    return _video_owner_token_hash(user_id)


@app.route("/api/outils/jobs/actifs")
def liste_jobs_outils():
    user_id = session.get("user_id")
    owner_hash = _video_owner_hash_for_request(user_id)
    return jsonify({"jobs": active_for_owner(user_id=user_id, owner_token_hash=owner_hash)})


@app.route("/api/outils/jobs/<job_id>/result")
def resultat_job_outil(job_id):
    user_id = session.get("user_id")
    owner_hash = _video_owner_hash_for_request(user_id)
    result = read_result(job_id, user_id=user_id, owner_token_hash=owner_hash)
    if result is None:
        return jsonify({"status": "not_found"}), 404
    data, mime_type, filename = result
    response = send_file(io.BytesIO(data), mimetype=mime_type or "video/mp4",
                         as_attachment=False, download_name=filename or "video-dashle.mp4",
                         max_age=3600)
    response.headers["Cache-Control"] = "private, max-age=3600"
    return response


@app.route("/api/outils/jobs/<job_id>/flux")
def flux_job_outil(job_id):
    user_id = session.get("user_id")
    owner_hash = _video_owner_hash_for_request(user_id)
    if snapshot_for_owner(job_id, user_id=user_id, owner_token_hash=owner_hash) is None:
        return jsonify({"status": "not_found"}), 404

    def generate():
        dernier = object()
        while True:
            current = snapshot_for_owner(job_id, user_id=user_id, owner_token_hash=owner_hash)
            if current is None:
                yield "data: " + json.dumps({"event":"action_failed","action":{
                    "id":job_id,"type":"video","step":"expired",
                    "message":"La génération vidéo n'est plus disponible.","status":"expired"
                }}, ensure_ascii=False) + "\n\n"
                return
            snapshot = (current["status"], current["progress"], current["message"], current["error"])
            if snapshot != dernier:
                dernier = snapshot
                status = current["status"]
                if status == "completed":
                    yield "data: " + json.dumps({"event":"action_completed","action":{
                        "id":job_id,"type":"video","step":"termine",
                        "message":"Génération vidéo terminée.",
                        "result":{"artifact":result_metadata(job_id,user_id=user_id,owner_token_hash=owner_hash) or {}}
                    }}, ensure_ascii=False) + "\n\n"
                    return
                if status in {"failed","cancelled","expired"}:
                    messages = {"failed":"La génération vidéo a échoué.",
                                "cancelled":"La génération vidéo a été annulée.",
                                "expired":"La génération vidéo a expiré."}
                    yield "data: " + json.dumps({"event":"action_failed","action":{
                        "id":job_id,"type":"video","step":status,
                        "message":messages[status],"status":status
                    }}, ensure_ascii=False) + "\n\n"
                    return
                yield "data: " + json.dumps({"event":"action_progress","action":{
                    "id":job_id,"type":"video","step":status or "generation",
                    "message":current["message"] or "Génération en cours…",
                    "result":{"progress":current["progress"]}
                }}, ensure_ascii=False) + "\n\n"
            time.sleep(2)

    return Response(stream_with_context(generate()), mimetype="text/event-stream",
                    headers={"Cache-Control":"no-cache, no-store","X-Accel-Buffering":"no"})


@app.route("/repondre_flux", methods=["POST"])
def repondre_flux():
    """Diffuse une réponse SSE. Gère visiteur et utilisateur connecté.

    Architecture deux requêtes pour les visiteurs :
    - Le générateur SSE ne modifie JAMAIS la session Flask (impossible en streaming).
    - Pour les visiteurs, la réponse complète est incluse dans le payload
      {termine: true, reponse: "..."} afin que le JS puisse la transmettre
      à /confirmer_message dans une requête séparée qui elle peut écrire la session.
    - Pour les utilisateurs connectés : sauvegarde BDD inchangée dans le générateur.
    """
    user_id = session.get("user_id")
    historique_recu = None

    if request.is_json:
        donnees = request.get_json(silent=True) or {}
        message = str(donnees.get("message", "")).strip()
        historique_recu = donnees.get("historique")
    else:
        message = request.form.get("message", "").strip()
        historique_recu = request.form.get("historique")

    if not message:
        return jsonify({"erreur": "Aucun message reçu."}), 400

    # --- Collecte du contexte selon le mode ---
    if user_id:
        conserver = _conserver_historique(user_id)
        if conserver:
            conversation_id = _conversation_pour_message(user_id)
            historique = _messages_conversation(
                user_id, conversation_id, limite=MAX_MESSAGES_CONTEXTE
            )
            resume = _resume_conversation(user_id, conversation_id)
            ajouter_message(user_id, conversation_id, message, "user")
            contexte_historique = historique + [{"auteur": "user", "texte": message}]
        else:
            conversation_id = _conversation_selectionnee(user_id)
            historique = _contexte_chat_temporaire(
                user_id, conversation_id, historique_recu, message
            )
            resume = _resume_conversation(user_id, conversation_id) if conversation_id else ""
            contexte_historique = historique
    else:
        conserver = False
        historique = list(_historique_visiteur())
        resume = _resume_visiteur()
        _ajouter_message_visiteur(message, "user")
        contexte_historique = historique + [{"auteur": "user", "texte": message}]
        conversation_id = None

    contexte_projet = (
        _arguments_contexte_projet(user_id, conversation_id) if user_id else {}
    )
    if not user_id:
        ensure_owner_token(session)
    action_request_id = re.sub(r"[^A-Za-z0-9._:-]", "", request.headers.get("X-Dashle-Action-ID", "")).strip()[:128]
    action_request_id = action_request_id or g.dashle_observabilite["request_id"]
    observabilite = getattr(g, "dashle_observabilite", None)
    debut_sse = perf_counter()
    ttfb_at = None
    resultat_sse = "success"

    @stream_with_context
    def generer():
        nonlocal resultat_sse
        morceaux = []
        action_id = action_request_id
        action_type = _demande_action_longue(message)
        try:
            if action_type:
                yield _evenement_action(
                    "action_started", action_id, action_type, "preparation",
                    "Analyse de votre demande…" if action_type in {"video", "recherche_web"} else (
                        "Préparation de la génération…" if action_type == "image" else "Préparation du document…"
                    ),
                )

                if action_type == "video":
                    try:
                        job_id = _lancer_job_video(message, user_id, conversation_id, action_id)
                    except ProviderUnavailable as exc:
                        message_indisponible = str(exc)
                        yield _evenement_action("action_failed", action_id, "video", "provider_unavailable",
                                                 message_indisponible, erreur=message_indisponible,
                                                 statut="provider_unavailable")
                        return
                    yield _evenement_action(
                        "action_progress", action_id, "video", "en_attente",
                        "Génération lancée. Suivi en arrière-plan…",
                        resultats={"job_id": job_id}
                    )
                    yield "data: " + json.dumps(
                        {"termine": True, "action_id": action_id, "tool_job_id": job_id},
                        ensure_ascii=False
                    ) + "\n\n"
                    return

                if action_type == "recherche_web":
                    try:
                        debut_outil = perf_counter()
                        answer, sources = _executer_recherche_web(message)
                        app.logger.info(
                            "tool=web_search provider=gemini duration_ms=%s status=success sources=%s",
                            round((perf_counter() - debut_outil) * 1000, 1), len(sources),
                        )
                        message_action = answer or "Recherche Web effectuée."
                        message_id_action = None
                        if user_id and conserver and conversation_id:
                            message_id_action = ajouter_message(user_id, conversation_id, message_action, "bot")
                        yield _evenement_action(
                            "action_completed", action_id, "recherche_web", "termine",
                            message_action,
                            resultats={"web_search": {"answer": message_action, "sources": sources},
                                       "message_id": message_id_action, "conversation_id": conversation_id}
                        )
                        payload_fin = {"termine": True, "message_id": message_id_action, "action_id": action_id}
                        if not user_id:
                            payload_fin["reponse"] = message_action
                        yield "data: " + json.dumps(payload_fin, ensure_ascii=False) + "\n\n"
                        return
                    except ProviderUnavailable as exc:
                        yield _evenement_action("action_failed", action_id, "recherche_web",
                                                 "provider_unavailable", str(exc), erreur=str(exc),
                                                 statut="provider_unavailable")
                        return
                    except ProviderTimeout:
                        yield _evenement_action("action_failed", action_id, "recherche_web", "erreur",
                                                 "La recherche Web a dépassé le délai autorisé.",
                                                 erreur="ProviderTimeout", statut="timeout")
                        return
                    except ProviderError:
                        app.logger.warning("tool=web_search provider=gemini status=error")
                        yield _evenement_action("action_failed", action_id, "recherche_web", "erreur",
                                                 "La recherche Web a échoué.",
                                                 erreur="ProviderError", statut="error")
                        return

                conversation_id_action = conversation_id
                historique_action = (
                    _messages_conversation(user_id, conversation_id_action, limite=MAX_MESSAGES_CONTEXTE)
                    if user_id and conversation_id_action else []
                )
                contexte_action = "\n".join(str(x.get("texte", "")) for x in historique_action[-12:])

                if action_type == "image":
                    bloque, quota = _quota_image_bloque(user_id)
                    if bloque:
                        yield _evenement_action(
                            "action_failed", action_id, "image", "quota", quota["message"],
                            resultats={"quota": quota}, erreur=quota["message"]
                        )
                        return
                    yield _evenement_action(
                        "action_progress", action_id, "image", "generation",
                        "Ton idée prend forme…"
                    )
                    yield _evenement_action(
                        "action_progress", action_id, "image", "generation",
                        "Création d'une première ébauche…"
                    )
                    debut_image = perf_counter()
                    try:
                        image_provider = PROVIDER_REGISTRY.get("image_generation")
                        if image_provider is None or not PROVIDER_REGISTRY.available("image_generation"):
                            raise ProviderUnavailable("La génération d'image n'est pas configurée sur DASHLE.")
                        generated = image_provider.generate(message, contexte_action, timeout_s=90)
                        raw, mime = generated.data, generated.mime_type
                    except ProviderUnavailable as exc:
                        _journaliser_image(observabilite, debut_image, "error")
                        yield _evenement_action(
                            "action_failed", action_id, "image", "provider_unavailable",
                            str(exc), erreur=str(exc), statut="provider_unavailable"
                        )
                        return
                    except Exception:
                        _journaliser_image(observabilite, debut_image, "error")
                        raise
                    _journaliser_image(observabilite, debut_image, "success")
                    yield _evenement_action(
                        "action_progress", action_id, "image", "finalisation",
                        "Finitions…"
                    )
                    if not _consommer_quota_image(user_id):
                        bloque, quota = _quota_image_bloque(user_id)
                        yield _evenement_action(
                            "action_failed", action_id, "image", "quota", quota["message"],
                            resultats={"quota": quota}, erreur=quota["message"]
                        )
                        return
                    saved = _enregistrer_element_bibliotheque(
                        user_id, "image", "image-dashle", mime, raw, conversation_id_action
                    ) if user_id else False
                    artifact = {
                        "type": "image", "mime_type": mime, "filename": "image-dashle.png",
                        "data": base64.b64encode(raw).decode("ascii"), "saved": saved,
                    }
                    message_action = "Image générée par DASHLE."
                else:
                    yield _evenement_action(
                        "action_progress", action_id, "pdf", "contenu",
                        "Génération du contenu…"
                    )
                    structure = structurer_document(
                        message, contexte_action, extraire_contenu_fourni(message)
                    )
                    yield _evenement_action(
                        "action_progress", action_id, "pdf", "mise_en_page",
                        "Mise en page du PDF…"
                    )
                    raw = rendre_pdf(structure)
                    yield _evenement_action(
                        "action_progress", action_id, "pdf", "generation",
                        "Génération du PDF…"
                    )
                    titre = secure_filename(structure["title"])[:120] or "dashle-document"
                    yield _evenement_action(
                        "action_progress", action_id, "pdf", "finalisation",
                        "Finalisation du document…"
                    )
                    saved = _enregistrer_element_bibliotheque(
                        user_id, "pdf", structure["title"], "application/pdf",
                        raw, conversation_id_action
                    ) if user_id else False
                    artifact = {
                        "type": "pdf", "mime_type": "application/pdf", "filename": titre + ".pdf",
                        "data": base64.b64encode(raw).decode("ascii"), "saved": saved,
                    }
                    message_action = "Voici le document PDF demandé."

                message_id_action = None
                if user_id and conserver and conversation_id:
                    message_id_action = ajouter_message(user_id, conversation_id, message_action, "bot")
                yield _evenement_action(
                    "action_completed", action_id, action_type, "termine",
                    message_action,
                    resultats={
                        "artifact": artifact, "message_id": message_id_action,
                        "conversation_id": conversation_id_action,
                    },
                )
                payload_fin = {"termine": True, "message_id": message_id_action, "action_id": action_id}
                if not user_id:
                    payload_fin["reponse"] = message_action
                yield "data: " + json.dumps(payload_fin, ensure_ascii=False) + "\n\n"
                return

            for morceau in streamer_message(
                message, contexte_historique, user_id, resume, **contexte_projet
            ):
                if not morceau:
                    continue
                morceau = str(morceau)
                if morceau.startswith("__DASHLE_CONNECTOR_CONFIRMATION__"):
                    try:
                        confirmation = json.loads(morceau.split("__DASHLE_CONNECTOR_CONFIRMATION__", 1)[1])
                        yield "data: " + json.dumps(
                            {"connector_confirmation": confirmation}, ensure_ascii=False
                        ) + "\n\n"
                    except (ValueError, TypeError):
                        yield "data: " + json.dumps(
                            {"morceau": "Je dois obtenir ta confirmation avant d'exécuter cette action externe."},
                            ensure_ascii=False
                        ) + "\n\n"
                    yield "data: " + json.dumps({"termine": True}, ensure_ascii=False) + "\n\n"
                    return
                morceaux.append(morceau)
                yield "data: " + json.dumps(
                    {"morceau": morceau}, ensure_ascii=False
                ) + "\n\n"

            reponse_complete = "".join(morceaux).strip()

            if user_id and conserver:
                # Utilisateur connecté : sauvegarde BDD directement dans le générateur.
                if reponse_complete:
                    mid = ajouter_message(user_id, conversation_id, reponse_complete, "bot")
                    _actualiser_resume_en_arriere_plan(user_id, conversation_id)
                else:
                    mid = None
                yield "data: " + json.dumps(
                    {"termine": True, "message_id": mid}, ensure_ascii=False
                ) + "\n\n"
            elif user_id:
                yield "data: " + json.dumps(
                    {"termine": True, "message_id": None}, ensure_ascii=False
                ) + "\n\n"
            else:
                # Visiteur : on NE MODIFIE PAS la session ici (headers déjà envoyés).
                # On renvoie la réponse complète au client ; c'est le JS qui appellera
                # /confirmer_message pour écrire proprement en session.
                yield "data: " + json.dumps(
                    {"termine": True, "message_id": None, "reponse": reponse_complete},
                    ensure_ascii=False,
                ) + "\n\n"

        except GeneratorExit:
            # Navigateur a fermé la connexion (interruption utilisateur).
            # On ne sauvegarde PAS une réponse incomplète.
            resultat_sse = "cancelled"
            return
        except Exception as err:
            resultat_sse = "error"
            app.logger.error(
                "dashle.sse_error request_id=%s endpoint=repondre_flux error_type=%s",
                observabilite["request_id"] if observabilite else "unknown",
                type(err).__name__,
            )
            if action_type:
                yield _evenement_action(
                    "action_failed", action_id, action_type, "echec",
                    "Échec de la génération",
                    erreur="La génération a échoué. Réessaie.",
                )
                yield "data: " + json.dumps(
                    {"termine": True, "message_id": None, "action_id": action_id},
                    ensure_ascii=False,
                ) + "\n\n"
            else:
                yield "data: " + json.dumps(
                    {"erreur": "Erreur pendant la génération. Réessaie."},
                    ensure_ascii=False,
                ) + "\n\n"

    flux = generer()

    def flux_observe():
        nonlocal ttfb_at
        for donnees in flux:
            if ttfb_at is None:
                ttfb_at = perf_counter()
            yield donnees
        _journaliser_sse(observabilite, debut_sse, ttfb_at, resultat_sse)

    return Response(
        flux_observe(),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "Pragma":        "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@app.route("/confirmer_message", methods=["POST"])
def confirmer_message():
    """Sauvegarde la réponse bot dans la session visiteur.

    Appelé par le JS uniquement pour les visiteurs anonymes, après réception
    de l'événement {termine: true} dans le flux SSE. Cette requête séparée
    garantit que le cookie de session est correctement écrit (impossible dans
    un générateur SSE en streaming).

    Validations :
    - Refusé si l'utilisateur est connecté (la BDD gère tout pour eux).
    - La réponse doit être une chaîne non vide, limitée à 16 000 caractères.
    - Le message utilisateur correspondant doit être présent dans la session
      (on vérifie que _historique_visiteur() contient au moins un message user)
      pour éviter d'injecter une réponse orpheline.
    """
    # Visiteurs uniquement — les connectés n'ont pas à appeler cet endpoint
    if session.get("user_id"):
        return jsonify({"erreur": "Endpoint réservé aux visiteurs."}), 403

    donnees = request.get_json(silent=True) or {}
    reponse_txt = donnees.get("reponse", "")

    if not isinstance(reponse_txt, str):
        return jsonify({"erreur": "Format invalide."}), 400
    reponse_txt = reponse_txt.strip()[:16_000]
    if not reponse_txt:
        return jsonify({"erreur": "Réponse vide."}), 400

    # Vérifier qu'il y a bien un échange en cours (au moins 1 message user)
    hist = _historique_visiteur()
    if not any(m.get("auteur") == "user" for m in hist):
        return jsonify({"erreur": "Aucun message utilisateur en session."}), 400

    # Éviter les doublons : si le dernier message est déjà une réponse bot
    # identique, on ne l'ajoute pas une seconde fois.
    if hist and hist[-1].get("auteur") == "bot" and hist[-1].get("texte") == reponse_txt:
        return jsonify({"ok": True, "note": "Déjà enregistré."})

    _ajouter_message_visiteur(reponse_txt, "bot")

    # Résumé visiteur : déclenché après 20 demandes utilisateur, puis toutes les 10.
    # On compte uniquement les messages auteur=="user" pour une granularité exacte.
    n_user = sum(1 for m in _historique_visiteur() if m.get("auteur") == "user")
    if n_user >= 20 and (n_user - 20) % 10 == 0:
        nouveau = resumer_conversation(_historique_visiteur(), _resume_visiteur())
        if nouveau:
            _maj_resume_visiteur(nouveau)

    session.modified = True
    return jsonify({"ok": True})


@app.route("/generer-image", methods=["POST"])
def generer_image_endpoint():
    user_id = session.get("user_id")
    donnees = request.get_json(silent=True) or request.form
    prompt = str(donnees.get("prompt", "")).strip()[:24000]
    if not prompt:
        return jsonify({"erreur": "Décris l’image à générer."}), 400
    bloque, quota = _quota_image_bloque(user_id)
    if bloque:
        return jsonify({"ok": False, "erreur": quota["message"], "quota": quota}), 429
    try:
        conversation_id = session.get("conversation_id")
        historique = _messages_conversation(user_id, conversation_id, limite=MAX_MESSAGES_CONTEXTE) if conversation_id else []
        contexte = "\n".join(str(x.get("texte", "")) for x in historique[-12:])
        raw, mime = generer_image(prompt, contexte)
        if not _consommer_quota_image(user_id):
            bloque, quota = _quota_image_bloque(user_id)
            return jsonify({"ok": False, "erreur": quota["message"], "quota": quota}), 429
        saved = _enregistrer_element_bibliotheque(user_id, "image", "image-dashle", mime, raw, conversation_id)
        return jsonify({"ok": True, "mime_type": mime, "filename": "image-dashle.png", "data": base64.b64encode(raw).decode("ascii"), "saved": saved})
    except Exception as exc:
        app.logger.exception("Échec endpoint génération image")
        return jsonify({"ok": False, "erreur": "La génération d’image a échoué.", "code": type(exc).__name__}), 502


@app.route("/generer-pdf", methods=["POST"])
def generer_pdf_endpoint():
    user_id = session.get("user_id")
    donnees = request.get_json(silent=True) or request.form
    demande = str(donnees.get("demande", "")).strip()[:24000]
    if not demande:
        return jsonify({"erreur": "Décris le document à produire."}), 400
    try:
        conversation_id = session.get("conversation_id")
        historique = _messages_conversation(user_id, conversation_id, limite=MAX_MESSAGES_CONTEXTE) if conversation_id else []
        contexte = "\n".join(str(x.get("texte", "")) for x in historique[-12:])
        structure = structurer_document(demande, contexte, extraire_contenu_fourni(demande))
        image_bytes = None
        if str(donnees.get("illustration", "")).lower() in {"1", "true", "oui"}:
            image_bytes, _ = generer_image(demande, contexte)
        raw = rendre_pdf(structure, image_bytes)
        titre = secure_filename(structure["title"])[:120] or "dashle-document"
        saved = _enregistrer_element_bibliotheque(user_id, "pdf", structure["title"], "application/pdf", raw, conversation_id) if user_id else False
        return jsonify({"ok": True, "mime_type": "application/pdf", "filename": titre + ".pdf", "data": base64.b64encode(raw).decode("ascii"), "saved": saved})
    except Exception as exc:
        app.logger.exception("Échec endpoint génération PDF")
        return jsonify({"ok": False, "erreur": "La génération du PDF a échoué.", "code": type(exc).__name__}), 502


@app.route("/repondre_image", methods=["POST"])
def repondre_image():
    user_id = session.get("user_id")

    if user_id:
        conserver = _conserver_historique(user_id)
        if conserver:
            conversation_id = _conversation_pour_message(user_id)
            historique = _messages_conversation(
                user_id, conversation_id, limite=MAX_MESSAGES_CONTEXTE
            )
            resume = _resume_conversation(user_id, conversation_id)
        else:
            conversation_id = _conversation_selectionnee(user_id)
            historique = _historique_recu_temporaire(request.form.get("historique"))
            if not historique and conversation_id:
                historique = _messages_conversation(
                    user_id, conversation_id, limite=MAX_MESSAGES_CONTEXTE
                )
            resume = _resume_conversation(user_id, conversation_id) if conversation_id else ""
    else:
        conserver = False
        historique = list(_historique_visiteur())
        resume = _resume_visiteur()
        conversation_id = None

    message = request.form.get("message", "").strip()
    fichier = request.files.get("image")
    if not fichier:
        return jsonify({"reponse": "Aucune image reçue."})

    image_bytes = fichier.read(IMAGE_UPLOAD_MAX_BYTES + 1)
    if len(image_bytes) > IMAGE_UPLOAD_MAX_BYTES:
        return jsonify({
            "reponse": "L’image est trop volumineuse. La limite avant compression est de 10 Mo. Réduis-la puis réessaie.",
            "code": "image_trop_volumineuse",
            "retryable": True,
        }), 413
    if not image_bytes:
        return jsonify({"reponse": "L'image reçue est vide.", "retryable": True}), 400

    mime_type = detecter_type_media(image_bytes)
    if not mime_type:
        return jsonify({
            "reponse": "Je n’ai pas pu reconnaître cette image. Choisis un JPEG, PNG, GIF, BMP ou WebP valide.",
            "code": "image_invalide",
            "retryable": True,
        }), 400

    if mime_type.startswith("image/") and detecter_demande_modification_image(message):
        provider = PROVIDER_REGISTRY.get("image_editing")
        if provider is None or not PROVIDER_REGISTRY.available("image_editing"):
            return jsonify({
                "reponse": "L'édition d'image n'est pas encore configurée sur DASHLE.",
                "status": "provider_unavailable",
            }), 501
        if _quota_image_bloque(user_id)[0]:
            bloque, quota = _quota_image_bloque(user_id)
            return jsonify({"reponse": quota["message"], "quota": quota}), 429
        try:
            edited = provider.edit(image_bytes, mime_type, message, timeout_s=90)
            if not _consommer_quota_image(user_id):
                bloque, quota = _quota_image_bloque(user_id)
                return jsonify({"reponse": quota["message"], "quota": quota}), 429
            saved = _enregistrer_element_bibliotheque(
                user_id, "image", "image-dashle-modifiee", edited.mime_type,
                edited.data, conversation_id
            ) if user_id else False
            return jsonify({
                "reponse": "Image modifiée par DASHLE.",
                "status": "success",
                "artifact": {
                    "type": "image", "mime_type": edited.mime_type,
                    "filename": edited.filename,
                    "data": base64.b64encode(edited.data).decode("ascii"),
                    "saved": saved,
                },
            })
        except ProviderUnavailable as exc:
            return jsonify({"reponse": str(exc), "status": "provider_unavailable"}), 501
        except ProviderTimeout:
            return jsonify({"reponse": "L'édition d'image a dépassé le délai autorisé.", "status": "timeout"}), 504
        except ProviderError:
            app.logger.warning("tool=image_editing provider=gemini status=error")
            return jsonify({"reponse": "Je n'ai pas pu modifier cette image pour le moment.", "status": "error"}), 502

    if mime_type.startswith("image/") and PIL_DISPONIBLE:
        try:
            with Image.open(io.BytesIO(image_bytes)) as img:
                img.verify()
        except Exception:
            return jsonify({
                "reponse": "Je n’ai pas pu lire cette image. Vérifie le fichier puis réessaie.",
                "code": "image_invalide",
                "retryable": True,
            }), 400

    bloque, quota = _quota_image_bloque(user_id) if mime_type.startswith("image/") else (False, None)
    if bloque:
        return jsonify({"reponse": quota["message"], "quota": quota}), 429

    image_b64 = base64.b64encode(image_bytes).decode("utf-8")
    type_media = "Vidéo" if mime_type.startswith("video/") else "Image"
    texte_msg = message or f"[{type_media} envoyée]"

    apercu_persistant = _miniature_image_data_uri(image_bytes) if mime_type.startswith("image/") else ""
    if user_id and conserver:
        ajouter_message(
            user_id, conversation_id, texte_msg, "user",
            image_preview=apercu_persistant,
        )
    elif not user_id:
        _ajouter_message_visiteur(texte_msg, "user")

    contexte_projet = (
        _arguments_contexte_projet(user_id, conversation_id) if user_id else {}
    )
    try:
        reponse = traiter_message_image(
            message, image_b64, mime_type, historique, resume,
            user_id=user_id, **contexte_projet,
        )
    except requests.RequestException:
        app.logger.warning("Échec réseau lors de l’analyse d’image")
        return jsonify({
            "reponse": "Je n’ai pas pu envoyer l’image. Réessaie.",
            "code": "image_reseau",
            "retryable": True,
        }), 502
    except Exception as exc:
        app.logger.warning("Échec analyse image (%s)", type(exc).__name__)
        return jsonify({
            "reponse": "Je n’ai pas pu analyser l’image. Réessaie.",
            "code": "image_analyse",
            "retryable": True,
        }), 502

    if mime_type.startswith("image/") and not _consommer_quota_image(user_id):
        bloque, quota = _quota_image_bloque(user_id)
        return jsonify({"reponse": quota["message"], "quota": quota}), 429

    if user_id and conserver:
        mid = ajouter_message(user_id, conversation_id, reponse, "bot")
        if texte_msg == "[Image envoyée]":
            titre_image = _titre_automatique(reponse.splitlines()[0].split(". ", 1)[0])
            if titre_image:
                with session_base() as db:
                    conversation = db.query(Conversation).filter_by(id=conversation_id, user_id=user_id).one_or_none()
                    if conversation and conversation.title == "Nouvelle conversation":
                        conversation.title = titre_image
        _actualiser_resume(user_id, conversation_id)
    elif user_id:
        mid = None
    else:
        _ajouter_message_visiteur(reponse, "bot")
        mid = None

    return jsonify({"reponse": reponse, "message_id": mid})


@app.errorhandler(413)
def fichier_trop_volumineux(_erreur):
    return jsonify({
        "reponse": "L’image est trop volumineuse. La limite avant compression est de 10 Mo. Réduis-la puis réessaie.",
        "code": "image_trop_volumineuse",
        "retryable": True,
    }), 413


# ---------------------------------------------------------------------------
# Routes — comptes utilisateurs
# ---------------------------------------------------------------------------

@app.route("/temps-reel")
def temps_reel():
    return render_template_string(
        TEMPS_REEL_PAGE,
        ville_defaut=os.environ.get("DASHLE_METEO_VILLE", ""),
        csrf_token=jeton_csrf(),
    )


@app.route("/api/temps-reel")
def api_temps_reel():
    ville = request.args.get("ville", "").strip()[:80]
    meteo = meteo_du_jour(ville) if ville else None
    return jsonify({
        "date_utc": datetime.now(timezone.utc).isoformat(),
        "ville": ville,
        "meteo": meteo,
        "actualites": actualites_recentes(8),
    })


LIBRARY_PLAN_LIMITS = {
    "free": 25 * 1024 * 1024,
    "pro": 500 * 1024 * 1024,
    "prime": 2 * 1024 * 1024 * 1024,
}
MAX_LIBRARY_ITEM_BYTES = 8 * 1024 * 1024


def _enregistrer_element_bibliotheque(user_id, type_element, titre, mime_type, contenu,
                                      conversation_id=None):
    """Persist a generated artifact when the signed-in user's quota allows it."""
    if not user_id or type_element not in {"pdf", "graphique", "analyse", "image"}:
        return False
    if isinstance(contenu, str):
        contenu = contenu.encode("utf-8")
    if not contenu or len(contenu) > MAX_LIBRARY_ITEM_BYTES:
        return False
    try:
        with session_base() as db:
            user = db.query(User).filter_by(id=user_id).with_for_update().one_or_none()
            if user is None:
                return False
            niveau = niveau_abonnement(user)
            limite = LIBRARY_PLAN_LIMITS.get(niveau, LIBRARY_PLAN_LIMITS["free"])
            utilise = int(db.query(func.coalesce(func.sum(LibraryItem.size_bytes), 0))
                          .filter_by(user_id=user_id).scalar() or 0)
            if utilise + len(contenu) > limite:
                return False
            if conversation_id and not db.query(Conversation.id).filter_by(
                    id=conversation_id, user_id=user_id).first():
                conversation_id = None
            db.add(LibraryItem(
                user_id=user_id, type=type_element, title=(titre or "Sans titre")[:200],
                mime_type=mime_type[:100], content=contenu, size_bytes=len(contenu),
                conversation_id=conversation_id,
            ))
        return True
    except Exception:
        app.logger.exception("Échec d'enregistrement d'un élément de bibliothèque")
        return False


@app.route("/bibliotheque")
def bibliotheque():
    user_id = session.get("user_id")
    if not user_id:
        return redirect(url_for("connexion"))
    filtre = request.args.get("type", "")
    recherche = request.args.get("q", "").strip()[:100]
    with session_base() as db:
        user = db.get(User, user_id)
        if user is None:
            return redirect(url_for("connexion"))
        requete = db.query(LibraryItem).filter_by(user_id=user_id)
        if filtre in {"pdf", "graphique", "analyse", "image"}:
            requete = requete.filter_by(type=filtre)
        if recherche:
            requete = requete.filter(LibraryItem.title.ilike(f"%{recherche}%"))
        elements = requete.order_by(LibraryItem.created_at.desc(), LibraryItem.id.desc()).limit(200).all()
        utilise = int(db.query(func.coalesce(func.sum(LibraryItem.size_bytes), 0))
                      .filter_by(user_id=user_id).scalar() or 0)
        limite = LIBRARY_PLAN_LIMITS.get(niveau_abonnement(user), LIBRARY_PLAN_LIMITS["free"])
        vues = [{"id": item.id, "type": item.type, "title": item.title,
                 "mime_type": item.mime_type, "size_bytes": item.size_bytes,
                 "created_at": item.created_at} for item in elements]
    return render_template(
        "bibliotheque.html", elements=vues, filtre=filtre, recherche=recherche,
        utilise=utilise, limite=limite, erreur=request.args.get("erreur"),
        csrf_token=jeton_csrf(),
    )


@app.route("/bibliotheque/<int:item_id>/telecharger")
def telecharger_element_bibliotheque(item_id):
    user_id = session.get("user_id")
    with session_base() as db:
        item = db.query(LibraryItem).filter_by(id=item_id, user_id=user_id).one_or_none()
        if item is None:
            return "Élément introuvable.", 404
        contenu, mime_type, titre, type_element = item.content, item.mime_type, item.title, item.type
    extension = {"application/pdf": ".pdf", "image/svg+xml": ".svg", "image/png": ".png",
                 "text/plain": ".txt", "text/csv": ".csv"}.get(mime_type, ".bin")
    nom = secure_filename(titre) or type_element
    return send_file(io.BytesIO(contenu), mimetype=mime_type, as_attachment=True,
                     download_name=nom[:150] + extension)


@app.route("/bibliotheque/<int:item_id>/supprimer", methods=["POST"])
def supprimer_element_bibliotheque(item_id):
    user_id = session.get("user_id")
    with session_base() as db:
        item = db.query(LibraryItem).filter_by(id=item_id, user_id=user_id).one_or_none()
        if item is None:
            return "Élément introuvable.", 404
        db.delete(item)
    return redirect(url_for("bibliotheque"))


@app.route("/telecharger-pdf-temps-reel", methods=["POST"])
def telecharger_pdf_temps_reel():
    """Generate an on-demand PDF from available weather and news data."""
    from reportlab.lib import colors
    from reportlab.lib.enums import TA_LEFT
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Table, TableStyle

    fuseau_demande = request.form.get("fuseau", "UTC").strip()[:80] or "UTC"
    try:
        zone = ZoneInfo(fuseau_demande)
    except (ZoneInfoNotFoundError, ValueError):
        fuseau_demande = "UTC"
        zone = timezone.utc
    maintenant = datetime.now(timezone.utc).astimezone(zone)
    jours = ("lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche")
    mois = ("janvier", "f\u00e9vrier", "mars", "avril", "mai", "juin", "juillet", "ao\u00fbt", "septembre", "octobre", "novembre", "d\u00e9cembre")
    date_locale = f"{jours[maintenant.weekday()]} {maintenant.day} {mois[maintenant.month - 1]} {maintenant.year} \u00e0 {maintenant:%H:%M:%S}"

    ville = request.form.get("ville", "").strip()[:80] or os.environ.get("DASHLE_METEO_VILLE", "").strip()[:80]
    meteo = meteo_du_jour(ville) if ville else None
    actualites = actualites_recentes(8) or []

    repertoire_polices = os.path.join(app.root_path, "static", "fonts")
    pdfmetrics.registerFont(TTFont("DashleUnicode", os.path.join(repertoire_polices, "DejaVuSans.ttf")))
    pdfmetrics.registerFont(TTFont("DashleUnicode-Bold", os.path.join(repertoire_polices, "DejaVuSans-Bold.ttf")))
    styles = getSampleStyleSheet()
    styles.add(ParagraphStyle(name="DashleTitle", parent=styles["Title"], fontName="DashleUnicode-Bold", fontSize=22, leading=28, textColor=colors.HexColor("#176b54"), alignment=TA_LEFT, spaceAfter=12))
    styles.add(ParagraphStyle(name="DashleHeading", parent=styles["Heading2"], fontName="DashleUnicode-Bold", fontSize=15, leading=20, textColor=colors.HexColor("#176b54"), spaceBefore=16, spaceAfter=7))
    styles.add(ParagraphStyle(name="DashleBody", parent=styles["BodyText"], fontName="DashleUnicode", fontSize=10.5, leading=16, spaceAfter=6))
    styles.add(ParagraphStyle(name="DashleLabel", parent=styles["BodyText"], fontName="DashleUnicode-Bold", fontSize=10.5, leading=16, spaceAfter=3))

    sujet = request.form.get("sujet", "").strip()[:2000]
    if sujet:
        try:
            reponse_sujet = traiter_message(sujet, user_id=session.get("user_id"))
        except Exception:
            app.logger.exception("Échec de génération du contenu PDF pour le sujet demandé")
            return jsonify({"erreur": "Le contenu du PDF n’a pas pu être généré."}), 502
        titre_sujet = sujet
        paragraphs = [
            Paragraph(html_escape(titre_sujet), styles["DashleTitle"]),
            Paragraph("Document préparé par DASHLE", styles["DashleLabel"]),
        ]
        for bloc in str(reponse_sujet or "").splitlines():
            bloc = bloc.strip()
            if bloc:
                paragraphs.append(Paragraph(html_escape(bloc), styles["DashleBody"]))
        sortie_sujet = io.BytesIO()
        SimpleDocTemplate(
            sortie_sujet, pagesize=A4, rightMargin=52, leftMargin=52,
            topMargin=48, bottomMargin=48, title=titre_sujet, author="DASHLE",
        ).build(paragraphs)
        _enregistrer_element_bibliotheque(
            session.get("user_id"), "pdf", titre_sujet, "application/pdf",
            sortie_sujet.getvalue(), session.get("conversation_id"),
        )
        sortie_sujet.seek(0)
        return send_file(
            sortie_sujet, mimetype="application/pdf", as_attachment=True,
            download_name="dashle-document.pdf",
        )

    sortie = io.BytesIO()
    document = SimpleDocTemplate(
        sortie, pagesize=A4, rightMargin=52, leftMargin=52,
        topMargin=48, bottomMargin=48, title="Le temps, maintenant",
        author="DASHLE",
    )
    contenu = [
        Paragraph("Le temps, maintenant", styles["DashleTitle"]),
        Paragraph("Date et heure locales", styles["DashleLabel"]),
        Paragraph(html_escape(date_locale) + f" <font color='#6b7e76'>({html_escape(fuseau_demande)})</font>", styles["DashleBody"]),
        Paragraph("M\u00e9t\u00e9o du jour", styles["DashleHeading"]),
    ]
    if not meteo or meteo.get("erreur"):
        contenu.extend([
            Paragraph("Ville", styles["DashleLabel"]),
            Paragraph(html_escape(ville or "Non renseign\u00e9e"), styles["DashleBody"]),
            Paragraph("La m\u00e9t\u00e9o n\u2019est pas configur\u00e9e sur le serveur.", styles["DashleBody"]),
        ])
    else:
        nom_ville = ", ".join(part for part in (meteo.get("ville"), meteo.get("pays")) if part)
        description_meteo = str(meteo.get("description") or "Conditions indisponibles").lower()
        symbole_meteo = "☂" if any(mot in description_meteo for mot in ("pluie", "bruine", "averse")) else "☁" if any(mot in description_meteo for mot in ("nuage", "couvert", "brume")) else "☀"
        carte_meteo = Table([[
            Paragraph(symbole_meteo, ParagraphStyle(name="DashleWeatherIcon", fontName="DashleUnicode", fontSize=30, textColor=colors.HexColor("#19765d"))),
            Paragraph(f"<b>{html_escape(nom_ville or ville or 'Météo')}</b><br/>{html_escape(str(meteo.get('description') or 'Conditions indisponibles')).capitalize()}", styles["DashleBody"]),
            Paragraph(f"<font size='28' color='#176b54'><b>{html_escape(str(meteo.get('temperature', '—')))}°</b></font>", styles["DashleBody"]),
        ]], colWidths=[48, 300, 130])
        carte_meteo.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#e8f5ed")),
            ("BOX", (0, 0), (-1, -1), 0.8, colors.HexColor("#cce5d9")),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("LEFTPADDING", (0, 0), (-1, -1), 12),
            ("RIGHTPADDING", (0, 0), (-1, -1), 12),
            ("TOPPADDING", (0, 0), (-1, -1), 13),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 13),
        ]))
        details_meteo = Table([[
            Paragraph("Minimum<br/><b>" + html_escape(str(meteo.get("minimum", "—"))) + " °C</b>", styles["DashleBody"]),
            Paragraph("Maximum<br/><b>" + html_escape(str(meteo.get("maximum", "—"))) + " °C</b>", styles["DashleBody"]),
            Paragraph("Humidité<br/><b>" + html_escape(str(meteo.get("humidite", "—"))) + " %</b>", styles["DashleBody"]),
            Paragraph("Ressenti<br/><b>" + html_escape(str(meteo.get("ressenti", "—"))) + " °C</b>", styles["DashleBody"]),
        ]], colWidths=[119.5] * 4)
        details_meteo.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#f1f7fc")),
            ("BOX", (0, 0), (-1, -1), 0.5, colors.HexColor("#d5e5ed")),
            ("INNERGRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#d5e5ed")),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("LEFTPADDING", (0, 0), (-1, -1), 9),
            ("TOPPADDING", (0, 0), (-1, -1), 9),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 9),
        ]))
        contenu.extend([carte_meteo, details_meteo])
    contenu.append(Paragraph("Source : OpenWeather.", styles["DashleBody"]))
    contenu.append(Paragraph("Actualit\u00e9s r\u00e9centes", styles["DashleHeading"]))
    if not actualites:
        contenu.append(Paragraph("Le flux d\u2019actualit\u00e9s est momentan\u00e9ment indisponible.", styles["DashleBody"]))
    else:
        for article in actualites:
            titre = html_escape(str(article.get("titre") or "Titre indisponible"))
            url = str(article.get("url") or "")
            parsed = urlparse(url)
            if parsed.scheme == "https" and parsed.netloc:
                titre = f'<link href="{html_escape(url, quote=True)}" color="#16765b">{titre}</link>'
            contenu.append(Paragraph("&#8226; " + titre, styles["DashleBody"]))
    contenu.append(Paragraph("Source : Le Monde (flux RSS).", styles["DashleBody"]))

    document.build(contenu)
    _enregistrer_element_bibliotheque(
        session.get("user_id"), "pdf", "Le temps, maintenant", "application/pdf",
        sortie.getvalue(), session.get("conversation_id"),
    )
    sortie.seek(0)
    return send_file(
        sortie,
        mimetype="application/pdf",
        as_attachment=True,
        download_name="dashle-temps-maintenant.pdf",
    )


TASK_PLAN_LIMITS = {"pro": {"tasks": 3, "daily_runs": 5}, "prime": {"tasks": 10, "daily_runs": 20}}


def _prochaine_execution(frequence, heure, fuseau, weekday=None, depuis=None):
    zone = ZoneInfo(fuseau)
    maintenant = depuis or datetime.now(timezone.utc)
    if maintenant.tzinfo is None:
        maintenant = maintenant.replace(tzinfo=timezone.utc)
    local = maintenant.astimezone(zone)
    heure_locale = datetime.strptime(heure, "%H:%M").time()
    date_cible = local.date()
    if frequence == "hebdomadaire":
        if weekday is None or not 0 <= int(weekday) <= 6:
            raise ValueError("Choisis le jour de la semaine.")
        date_cible += timedelta(days=(int(weekday) - date_cible.weekday()) % 7)
    candidat = datetime.combine(date_cible, heure_locale, tzinfo=zone)
    if candidat <= local:
        date_cible += timedelta(days=7 if frequence == "hebdomadaire" else 1)
        candidat = datetime.combine(date_cible, heure_locale, tzinfo=zone)
    return candidat.astimezone(timezone.utc).replace(tzinfo=None)


def _notification_tache(db, user_id, task_id, titre, texte):
    db.add(UserNotification(user_id=user_id, task_id=task_id, title=titre, body=texte))


@app.route("/taches-planifiees", methods=["GET", "POST"])
def taches_planifiees():
    user_id = session.get("user_id")
    if not user_id:
        return redirect(url_for("connexion"))
    erreur = request.args.get("erreur")
    succes = request.args.get("succes")
    with session_base() as db:
        user = db.get(User, user_id)
        niveau = niveau_abonnement(user)
        limites = TASK_PLAN_LIMITS.get(niveau)
        if request.method == "POST":
            if not limites:
                erreur = "Les tâches planifiées sont réservées aux offres Pro et Prime."
            else:
                consigne = request.form.get("instruction", "").strip()[:2000]
                frequence = request.form.get("frequency", "")
                heure = request.form.get("run_time", "")
                fuseau = request.form.get("timezone", "UTC").strip()[:80]
                jour = request.form.get("weekday", "")
                try:
                    weekday = int(jour) if frequence == "hebdomadaire" else None
                    prochaine = _prochaine_execution(frequence, heure, fuseau, weekday)
                    if not consigne:
                        raise ValueError("La consigne est obligatoire.")
                except (ValueError, TypeError, ZoneInfoNotFoundError):
                    erreur = "Vérifie la consigne, l’heure, le fuseau et le jour choisi."
                else:
                    nombre = db.query(func.count(ScheduledTask.id)).filter_by(
                        user_id=user_id, active=True
                    ).scalar() or 0
                    if nombre >= limites["tasks"]:
                        erreur = f"Ton offre autorise au maximum {limites['tasks']} tâches actives."
                    else:
                        db.add(ScheduledTask(
                            user_id=user_id, instruction=consigne, frequency=frequence,
                            run_time=heure, timezone=fuseau, weekday=weekday,
                            next_run_at=prochaine,
                        ))
                        succes = "Tâche planifiée."
        tasks = db.query(ScheduledTask).filter_by(user_id=user_id).order_by(
            ScheduledTask.created_at.desc()
        ).all()
        notifications = db.query(UserNotification).filter_by(user_id=user_id).order_by(
            UserNotification.created_at.desc()
        ).limit(20).all()
        vues = [{"id": task.id, "instruction": task.instruction, "frequency": task.frequency,
                 "run_time": task.run_time, "timezone": task.timezone, "weekday": task.weekday,
                 "active": task.active, "failures": task.consecutive_failures,
                 "next_run_at": task.next_run_at, "last_run_at": task.last_run_at}
                for task in tasks]
        alertes = [{"id": item.id, "title": item.title, "body": item.body,
                    "read": item.read, "created_at": item.created_at}
                   for item in notifications]
    return render_template(
        "taches_planifiees.html", tasks=vues, notifications=alertes,
        niveau=niveau, limites=limites, erreur=erreur, succes=succes,
        csrf_token=jeton_csrf(),
    )


@app.route("/taches-planifiees/<int:task_id>/basculer", methods=["POST"])
def basculer_tache_planifiee(task_id):
    user_id = session.get("user_id")
    with session_base() as db:
        task = db.query(ScheduledTask).filter_by(id=task_id, user_id=user_id).one_or_none()
        if task is None:
            return "Tâche introuvable.", 404
        task.active = not task.active
        if task.active:
            task.consecutive_failures = 0
            task.next_run_at = _prochaine_execution(
                task.frequency, task.run_time, task.timezone, task.weekday
            )
    return redirect(url_for("taches_planifiees"))


@app.route("/taches-planifiees/<int:task_id>/supprimer", methods=["POST"])
def supprimer_tache_planifiee(task_id):
    user_id = session.get("user_id")
    with session_base() as db:
        task = db.query(ScheduledTask).filter_by(id=task_id, user_id=user_id).one_or_none()
        if task is None:
            return "Tâche introuvable.", 404
        db.delete(task)
    return redirect(url_for("taches_planifiees"))


@app.route("/taches-planifiees/notifications/<int:notification_id>/lue", methods=["POST"])
def lire_notification_tache(notification_id):
    user_id = session.get("user_id")
    with session_base() as db:
        notification = db.query(UserNotification).filter_by(
            id=notification_id, user_id=user_id
        ).one_or_none()
        if notification is None:
            return "Notification introuvable.", 404
        notification.read = True
    return redirect(url_for("taches_planifiees"))


@app.route("/internal/cron/run", methods=["POST"])
def executer_taches_cron():
    secret = os.environ.get("CRON_SECRET", "")
    fourni = request.headers.get("X-Cron-Secret", "")
    if not secret or not fourni or not hmac.compare_digest(secret, fourni):
        return jsonify({"erreur": "Non autorisé."}), 403

    maintenant = datetime.now(timezone.utc).replace(tzinfo=None)
    avec = 0
    with session_base() as db:
        ids = [row[0] for row in db.query(ScheduledTask.id).filter(
            ScheduledTask.active.is_(True), ScheduledTask.next_run_at <= maintenant
        ).order_by(ScheduledTask.next_run_at).limit(50).all()]
        for task_id in ids:
            task = db.query(ScheduledTask).filter_by(id=task_id).with_for_update(skip_locked=True).one_or_none()
            if task is None or not task.active or task.next_run_at > maintenant:
                continue
            user = db.query(User).filter_by(id=task.user_id).with_for_update().one_or_none()
            limites = TASK_PLAN_LIMITS.get(niveau_abonnement(user)) if user else None
            if not limites:
                task.active = False
                if user:
                    _notification_tache(db, user.id, task.id, "Tâche mise en pause",
                                        "Cette fonction nécessite une offre Pro ou Prime.")
                continue
            try:
                zone = ZoneInfo(task.timezone)
                local_now = datetime.now(timezone.utc).astimezone(zone)
                debut_local = datetime.combine(local_now.date(), datetime.min.time(), tzinfo=zone)
                debut_utc = debut_local.astimezone(timezone.utc).replace(tzinfo=None)
                count = db.query(func.count(ScheduledTaskRun.id)).filter(
                    ScheduledTaskRun.user_id == user.id,
                    ScheduledTaskRun.executed_at >= debut_utc,
                    ScheduledTaskRun.executed_at <= maintenant,
                ).scalar() or 0
                if count >= limites["daily_runs"]:
                    task.next_run_at = _prochaine_execution(
                        task.frequency, task.run_time, task.timezone, task.weekday
                    )
                    continue
                task.last_run_at = maintenant
                task.next_run_at = _prochaine_execution(
                    task.frequency, task.run_time, task.timezone, task.weekday
                )
                total = int(db.query(func.coalesce(func.sum(LibraryItem.size_bytes), 0))
                            .filter_by(user_id=user.id).scalar() or 0)
                limite_stockage = LIBRARY_PLAN_LIMITS.get(niveau_abonnement(user), LIBRARY_PLAN_LIMITS["free"])
                if total >= limite_stockage:
                    raise RuntimeError("library_quota")
                resultat = str(traiter_message(task.instruction, [], user.id, "") or "").strip()
                if not resultat:
                    raise RuntimeError("empty_response")
                contenu = resultat.encode("utf-8")[:MAX_LIBRARY_ITEM_BYTES]
                db.add(LibraryItem(
                    user_id=user.id, type="analyse", title=("Tâche — " + task.instruction)[:200],
                    mime_type="text/plain", content=contenu, size_bytes=len(contenu),
                ))
                task.consecutive_failures = 0
                db.add(ScheduledTaskRun(task_id=task.id, user_id=user.id, success=True,
                                        executed_at=maintenant))
                _notification_tache(db, user.id, task.id, "Tâche terminée",
                                    "Le résultat a été enregistré dans ta bibliothèque.")
            except Exception as exc:
                task.consecutive_failures += 1
                db.add(ScheduledTaskRun(task_id=task.id, user_id=user.id, success=False,
                                        executed_at=maintenant, error=type(exc).__name__[:300]))
                if task.consecutive_failures >= 3:
                    task.active = False
                    _notification_tache(db, user.id, task.id, "Tâche désactivée après 3 échecs",
                                        "La tâche a échoué trois fois de suite. Vérifie sa consigne ou ton accès à DASHLE.")
            avec += 1
            break
    return jsonify({"traite": avec})


@app.route("/planification", methods=["GET", "POST"])
def planification():
    user_id = session.get("user_id")
    erreur = None
    if request.method == "POST":
        titre = request.form.get("title", "").strip()[:200]
        valeur_date = request.form.get("due_at", "").strip()
        try:
            date_echeance = datetime.fromisoformat(valeur_date.replace("Z", "+00:00"))
            if date_echeance.tzinfo:
                date_echeance = date_echeance.astimezone(timezone.utc).replace(tzinfo=None)
            if not titre:
                raise ValueError
            with session_base() as db:
                db.add(Reminder(user_id=user_id, title=titre, due_at=date_echeance))
            return redirect(url_for("planification"))
        except ValueError:
            erreur = "Indique un titre et une date valides."
    with session_base() as db:
        rappels = db.query(Reminder).filter_by(user_id=user_id).order_by(Reminder.completed, Reminder.due_at).all()
        return render_template_string(ESPACE_PAGE, mode="rappels", titre="Planification", intro="Crée des rappels datés et suis les tâches depuis ton compte.", rappels=rappels, erreur=erreur, csrf_token=jeton_csrf())


@app.route("/planification/<int:reminder_id>/basculer", methods=["POST"])
def modifier_rappel(reminder_id):
    with session_base() as db:
        rappel = db.query(Reminder).filter_by(id=reminder_id, user_id=session.get("user_id")).one_or_none()
        if rappel:
            rappel.completed = not rappel.completed
    return redirect(url_for("planification"))


@app.route("/planification/<int:reminder_id>/supprimer", methods=["POST"])
def supprimer_rappel(reminder_id):
    with session_base() as db:
        rappel = db.query(Reminder).filter_by(id=reminder_id, user_id=session.get("user_id")).one_or_none()
        if rappel:
            db.delete(rappel)
    return redirect(url_for("planification"))


PROJECT_PLAN_LIMITS = {
    "free": {"projects": 1, "storage_bytes": 10 * 1024 * 1024},
    "pro": {"projects": 10, "storage_bytes": 100 * 1024 * 1024},
    "prime": {"projects": None, "storage_bytes": 500 * 1024 * 1024},
}
MAX_PROJECT_FILE_BYTES = 7 * 1024 * 1024


def _limites_projets(user):
    return PROJECT_PLAN_LIMITS[niveau_abonnement(user)]


def _stockage_projets_utilise(db, user_id):
    return int(db.query(func.coalesce(func.sum(ProjectFile.size_bytes), 0))
               .join(Project, Project.id == ProjectFile.project_id)
               .filter(Project.user_id == user_id).scalar() or 0)


def _contexte_projet_pour_conversation(user_id, conversation_id=None):
    """Charge le contexte d'un projet uniquement si celui-ci appartient au compte."""
    if not user_id:
        return "", ""
    if conversation_id:
        with session_base() as db:
            conversation = db.query(Conversation).filter_by(
                id=conversation_id, user_id=user_id
            ).one_or_none()
            project_id = conversation.project_id if conversation else None
    else:
        project_id = session.get("projet_temporaire_id")
    if not project_id:
        return "", ""

    with session_base() as db:
        projet = db.query(Project).filter_by(id=project_id, user_id=user_id).one_or_none()
        if projet is None:
            return "", ""
        instructions = (projet.instructions or "")[:2000]
        fichiers = db.query(ProjectFile.filename, ProjectFile.extracted_text).filter_by(
            project_id=projet.id
        ).order_by(ProjectFile.created_at, ProjectFile.id).all()

    extraits = []
    restant = 4000
    for nom_fichier, texte in fichiers:
        if restant <= 0:
            break
        extrait = (texte or "")[:min(1200, restant)]
        if extrait:
            entete = f"\n[{nom_fichier}]\n"
            morceau = (entete + extrait)[:restant]
            extraits.append(morceau)
            restant -= len(morceau)
    return instructions, "".join(extraits)


def _arguments_contexte_projet(user_id, conversation_id=None):
    instructions, fichiers = _contexte_projet_pour_conversation(
        user_id, conversation_id
    )
    if not instructions and not fichiers:
        return {}
    return {
        "instructions_projet": instructions,
        "fichiers_projet": fichiers,
    }


@app.route("/projets")
def projets():
    user_id = session.get("user_id")
    if not user_id:
        return redirect(url_for("connexion"))
    with session_base() as db:
        user = db.get(User, user_id)
        if user is None:
            return redirect(url_for("connexion"))
        limites = _limites_projets(user)
        liste = db.query(Project).filter_by(user_id=user_id).order_by(Project.name).all()
        conversations = db.query(Conversation).filter_by(
            user_id=user_id, archivee=False
        ).order_by(Conversation.updated_at.desc()).all()
        fichiers = db.query(ProjectFile).join(Project).filter(
            Project.user_id == user_id
        ).order_by(ProjectFile.created_at.desc()).all()
        par_projet = {projet.id: [] for projet in liste}
        for fichier in fichiers:
            par_projet.setdefault(fichier.project_id, []).append({
                "id": fichier.id,
                "filename": fichier.filename,
                "size_bytes": fichier.size_bytes,
            })
        vues = []
        for projet in liste:
            vues.append({
                "id": projet.id,
                "name": projet.name,
                "instructions": projet.instructions,
                "files": par_projet.get(projet.id, []),
                "conversations": [
                    {"id": conv.id, "title": conv.title}
                    for conv in conversations if conv.project_id == projet.id
                ],
            })
        sans_projet = [
            {"id": conv.id, "title": conv.title, "project_id": conv.project_id}
            for conv in conversations if conv.project_id is None
        ]
        nombre = db.query(func.count(Project.id)).filter_by(user_id=user_id).scalar() or 0
        stockage = _stockage_projets_utilise(db, user_id)
        erreur = request.args.get("erreur")
        succes = request.args.get("succes")
        limite_projets = limites["projects"]
        quota = {
            "projects_used": nombre,
            "projects_limit": limite_projets,
            "storage_used": stockage,
            "storage_limit": limites["storage_bytes"],
            "file_limit": MAX_PROJECT_FILE_BYTES,
        }
        return render_template_string(
            ESPACE_PAGE,
            mode="projets", titre="Projets", intro="Organise tes conversations, consignes et fichiers par projet.",
            projets=vues, conversations=sans_projet, quota=quota,
            erreur=erreur, succes=succes, csrf_token=jeton_csrf(),
        )


@app.route("/projets/creer", methods=["POST"])
def creer_projet():
    user_id = session.get("user_id")
    if not user_id:
        return redirect(url_for("connexion"))
    nom = request.form.get("name", "").strip()
    instructions = request.form.get("instructions", "").strip()
    if not nom or len(nom) > 100 or len(instructions) > 2000:
        return redirect(url_for("projets", erreur="Vérifie le nom et les consignes (2 000 caractères maximum)."))
    with session_base() as db:
        user = db.query(User).filter_by(id=user_id).with_for_update().one_or_none()
        if user is None:
            return redirect(url_for("connexion"))
        limite = _limites_projets(user)["projects"]
        compte = db.query(func.count(Project.id)).filter_by(user_id=user_id).scalar() or 0
        if limite is not None and compte >= limite:
            return redirect(url_for("projets", erreur=f"Ton offre limite les projets à {limite}. Supprime un projet ou change d’offre."))
        db.add(Project(user_id=user_id, name=nom, instructions=instructions))
    return redirect(url_for("projets", succes="Projet créé."))


@app.route("/projets/<int:project_id>/modifier", methods=["POST"])
def modifier_projet(project_id):
    user_id = session.get("user_id")
    if not user_id:
        return redirect(url_for("connexion"))
    nom = request.form.get("name", "").strip()
    instructions = request.form.get("instructions", "").strip()
    if not nom or len(nom) > 100 or len(instructions) > 2000:
        return redirect(url_for("projets", erreur="Vérifie le nom et les consignes (2 000 caractères maximum)."))
    with session_base() as db:
        projet = db.query(Project).filter_by(id=project_id, user_id=user_id).one_or_none()
        if projet is None:
            return "Projet introuvable.", 404
        projet.name = nom
        projet.instructions = instructions
    return redirect(url_for("projets", succes="Projet mis à jour."))


@app.route("/projets/<int:project_id>/supprimer", methods=["POST"])
def supprimer_projet(project_id):
    user_id = session.get("user_id")
    if not user_id:
        return redirect(url_for("connexion"))
    with session_base() as db:
        projet = db.query(Project).filter_by(id=project_id, user_id=user_id).one_or_none()
        if projet is None:
            return "Projet introuvable.", 404
        # Les conversations survivent à la suppression du projet et redeviennent générales.
        db.query(Conversation).filter_by(
            user_id=user_id, project_id=project_id
        ).update({Conversation.project_id: None}, synchronize_session=False)
        db.delete(projet)
    if session.get("projet_temporaire_id") == project_id:
        session.pop("projet_temporaire_id", None)
    return redirect(url_for("projets", succes="Projet supprimé ; les conversations ont été conservées."))


@app.route("/projets/<int:project_id>/conversation", methods=["POST"])
def nouvelle_conversation_projet(project_id):
    user_id = session.get("user_id")
    if not user_id:
        return redirect(url_for("connexion"))
    with session_base() as db:
        projet = db.query(Project).filter_by(id=project_id, user_id=user_id).one_or_none()
        if projet is None:
            return "Projet introuvable.", 404
        preference = db.query(UserPreference).filter_by(user_id=user_id).one_or_none()
        conserver = preference is None or preference.conserver_historique
        if conserver:
            conversation = Conversation(user_id=user_id, project_id=projet.id)
            db.add(conversation)
            db.flush()
            session["conversation_id"] = conversation.id
            session.pop("projet_temporaire_id", None)
        else:
            # Respecte la préférence d'historique : le projet reste actif dans la session,
            # mais aucune conversation n'est créée en base.
            session.pop("conversation_id", None)
            session["projet_temporaire_id"] = projet.id
    return redirect(url_for("accueil"))


@app.route("/projets/<int:project_id>/fichiers", methods=["POST"])
def ajouter_fichier_projet(project_id):
    user_id = session.get("user_id")
    if not user_id:
        return redirect(url_for("connexion"))
    fichier = request.files.get("file")
    nom = secure_filename(fichier.filename or "") if fichier else ""
    if not fichier or not nom:
        return redirect(url_for("projets", erreur="Choisis un fichier CSV, Excel XLSX, PDF, TXT ou Markdown."))
    with session_base() as db:
        projet = db.query(Project.id).filter_by(id=project_id, user_id=user_id).one_or_none()
        if projet is None:
            return "Projet introuvable.", 404
    contenu = fichier.stream.read(MAX_PROJECT_FILE_BYTES + 1)
    if len(contenu) > MAX_PROJECT_FILE_BYTES:
        return redirect(url_for("projets", erreur="Un fichier ne peut pas dépasser 7 Mio."))
    from project_files import extract_project_file
    try:
        mime_type, texte = extract_project_file(nom, contenu)
    except ValueError as exc:
        return redirect(url_for("projets", erreur=str(exc)))

    with session_base() as db:
        user = db.query(User).filter_by(id=user_id).with_for_update().one_or_none()
        projet = db.query(Project).filter_by(id=project_id, user_id=user_id).one_or_none()
        if user is None or projet is None:
            return "Projet introuvable.", 404
        stockage_max = _limites_projets(user)["storage_bytes"]
        stockage_actuel = _stockage_projets_utilise(db, user_id)
        if stockage_actuel + len(contenu) > stockage_max:
            max_mio = stockage_max // (1024 * 1024)
            return redirect(url_for("projets", erreur=f"Le stockage de fichiers de ton offre est limité à {max_mio} Mio."))
        db.add(ProjectFile(
            project_id=projet.id, filename=nom, mime_type=mime_type,
            size_bytes=len(contenu), content=contenu, extracted_text=texte,
        ))
    return redirect(url_for("projets", succes="Fichier ajouté au projet."))


@app.route("/projets/<int:project_id>/fichiers/<int:file_id>/telecharger")
def telecharger_fichier_projet(project_id, file_id):
    user_id = session.get("user_id")
    if not user_id:
        return redirect(url_for("connexion"))
    with session_base() as db:
        fichier = db.query(ProjectFile).join(Project).filter(
            ProjectFile.id == file_id, ProjectFile.project_id == project_id,
            Project.user_id == user_id,
        ).one_or_none()
        if fichier is None:
            return "Fichier introuvable.", 404
        contenu = fichier.content
        nom, mime_type = fichier.filename, fichier.mime_type
    return send_file(io.BytesIO(contenu), mimetype=mime_type, as_attachment=True, download_name=nom)


@app.route("/projets/<int:project_id>/fichiers/<int:file_id>/supprimer", methods=["POST"])
def supprimer_fichier_projet(project_id, file_id):
    user_id = session.get("user_id")
    if not user_id:
        return redirect(url_for("connexion"))
    with session_base() as db:
        fichier = db.query(ProjectFile).join(Project).filter(
            ProjectFile.id == file_id, ProjectFile.project_id == project_id,
            Project.user_id == user_id,
        ).one_or_none()
        if fichier is None:
            return "Fichier introuvable.", 404
        db.delete(fichier)
    return redirect(url_for("projets", succes="Fichier supprimé."))


@app.route("/projets/conversation/<int:conversation_id>", methods=["POST"])
def affecter_conversation(conversation_id):
    user_id = session.get("user_id")
    if not user_id:
        return redirect(url_for("connexion"))
    selection = request.form.get("project_id", "").strip()
    try:
        projet_id = int(selection) if selection else None
    except ValueError:
        projet_id = -1
    with session_base() as db:
        conv = db.query(Conversation).filter_by(id=conversation_id, user_id=user_id).one_or_none()
        if conv:
            if not selection:
                conv.project_id = None
            else:
                projet = db.query(Project).filter_by(id=projet_id, user_id=user_id).one_or_none()
                if projet:
                    conv.project_id = projet.id
    return redirect(url_for("projets"))


@app.route("/plugins", methods=["GET", "POST"])
def plugins():
    user_id = session.get("user_id")
    definitions = {
        "meteo": ("Météo", "Ajoute les données météo aux réponses quand tu les demandes."),
        "actualites": ("Actualités", "Ajoute des titres RSS récents aux réponses quand tu les demandes."),
        "statistiques": ("Mode statistique", "Autorise l’outil d’analyse de fichiers pour les offres Pro et Prime."),
    }
    if request.method == "POST":
        with session_base() as db:
            for cle in definitions:
                plugin = db.query(UserPlugin).filter_by(user_id=user_id, plugin=cle).one_or_none()
                actif = request.form.get("plugin_" + cle) == "1"
                if plugin:
                    plugin.enabled = actif
                else:
                    db.add(UserPlugin(user_id=user_id, plugin=cle, enabled=actif))
        return redirect(url_for("plugins"))
    with session_base() as db:
        preferences = {p.plugin: p.enabled for p in db.query(UserPlugin).filter_by(user_id=user_id).all()}
    etat = {cle: preferences.get(cle, True) for cle in definitions}
    options = [(cle, *texte) for cle, texte in definitions.items()]
    return render_template_string(ESPACE_PAGE, mode="plugins", titre="Plugins", intro="Active ou désactive les fonctions proposées par Dashle.", etat=etat, options=options, erreur=None, csrf_token=jeton_csrf())


@app.route("/statistiques", methods=["GET", "POST"])
def statistiques():
    user_id = session.get("user_id")
    maintenant = datetime.utcnow()
    jour_debut = maintenant.replace(hour=0, minute=0, second=0, microsecond=0)
    jour_fin = jour_debut + timedelta(days=1)
    erreur = None
    calculs = interpretation = metriques = None

    with session_base() as db:
        user = db.get(User, user_id)
        if not user:
            return redirect(url_for("connexion"))
        niveau = niveau_abonnement(user)
        if niveau == "free" and user.subscription_expires_at and user.subscription_expires_at <= maintenant:
            user.subscription_level = "free"
        autorise = niveau in {"pro", "prime"}
        plugin_active = db.query(UserPlugin).filter_by(
            user_id=user_id, plugin="statistiques", enabled=False
        ).one_or_none() is None
        utilise = db.query(StatisticalAnalysisUsage).filter(
            StatisticalAnalysisUsage.user_id == user_id,
            StatisticalAnalysisUsage.created_at >= jour_debut,
            StatisticalAnalysisUsage.created_at < jour_fin,
        ).count() if autorise and plugin_active else 0

    restant = max(0, 25 - utilise) if autorise and plugin_active else 0
    if request.method == "POST" and not plugin_active:
        erreur = "Active le mode statistique dans la page Plugins pour lancer une analyse."
    elif request.method == "POST" and not autorise:
        erreur = "Les analyses statistiques nécessitent Dashle Pro ou Dashle Prime."
    elif request.method == "POST":
        fichier = request.files.get("fichier")
        question = request.form.get("question", "").strip()[:1000]
        if not fichier or not fichier.filename or not question:
            erreur = "Choisis un fichier et précise la question à analyser."
        elif restant <= 0:
            erreur = "La limite de 25 analyses statistiques par jour est atteinte. Réessaie demain."
        else:
            try:
                contenu = fichier.read()
                calculs, metriques = analyser_fichier(contenu, fichier.filename, question)
            except ValueError as exc:
                erreur = str(exc)
            except Exception as exc:
                print("ERREUR analyse statistique :", type(exc).__name__)
                erreur = "Le fichier n’a pas pu être lu. Vérifie son format, ses colonnes et son encodage."

            if metriques:
                # Le verrou utilisateur empêche deux requêtes simultanées de
                # dépasser le quota sur PostgreSQL.
                with session_base() as db:
                    user = db.query(User).filter_by(id=user_id).with_for_update().one_or_none()
                    if not user:
                        return redirect(url_for("connexion"))
                    utilise = db.query(StatisticalAnalysisUsage).filter(
                        StatisticalAnalysisUsage.user_id == user_id,
                        StatisticalAnalysisUsage.created_at >= jour_debut,
                        StatisticalAnalysisUsage.created_at < jour_fin,
                    ).count()
                    if utilise >= 25:
                        metriques = None
                        erreur = "La limite de 25 analyses statistiques par jour est atteinte. Réessaie demain."
                    else:
                        db.add(StatisticalAnalysisUsage(user_id=user_id))
                        restant = 24 - utilise
                if metriques:
                    prompt = (
                        "Analyse statistique calculée côté serveur (résultats numériques fiables) :\n"
                        + calculs + "\n\nQuestion de l’utilisateur : " + question
                        + "\nInterprète les résultats sans modifier les nombres et rappelle les hypothèses utiles."
                    )
                    interpretation = traiter_message(prompt, [], user_id, "")
                    library_conversation_id = session.get("conversation_id")
                    titre_analyse = f"Analyse — {secure_filename(fichier.filename) or 'données'}"
                    _enregistrer_element_bibliotheque(
                        user_id, "analyse", titre_analyse,
                        "text/plain", f"Question : {question}\n\n{calculs}\n\nInterprétation DASHLE :\n{interpretation or ''}",
                        library_conversation_id,
                    )
                    graphique = (metriques or {}).get("graphique") or ""
                    if graphique.startswith("data:image/svg+xml;base64,"):
                        try:
                            svg = base64.b64decode(graphique.split(",", 1)[1], validate=True)
                            _enregistrer_element_bibliotheque(
                                user_id, "graphique", f"Graphique — {secure_filename(fichier.filename) or 'analyse'}",
                                "image/svg+xml", svg, library_conversation_id,
                            )
                        except (ValueError, base64.binascii.Error):
                            app.logger.warning("Graphique statistique non sauvegardé : SVG invalide")

    return render_template_string(
        STATISTIQUES_PAGE,
        autorise=autorise,
        plugin_active=plugin_active,
        niveau=niveau,
        restant=restant,
        erreur=erreur,
        calculs=calculs,
        interpretation=interpretation,
        metriques=metriques,
        csrf_token=jeton_csrf(),
    ), (403 if request.method == "POST" and (not autorise or not plugin_active) else 200)


OFFRES_ABONNEMENT = {code: offre for code, offre in OFFRES_DASHLE.items() if code != "free"}


def _date_apres_mois(date, nombre):
    mois_indexe = date.month - 1 + nombre
    annee = date.year + mois_indexe // 12
    mois = mois_indexe % 12 + 1
    jour = min(date.day, calendar.monthrange(annee, mois)[1])
    return date.replace(year=annee, month=mois, day=jour)


_TAUX_CACHE = {"at": None, "eur": 655.957, "usd": 0.90}

def _taux_indicatifs():
    maintenant = datetime.utcnow()
    at = _TAUX_CACHE["at"]
    if at and maintenant - at < timedelta(hours=6):
        return {"eur": _TAUX_CACHE["eur"], "usd": _TAUX_CACHE["usd"]}
    try:
        response = requests.get(
            "https://api.frankfurter.app/latest?from=EUR&to=USD,XOF",
            timeout=5,
            headers={"Accept": "application/json", "User-Agent": "DASHLE/1.0"},
        )
        data = response.json()
        rates = data.get("rates", {})
        eur_xof = float(rates.get("XOF", 655.957))
        usd_per_eur = float(rates.get("USD", 0.90))
        if eur_xof > 0 and usd_per_eur > 0:
            _TAUX_CACHE.update({"at": maintenant, "eur": eur_xof, "usd": usd_per_eur})
    except (requests.RequestException, ValueError, TypeError):
        pass
    return {"eur": _TAUX_CACHE["eur"], "usd": _TAUX_CACHE["usd"]}

def _moyens_labels(moyens):
    return {m: {"paydunya": "Mobile Money + carte · PayDunya", "cinetpay": "CinetPay", "stripe": "Carte bancaire"}[m] for m in moyens}

def _rendre_tarifs(erreur=None):
    user_id = session.get("user_id")
    niveau = "free"
    if user_id:
        with session_base() as db:
            user = db.get(User, user_id)
            if user:
                niveau = niveau_abonnement(user)
    offres = [
        (code, offre["nom"], offre["mensuel"], offre["annuel"], offre["avantages"])
        for code, offre in OFFRES_ABONNEMENT.items()
    ]
    pays = None
    moyens = ["stripe"]
    if user_id:
        with session_base() as db:
            user = db.get(User, user_id)
            pays = user.pays if user else None
            moyens = _moyens_paiement_pays(pays)
    taux = _taux_indicatifs()
    return render_template_string(
        TARIFS_PAGE,
        utilisateur=session.get("user_email"),
        niveau=niveau,
        offres=offres,
        offre_free=OFFRES_DASHLE["free"],
        pays_utilisateur=pays,
        moyens_paiement=moyens,
        taux_eur=taux["eur"],
        taux_usd=taux["usd"],
        csrf_token=jeton_csrf() if user_id else "",
        erreur=erreur or request.args.get("erreur"),
    )


@app.route("/tarifs")
def tarifs():
    return _rendre_tarifs()


FACTURES_PAGE = """
<!doctype html><html lang="fr"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Factures — Dashle</title><style>
body{font:15px/1.5 Segoe UI,sans-serif;color:#18352c;background:#f3f8f6;margin:0}.page{max-width:980px;margin:auto;padding:28px 16px 50px}
a{color:#187a60;text-decoration:none;font-weight:600}.card{background:#fff;border:1px solid #dce9e4;border-radius:16px;padding:18px;margin:14px 0;overflow:auto}
table{width:100%;border-collapse:collapse}th,td{text-align:left;padding:11px 8px;border-bottom:1px solid #edf2f0;white-space:nowrap}.note{color:#71837b;font-size:13px}
</style></head><body><main class="page"><a href="{{ url_for('tarifs') }}">← Tarifs</a><h1>Factures</h1>
<p class="note">Historique des paiements et factures Dashle. Tous les règlements sont enregistrés en XOF.</p>
<section class="card">{% if paiements %}<table><thead><tr><th>Date</th><th>Référence</th><th>Offre</th><th>Montant</th><th>Statut</th><th>Pays</th><th>Moyen</th></tr></thead><tbody>
{% for p in paiements %}<tr><td>{{ p.date }}</td><td>{{ p.reference }}</td><td>{{ p.tier }}</td><td>{{ '{:,}'.format(p.amount).replace(',', ' ') }} XOF</td><td>{{ p.status }}</td><td>{{ p.pays or '—' }}</td><td>{{ p.moyen }}</td></tr>{% endfor %}
</tbody></table>{% else %}<p>Aucune transaction pour le moment.</p>{% endif %}</section></main></body></html>
"""

@app.route("/factures")
def factures():
    user_id = session.get("user_id")
    if not user_id:
        return redirect(url_for("connexion", next=url_for("factures")))
    with session_base() as db:
        paiements_db = db.query(SubscriptionPayment).filter_by(user_id=user_id).order_by(SubscriptionPayment.created_at.desc()).all()
        paiements = [{
            "date": p.created_at.strftime("%d/%m/%Y %H:%M") if p.created_at else "—",
            "reference": p.reference,
            "tier": p.tier,
            "amount": p.amount,
            "status": p.status,
            "pays": p.pays,
            "moyen": {"paydunya":"PayDunya","cinetpay":"CinetPay","stripe":"Carte bancaire"}.get(p.provider,p.provider),
        } for p in paiements_db]
    return render_template_string(FACTURES_PAGE, paiements=paiements)

@app.route("/abonnement/retour")
def paiement_retour():
    token = request.args.get("token", "").strip()
    if token:
        ok, message, code = _confirmer_paydunya_token(token)
        if ok:
            return redirect(url_for("tarifs", retour=1))
        if code not in {200, 404}:
            return _rendre_tarifs(message), code
    return _rendre_tarifs()


ADMIN_PAGE = """
<!doctype html><html lang="fr"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Administration Dashle</title><style>
*{box-sizing:border-box}body{margin:0;padding:32px 16px;background:#f3f7f5;color:#18312b;font:15px Segoe UI,sans-serif}
main{max-width:900px;margin:auto}.card{background:white;border:1px solid #e2ebe7;border-radius:16px;padding:22px;margin:18px 0;box-shadow:0 8px 28px #1746370b}
h1{margin:0;color:#168c65}form.search{display:flex;gap:10px}input,select,button{font:inherit;padding:11px 12px;border:1px solid #ccd9d3;border-radius:9px}input{flex:1;min-width:0}button{background:linear-gradient(110deg,#22c55e,#3b82f6);color:white;border:0;font-weight:600;cursor:pointer}
.user{display:grid;grid-template-columns:minmax(170px,1fr) auto auto;align-items:center;gap:12px}.email{font-weight:600;overflow-wrap:anywhere}.badge{padding:5px 10px;border-radius:99px;background:#e8f7ee;text-transform:capitalize}.actions{display:flex;align-items:center;gap:8px}.manual{color:#087b5b;font-size:13px}.empty{color:#687b73}@media(max-width:640px){.user{grid-template-columns:1fr}.actions{flex-wrap:wrap}}
</style></head><body><main><h1>Administration DASHLE</h1><p>Gestion manuelle des accès aux paliers.</p>
<section class="card"><form class="search" method="get"><input name="q" type="search" value="{{ recherche }}" placeholder="Rechercher par e-mail" aria-label="Rechercher par e-mail"><button>Rechercher</button></form></section>
{% if utilisateurs %}{% for utilisateur, niveau in utilisateurs %}<section class="card user"><div><div class="email">{{ utilisateur.email }}</div><span class="badge">{{ niveau }}</span>{% if utilisateur.acces_manuel %}<div class="manual">Accès manuel actif</div>{% endif %}</div>
<form class="actions" method="post"><input type="hidden" name="csrf_token" value="{{ csrf_token }}"><input type="hidden" name="user_id" value="{{ utilisateur.id }}"><input type="hidden" name="q" value="{{ recherche }}"><select name="palier" aria-label="Nouveau palier"><option value="free" {{ 'selected' if niveau == 'free' }}>Free</option><option value="pro" {{ 'selected' if niveau == 'pro' }}>Pro</option><option value="prime" {{ 'selected' if niveau == 'prime' }}>Prime</option></select><button name="action" value="appliquer">Appliquer</button></form>
{% if utilisateur.acces_manuel %}<form method="post"><input type="hidden" name="csrf_token" value="{{ csrf_token }}"><input type="hidden" name="user_id" value="{{ utilisateur.id }}"><input type="hidden" name="q" value="{{ recherche }}"><button name="action" value="retirer">Retirer l'accès manuel</button></form>{% endif %}</section>{% endfor %}
{% elif recherche %}<section class="card empty">Aucun compte trouvé pour cette adresse.</section>{% endif %}
</main></body></html>
"""

ADMIN_NAVIGATION = (
    ("dashboard", "Dashboard", "M3 3h7v7H3zM14 3h7v5h-7zM14 12h7v9h-7zM3 14h7v7H3z"),
    ("users", "Utilisateurs", "M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2M9 11a4 4 0 1 0 0-8 4 4 0 0 0 0 8M20 8v6M23 11h-6"),
    ("conversations", "Conversations", "M21 11.5a8.4 8.4 0 0 1-.9 3.8 8.5 8.5 0 0 1-7.6 4.7 8.4 8.4 0 0 1-3.8-.9L3 21l1.9-5.7a8.4 8.4 0 0 1-.9-3.8 8.5 8.5 0 0 1 4.7-7.6 8.4 8.4 0 0 1 3.8-.9h.5a8.5 8.5 0 0 1 8 8z"),
    ("memory", "Mémoire", "M12 2a7 7 0 0 0-4 12.7V18h8v-3.3A7 7 0 0 0 12 2zM9 22h6M9 18h6"),
    ("activity", "Activité", "M3 12h4l3-9 4 18 3-9h4"),
    ("statistics", "Statistiques", "M4 19V5M4 19h17M8 15l3-4 3 2 5-7"),
    ("system", "Système", "M12 8v4l3 2M21 12a9 9 0 1 1-18 0 9 9 0 0 1 18 0z"),
    ("security", "Sécurité", "M12 22s8-4 8-11V5l-8-3-8 3v6c0 7 8 11 8 11zM9 12l2 2 4-4"),
    ("backups", "Sauvegardes", "M4 4v6h6M5.6 15a7 7 0 1 0 .4-7M12 7v5l3 2"),
    ("settings", "Paramètres", "M12 8a4 4 0 1 0 0 8 4 4 0 0 0 0-8zM19 13a7 7 0 0 0-.2-1.5l1.3-1-1.5-2.6-1.6.6a7 7 0 0 0-2.6-1.5L14 5h-3l-.4 2a7 7 0 0 0-2.6 1.5l-1.6-.6-1.5 2.6 1.3 1A7 7 0 0 0 6 13l-1.3 1 1.5 2.6 1.6-.6a7 7 0 0 0 2.6 1.5l.4 2h3l.4-2a7 7 0 0 0 2.6-1.5l1.6.6 1.5-2.6z"),
)
ADMIN_TITLES = {code: label for code, label, _ in ADMIN_NAVIGATION}


def _journaliser_action_admin(db, admin_user_id, action, section="", target_user_id=None, details=""):
    db.add(AdminAuditLog(
        admin_user_id=admin_user_id,
        action=action[:40],
        section=section[:40],
        target_user_id=target_user_id,
        details=details[:200],
    ))


@app.route("/admin", methods=["GET", "POST"])
def admin():
    user_id = session.get("user_id")
    recherche = request.values.get("q", "").strip()[:254]
    section = request.args.get("section", "dashboard")
    if section not in ADMIN_TITLES:
        section = "dashboard"
    try:
        page = max(1, int(request.values.get("page", "1")))
    except (TypeError, ValueError):
        page = 1
    page_size = 25
    with session_base() as db:
        administrateur = db.get(User, user_id) if user_id else None
        if not administrateur or administrateur.email.strip().lower() not in emails_owner():
            motif = "session absente" if not user_id else "compte de session introuvable" if not administrateur else "compte non présent dans OWNER_EMAILS"
            app.logger.warning("Accès /admin refusé (404) : %s", motif)
            return "Not Found", 404
        if request.method == "GET":
            _journaliser_action_admin(db, user_id, "consultation", section)

    if request.method == "POST":
        try:
            cible_id = int(request.form.get("user_id", ""))
        except (TypeError, ValueError):
            return "Not Found", 404
        action = request.form.get("action")
        with session_base() as db:
            cible = db.get(User, cible_id)
            if not cible:
                return "Not Found", 404
            if action == "appliquer":
                palier = request.form.get("palier", "")
                if palier not in {"free", "pro", "prime"}:
                    return "Choix de palier invalide", 400
                cible.palier = palier
                cible.acces_manuel = True
                action_journal = "modification_forfait"
                detail_action = f"palier manuel défini: {palier}"
            elif action == "retirer":
                cible.palier = None
                cible.acces_manuel = False
                action_journal = "retrait_acces_manuel"
                detail_action = "accès manuel retiré"
            elif action == "suspendre":
                if cible.id == user_id or cible.email.strip().lower() in emails_owner():
                    return "Un compte propriétaire ne peut pas être suspendu ici.", 400
                cible.is_active = False
                action_journal = "suspension_compte"
                detail_action = "compte suspendu"
            elif action == "reactiver":
                cible.is_active = True
                action_journal = "reactivation_compte"
                detail_action = "compte réactivé"
            else:
                return "Action invalide", 400
            _journaliser_action_admin(db, user_id, action_journal, "users", cible.id, detail_action)
        return redirect(url_for("admin", section="users", q=recherche, page=request.form.get("page", 1)))

    with session_base() as db:
        maintenant = datetime.utcnow()
        requete = db.query(User)
        if recherche:
            requete = requete.filter(User.email.ilike(f"%{recherche}%"))
        total_utilisateurs = requete.count()
        pages = max(1, (total_utilisateurs + page_size - 1) // page_size)
        page = min(page, pages)
        comptes = requete.order_by(User.email).offset((page - 1) * page_size).limit(page_size).all()
        activite_comptes = {
            uid: (nombre, derniere)
            for uid, nombre, derniere in db.query(
                Conversation.user_id,
                func.count(Conversation.id),
                func.max(Conversation.updated_at),
            ).group_by(Conversation.user_id).all()
        }
        utilisateurs = []
        for compte in comptes:
            nb_conv, derniere = activite_comptes.get(compte.id, (0, None))
            utilisateurs.append({
                "id": compte.id,
                "email": compte.email,
                "created": compte.created_at.strftime("%d/%m/%Y") if compte.created_at else "—",
                "conversations": nb_conv,
                "activite": derniere.strftime("%d/%m/%Y") if derniere else "Aucune",
                "niveau": niveau_abonnement(compte),
                "manuel": compte.acces_manuel,
                "active": compte.is_active,
                "owner": compte.email.strip().lower() in emails_owner(),
            })

        nb_conversations = db.query(Conversation).count()
        nb_messages = db.query(Message).count()
        nb_memoires = db.query(UserMemory).count()
        nb_utilisateurs_memoire = db.query(UserMemory.user_id).distinct().count()
        nouveaux_7j = db.query(User).filter(User.created_at >= maintenant - timedelta(days=7)).count()
        actifs_30j = db.query(Conversation.user_id).filter(
            Conversation.updated_at >= maintenant - timedelta(days=30)
        ).distinct().count()
        comptages_messages = dict(
            db.query(Message.conversation_id, func.count(Message.id))
            .group_by(Message.conversation_id).all()
        )
        lignes_conversations = db.query(Conversation, User.email).join(
            User, Conversation.user_id == User.id
        ).order_by(Conversation.updated_at.desc()).limit(100).all()
        conversations = [{
            "email": email,
            "titre": conv.title,
            "creee": conv.created_at.strftime("%d/%m/%Y %H:%M") if conv.created_at else "—",
            "activite": conv.updated_at.strftime("%d/%m/%Y %H:%M") if conv.updated_at else "—",
            "messages": comptages_messages.get(conv.id, 0),
            "archivee": conv.archivee,
        } for conv, email in lignes_conversations]
        journal = []
        if section == "activity":
            evenements = db.query(AdminAuditLog, User.email).outerjoin(
                User, AdminAuditLog.admin_user_id == User.id
            ).order_by(AdminAuditLog.created_at.desc()).limit(100).all()
            for evenement, auteur in evenements:
                compte_cible = db.get(User, evenement.target_user_id) if evenement.target_user_id else None
                journal.append({
                    "date": evenement.created_at.strftime("%d/%m/%Y %H:%M:%S") if evenement.created_at else "—",
                    "admin": auteur or "Compte supprimé",
                    "action": evenement.action,
                    "section": evenement.section,
                    "target": compte_cible.email if compte_cible else "",
                    "details": evenement.details,
                })
        backend = db.bind.dialect.name
        try:
            db.execute(text("SELECT 1"))
            base_ok = True
        except Exception:
            db.rollback()
            base_ok = False

    regles = {rule.endpoint for rule in app.url_map.iter_rules()}
    etats = [
        ("Base de données", "Opérationnel" if base_ok else "Erreur", "ok" if base_ok else "warn", f"Connexion SQL testée · {backend}"),
        ("API IA", "Configurée" if os.environ.get("GEMINI_API_KEY") else "Absente", "ok" if os.environ.get("GEMINI_API_KEY") else "warn", "Présence de configuration seulement; aucun appel facturé"),
        ("SSE", "Route présente" if "repondre_flux" in regles else "Absente", "ok" if "repondre_flux" in regles else "warn", "Le navigateur et le réseau ne sont pas testés ici"),
        ("Vocal", "Navigateur", "unknown", "Dépend des permissions et capacités du navigateur client"),
        ("Authentification", "Routes présentes" if {"connexion", "inscription", "deconnexion"}.issubset(regles) else "À vérifier", "ok" if {"connexion", "inscription", "deconnexion"}.issubset(regles) else "warn", "Présence des routes; aucun scénario utilisateur simulé"),
        ("Sessions", "Protégées" if app.config.get("SESSION_COOKIE_HTTPONLY") and app.config.get("SESSION_COOKIE_SAMESITE") else "À vérifier", "ok" if app.config.get("SESSION_COOKIE_HTTPONLY") and app.config.get("SESSION_COOKIE_SAMESITE") else "warn", "HttpOnly et SameSite vérifiés dans la configuration Flask"),
        ("Mémoire", "Table accessible" if base_ok else "Indisponible", "ok" if base_ok else "warn", "Valeurs privées non consultées"),
        ("Sitemap / robots", "Routes présentes" if {"sitemap_xml", "robots_txt"}.issubset(regles) else "À vérifier", "ok" if {"sitemap_xml", "robots_txt"}.issubset(regles) else "warn", "Enregistrement Flask vérifié"),
    ]
    securite = [
        ("Contrôle propriétaire côté serveur", "Actif", "ok", "Vérification OWNER_EMAILS avant rendu"),
        ("Protection CSRF", "Configurée", "ok", "Contrôle global des POST authentifiés"),
        ("Cookie HttpOnly", "Actif" if app.config.get("SESSION_COOKIE_HTTPONLY") else "Inactif", "ok" if app.config.get("SESSION_COOKIE_HTTPONLY") else "warn", "Valeur de configuration Flask"),
        ("Cookie Secure", "Actif" if app.config.get("SESSION_COOKIE_SECURE") else "Inactif", "ok" if app.config.get("SESSION_COOKIE_SECURE") else "warn", "Doit être actif en HTTPS de production"),
    ]
    configuration = []
    for nom in ("DATABASE_URL", "GEMINI_API_KEY", "OWNER_EMAILS", "FLASK_SECRET_KEY", "PAYDUNYA_MASTER_KEY", "PAYDUNYA_PRIVATE_KEY", "PAYDUNYA_TOKEN", "STRIPE_SECRET_KEY", "OPENWEATHER_API_KEY"):
        present = bool(os.environ.get(nom))
        configuration.append((nom, "Configurée" if present else "Absente", "ok" if present else "warn", "Valeur masquée"))
    cartes = [
        ("Utilisateurs inscrits", total_utilisateurs, "Comptes dans la base"),
        ("Comptes récents", nouveaux_7j, "Créés sur les 7 derniers jours"),
        ("Actifs estimés", actifs_30j, "Avec conversation mise à jour en 30 jours"),
        ("Conversations", nb_conversations, "Lignes enregistrées"),
        ("Messages", nb_messages, "Lignes enregistrées"),
        ("Souvenirs", nb_memoires, "Valeurs privées non exposées"),
    ]
    memoire_cartes = [
        ("Souvenirs enregistrés", nb_memoires, "Comptage SQL réel"),
        ("Comptes concernés", nb_utilisateurs_memoire, "Identifiants distincts"),
        ("Synchronisation", "En ligne" if base_ok else "Indisponible", "État de la connexion SQL"),
    ]
    return render_template(
        "admin.html",
        section=section,
        titres=ADMIN_TITLES,
        navigation=ADMIN_NAVIGATION,
        admin_email=administrateur.email,
        utilisateurs=utilisateurs,
        total_utilisateurs=total_utilisateurs,
        pages=pages,
        page=page,
        page_size=page_size,
        recherche=recherche,
        cartes=cartes,
        memoire_cartes=memoire_cartes,
        conversations=conversations,
        journal=journal,
        etats=etats,
        securite=securite,
        configuration=configuration,
        csrf_token=jeton_csrf(),
    )


def _paydunya_config():
    """Retourne la configuration PayDunya sans exposer les secrets."""
    mode = os.environ.get("PAYDUNYA_MODE", "test").strip().lower()
    if mode not in {"test", "live"}:
        mode = "test"
    master_key = os.environ.get("PAYDUNYA_MASTER_KEY", "").strip()
    private_key = os.environ.get("PAYDUNYA_PRIVATE_KEY", "").strip()
    token = os.environ.get("PAYDUNYA_TOKEN", "").strip()
    base_url = (
        "https://app.paydunya.com/api/v1"
        if mode == "live"
        else "https://app.paydunya.com/sandbox-api/v1"
    )
    return mode, master_key, private_key, token, base_url


def _confirmer_paydunya_token(invoice_token, expected_reference=None):
    """Confirme une facture PayDunya et applique l'abonnement seulement si elle est payée."""
    mode, master_key, private_key, api_token, base_url = _paydunya_config()
    if not all((master_key, private_key, api_token, invoice_token)):
        return False, "PayDunya n’est pas encore configuré sur le serveur.", 503

    try:
        response = requests.get(
            f"{base_url}/checkout-invoice/confirm/{invoice_token}",
            headers={
                "Content-Type": "application/json",
                "PAYDUNYA-MASTER-KEY": master_key,
                "PAYDUNYA-PRIVATE-KEY": private_key,
                "PAYDUNYA-TOKEN": api_token,
                "User-Agent": "DASHLE/1.0",
                "Accept": "application/json",
            },
            timeout=15,
        )
        body = response.json()
    except (requests.RequestException, ValueError):
        return False, "Impossible de vérifier le paiement PayDunya.", 502

    if not response.ok or body.get("response_code") != "00":
        return False, "Le paiement PayDunya n’a pas pu être vérifié.", 502

    invoice = body.get("invoice") or {}
    status = str(body.get("status") or invoice.get("status") or "").lower()
    custom_data = body.get("custom_data") or {}
    reference = str(custom_data.get("dashle_reference") or expected_reference or "").strip()
    if status != "completed" or not reference:
        return False, "Le paiement PayDunya n’est pas confirmé.", 200

    received_amount = invoice.get("total_amount")
    try:
        received_amount = int(float(received_amount))
    except (TypeError, ValueError):
        return False, "Montant PayDunya invalide.", 200

    maintenant = datetime.utcnow()
    with session_base() as db:
        payment = db.query(SubscriptionPayment).filter_by(
            reference=reference, provider="paydunya"
        ).with_for_update().one_or_none()
        if not payment:
            return False, "Paiement Dashle introuvable.", 404
        if received_amount != payment.amount or payment.currency != "XOF":
            return False, "Montant du paiement PayDunya invalide.", 200
        if payment.status == "paid":
            return True, "Paiement déjà confirmé.", 200
        user = db.get(User, payment.user_id)
        if not user:
            return False, "Utilisateur Dashle introuvable.", 404
        depart = user.subscription_expires_at
        if not depart or depart < maintenant:
            depart = maintenant
        user.subscription_level = payment.tier
        user.subscription_provider = "paydunya"
        user.provider_subscription_id = str(invoice_token)
        user.subscription_expires_at = _date_apres_mois(
            depart, 1 if payment.cadence == "monthly" else 12
        )
        payment.provider_reference = str(invoice_token)
        payment.status = "paid"
        payment.paid_at = maintenant
    return True, "Paiement confirmé.", 200


def _hash_paydunya_valide(data, master_key):
    """Vérifie le hash SHA-512 envoyé par PayDunya dans son callback."""
    received_hash = str(data.get("hash", "")).strip().lower()
    if not received_hash or not master_key:
        return False
    expected_hash = hashlib.sha512(master_key.encode("utf-8")).hexdigest().lower()
    return hmac.compare_digest(received_hash, expected_hash)


@app.route("/paiement/paydunya/callback", methods=["POST"])
def paydunya_callback():
    mode, master_key, _, _, _ = _paydunya_config()
    if not master_key:
        return jsonify({"erreur": "PayDunya non configuré."}), 503

    raw_data = request.form.get("data")
    if raw_data:
        try:
            data = json.loads(raw_data) if isinstance(raw_data, str) else raw_data
        except (TypeError, ValueError):
            return jsonify({"ok": False}), 400
    else:
        payload = request.get_json(silent=True) or {}
        data = payload.get("data", payload)

    if not isinstance(data, dict) or not _hash_paydunya_valide(data, master_key):
        return jsonify({"ok": False}), 400

    invoice = data.get("invoice") or {}
    token = str(data.get("token") or invoice.get("token") or "").strip()
    status = str(data.get("status") or "").lower()
    custom_data = data.get("custom_data") or {}
    reference = str(custom_data.get("dashle_reference") or "").strip()

    if status in {"cancelled", "failed"}:
        if reference:
            with session_base() as db:
                payment = db.query(SubscriptionPayment).filter_by(
                    reference=reference, provider="paydunya"
                ).one_or_none()
                if payment and payment.status == "pending":
                    payment.status = "cancelled" if status == "cancelled" else "failed"
        return jsonify({"ok": True})

    if status != "completed" or not token:
        return jsonify({"ok": True})

    ok, _, code = _confirmer_paydunya_token(token, expected_reference=reference)
    return jsonify({"ok": ok}), 200 if ok else code


@app.route("/paiement/initier", methods=["POST"])
def initier_paiement():
    user_id = session.get("user_id")
    if not user_id:
        return redirect(url_for("connexion", next=url_for("tarifs")))

    tier = request.form.get("tier", "")
    cadence = request.form.get("cadence", "monthly")
    provider = request.form.get("provider", "")
    if tier not in OFFRES_ABONNEMENT or cadence not in {"monthly", "annual"}:
        return _rendre_tarifs("Choix d’offre invalide."), 400
    offre = OFFRES_ABONNEMENT[tier]
    amount = offre["mensuel"] if cadence == "monthly" else offre["annuel"]
    currency = "XOF"
    with session_base() as db:
        user = db.get(User, user_id)
        if not user or user.pays not in PAYS_CODES or not user.telephone:
            return _rendre_tarifs("Complète ton pays et ton numéro de téléphone dans Paramètres avant de payer."), 400
        pays_client = user.pays
        moyens_autorises = _moyens_paiement_pays(pays_client)
    if provider not in moyens_autorises:
        return _rendre_tarifs("Ce moyen de paiement n'est pas disponible pour ton pays."), 400
    if currency not in {"XOF", "XAF"}:
        return _rendre_tarifs("La devise de paiement doit être XOF ou XAF."), 503
    stripe_currency = os.environ.get("STRIPE_CURRENCY", "XOF").upper()
    if stripe_currency not in {"XOF", "XAF"}:
        return _rendre_tarifs("La devise de paiement doit être XOF ou XAF."), 503
    reference = "DASHLE-" + secrets.token_hex(16)

    with session_base() as db:
        user = db.get(User, user_id)
        if not user:
            session.clear()
            return redirect(url_for("connexion"))
        payment = SubscriptionPayment(
            user_id=user_id, reference=reference, provider=provider,
            tier=tier, cadence=cadence, amount=amount, currency="XOF",
            pays=pays_client, moyen_paiement=provider,
        )
        db.add(payment)

    if provider == "paydunya":
        mode, master_key, private_key, api_token, base_url = _paydunya_config()
        if mode != "test" and mode != "live":
            return _rendre_tarifs("Mode PayDunya invalide."), 503
        if not all((master_key, private_key, api_token)):
            with session_base() as db:
                payment = db.query(SubscriptionPayment).filter_by(reference=reference).one_or_none()
                if payment:
                    payment.status = "failed"
            return _rendre_tarifs("PayDunya n’est pas encore configuré sur le serveur."), 503

        with session_base() as db:
            user = db.get(User, user_id)
            nom = user.nom or ""
            email = user.email

        payload = {
            "invoice": {
                "total_amount": amount,
                "description": f"Abonnement {offre['nom']} {cadence}",
                "customer": {"name": nom, "email": email, "phone": user.telephone},
                "channels": ["card"],
            },
            "store": {
                "name": os.environ.get("PAYDUNYA_STORE_NAME", "Dashle"),
                "website_url": request.host_url.rstrip("/"),
            },
            "custom_data": {
                "dashle_reference": reference,
                "dashle_user_id": str(user_id),
                "dashle_tier": tier,
                "dashle_cadence": cadence,
                "dashle_country": pays_client,
                "dashle_payment_method": provider,
            },
            "actions": {
                "cancel_url": url_for("tarifs", _external=True),
                "return_url": url_for("paiement_retour", _external=True),
                "callback_url": url_for("paydunya_callback", _external=True),
            },
        }
        try:
            response = requests.post(
                f"{base_url}/checkout-invoice/create",
                json=payload,
                headers={
                    "Content-Type": "application/json",
                    "PAYDUNYA-MASTER-KEY": master_key,
                    "PAYDUNYA-PRIVATE-KEY": private_key,
                    "PAYDUNYA-TOKEN": api_token,
                    "User-Agent": "DASHLE/1.0",
                    "Accept": "application/json",
                },
                timeout=20,
            )
            body = response.json()
            payment_url = body.get("response_text")
            invoice_token = body.get("token")
            if response.ok and body.get("response_code") == "00" and payment_url and invoice_token:
                with session_base() as db:
                    payment = db.query(SubscriptionPayment).filter_by(reference=reference).one()
                    payment.provider_reference = invoice_token
                return redirect(payment_url, code=303)
        except (requests.RequestException, ValueError, KeyError):
            pass
        with session_base() as db:
            payment = db.query(SubscriptionPayment).filter_by(reference=reference).one_or_none()
            if payment:
                payment.status = "failed"
        return _rendre_tarifs("Impossible de créer le paiement PayDunya. Réessaie plus tard."), 502

        return _rendre_tarifs("Impossible de créer le paiement CinetPay. Réessaie plus tard."), 502

    if provider == "cinetpay":
        api_key = os.environ.get("CINETPAY_API_KEY")
        site_id = os.environ.get("CINETPAY_SITE_ID")
        if not api_key or not site_id:
            return _rendre_tarifs("CinetPay n’est pas encore configuré sur le serveur."), 503
        with session_base() as db:
            user = db.get(User, user_id)
            email = user.email
            telephone = user.telephone
        payload = {
            "apikey": api_key,
            "site_id": site_id,
            "transaction_id": reference,
            "amount": amount,
            "currency": currency,
            "description": f"Abonnement {offre['nom']} {cadence}",
            "return_url": url_for("paiement_retour", _external=True) + "?retour=1",
            "notify_url": url_for("cinetpay_notification", _external=True),
            "channels": "ALL",
            "lang": "fr",
            "customer_id": str(user_id),
            "customer_email": email,
            "customer_phone_number": telephone,
            "metadata": f"{user_id}:{tier}:{cadence}:{pays_client}:{provider}",
        }
        try:
            response = requests.post(
                "https://api-checkout.cinetpay.com/v2/payment",
                json=payload,
                headers={"User-Agent": "DASHLE/1.0", "Accept": "application/json"},
                timeout=20,
            )
            body = response.json()
            if response.ok and body.get("code") == "201":
                with session_base() as db:
                    payment = db.query(SubscriptionPayment).filter_by(reference=reference).one()
                    payment.provider_reference = body.get("data", {}).get("payment_token")
                return redirect(body["data"]["payment_url"], code=303)
        except (requests.RequestException, ValueError, KeyError):
            pass
        with session_base() as db:
            payment = db.query(SubscriptionPayment).filter_by(reference=reference).one_or_none()
            if payment:
                payment.status = "failed"
        return _rendre_tarifs("Impossible de créer le paiement CinetPay. Réessaie plus tard."), 502

    if provider == "stripe":
        api_key = os.environ.get("STRIPE_SECRET_KEY")
        if not api_key:
            return _rendre_tarifs("Stripe n’est pas encore configuré sur le serveur."), 503
        interval = "month" if cadence == "monthly" else "year"
        form = {
            "mode": "subscription",
            "line_items[0][price_data][currency]": stripe_currency.lower(),
            "line_items[0][price_data][unit_amount]": amount,
            "line_items[0][price_data][product_data][name]": offre["nom"],
            "line_items[0][price_data][recurring][interval]": interval,
            "client_reference_id": str(user_id),
            "metadata[dashle_reference]": reference,
            "metadata[dashle_tier]": tier,
            "metadata[dashle_cadence]": cadence,
            "metadata[dashle_country]": pays_client,
            "metadata[dashle_payment_method]": provider,
            "subscription_data[metadata][dashle_reference]": reference,
            "subscription_data[metadata][dashle_tier]": tier,
            "subscription_data[metadata][dashle_cadence]": cadence,
            "subscription_data[metadata][dashle_user_id]": str(user_id),
            "subscription_data[metadata][dashle_country]": pays_client,
            "success_url": url_for("paiement_retour", _external=True) + "?retour=1&session_id={CHECKOUT_SESSION_ID}",
            "cancel_url": url_for("tarifs", _external=True),
        }
        try:
            response = requests.post(
                "https://api.stripe.com/v1/checkout/sessions",
                data=form,
                auth=(api_key, ""),
                timeout=20,
            )
            body = response.json()
            if response.ok and body.get("url"):
                with session_base() as db:
                    payment = db.query(SubscriptionPayment).filter_by(reference=reference).one()
                    payment.provider_reference = body.get("id")
                return redirect(body["url"], code=303)
        except (requests.RequestException, ValueError, KeyError):
            pass
        with session_base() as db:
            payment = db.query(SubscriptionPayment).filter_by(reference=reference).one_or_none()
            if payment:
                payment.status = "failed"
        return _rendre_tarifs("Impossible de créer la session Stripe. Vérifie la configuration et réessaie."), 502

    with session_base() as db:
        payment = db.query(SubscriptionPayment).filter_by(reference=reference).one_or_none()
        if payment:
            payment.status = "failed"
    return _rendre_tarifs("Mode de paiement inconnu."), 400


@app.route("/paiement/cinetpay/notification", methods=["GET", "POST"])
def cinetpay_notification():
    if request.method == "GET":
        return jsonify({"ok": True})
    api_key = os.environ.get("CINETPAY_API_KEY")
    site_id = os.environ.get("CINETPAY_SITE_ID")
    if not api_key or not site_id:
        return jsonify({"erreur": "CinetPay non configuré."}), 503
    event = request.get_json(silent=True) or request.form
    reference = str(event.get("cpm_trans_id", event.get("transaction_id", "")))
    if not reference or (event.get("cpm_site_id") and str(event.get("cpm_site_id")) != site_id):
        return jsonify({"ok": True})
    with session_base() as db:
        payment = db.query(SubscriptionPayment).filter_by(
            reference=reference, provider="cinetpay"
        ).one_or_none()
        if not payment or payment.status == "paid":
            return jsonify({"ok": True})
        expected_amount, expected_currency = payment.amount, payment.currency
    try:
        response = requests.post(
            "https://api-checkout.cinetpay.com/v2/payment/check",
            json={"apikey": api_key, "site_id": site_id, "transaction_id": reference},
            headers={"User-Agent": "DASHLE/1.0", "Accept": "application/json"},
            timeout=15,
        )
        body = response.json()
        data = body.get("data", {})
        if not response.ok or body.get("code") != "00" or data.get("status") != "ACCEPTED":
            return jsonify({"ok": True})
        if int(data.get("amount", -1)) != expected_amount or str(data.get("currency", "")).upper() != expected_currency:
            return jsonify({"ok": True})
    except (requests.RequestException, ValueError, TypeError):
        return jsonify({"ok": True})

    maintenant = datetime.utcnow()
    with session_base() as db:
        payment = db.query(SubscriptionPayment).filter_by(reference=reference).with_for_update().one_or_none()
        if not payment or payment.status == "paid":
            return jsonify({"ok": True})
        user = db.get(User, payment.user_id)
        if user:
            depart = user.subscription_expires_at
            if not depart or depart < maintenant:
                depart = maintenant
            user.subscription_level = payment.tier
            user.subscription_provider = "cinetpay"
            user.provider_subscription_id = None
            user.subscription_expires_at = _date_apres_mois(depart, 1 if payment.cadence == "monthly" else 12)
            payment.status = "paid"
            payment.paid_at = maintenant
    return jsonify({"ok": True})


@app.route("/paiement/stripe/webhook", methods=["POST"])
def stripe_webhook():
    secret = os.environ.get("STRIPE_WEBHOOK_SECRET")
    signature = request.headers.get("Stripe-Signature", "")
    if not secret:
        return jsonify({"erreur": "Webhook Stripe non configuré."}), 503
    try:
        parties = dict(element.split("=", 1) for element in signature.split(",") if "=" in element)
        timestamp = int(parties["t"])
        signature_attendue = hmac.new(
            secret.encode(), str(timestamp).encode() + b"." + request.get_data(), hashlib.sha256
        ).hexdigest()
        if abs(datetime.utcnow().timestamp() - timestamp) > 300 or not hmac.compare_digest(signature_attendue, parties["v1"]):
            return jsonify({"erreur": "Signature invalide."}), 400
        event = request.get_json(force=True)
    except (KeyError, ValueError, TypeError):
        return jsonify({"erreur": "Événement Stripe invalide."}), 400

    event_type = event.get("type", "")
    subscription = (event.get("data") or {}).get("object") or {}
    if event_type.startswith("customer.subscription."):
        subscription_id = subscription.get("id")
        metadata = subscription.get("metadata") or {}
        reference = metadata.get("dashle_reference")
        with session_base() as db:
            payment = None
            if reference:
                payment = db.query(SubscriptionPayment).filter_by(
                    reference=reference, provider="stripe"
                ).with_for_update().one_or_none()
            if payment is None and subscription_id:
                payment = db.query(SubscriptionPayment).filter_by(
                    provider="stripe", provider_reference=subscription_id
                ).with_for_update().order_by(SubscriptionPayment.created_at.desc()).first()
            if payment:
                user = db.get(User, payment.user_id)
                status = subscription.get("status")
                if user and status in {"active", "trialing"}:
                    periode_fin = subscription.get("current_period_end")
                    user.subscription_level = payment.tier
                    user.subscription_provider = "stripe"
                    user.provider_subscription_id = subscription_id
                    user.subscription_expires_at = datetime.utcfromtimestamp(periode_fin) if periode_fin else _date_apres_mois(
                        datetime.utcnow(), 1 if payment.cadence == "monthly" else 12
                    )
                    payment.provider_reference = subscription_id
                    payment.status = "paid"
                    payment.paid_at = payment.paid_at or datetime.utcnow()
                elif user and (status in {"canceled", "incomplete_expired"}) \
                        and user.subscription_provider == "stripe" \
                        and user.provider_subscription_id == subscription_id:
                    user.subscription_level = "free"
                    user.subscription_expires_at = None
                    user.subscription_provider = None
                    user.provider_subscription_id = None
                    payment.status = "cancelled"
    return jsonify({"received": True})

@app.route("/inscription", methods=["GET", "POST"])
def inscription():
    erreur = None
    if request.method == "POST":
        email    = request.form.get("email", "").strip().lower()
        nom      = request.form.get("nom", "").strip()
        password = request.form.get("password", "")
        pays     = request.form.get("pays", "").strip().upper()
        telephone_saisi = request.form.get("telephone", "").strip()
        indicatif_recu = request.form.get("indicatif", "").strip()
        app.logger.info(
            "inscription POST: pays=%s indicatif=%s numero_length=%d",
            pays or "<vide>",
            indicatif_recu or "<vide>",
            len(telephone_saisi),
        )
        telephone, telephone_national = _normaliser_telephone(pays, telephone_saisi)
        if not nom or len(nom) > 160:
            erreur = "Indique ton nom (160 caractères maximum)."
        elif "@" not in email or len(email) > 254:
            erreur = "Indique une adresse e-mail valide."
        elif pays not in PAYS_CODES:
            erreur = "Sélectionne ton pays."
        elif not telephone:
            erreur = "Indique un numéro de téléphone valide pour le pays sélectionné."
        elif len(password) < 8:
            erreur = "Le mot de passe doit contenir au moins 8 caractères."
        else:
            try:
                with session_base() as db:
                    user = User(
                        email=email,
                        nom=nom,
                        pays=pays,
                        telephone=telephone,
                        telephone_national=telephone_national,
                        password_hash=generate_password_hash(password),
                    )
                    db.add(user)
                    db.flush()
                    # Proposer le transfert de la conversation visiteur
                    hist_visiteur = list(_historique_visiteur())
                    session.clear()
                    session.permanent = True
                    session["user_id"]    = user.id
                    session["user_email"] = user.email
                    if hist_visiteur:
                        session["transfert_en_attente"] = hist_visiteur
                return redirect(url_for("accueil"))
            except IntegrityError:
                erreur = "Cette adresse e-mail est déjà utilisée."

    return render_template_string(
        AUTH_PAGE,
        titre="Créer un compte",
        action="S'inscrire",
        erreur=erreur,
        lien="connexion",
        texte_lien="Déjà un compte ?",
        libelle_lien="Se connecter",
        autocomplete="new-password",
        afficher_nom=True,
        afficher_pays=True,
        pays_profil=PAYS_PROFIL,
        csrf_token=jeton_csrf(),
        inscription_email=email if request.method == "POST" else "",
        inscription_nom=nom if request.method == "POST" else "",
        inscription_pays=pays if request.method == "POST" else "",
        inscription_indicatif=indicatif_recu if request.method == "POST" else "",
        inscription_telephone=telephone_saisi if request.method == "POST" else "",
    )


@app.route("/connexion", methods=["GET", "POST"])
def connexion():
    erreur = None
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        with session_base() as db:
            user = db.query(User).filter_by(email=email).one_or_none()
            if user and user.is_active and check_password_hash(user.password_hash, request.form.get("password", "")):
                if user.email.strip().lower() in emails_owner():
                    _journaliser_action_admin(db, user.id, "connexion_admin")
                hist_visiteur = list(_historique_visiteur())
                session.clear()
                session.permanent = True
                session["user_id"]    = user.id
                session["user_email"] = user.email
                # Mémoriser l'historique visiteur pour proposer le transfert
                if hist_visiteur:
                    session["transfert_en_attente"] = hist_visiteur
                return redirect(url_for("accueil"))
        erreur = "Adresse e-mail ou mot de passe incorrect."

    return render_template_string(
        AUTH_PAGE,
        titre="Connexion",
        action="Se connecter",
        erreur=erreur,
        lien="inscription",
        texte_lien="Pas encore de compte ?",
        libelle_lien="S'inscrire",
        autocomplete="current-password",
        afficher_nom=False,
        csrf_token=jeton_csrf(),
    )


@app.route("/transferer_conversation", methods=["POST"])
def transferer_conversation():
    """Transfère la conversation visiteur vers le compte connecté.

    Appelé depuis l'accueil si session['transfert_en_attente'] est présent.
    Ne crée des messages que si la conversation courante est vide.
    """
    user_id = session.get("user_id")
    if not user_id:
        return jsonify({"erreur": "Non connecté."}), 401
    if not _conserver_historique(user_id):
        return jsonify({"ok": True, "transfere": 0, "note": "La conservation de l'historique est désactivée."})

    hist = session.pop("transfert_en_attente", None)
    if not hist:
        return jsonify({"ok": True, "transfere": 0})

    conversation_id = _conv_courante(user_id) or _creer_conversation(user_id)
    # Ne transférer que si la conversation cible est vide
    existants = _messages_conversation(user_id, conversation_id)
    if existants:
        return jsonify({"ok": True, "transfere": 0, "note": "Conversation non vide, transfert ignoré."})

    n = 0
    for msg in hist:
        try:
            ajouter_message(user_id, conversation_id, msg["texte"], msg["auteur"])
            n += 1
        except Exception:
            pass

    return jsonify({"ok": True, "transfere": n})


@app.route("/mot-de-passe", methods=["POST"])
def changer_mot_de_passe():
    user_id = session.get("user_id")
    if not user_id:
        return redirect(url_for("connexion"))
    ancien      = request.form.get("ancien_password", "")
    nouveau     = request.form.get("nouveau_password", "")
    confirmation = request.form.get("confirmation_password", "")
    if len(nouveau) < 8 or nouveau != confirmation:
        return redirect(url_for("securite", erreur="Le nouveau mot de passe est invalide."))
    with session_base() as db:
        user = db.query(User).filter_by(id=user_id).one_or_none()
        if user is None or not check_password_hash(user.password_hash, ancien):
            return redirect(url_for("securite", erreur="L'ancien mot de passe est incorrect."))
        user.password_hash = generate_password_hash(nouveau)
    return redirect(url_for("securite", succes="Mot de passe modifié."))


@app.route("/compte/supprimer", methods=["POST"])
def supprimer_compte():
    user_id = session.get("user_id")
    if not user_id:
        return redirect(url_for("connexion"))
    if request.form.get("confirmation", "").strip().lower() != "supprimer":
        return redirect(url_for("securite", erreur="Écris supprimer pour confirmer."))
    with session_base() as db:
        user = db.query(User).filter_by(id=user_id).one_or_none()
        if user is not None:
            db.query(UserMemory).filter_by(user_id=user.id).delete()
            db.query(MessageFeedback).filter_by(user_id=user.id).delete()
            db.query(UserPreference).filter_by(user_id=user.id).delete()
            db.query(ShareLink).filter(
                ShareLink.conversation_id.in_(
                    db.query(Conversation.id).filter_by(user_id=user.id)
                )
            ).delete(synchronize_session=False)
            db.delete(user)
    session.clear()
    return redirect(url_for("inscription"))


@app.route("/deconnexion", methods=["POST"])
def deconnexion():
    session.clear()
    return redirect(url_for("accueil"))


# ---------------------------------------------------------------------------
# Santé
# ---------------------------------------------------------------------------

@app.route("/health")
def health():
    try:
        with session_base() as db:
            db.execute(text("SELECT 1"))
    except Exception as exc:
        app.logger.error("Health check base indisponible : %s", type(exc).__name__)
        return jsonify({"ok": False, "service": "dashle"}), 503
    return jsonify({"ok": True, "service": "dashle"})


# ---------------------------------------------------------------------------
# Point d'entrée
# ---------------------------------------------------------------------------

_routes_demarrage = sorted(str(rule) for rule in app.url_map.iter_rules())
_route_admin_demarrage = next((route for route in _routes_demarrage if "'/admin'" in route), None)
app.logger.info(
    "DASHLE URL map au démarrage (%d routes):\n%s\nDASHLE /admin: %s",
    len(_routes_demarrage),
    "\n".join(_routes_demarrage),
    _route_admin_demarrage or "ABSENTE",
)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
