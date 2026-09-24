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
import secrets
from datetime import datetime
from flask import (
    Flask, Response, request, render_template_string,
    redirect, stream_with_context, url_for, session, jsonify,
)
from werkzeug.security import check_password_hash, generate_password_hash
from sqlalchemy.exc import IntegrityError
from app import streamer_message, traiter_message, traiter_message_image
from brain import resumer_conversation
from database import (
    Conversation, Message, MessageFeedback, ShareLink, User,
    UserMemory, UserPreference, initialiser_base, session_base,
)

try:
    from PIL import Image
    PIL_DISPONIBLE = True
except Exception:
    PIL_DISPONIBLE = False

app = Flask(__name__)
app.config.update(
    SECRET_KEY=os.environ.get("FLASK_SECRET_KEY") or os.urandom(32),
    MAX_CONTENT_LENGTH=8 * 1024 * 1024,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.environ.get("SESSION_COOKIE_SECURE", "1") != "0",
)
initialiser_base()

# Nombre maximal de messages conservés en session pour les visiteurs anonymes.
MAX_HISTORIQUE_VISITEUR = 30


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
    "accueil", "repondre_flux", "repondre", "repondre_image",
    "health",
}


@app.before_request
def exiger_connexion():
    """Autorise les visiteurs sur les routes publiques et de chat.
    Redirige vers /connexion uniquement pour les routes qui nécessitent
    vraiment un compte (paramètres, sécurité, gestion de conversations, etc.).
    """
    if request.endpoint in _ROUTES_PUBLIQUES or "user_id" in session:
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


def _resume_visiteur() -> str:
    return session.get("resume_visiteur", "")


def _maj_resume_visiteur(nouveau_resume: str):
    session["resume_visiteur"] = nouveau_resume
    session.modified = True


# ---------------------------------------------------------------------------
# Helpers utilisateur connecté
# ---------------------------------------------------------------------------

def _conv_courante(user_id):
    """Renvoie l'identifiant d'une conversation active pour l'utilisateur."""
    conversation_id = session.get("conversation_id")
    with session_base() as db:
        conversation = db.query(Conversation).filter_by(
            id=conversation_id, user_id=user_id
        ).one_or_none()
        if conversation is None:
            conversation = Conversation(user_id=user_id)
            db.add(conversation)
            db.flush()
            conversation_id = conversation.id
    session["conversation_id"] = conversation_id
    return conversation_id


def _liste_conversations(user_id):
    with session_base() as db:
        convs = (
            db.query(Conversation)
            .filter_by(user_id=user_id, archivee=False)
            .order_by(Conversation.updated_at.desc())
            .all()
        )
        return [{"id": c.id, "titre": c.title} for c in convs]


def _messages_conversation(user_id, conversation_id):
    with session_base() as db:
        conv = db.query(Conversation).filter_by(
            id=conversation_id, user_id=user_id
        ).one_or_none()
        if conv is None:
            return []
        return [
            {
                "id": m.id,
                "auteur": m.auteur,
                "texte": m.texte,
                "date": m.created_at.isoformat(),
            }
            for m in conv.messages
        ]


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
    historique = _messages_conversation(user_id, conversation_id)
    n = len(historique)
    if n < 20:
        return
    if (n - 20) % 10 != 0:
        return
    resume = _resume_conversation(user_id, conversation_id)
    nouveau = resumer_conversation(historique, resume)
    if nouveau and nouveau != resume:
        with session_base() as db:
            conv = db.query(Conversation).filter_by(
                id=conversation_id, user_id=user_id
            ).one_or_none()
            if conv is not None:
                conv.resume = nouveau


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
    "voix_nom": "",
    "voix_vitesse": 1.0,
    "voix_tonalite": 1.0,
    "voix_volume": 1.0,
}


def ajouter_message(user_id, conversation_id, texte, auteur):
    with session_base() as db:
        conv = db.query(Conversation).filter_by(
            id=conversation_id, user_id=user_id
        ).one_or_none()
        if conv is None:
            raise LookupError("Conversation introuvable")
        msg = Message(conversation_id=conv.id, auteur=auteur, texte=texte)
        db.add(msg)
        db.flush()
        if auteur == "user" and conv.title == "Nouvelle conversation":
            conv.title = texte[:48] or conv.title
        conv.updated_at = datetime.utcnow()
        return msg.id


# ---------------------------------------------------------------------------
# CSS — interface modernisée (identité visuelle Dashle conservée)
# ---------------------------------------------------------------------------

_CSS = """
* { box-sizing: border-box; }

:root {
  --vert: #10A37F;
  --vert-fonce: #087355;
  --vert-clair: #e6f7f1;
  --texte: #17251f;
  --fond: #ffffff;
  --fond-secondaire: #f7fbf9;
  --bordure: #dce7e2;
  --msg-user: #DCF8C6;
  --msg-bot: #f0f0f0;
  --sidebar-bg: #ffffff;
  --header-bg: #10A37F;
  --radius: 16px;
  --transition: 0.18s ease;
}

body.theme-sombre {
  --texte: #e8f5ef;
  --fond: #101816;
  --fond-secondaire: #17231f;
  --bordure: #294238;
  --msg-bot: #1e2e29;
  --sidebar-bg: #17231f;
}

body {
  font-family: 'Segoe UI', system-ui, -apple-system, sans-serif;
  margin: 0;
  background: var(--fond);
  color: var(--texte);
  height: 100vh;
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

header img.logo { height: 28px; border-radius: 50%; }

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

#banniere-visiteur a {
  color: var(--vert-fonce);
  font-weight: 600;
  text-decoration: none;
  white-space: nowrap;
}

#banniere-visiteur a:hover { text-decoration: underline; }

#banniere-visiteur .spacer { flex: 1; }

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
  height: 100%;
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
  font-size: 15px;
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

body.theme-sombre .msg.bot { color: var(--texte); }

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
  color: #6b7c76;
  border-radius: 7px;
  padding: 4px 7px;
  cursor: pointer;
  font-size: 13px;
  transition: background var(--transition), color var(--transition);
}

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
  padding: 10px 12px;
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
  border-color: var(--vert);
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
button.micro.actif { color: var(--vert); background: var(--vert-clair); }

button.vocal {
  background: #3B82F6;
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
  transition: background var(--transition), box-shadow var(--transition);
  flex-shrink: 0;
}

button.vocal:hover   { background: #2563eb; }
button.vocal.vocal-on { box-shadow: 0 0 0 2px var(--vert); }

button.vocal.ecoute {
  animation: pulse-vocal 1.1s infinite ease-in-out;
}

button.vocal.parle { background: #1d4ed8; }

@keyframes pulse-vocal {
  0%, 100% { box-shadow: 0 0 0 0   rgba(59,130,246,0.55); }
  50%       { box-shadow: 0 0 0 8px rgba(59,130,246,0);    }
}

button.envoyer {
  background: var(--vert);
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
  transition: background var(--transition), opacity var(--transition), transform var(--transition);
}

button.envoyer:hover:not(:disabled) {
  background: var(--vert-fonce);
  transform: scale(1.05);
}

button.envoyer:disabled { opacity: 0.45; cursor: default; }

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
    #d7fff0 0%, #60e4b4 13%, #10a37f 43%, #087355 72%, #023d31 100%
  );
  box-shadow:
    0 0 22px rgba(101,255,198,.9),
    0 0 72px rgba(16,163,127,.65),
    inset -16px -18px 28px rgba(0,45,34,.48);
  animation: respiration-orbe 3.8s ease-in-out infinite;
}

.orbe-dashle::after {
  content: "";
  position: absolute;
  inset: -14%;
  border: 1px solid rgba(173,255,224,.48);
  border-radius: 50%;
  animation: halo-orbe 2.8s ease-in-out infinite;
}

#mode-vocal[data-etat="ecoute"]    .orbe-dashle { animation-duration: 1.35s; box-shadow: 0 0 30px rgba(135,255,213,.95), 0 0 100px rgba(16,163,127,.8), inset -16px -18px 28px rgba(0,45,34,.48); }
#mode-vocal[data-etat="reflexion"] .orbe-dashle { animation-duration: 1.9s;  filter: hue-rotate(18deg); }
#mode-vocal[data-etat="parle"]     .orbe-dashle { animation-duration: .85s;  box-shadow: 0 0 34px rgba(188,255,224,1), 0 0 120px rgba(16,163,127,.9), inset -16px -18px 28px rgba(0,45,34,.48); }

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
<title>Dashle</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="manifest" href="/static/manifest.json">
<meta name="theme-color" content="#10A37F">
<script>
if ('serviceWorker' in navigator) {
  navigator.serviceWorker.register('/static/service-worker.js');
}
</script>
<style>{{ css }}</style>
</head>
<body class="theme-{{ preferences.theme }}">

<header>
  <button class="icon-btn" onclick="document.getElementById('sidebar').style.display='block';document.getElementById('voile').style.display='block';" aria-label="Menu" title="Menu">&#9776;</button>
  <div class="logo-wrap">
    <img class="logo" src="{{ url_for('static', filename='logo.png') }}" alt="Dashle">
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
  <a href="{{ url_for('connexion') }}">Se connecter</a>
  <span style="margin:0 4px;">·</span>
  <a href="{{ url_for('inscription') }}">Créer un compte</a>
</div>
{% endif %}

<div id="voile" onclick="document.getElementById('sidebar').style.display='none';this.style.display='none';"></div>

<div id="sidebar">
  <h2>Dashle</h2>
  {% if utilisateur %}
    <input class="recherche-conversations" id="recherche-conversations" type="search" placeholder="Rechercher dans l'historique..." aria-label="Rechercher dans l'historique">
    <form action="{{ url_for('nouvelle_conv') }}" method="post" style="margin:0;">
      <input type="hidden" name="csrf_token" value="{{ csrf_token }}">
      <button class="nouvelle" type="submit" style="width:100%;text-align:left;">+ Nouvelle conversation</button>
    </form>
    {% for conv in conversations %}
      <div class="ligne-conversation" data-titre="{{ conv.titre|lower }}" style="display:flex;align-items:center;">
        <a href="{{ url_for('charger_conv', i=conv.id) }}" style="flex:1;">{{ conv.titre }}</a>
        <button type="button" title="Partager" aria-label="Partager" onclick="partagerConversation({{ conv.id }})" style="border:0;background:none;cursor:pointer;padding:8px;">🔗</button>
        <form action="{{ url_for('archiver_conv', i=conv.id) }}" method="post" style="margin:0;"><input type="hidden" name="csrf_token" value="{{ csrf_token }}"><button type="submit" title="Archiver" style="border:0;background:none;cursor:pointer;padding:8px;">🗃</button></form>
        <form action="{{ url_for('supprimer_conv', i=conv.id) }}" method="post" style="margin:0;">
          <input type="hidden" name="csrf_token" value="{{ csrf_token }}">
          <button type="submit" onclick="return confirm('Supprimer cette conversation ?');" aria-label="Supprimer" style="color:#c00;padding:8px 12px;border:0;background:none;cursor:pointer;font-size:20px;">&times;</button>
        </form>
      </div>
    {% endfor %}
  {% else %}
    <div style="padding:16px 18px;font-size:14px;color:#71837b;">
      Connecte-toi pour sauvegarder tes conversations.
    </div>
    <a href="{{ url_for('connexion') }}">→ Se connecter</a>
    <a href="{{ url_for('inscription') }}">✚ Créer un compte</a>
  {% endif %}
  <div class="menu-section">Navigation</div>
  {% if utilisateur %}
    <a href="{{ url_for('parametres') }}">⚙ Paramètres</a>
  {% endif %}
  <a href="#" onclick="return false;" title="Bientôt disponible">📅 Planification <small>(bientôt)</small></a>
  <a href="#" onclick="return false;" title="Bientôt disponible">🔌 Plugins <small>(bientôt)</small></a>
  <a href="#" onclick="return false;" title="Bientôt disponible">📁 Projets <small>(bientôt)</small></a>
</div>

<div id="chat">
  {% for m in messages %}
    <div class="message-wrap {{ 'user' if m.auteur == 'user' else 'bot' }}">
      <div class="msg {{ 'user' if m.auteur == 'user' else 'bot' }}" data-message-id="{{ m.get('id','') }}">{{ m.texte }}</div>
      {% if m.auteur == 'bot' %}
      <div class="actions-reponse">
        <button type="button" class="action-copier" title="Copier">📋</button>
        {% if utilisateur %}
          <button type="button" class="action-feedback" data-valeur="positif" title="J'aime">👍</button>
          <button type="button" class="action-feedback" data-valeur="negatif" title="Je n'aime pas">👎</button>
          <button type="button" class="action-partager" title="Partager">🔗</button>
          <button type="button" class="action-regenerer" title="Régénérer">🔄</button>
        {% endif %}
        <button type="button" class="action-lire" title="Lecture / pause">▶</button>
        <button type="button" class="action-stop" title="Arrêter">⏹</button>
        <span class="lecture-etat"></span>
      </div>
      {% endif %}
    </div>
  {% endfor %}
</div>

<section id="mode-vocal" data-etat="attente" aria-label="Conversation vocale" aria-hidden="true">
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
      <div class="orbe-dashle"></div>
    </div>
    <div class="etat-vocal" id="etat-vocal">En attente</div>
  </div>
</section>

<form class="bas" id="form-message" autocomplete="off" method="post" action="">
  <input type="file" id="image-input" accept="image/*,video/*" style="display:none;">
  <button type="button" id="btn-attach" style="background:none;border:none;cursor:pointer;flex-shrink:0;padding:0;width:34px;height:34px;" title="Joindre une image ou vidéo" aria-label="Joindre un fichier" onclick="document.getElementById('image-input').click();">
    <img src="{{ url_for('static', filename='icon-attach.png') }}" style="width:34px;height:34px;display:block;border-radius:8px;" alt="">
  </button>
  <textarea id="message" name="message" rows="1" placeholder="Écris à Dashle..." aria-label="Message"></textarea>
  <div class="groupe-actions">
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

<div id="apercu-fichier" aria-live="polite">
  <img id="apercu-fichier-media" alt="Aperçu du fichier sélectionné">
  <div id="apercu-fichier-info">
    <span id="apercu-fichier-nom"></span>
    <span id="apercu-fichier-type"></span>
  </div>
  <button type="button" id="retirer-fichier" aria-label="Retirer le fichier" title="Retirer">&times;</button>
</div>

<div id="statut-vocal" aria-live="polite"></div>
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

// CSRF token injecté côté serveur
const csrfToken        = __CSRF_TOKEN__;
const preferencesVocales = __PREFS_VOCALES__;
const estConnecte      = __EST_CONNECTE__;
const urlFlux          = __URL_FLUX__;
const urlImage         = __URL_IMAGE__;
const conversationId   = __CONV_ID__;

let reco = null;

// États vocaux
let vocalActif          = false;
let modeActuel          = 'texte';   // 'texte' | 'dictee' | 'vocal'
let requeteActiveController = null;
let reponseEnCours      = false;
let interruptionDemandee = false;

// VAD (Voice Activity Detection) — interruption pendant que Dashle parle
let vadStream       = null;
let vadAudioContext = null;
let vadAnalyser     = null;
let vadSource       = null;
let vadAnimation    = null;
let vadDerniereDetection = 0;
let vadDebutParole  = 0;
let vadPret         = false;

// Flag : vrai pendant toute la durée d'une synthèse vocale pour éviter
// que le VAD ne déclenche une interruption sur la voix de Dashle lui-même.
let vadSuspendu = false;

// Seuils VAD : assez hauts pour ne pas se déclencher sur la voix de synthèse
// (echoCancellation atténue déjà le retour, on affine avec le seuil)
const VAD_SEUIL    = 0.052;   // RMS minimal pour considérer "parole humaine"
const VAD_DUREE_MIN = 130;    // ms de parole continue avant interruption
const VAD_COOLDOWN  = 1200;   // ms minimum entre deux interruptions

// =====================================================================
// Helpers UI
// =====================================================================
function afficherEtatVocal(etat, libelle) {
  modeVocalEl.dataset.etat = etat;
  etatVocalEl.textContent  = libelle;
}

function ouvrirModeVocal() {
  modeVocalEl.classList.add('visible');
  modeVocalEl.setAttribute('aria-hidden', 'false');
}

function fermerModeVocal() {
  modeVocalEl.classList.remove('visible');
  modeVocalEl.setAttribute('aria-hidden', 'true');
}

function afficherStatutVocal(texte) {
  statutVocal.textContent = texte;
  statutVocal.classList.toggle('visible', !!texte);
}

function ajouterMessage(texte, classe) {
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

function ajouterReponse(texte, messageId) {
  const enveloppe = document.createElement('div');
  enveloppe.className = 'message-wrap bot';
  const message = document.createElement('div');
  message.className = 'msg bot';
  message.dataset.messageId = messageId || '';
  message.textContent = texte;

  let actionsHtml = '<div class="actions-reponse">'
    + '<button type="button" class="action-copier" title="Copier">📋</button>';
  if (estConnecte) {
    actionsHtml += '<button type="button" class="action-feedback" data-valeur="positif" title="J\'aime">👍</button>'
      + '<button type="button" class="action-feedback" data-valeur="negatif" title="Je n\'aime pas">👎</button>'
      + '<button type="button" class="action-partager" title="Partager">🔗</button>'
      + '<button type="button" class="action-regenerer" title="Régénérer">🔄</button>';
  }
  actionsHtml += '<button type="button" class="action-lire" title="Lecture / pause">▶</button>'
    + '<button type="button" class="action-stop" title="Arrêter">⏹</button>'
    + '<span class="lecture-etat"></span></div>';

  enveloppe.innerHTML = actionsHtml;
  enveloppe.insertBefore(message, enveloppe.firstChild);
  chat.appendChild(enveloppe);
  chat.scrollTop = chat.scrollHeight;
  return enveloppe;
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

// =====================================================================
// Gestion des générations SSE
// =====================================================================
function arreterGeneration() {
  if (requeteActiveController) {
    try { requeteActiveController.abort(); } catch(e) {}
    requeteActiveController = null;
  }
  reponseEnCours = false;
}

// =====================================================================
// VAD — détection de voix pendant que Dashle parle
// =====================================================================
async function demarrerVAD() {
  if (vadPret || !navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) return vadPret;
  try {
    vadStream = await navigator.mediaDevices.getUserMedia({
      audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true, channelCount: 1 }
    });
    const AudioCtx = window.AudioContext || window.webkitAudioContext;
    if (!AudioCtx) return false;
    vadAudioContext = new AudioCtx();
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
}

function surveillerParole() {
  if (!vadPret || !vadAnalyser || !vocalActif) return;
  const donnees = new Uint8Array(vadAnalyser.fftSize);

  const verifier = function() {
    if (!vadPret || !vadAnalyser || !vocalActif || vadSuspendu) return;
    vadAnalyser.getByteTimeDomainData(donnees);
    let somme = 0;
    for (let i = 0; i < donnees.length; i++) {
      const x = (donnees[i] - 128) / 128;
      somme += x * x;
    }
    const rms = Math.sqrt(somme / donnees.length);
    const now = performance.now();

    // N'interrompt que si Dashle génère OU parle
    const dashleOccupe = reponseEnCours
      || (window.speechSynthesis && window.speechSynthesis.speaking);

    if (dashleOccupe && rms >= VAD_SEUIL) {
      if (!vadDebutParole) vadDebutParole = now;
      if (now - vadDebutParole >= VAD_DUREE_MIN
          && now - vadDerniereDetection >= VAD_COOLDOWN) {
        vadDerniereDetection = now;
        vadDebutParole = 0;
        interrompreDashle();
      }
    } else {
      if (!dashleOccupe) vadDebutParole = 0;
    }
    vadAnimation = requestAnimationFrame(verifier);
  };
  verifier();
}

// =====================================================================
// Mode vocal — interruption et séquencement
// =====================================================================
function interrompreDashle() {
  if (!vocalActif) return;
  interruptionDemandee = true;

  // 1. Stopper immédiatement la synthèse vocale
  if ('speechSynthesis' in window) window.speechSynthesis.cancel();

  // 2. Stopper la génération SSE
  arreterGeneration();

  // 3. Réinitialiser l'UI lecture
  if (lectureActuelle) {
    lectureActuelle.classList.remove('actif');
    lectureActuelle.textContent = '▶';
  }
  lectureActuelle = null;
  utteranceActuelle = null;

  // 4. Reprendre l'écoute immédiatement
  afficherEtatVocal('ecoute', "Je t'écoute...");
  afficherStatutVocal("🎙️ Vas-y, je t'écoute.");
  btnVocal.classList.add('ecoute');
  btnVocal.classList.remove('parle');

  // 5. Redémarrer la reconnaissance vocale
  try { reco && reco.abort(); } catch(e) {}
  setTimeout(function() {
    if (!vocalActif || !reco) return;
    interruptionDemandee = false;
    try { reco.start(); } catch(e) {}
  }, 100);
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
    if (!vocalActif) return;
    modeActuel = 'vocal';
    ouvrirModeVocal();
    afficherEtatVocal('ecoute', 'Dashle écoute...');
    btnVocal.classList.add('ecoute');
    btnVocal.classList.remove('parle');
    afficherStatutVocal("🎧 Je t'écoute...");
    try { reco.start(); } catch(e) {}
  }

  btnMicro.onclick = function() {
    if (vocalActif) return;
    modeActuel = 'dictee';
    btnMicro.classList.add('actif');
    try { reco.start(); } catch(e) {}
  };

  btnVocal.onclick = async function() {
    vocalActif = !vocalActif;
    if (vocalActif) {
      btnVocal.classList.add('vocal-on');
      ouvrirModeVocal();
      interruptionDemandee = false;
      await demarrerVAD();
      try { reco.stop(); } catch(e) {}
      setTimeout(demarrerEcouteVocale, 80);
    } else {
      btnVocal.classList.remove('vocal-on', 'ecoute', 'parle');
      fermerModeVocal();
      afficherEtatVocal('attente', 'En attente');
      afficherStatutVocal('');
      try { reco.stop(); } catch(e) {}
      arreterGeneration();
      if ('speechSynthesis' in window) window.speechSynthesis.cancel();
      arreterVAD();
    }
  };

  reco.onresult = function(e) {
    const transcript = (e.results[0][0].transcript || '').trim();
    if (!transcript) return;
    champ.value = transcript;
    champ.style.height = 'auto';
    if (modeActuel === 'vocal') {
      interruptionDemandee = false;
      afficherEtatVocal('reflexion', 'Dashle réfléchit...');
      afficherStatutVocal('');
      try { form.requestSubmit(); } catch(errSub) { form.dispatchEvent(new Event('submit', { bubbles: true, cancelable: true })); }
    }
  };

  reco.onend = function() {
    btnMicro.classList.remove('actif');
    if (!vocalActif) btnVocal.classList.remove('ecoute');
    // En mode vocal : si pas d'interruption et pas de génération en cours,
    // on redemarre l'écoute après un court délai de sécurité.
    if (vocalActif && !reponseEnCours && !interruptionDemandee) {
      setTimeout(function() {
        if (vocalActif && !reponseEnCours) {
          try { reco.start(); } catch(e) {}
        }
      }, 200);
    }
  };

  reco.onerror = function(e) {
    btnMicro.classList.remove('actif');
    if (vocalActif && e.error !== 'aborted') {
      afficherEtatVocal('attente', 'En attente du micro...');
      afficherStatutVocal("🎧 Petit souci, je réessaie...");
      setTimeout(demarrerEcouteVocale, 700);
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
  btnVocal.style.display = 'none';
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
    lectureActuelle.classList.remove('actif');
    lectureActuelle.textContent = '▶';
    const act = lectureActuelle.closest('.actions-reponse');
    if (act) act.querySelector('.lecture-etat').textContent = '';
  }
  lectureActuelle   = null;
  utteranceActuelle = null;
}

function nettoyerPourLecture(texte) {
  // Retire les marqueurs Markdown courants (ne modifie PAS le textContent affiché)
  var propre = texte
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

function lireReponse(bouton) {
  if (!('speechSynthesis' in window)) {
    bouton.closest('.actions-reponse').querySelector('.lecture-etat').textContent = 'Voix indisponible';
    return;
  }
  const texte = bouton.closest('.message-wrap').querySelector('.msg').textContent;
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
  utteranceActuelle = new SpeechSynthesisUtterance(nettoyerPourLecture(texte));
  utteranceActuelle.lang   = 'fr-FR';
  utteranceActuelle.voice  = choisirVoixFrancaise();
  utteranceActuelle.rate   = Number(preferencesVocales.voix_vitesse) || 1;
  utteranceActuelle.pitch  = Number(preferencesVocales.voix_tonalite) || 1;
  utteranceActuelle.volume = Number(preferencesVocales.voix_volume) || 1;
  lectureActuelle = bouton;
  bouton.classList.add('actif');
  bouton.textContent = '⏸';
  etat.textContent = 'Lecture';

  utteranceActuelle.onend = function() {
    vadSuspendu = false;
    const vocal = window._dashleVocal;
    const enModeVocal = vocal && vocal.estActif();
    arreterLecture();
    // Séquencement vocal : on reprend l'écoute APRÈS la fin de la synthèse,
    // jamais pendant (évite que Dashle détecte sa propre voix).
    if (enModeVocal && !interruptionDemandee) {
      // Petit délai de sécurité pour laisser l'echo disparaître
      setTimeout(vocal.reprendreEcoute, 250);
    }
  };

  utteranceActuelle.onerror = function() {
    etat.textContent = 'Erreur audio';
    vadSuspendu = false;
    arreterLecture();
    if (window._dashleVocal && window._dashleVocal.estActif()) {
      setTimeout(window._dashleVocal.reprendreEcoute, 400);
    }
  };

  vadSuspendu = true;
  window.speechSynthesis.speak(utteranceActuelle);
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
inputImage.addEventListener('change', function(e) {
  fichierImage = e.target.files[0] || null;
  afficherApercuFichier(fichierImage);
});
document.getElementById('retirer-fichier').addEventListener('click', effacerApercuFichier);

// =====================================================================
// Contrôles mode vocal plein écran
// =====================================================================
document.getElementById('reduire-vocal').addEventListener('click', fermerModeVocal);
document.getElementById('fermer-vocal').addEventListener('click', function() {
  vocalActif = false;
  btnVocal.classList.remove('vocal-on', 'ecoute', 'parle');
  try { reco && reco.stop(); } catch(e) {}
  afficherStatutVocal('');
  fermerModeVocal();
  afficherEtatVocal('attente', 'En attente');
  if ('speechSynthesis' in window) window.speechSynthesis.cancel();
  arreterVAD();
});

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
// Recherche dans le sidebar (utilisateurs connectés)
// =====================================================================
const rechercheEl = document.getElementById('recherche-conversations');
if (rechercheEl) {
  rechercheEl.addEventListener('input', function() {
    const terme = this.value.toLowerCase().trim();
    document.querySelectorAll('.ligne-conversation').forEach(function(l) {
      l.style.display = !terme || l.dataset.titre.includes(terme) ? 'flex' : 'none';
    });
  });
}

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
    await navigator.clipboard.writeText(message.textContent);
    bouton.classList.add('actif');
    setTimeout(function() { bouton.classList.remove('actif'); }, 1200);

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
});

form.addEventListener('submit', async function(e) {
  e.preventDefault();
  const texte = champ.value.trim();
  if (!texte && !fichierImage) return;

  // Couper toute lecture en cours si l'utilisateur envoie manuellement
  if ('speechSynthesis' in window && window.speechSynthesis.speaking) arreterLecture();

  // --- Envoi image ---
  if (fichierImage) {
    ajouterMessage(texte || '📷 Image envoyée', 'user');
    champ.value = '';
    champ.style.height = 'auto';
    afficherReflexion();

    const fd = new FormData();
    fd.append('message', texte);
    fd.append('image', fichierImage);
    const headers = {};
    if (estConnecte) headers['X-CSRF-Token'] = csrfToken;

    try {
      const res  = await fetch(urlImage, { method: 'POST', headers, body: fd });
      const data = await res.json();
      retirerReflexion();
      if (!res.ok) throw new Error(data.erreur || 'Erreur image');
      ajouterReponse(data.reponse, data.message_id);
    } catch(err) {
      retirerReflexion();
      ajouterMessage("Erreur d'envoi de l'image. Réessaie.", 'bot');
    }
    fichierImage = null;
    effacerApercuFichier();
    return;
  }

  if (!texte) return;

  ajouterMessage(texte, 'user');
  champ.value = '';
  champ.style.height = 'auto';
  afficherReflexion();

  const controller = new AbortController();
  requeteActiveController = controller;
  reponseEnCours  = true;
  interruptionDemandee = false;

  let reponseElement = null;
  let messageElement = null;

  const headers = {
    'Content-Type': 'application/x-www-form-urlencoded;charset=UTF-8',
    'Accept': 'text/event-stream',
  };
  if (estConnecte) headers['X-CSRF-Token'] = csrfToken;

  try {
    const res = await fetch(urlFlux, {
      method:  'POST',
      headers,
      body:    'message=' + encodeURIComponent(texte),
      signal:  controller.signal,
      cache:   'no-store',
    });

    retirerReflexion();

    if (!res.ok || !res.body) {
      let detail = '';
      try { detail = (await res.json()).erreur || ''; } catch(ex) {}
      throw new Error(detail || 'Flux indisponible (' + res.status + ')');
    }

    reponseElement = ajouterReponse('', '');
    messageElement = reponseElement.querySelector('.msg');

    const lecteur  = res.body.getReader();
    const decodeur = new TextDecoder();
    let tampon      = '';
    let reponseTexte = '';
    let messageId   = null;

    while (true) {
      const { done, value } = await lecteur.read();
      if (done) break;
      tampon += decodeur.decode(value, { stream: true });
      const lignes = tampon.split('\n');
      tampon = lignes.pop();

      for (const ligne of lignes) {
        if (!ligne.startsWith('data:')) continue;
        let ev;
        try { ev = JSON.parse(ligne.slice(5).trim()); } catch(ex) { continue; }
        if (ev.erreur) throw new Error(ev.erreur);
        if (ev.morceau) {
          reponseTexte += ev.morceau;
          messageElement.textContent = reponseTexte;
          chat.scrollTop = chat.scrollHeight;
        }
        if (ev.termine) messageId = ev.message_id;
      }
    }

    messageElement.dataset.messageId = messageId || '';
    reponseEnCours = false;
    requeteActiveController = null;

    // Lecture vocale si le mode vocal est actif
    const vocal = window._dashleVocal;
    if (vocal && vocal.estActif() && reponseTexte) {
      vocal.marquerParle();
      lireReponse(reponseElement.querySelector('.action-lire'));
      // L'écoute reprendra via utteranceActuelle.onend (après la synthèse)
    }

    if (reponseTexte && reponseTexte.toLowerCase().includes('quota')) bloquerEnvoi(30);

  } catch(err) {
    retirerReflexion();
    reponseEnCours = false;
    if (requeteActiveController === controller) requeteActiveController = null;

    if (err && err.name === 'AbortError') {
      if (reponseElement) reponseElement.remove();
      if (vocalActif) {
        setTimeout(function() {
          if (vocalActif && reco) { try { reco.start(); } catch(ex) {} }
        }, 100);
      }
      return;
    }

    if (reponseElement && !reponseElement.querySelector('.msg').textContent.trim()) {
      reponseElement.remove();
    }
    ajouterMessage("Erreur de connexion. Réessaie.", 'bot');
    if (window._dashleVocal && window._dashleVocal.estActif()) {
      setTimeout(window._dashleVocal.reprendreEcoute, 700);
    }
  }
});
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
.marque{color:#10A37F;font-weight:700;font-size:18px}
h1{margin:4px 0 20px;font-size:20px}
.message{padding:12px 16px;margin:12px 0;border-radius:14px;white-space:pre-wrap;line-height:1.5;background:#fff;box-shadow:0 2px 8px rgba(0,0,0,0.06)}
.user{margin-left:15%;background:#e2f7ed}
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
:root{font-family:Segoe UI,sans-serif;color:#17251f;background:#f4f8f6}
*{box-sizing:border-box}body{margin:0}
.page{max-width:760px;margin:auto;padding:24px 18px 50px}
.bar{display:flex;justify-content:space-between;align-items:center;margin-bottom:22px}
.bar a{color:#10A37F;text-decoration:none;font-weight:600;margin-left:12px}
.carte{background:#fff;border:1px solid #dceae4;border-radius:14px;padding:18px;margin:12px 0}
.carte h2{font-size:15px;margin:0 0 14px;color:#10A37F}
label{display:flex;justify-content:space-between;gap:14px;align-items:center;padding:10px 0;border-top:1px solid #edf2f0}
label:first-of-type{border-top:0}
select,input[type=checkbox],input[type=range]{accent-color:#10A37F}
select{max-width:100%;padding:7px;border:1px solid #dceae4;border-radius:7px}
input[type=range]{width:160px}
button{border:0;border-radius:9px;background:#10A37F;color:#fff;padding:10px 14px;cursor:pointer}
.secondaire{background:#e5f3ed;color:#087355}
.note{color:#71837b;font-size:13px}
</style></head>
<body><main class="page">
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
    <p class="note">La modification de l'adresse e-mail et la récupération de compte ne sont pas encore disponibles.</p>
  </section>
  <section class="carte"><h2>Apparence</h2>
    <label>Thème
      <select name="theme">
        <option value="clair"  {% if preferences.theme == 'clair'  %}selected{% endif %}>Clair</option>
        <option value="sombre" {% if preferences.theme == 'sombre' %}selected{% endif %}>Sombre</option>
      </select>
    </label>
  </section>
  <section class="carte"><h2>Voix</h2>
    <label>Voix activée<input type="checkbox" name="voix_active" {% if preferences.voix_active %}checked{% endif %}></label>
    <label>Voix française<select id="voix-select" name="voix_nom" data-selection="{{ preferences.voix_nom }}"><option value="">Automatique</option></select></label>
    <label>Vitesse<input type="range" name="voix_vitesse" min="0.6" max="1.4" step="0.05" value="{{ preferences.voix_vitesse }}"><output id="vitesse-valeur">{{ preferences.voix_vitesse }}</output></label>
    <label>Tonalité<input type="range" name="voix_tonalite" min="0.7" max="1.3" step="0.05" value="{{ preferences.voix_tonalite }}"><output id="tonalite-valeur">{{ preferences.voix_tonalite }}</output></label>
    <label>Volume<input type="range" name="voix_volume" min="0.2" max="1" step="0.05" value="{{ preferences.voix_volume }}"><output id="volume-valeur">{{ preferences.voix_volume }}</output></label>
    <button type="button" class="secondaire" id="tester-voix">▶ Tester la voix</button>
    <p class="note">Dashle privilégie automatiquement une voix féminine française. La lecture automatique reste désactivée par défaut.</p>
  </section>
  <section class="carte"><h2>Conversations et confidentialité</h2>
    <label>Conserver l'historique<input type="checkbox" name="conserver_historique" {% if preferences.conserver_historique %}checked{% endif %}></label>
    <p class="note">Les conversations partagées utilisent un lien révocable et ne montrent pas les informations du compte.</p>
  </section>
  <section class="carte"><h2>Sécurité</h2>
    <p class="note">Les mots de passe sont hachés. <a href="{{ url_for('securite') }}">Gérer la sécurité du compte →</a></p>
  </section>
  <button type="submit">Enregistrer</button>
</form>
<script>
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
</script>
</main></body></html>
"""

SECURITY_PAGE = """
<!doctype html><html lang="fr"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Dashle - Sécurité</title>
<style>
:root{font-family:Segoe UI,sans-serif;color:#17251f;background:#f4f8f6}*{box-sizing:border-box}body{margin:0}
.page{max-width:620px;margin:auto;padding:24px 18px 50px}
.bar{display:flex;justify-content:space-between;align-items:center;margin-bottom:22px}
.bar a{color:#10A37F;text-decoration:none;font-weight:600}
.carte{background:#fff;border:1px solid #dceae4;border-radius:14px;padding:18px;margin:12px 0}
.carte h2{font-size:15px;margin:0 0 14px;color:#10A37F}
label{display:block;margin-top:12px;font-size:14px}
input{display:block;width:100%;margin-top:5px;padding:10px;border:1px solid #dceae4;border-radius:8px}
button{border:0;border-radius:9px;background:#10A37F;color:#fff;padding:10px 14px;margin-top:16px;cursor:pointer}
.danger{background:#b42318}
.note{color:#71837b;font-size:13px}
.message{padding:10px;border-radius:8px;background:#e5f3ed;color:#087355}
</style></head>
<body><main class="page">
<div class="bar"><div><strong>Dashle</strong><h1>Sécurité</h1></div><a href="{{ url_for('parametres') }}">← Paramètres</a></div>
{% if erreur %}<p class="note">{{ erreur }}</p>{% endif %}
{% if succes %}<p class="message">{{ succes }}</p>{% endif %}
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

AUTH_PAGE = """
<!doctype html><html lang="fr"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Dashle — {{ titre }}</title>
<style>
body{font-family:Segoe UI,sans-serif;background:#f5f7f6;margin:0;display:grid;place-items:center;min-height:100vh}
.carte{width:min(380px,90vw);padding:32px 28px;background:#fff;border-radius:16px;box-shadow:0 4px 24px rgba(0,0,0,0.09)}
.logo-titre{display:flex;align-items:center;gap:10px;margin-bottom:4px}
img.logo{height:36px;border-radius:50%}
h1{color:#10A37F;margin:0;font-size:22px}
h2{color:#333;margin:0 0 20px;font-size:16px;font-weight:500}
label,input,button{display:block;width:100%;box-sizing:border-box}
label{margin-top:14px;font-size:14px;color:#444}
input{padding:10px 12px;margin-top:5px;border:1px solid #ddd;border-radius:8px;font:inherit;font-size:15px}
input:focus{outline:none;border-color:#10A37F}
button{margin-top:22px;padding:12px;border:0;border-radius:10px;background:#10A37F;color:#fff;cursor:pointer;font-size:15px;font-weight:600;transition:background .15s}
button:hover{background:#087355}
.erreur{color:#b00020;font-size:13px;margin-top:8px}
p{font-size:14px;color:#555;margin-top:16px}
p a{color:#10A37F;font-weight:600;text-decoration:none}
.visiteur{display:block;text-align:center;margin-top:12px;font-size:13px;color:#71837b}
.visiteur a{color:#10A37F}
</style></head>
<body><main class="carte">
<div class="logo-titre">
  <img class="logo" src="/static/logo.png" alt="Dashle">
  <h1>Dashle</h1>
</div>
<h2>{{ titre }}</h2>
{% if erreur %}<p class="erreur">{{ erreur }}</p>{% endif %}
<form method="post">
  <input type="hidden" name="csrf_token" value="{{ csrf_token }}">
  <label>E-mail<input name="email" type="email" required maxlength="254" autocomplete="email"></label>
  <label>Mot de passe<input name="password" type="password" required minlength="8" autocomplete="{{ autocomplete }}"></label>
  <button type="submit">{{ action }}</button>
</form>
<p>{{ texte_lien }} <a href="{{ url_for(lien) }}">{{ libelle_lien }}</a></p>
<span class="visiteur">Pas encore prêt ? <a href="{{ url_for('accueil') }}">Continuer sans compte →</a></span>
</main></body></html>
"""


# ---------------------------------------------------------------------------
# Helper : rendu de PAGE avec toutes les variables communes
# ---------------------------------------------------------------------------

def _rendre_page(messages, utilisateur=None, conversations=None, conversation_id=None, preferences=None):
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
    csrf_val = jeton_csrf() if est_connecte else "null"

    html = render_template_string(
        PAGE,
        css=_CSS,
        messages=messages,
        utilisateur={"email": utilisateur} if utilisateur else None,
        conversations=conversations or [],
        conversation_id=conversation_id or 0,
        preferences=prefs,
        csrf_token=jeton_csrf(),
    )

    # Injection des constantes JS
    html = html.replace("__CSRF_TOKEN__",   json.dumps(jeton_csrf() if est_connecte else None))
    html = html.replace("__PREFS_VOCALES__", json.dumps({
        "voix_nom":      prefs["voix_nom"],
        "voix_vitesse":  float(prefs["voix_vitesse"]),
        "voix_tonalite": float(prefs["voix_tonalite"]),
        "voix_volume":   float(prefs["voix_volume"]),
    }))
    html = html.replace("__EST_CONNECTE__",  "true" if est_connecte else "false")
    html = html.replace("__URL_FLUX__",      json.dumps(url_flux))
    html = html.replace("__URL_IMAGE__",     json.dumps(url_image))
    html = html.replace("__CONV_ID__",       str(conversation_id or 0))
    return html


# ---------------------------------------------------------------------------
# Routes — chat principal
# ---------------------------------------------------------------------------

@app.route("/")
def accueil():
    user_id = session.get("user_id")

    if user_id:
        # Utilisateur connecté — comportement existant
        conversation_id = _conv_courante(user_id)
        return _rendre_page(
            messages=_messages_conversation(user_id, conversation_id),
            utilisateur=session["user_email"],
            conversations=_liste_conversations(user_id),
            conversation_id=conversation_id,
            preferences=_preferences(user_id),
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
    if not user_id:
        # Visiteur : effacer la conversation temporaire
        session.pop("historique_visiteur", None)
        session.pop("resume_visiteur", None)
        return redirect(url_for("accueil"))
    with session_base() as db:
        conv = Conversation(user_id=user_id)
        db.add(conv)
        db.flush()
        session["conversation_id"] = conv.id
    return redirect(url_for("accueil"))


@app.route("/conv/<int:i>")
def charger_conv(i):
    user_id = session.get("user_id")
    if not user_id:
        return redirect(url_for("accueil"))
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
    reponse = traiter_message(dernier_user, historique, user_id, resume)
    mid = ajouter_message(user_id, conv_id, reponse, "bot")
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

@app.route("/parametres", methods=["GET", "POST"])
def parametres():
    user_id = session.get("user_id")
    if not user_id:
        return redirect(url_for("connexion"))

    if request.method == "POST":
        def num(nom, lo, hi, defaut):
            try:
                return max(lo, min(hi, float(request.form.get(nom, defaut))))
            except (TypeError, ValueError):
                return defaut

        with session_base() as db:
            prefs = db.query(UserPreference).filter_by(user_id=user_id).one_or_none()
            if prefs is None:
                prefs = UserPreference(user_id=user_id)
                db.add(prefs)
            prefs.theme = "sombre" if request.form.get("theme") == "sombre" else "clair"
            prefs.voix_active = request.form.get("voix_active") == "on"
            prefs.lecture_automatique = False
            prefs.conserver_historique = request.form.get("conserver_historique") == "on"
            prefs.voix_nom     = request.form.get("voix_nom", "")[:160]
            prefs.voix_vitesse = num("voix_vitesse", 0.6, 1.4, 1.0)
            prefs.voix_tonalite = num("voix_tonalite", 0.7, 1.3, 1.0)
            prefs.voix_volume  = num("voix_volume", 0.2, 1.0, 1.0)
        return redirect(url_for("parametres"))

    return render_template_string(
        SETTINGS_PAGE,
        utilisateur=session["user_email"],
        preferences=_preferences(user_id),
        csrf_token=jeton_csrf(),
        erreur=request.args.get("erreur"),
        succes=request.args.get("succes"),
    )


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

@app.route("/repondre", methods=["POST"])
def repondre():
    """Endpoint JSON synchrone (non-streaming)."""
    user_id = session.get("user_id")
    message = request.form.get("message", "").strip()
    if not message:
        return jsonify({"reponse": ""})

    if user_id:
        conversation_id = _conv_courante(user_id)
        historique = _messages_conversation(user_id, conversation_id)
        resume = _resume_conversation(user_id, conversation_id)
        ajouter_message(user_id, conversation_id, message, "user")
        reponse = traiter_message(
            message, historique + [{"auteur": "user", "texte": message}], user_id, resume
        )
        mid = ajouter_message(user_id, conversation_id, reponse, "bot")
        _actualiser_resume(user_id, conversation_id)
        return jsonify({"reponse": reponse, "message_id": mid})
    else:
        # Visiteur
        historique = _historique_visiteur()
        resume = _resume_visiteur()
        _ajouter_message_visiteur(message, "user")
        reponse = traiter_message(
            message, historique + [{"auteur": "user", "texte": message}], None, resume
        )
        _ajouter_message_visiteur(reponse, "bot")
        return jsonify({"reponse": reponse, "message_id": None})


@app.route("/repondre_flux", methods=["POST"])
def repondre_flux():
    """Diffuse une réponse SSE. Gère visiteur et utilisateur connecté.

    Corrections :
    - Accessible aux visiteurs anonymes (historique en session).
    - GeneratorExit capturé proprement sans sauvegarder une réponse incomplète.
    - Erreurs retournées en JSON SSE compréhensible.
    - Résumé visiteur mis à jour toutes les 20 demandes.
    """
    user_id = session.get("user_id")

    if request.is_json:
        donnees = request.get_json(silent=True) or {}
        message = str(donnees.get("message", "")).strip()
    else:
        message = request.form.get("message", "").strip()

    if not message:
        return jsonify({"erreur": "Aucun message reçu."}), 400

    # --- Collecte du contexte selon le mode ---
    if user_id:
        conversation_id = _conv_courante(user_id)
        historique = _messages_conversation(user_id, conversation_id)
        resume = _resume_conversation(user_id, conversation_id)
        ajouter_message(user_id, conversation_id, message, "user")
        contexte_historique = historique + [{"auteur": "user", "texte": message}]
    else:
        historique = list(_historique_visiteur())
        resume = _resume_visiteur()
        _ajouter_message_visiteur(message, "user")
        contexte_historique = historique + [{"auteur": "user", "texte": message}]
        conversation_id = None

    @stream_with_context
    def generer():
        morceaux = []
        try:
            for morceau in streamer_message(
                message, contexte_historique, user_id, resume
            ):
                if not morceau:
                    continue
                morceau = str(morceau)
                morceaux.append(morceau)
                yield "data: " + json.dumps(
                    {"morceau": morceau}, ensure_ascii=False
                ) + "\n\n"

            reponse_complete = "".join(morceaux).strip()

            if user_id and reponse_complete:
                mid = ajouter_message(user_id, conversation_id, reponse_complete, "bot")
                _actualiser_resume(user_id, conversation_id)
                yield "data: " + json.dumps(
                    {"termine": True, "message_id": mid}, ensure_ascii=False
                ) + "\n\n"
            else:
                # Visiteur : stocker dans la session (hors du générateur,
                # on utilise un flag pour signaler la fin proprement)
                if reponse_complete:
                    _ajouter_message_visiteur(reponse_complete, "bot")
                    # Résumé visiteur toutes les 20 demandes
                    n = len(_historique_visiteur())
                    if n >= 20 and (n - 20) % 10 == 0:
                        nouveau = resumer_conversation(_historique_visiteur(), _resume_visiteur())
                        if nouveau:
                            _maj_resume_visiteur(nouveau)
                    session.modified = True
                yield "data: " + json.dumps(
                    {"termine": True, "message_id": None}, ensure_ascii=False
                ) + "\n\n"

        except GeneratorExit:
            # Navigateur a fermé la connexion (interruption utilisateur).
            # On ne sauvegarde PAS une réponse incomplète.
            return
        except Exception as err:
            print("ERREUR /repondre_flux :", repr(err))
            yield "data: " + json.dumps(
                {"erreur": "Erreur pendant la génération. Réessaie."},
                ensure_ascii=False,
            ) + "\n\n"

    return Response(
        generer(),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "Pragma":        "no-cache",
            "X-Accel-Buffering": "no",
            "Connection":    "keep-alive",
        },
    )


@app.route("/repondre_image", methods=["POST"])
def repondre_image():
    user_id = session.get("user_id")

    if user_id:
        conversation_id = _conv_courante(user_id)
        historique = _messages_conversation(user_id, conversation_id)
        resume = _resume_conversation(user_id, conversation_id)
    else:
        historique = list(_historique_visiteur())
        resume = _resume_visiteur()
        conversation_id = None

    message = request.form.get("message", "").strip()
    fichier = request.files.get("image")
    if not fichier:
        return jsonify({"reponse": "Aucune image reçue."})

    image_bytes = fichier.read()
    if not image_bytes:
        return jsonify({"reponse": "L'image reçue est vide."}), 400

    mime_type = detecter_type_media(image_bytes)
    if not mime_type:
        return jsonify({"reponse": "Le fichier envoyé n'est pas une image ou une vidéo valide."}), 400

    if mime_type.startswith("image/") and PIL_DISPONIBLE:
        try:
            with Image.open(io.BytesIO(image_bytes)) as img:
                img.verify()
        except Exception:
            return jsonify({"reponse": "Le fichier envoyé n'est pas une image valide."}), 400

    image_b64 = base64.b64encode(image_bytes).decode("utf-8")
    type_media = "Vidéo" if mime_type.startswith("video/") else "Image"
    texte_msg = message or f"[{type_media} envoyée]"

    if user_id:
        ajouter_message(user_id, conversation_id, texte_msg, "user")
    else:
        _ajouter_message_visiteur(texte_msg, "user")

    reponse = traiter_message_image(message, image_b64, mime_type, historique, resume)

    if user_id:
        mid = ajouter_message(user_id, conversation_id, reponse, "bot")
        _actualiser_resume(user_id, conversation_id)
    else:
        _ajouter_message_visiteur(reponse, "bot")
        mid = None

    return jsonify({"reponse": reponse, "message_id": mid})


@app.errorhandler(413)
def fichier_trop_volumineux(_erreur):
    return jsonify({"reponse": "Le fichier est trop volumineux (maximum : 8 Mo)."}), 413


# ---------------------------------------------------------------------------
# Routes — comptes utilisateurs
# ---------------------------------------------------------------------------

@app.route("/inscription", methods=["GET", "POST"])
def inscription():
    erreur = None
    if request.method == "POST":
        email    = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        if "@" not in email or len(email) > 254:
            erreur = "Indique une adresse e-mail valide."
        elif len(password) < 8:
            erreur = "Le mot de passe doit contenir au moins 8 caractères."
        else:
            try:
                with session_base() as db:
                    user = User(
                        email=email,
                        password_hash=generate_password_hash(password),
                    )
                    db.add(user)
                    db.flush()
                    # Proposer le transfert de la conversation visiteur
                    hist_visiteur = list(_historique_visiteur())
                    session.clear()
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
        csrf_token=jeton_csrf(),
    )


@app.route("/connexion", methods=["GET", "POST"])
def connexion():
    erreur = None
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        with session_base() as db:
            user = db.query(User).filter_by(email=email).one_or_none()
            if user and check_password_hash(user.password_hash, request.form.get("password", "")):
                hist_visiteur = list(_historique_visiteur())
                session.clear()
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

    hist = session.pop("transfert_en_attente", None)
    if not hist:
        return jsonify({"ok": True, "transfere": 0})

    conversation_id = _conv_courante(user_id)
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
    return jsonify({"ok": True, "service": "dashle"})


# ---------------------------------------------------------------------------
# Point d'entrée
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
