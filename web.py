import os
import io
import base64
import json
import secrets
from datetime import datetime
from flask import Flask, Response, request, render_template_string, redirect, stream_with_context, url_for, session, jsonify
from werkzeug.security import check_password_hash, generate_password_hash
from sqlalchemy.exc import IntegrityError
from app import streamer_message, traiter_message, traiter_message_image
from brain import resumer_conversation
from database import (Conversation, Message, MessageFeedback, ShareLink, User,
                      UserMemory,
                      UserPreference, initialiser_base, session_base)

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
    SESSION_COOKIE_SECURE=os.environ.get("SESSION_COOKIE_SECURE") == "1",
)
initialiser_base()


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


def jeton_csrf():
    if "csrf_token" not in session:
        session["csrf_token"] = secrets.token_urlsafe(32)
    return session["csrf_token"]


@app.before_request
def verifier_csrf():
    if request.method != "POST":
        return None
    token = request.form.get("csrf_token") or request.headers.get("X-CSRF-Token")
    if not token or not secrets.compare_digest(token, session.get("csrf_token", "")):
        return jsonify({"reponse": "Requête invalide. Recharge la page puis réessaie."}), 400
    return None


@app.before_request
def exiger_connexion():
    publiques = {"static", "connexion", "inscription", "partage"}
    if request.endpoint not in publiques and "user_id" not in session:
        return redirect(url_for("connexion"))
    return None

PAGE = """
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
<style>
  * { box-sizing: border-box; }
  body { font-family: 'Segoe UI', sans-serif; margin: 0; background: #fff; color:#17251f; height: 100vh; display: flex; flex-direction: column; }
  body.theme-sombre { background:#101816; color:#e8f5ef; }
  body.theme-sombre #sidebar, body.theme-sombre .bot, body.theme-sombre #apercu-fichier { background:#17231f; color:#e8f5ef; border-color:#294238; }
  body.theme-sombre #sidebar a, body.theme-sombre #sidebar button { color:#e8f5ef; background:#17231f; border-color:#294238; }
  body.theme-sombre form.bas, body.theme-sombre form.bas textarea { background:#101816; color:#e8f5ef; border-color:#294238; }
  header { background: #10A37F; color: white; padding: 14px 16px; display: flex; align-items: center; }
  header .titre { font-weight: bold; font-size: 18px; margin-left: 10px; flex: 1; }
  header button { background: none; border: none; color: white; font-size: 20px; cursor: pointer; }

  #voile { display:none; position:fixed; top:0; left:0; width:100%; height:100%; background:rgba(0,0,0,0.3); z-index:5; }
  #sidebar { display:none; position:fixed; top:0; left:0; width:82%; max-width:320px; height:100%; background:#fff; z-index:6; overflow-y:auto; box-shadow:2px 0 8px rgba(0,0,0,0.15); }
  #sidebar h2 { padding: 20px 18px 10px; margin:0; }
  #sidebar a, #sidebar button.nouvelle { display:block; width:100%; padding:12px 18px; text-decoration:none; color:#111; border:0; border-bottom:1px solid #eee; background:#fff; font:inherit; cursor:pointer; }
  #sidebar a.nouvelle, #sidebar button.nouvelle { color:#10A37F; font-weight:bold; }

  #chat { flex:1; overflow-y:auto; padding: 14px; width:min(900px,100%); margin:0 auto; }
  .msg { max-width: 80%; padding: 10px 14px; border-radius: 14px; margin-bottom: 10px; white-space: pre-wrap; line-height:1.4; }
  .user { background:#DCF8C6; margin-left:auto; }
  .bot { background:#f0f0f0; margin-right:auto; }
  .message-wrap { max-width:82%; margin-bottom:14px; }
  .message-wrap.user { margin-left:auto; }
  .message-wrap.bot { margin-right:auto; }
  .message-wrap .msg { max-width:100%; margin-bottom:4px; }
  .actions-reponse { display:flex; gap:3px; padding:2px 6px; }
  .actions-reponse button { border:0; background:transparent; color:#6b7c76; border-radius:7px; padding:4px 6px; cursor:pointer; font-size:13px; }
  .actions-reponse button:hover, .actions-reponse button.actif { background:#e5f3ed; color:#087355; }
  .actions-reponse .lecture-etat { font-size:12px; color:#10A37F; min-width:48px; align-self:center; }

  /* Indicateur "Dashle réfléchit" : point qui pulse */
  .reflexion { display:flex; align-items:center; gap:6px; padding: 10px 14px; }
  .dot { width:10px; height:10px; border-radius:50%; background:#10A37F; animation: pulse 0.9s infinite ease-in-out; }
  @keyframes pulse { 0%,100% { transform: scale(0.7); opacity:0.5; } 50% { transform: scale(1); opacity:1; } }

  form.bas { display:flex; gap:6px; padding:10px; border-top:1px solid #eee; align-items:center; }
  form.bas textarea { flex:1; resize:none; border:1px solid #ddd; border-radius:18px; padding:10px 14px; font-size:15px; max-height:100px; }
  form.bas textarea:focus { outline: none; }

  /* Groupe micro + envoyer, collés à droite comme sur Claude */
  .groupe-actions { display:flex; align-items:center; gap:2px; flex-shrink:0; }

  button.micro {
    background: none;
    border: none;
    cursor: pointer;
    width: 32px;
    height: 32px;
    border-radius: 50%;
    font-size: 16px;
    display: flex;
    align-items: center;
    justify-content: center;
    color: #6b6b6b;
    transition: background 0.15s ease, color 0.15s ease, opacity 0.15s ease;
  }
  button.micro:hover { background: #f0f0f0; color: #10A37F; }
  button.micro.actif { color: #10A37F; background: #e6f7f1; }

  /* Bouton mode conversation vocale (boucle écoute/réponse), rond bleu façon assistant vocal */
  button.vocal {
    background: #3B82F6;
    border: none;
    cursor: pointer;
    width: 32px;
    height: 32px;
    border-radius: 50%;
    display: flex;
    align-items: center;
    justify-content: center;
    color: #fff;
    box-shadow: 0 1px 4px rgba(0,0,0,0.18);
    transition: background 0.15s ease, box-shadow 0.15s ease;
  }
  button.vocal:hover { background: #2f6fe0; }
  button.vocal.vocal-on { box-shadow: 0 0 0 2px #10A37F; }
  button.vocal.ecoute { animation: pulse-vocal 1.1s infinite ease-in-out; }
  button.vocal.parle { background: #0b5ed7; }
  @keyframes pulse-vocal { 0%,100% { box-shadow: 0 0 0 0 rgba(59,130,246,0.55); } 50% { box-shadow: 0 0 0 8px rgba(59,130,246,0); } }

  #statut-vocal { text-align:center; font-size:12px; color:#10A37F; padding: 0 10px 6px; display:none; }
  #statut-vocal.visible { display:block; }
  .recherche-conversations { margin:0 14px 10px; padding:9px 11px; width:calc(100% - 28px); border:1px solid #dce7e2; border-radius:9px; }
  .menu-section { padding:12px 18px 5px; color:#71837b; font-size:11px; text-transform:uppercase; letter-spacing:.08em; }

  #mode-vocal { display:none; position:fixed; inset:0; z-index:4; overflow:hidden; color:#effff8;
    background:radial-gradient(circle at 50% 42%, #1b8d67 0%, #07543f 36%, #032d25 72%, #011b18 100%); }
  #mode-vocal.visible { display:flex; flex-direction:column; }
  #mode-vocal::before { content:""; position:absolute; inset:-30%; opacity:.45; pointer-events:none;
    background:radial-gradient(ellipse at 30% 20%, rgba(87,255,190,.22), transparent 35%),
      radial-gradient(ellipse at 75% 78%, rgba(16,163,127,.28), transparent 38%);
    animation: fond-vocal 14s ease-in-out infinite alternate; }
  @keyframes fond-vocal { from { transform:translate3d(-2%, -1%, 0) scale(1); } to { transform:translate3d(2%, 1%, 0) scale(1.08); } }
  .vocal-entete { position:relative; z-index:1; display:flex; align-items:center; justify-content:space-between; padding:18px 20px; }
  .vocal-entete strong { font-size:16px; letter-spacing:.02em; }
  .vocal-commandes { display:flex; gap:8px; }
  .vocal-commandes button { border:1px solid rgba(255,255,255,.25); border-radius:20px; padding:8px 12px; color:#effff8;
    background:rgba(0,0,0,.16); cursor:pointer; }
  .vocal-commandes button:hover { background:rgba(255,255,255,.14); }
  .scene-vocale { position:relative; z-index:1; display:grid; place-items:center; flex:1; min-height:0; }
  .systeme-solaire { position:relative; width:min(78vw, 430px); aspect-ratio:1; }
  .orbite { position:absolute; left:50%; top:50%; width:var(--taille); height:var(--taille); border:1px solid rgba(169,255,221,.24);
    border-radius:50%; transform:translate(-50%, -50%); animation:rotation-orbite var(--vitesse) linear infinite; }
  .orbite:nth-child(2) { animation-direction:reverse; }
  .orbite:nth-child(3) { animation-delay:-4s; }
  @keyframes rotation-orbite { to { transform:translate(-50%, -50%) rotate(360deg); } }
  .planete { position:absolute; left:50%; top:50%; width:var(--diametre); height:var(--diametre); margin:calc(var(--diametre) / -2);
    border-radius:50%; background:var(--couleur); box-shadow:0 0 12px var(--couleur); transform:translateX(calc(var(--taille) / 2)); }
  .orbe-dashle { position:absolute; left:50%; top:50%; width:clamp(104px, 25vw, 150px); aspect-ratio:1; transform:translate(-50%, -50%);
    border-radius:50%; background:radial-gradient(circle at 34% 28%, #d7fff0 0%, #60e4b4 13%, #10a37f 43%, #087355 72%, #023d31 100%);
    box-shadow:0 0 22px rgba(101,255,198,.9), 0 0 72px rgba(16,163,127,.65), inset -16px -18px 28px rgba(0,45,34,.48);
    animation:respiration-orbe 3.8s ease-in-out infinite; }
  .orbe-dashle::after { content:""; position:absolute; inset:-14%; border:1px solid rgba(173,255,224,.48); border-radius:50%; animation:halo-orbe 2.8s ease-in-out infinite; }
  #mode-vocal[data-etat="ecoute"] .orbe-dashle { animation-duration:1.35s; box-shadow:0 0 30px rgba(135,255,213,.95), 0 0 100px rgba(16,163,127,.8), inset -16px -18px 28px rgba(0,45,34,.48); }
  #mode-vocal[data-etat="reflexion"] .orbe-dashle { animation-duration:1.9s; filter:hue-rotate(18deg); }
  #mode-vocal[data-etat="parle"] .orbe-dashle { animation-duration:.85s; box-shadow:0 0 34px rgba(188,255,224,1), 0 0 120px rgba(16,163,127,.9), inset -16px -18px 28px rgba(0,45,34,.48); }
  @keyframes respiration-orbe { 0%,100% { transform:translate(-50%, -50%) scale(.96); } 50% { transform:translate(-50%, -50%) scale(1.04); } }
  @keyframes halo-orbe { 0%,100% { transform:scale(.92); opacity:.3; } 50% { transform:scale(1.08); opacity:.8; } }
  .etat-vocal { position:absolute; left:50%; bottom:8%; transform:translateX(-50%); min-width:180px; text-align:center; color:#c9ffeb; font-size:14px; }

  #apercu-fichier { display:none; align-items:center; gap:10px; margin:0 10px 8px; padding:8px 10px; border:1px solid #d9e5e1; border-radius:12px; background:#f7fbf9; }
  #apercu-fichier.visible { display:flex; }
  #apercu-fichier-media { width:58px; height:58px; flex:0 0 58px; border-radius:9px; object-fit:cover; background:#e7f2ee; }
  video#apercu-fichier-media { object-fit:contain; }
  #apercu-fichier-info { min-width:0; flex:1; color:#1e302b; font-size:12px; }
  #apercu-fichier-nom { display:block; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; font-weight:600; }
  #apercu-fichier-type { display:block; margin-top:3px; color:#688078; }
  #retirer-fichier { width:30px; height:30px; flex:0 0 30px; border:0; border-radius:50%; background:transparent; color:#6b7c76; cursor:pointer; font-size:20px; }
  #retirer-fichier:hover { background:#e8f2ee; color:#b00020; }
  @media (max-width:600px) { .vocal-entete { padding:14px; } .systeme-solaire { width:min(86vw, 360px); } .etat-vocal { bottom:5%; } }
  @media (prefers-reduced-motion:reduce) { #mode-vocal::before, .orbite, .orbe-dashle, .orbe-dashle::after { animation-play-state:paused; } }

  button.envoyer {
    background:#10A37F; color:white; border:none; border-radius:50%;
    width:36px; height:36px; font-size:15px; flex-shrink:0;
    display:flex; align-items:center; justify-content:center;
    cursor:pointer; transition: background 0.15s ease, opacity 0.15s ease;
  }
  button.envoyer:disabled { opacity:0.5; cursor:default; }
</style>
</head>
<body class="theme-{{ preferences.theme }}">

<header>
  <button onclick="document.getElementById('sidebar').style.display='block';document.getElementById('voile').style.display='block';">&#9776;</button>
  <img src="{{ url_for('static', filename='logo.png') }}" alt="Dashle" style="height:28px; margin-left:8px; border-radius:50%;">
  <div class="titre">Dashle</div>
  <span style="font-size:12px;margin-right:10px;">{{ utilisateur.email }}</span>
  <form action="{{ url_for('deconnexion') }}" method="post" style="margin:0;">
    <input type="hidden" name="csrf_token" value="{{ csrf_token }}">
    <button type="submit" title="Se déconnecter">&#x23FB;</button>
  </form>
  <form action="{{ url_for('nouvelle_conv') }}" method="post" style="margin:0;">
    <input type="hidden" name="csrf_token" value="{{ csrf_token }}">
    <button type="submit" aria-label="Nouvelle conversation">+</button>
  </form>
</header>

<div id="voile" onclick="document.getElementById('sidebar').style.display='none';this.style.display='none';"></div>
<div id="sidebar">
  <h2>Dashle</h2>
  <input class="recherche-conversations" id="recherche-conversations" type="search" placeholder="Rechercher dans l'historique..." aria-label="Rechercher dans l'historique">
  <form action="{{ url_for('nouvelle_conv') }}" method="post" style="margin:0;">
    <input type="hidden" name="csrf_token" value="{{ csrf_token }}">
    <button class="nouvelle" type="submit" style="width:100%;text-align:left;">+ Nouvelle conversation</button>
  </form>
  {% for i, conv in enumerate(conversations) %}
    <div class="ligne-conversation" data-titre="{{ conv.titre|lower }}" style="display:flex;align-items:center;">
      <a href="{{ url_for('charger_conv', i=conv.id) }}" style="flex:1;">{{ conv.titre }}</a>
      <button type="button" title="Partager" aria-label="Partager" onclick="partagerConversation({{ conv.id }})" style="border:0;background:none;cursor:pointer;padding:8px;">🔗</button>
      <form action="{{ url_for('archiver_conv', i=conv.id) }}" method="post" style="margin:0;"><input type="hidden" name="csrf_token" value="{{ csrf_token }}"><button type="submit" title="Archiver" aria-label="Archiver" style="border:0;background:none;cursor:pointer;padding:8px;">🗃</button></form>
      <form action="{{ url_for('supprimer_conv', i=conv.id) }}" method="post" style="margin:0;">
        <input type="hidden" name="csrf_token" value="{{ csrf_token }}">
        <button type="submit" onclick="return confirm('Supprimer cette conversation ?');" aria-label="Supprimer cette conversation" style="color:#c00;padding:8px 12px;border:0;background:none;cursor:pointer;font-size:20px;">&times;</button>
      </form>
    </div>
  {% endfor %}
  <div class="menu-section">Navigation</div>
  <a href="{{ url_for('parametres') }}">⚙ Paramètres</a>
  <a href="#" onclick="return false;" title="Fonctionnalité non disponible">📅 Planification <small>(bientôt)</small></a>
  <a href="#" onclick="return false;" title="Fonctionnalité non disponible">🔌 Plugins / Extensions <small>(bientôt)</small></a>
  <a href="#" onclick="return false;" title="Fonctionnalité non disponible">📁 Projets <small>(bientôt)</small></a>
</div>

<div id="chat">
  {% for m in messages %}
    <div class="message-wrap {{ 'user' if m.auteur == 'user' else 'bot' }}">
      <div class="msg {{ 'user' if m.auteur == 'user' else 'bot' }}" data-message-id="{{ m.id }}">{{ m.texte }}</div>
      {% if m.auteur == 'bot' %}
      <div class="actions-reponse">
        <button type="button" class="action-copier" title="Copier">📋</button><button type="button" class="action-feedback" data-valeur="positif" title="J'aime">👍</button><button type="button" class="action-feedback" data-valeur="negatif" title="Je n'aime pas">👎</button><button type="button" class="action-partager" title="Partager la conversation">🔗</button><button type="button" class="action-regenerer" title="Régénérer">🔄</button><button type="button" class="action-lire" title="Lecture / pause">▶</button><button type="button" class="action-stop" title="Arrêter">⏹</button><span class="lecture-etat"></span>
      </div>
      {% endif %}
    </div>
  {% endfor %}
</div>

<section id="mode-vocal" data-etat="attente" aria-label="Mode vocal" aria-hidden="true">
  <div class="vocal-entete">
    <strong>Conversation vocale</strong>
    <div class="vocal-commandes">
      <button type="button" id="reduire-vocal" title="Revenir au chat en gardant la conversation active">Réduire</button>
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

<form class="bas" id="form-message" autocomplete="off" method="post" action="{{ url_for('repondre_flux') }}"><input type="hidden" name="csrf_token" value="{{ csrf_token }}">
  <input type="file" id="image-input" accept="image/*,video/*" style="display:none;">
  <button type="button" id="btn-attach" style="background:none;border:none;cursor:pointer;flex-shrink:0;padding:0;width:34px;height:34px;" onclick="document.getElementById('image-input').click();"><img src="{{ url_for('static', filename='icon-attach.png') }}" style="width:34px;height:34px;display:block;border-radius:8px;"></button>
  <textarea id="message" name="message" rows="1" placeholder="Écris à Dashle..." required></textarea>
  <div class="groupe-actions">
    <button type="button" class="vocal" id="btn-vocal" title="Discuter en vocal avec Dashle">
      <svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round">
        <line x1="2" y1="9" x2="2" y2="15"></line>
        <line x1="7" y1="6" x2="7" y2="18"></line>
        <line x1="12" y1="3" x2="12" y2="21"></line>
        <line x1="17" y1="6" x2="17" y2="18"></line>
        <line x1="22" y1="9" x2="22" y2="15"></line>
      </svg>
    </button>
    <button type="button" class="micro" id="btn-micro">🎙️</button>
    <button class="envoyer" type="submit" id="btn-envoyer">&#10148;</button>
  </div>
</form>
<div id="apercu-fichier" aria-live="polite">
  <img id="apercu-fichier-media" alt="Aperçu du fichier sélectionné">
  <div id="apercu-fichier-info"><span id="apercu-fichier-nom"></span><span id="apercu-fichier-type"></span></div>
  <button type="button" id="retirer-fichier" aria-label="Retirer le fichier sélectionné" title="Retirer le fichier">&times;</button>
</div>
<div id="statut-vocal"></div>

<script>
const chat = document.getElementById('chat');
const form = document.getElementById('form-message');
const champ = document.getElementById('message');
const btnEnvoyer = document.getElementById('btn-envoyer');
const btnMicro = document.getElementById('btn-micro');
const btnVocal = document.getElementById('btn-vocal');
const statutVocal = document.getElementById('statut-vocal');
const modeVocal = document.getElementById('mode-vocal');
const etatVocal = document.getElementById('etat-vocal');
const apercuFichier = document.getElementById('apercu-fichier');
let apercuMedia = document.getElementById('apercu-fichier-media');
const apercuNom = document.getElementById('apercu-fichier-nom');
const apercuType = document.getElementById('apercu-fichier-type');
const inputImage = document.getElementById('image-input');
const csrfToken = {{ csrf_token|tojson }};
const preferencesVocales = {{ preferences|tojson }};
let reco = null;

let vocalActif = false;   // mode "conversation vocale en boucle" activé ou non
let vocalReduit = false;  // mode vocal actif mais panneau réduit
let modeActuel = 'texte'; // 'texte' | 'dictee' | 'vocal' : d'où vient la dernière écoute
let ecouteActive = false;   // une reconnaissance vocale est en cours (évite deux start())
let reponseEnCours = false; // Dashle est en train de répondre : on ne s'écoute pas soi-même
let microBloque = false;    // micro refusé par le navigateur : on arrête de réessayer

function afficherEtatVocal(etat, libelle) {
  modeVocal.dataset.etat = etat;
  etatVocal.textContent = libelle;
}

function ouvrirModeVocal() {
  modeVocal.classList.add('visible');
  modeVocal.setAttribute('aria-hidden', 'false');
}

function fermerModeVocal() {
  modeVocal.classList.remove('visible');
  modeVocal.setAttribute('aria-hidden', 'true');
}

function afficherStatutVocal(texte) {
  statutVocal.textContent = texte;
  statutVocal.classList.toggle('visible', !!texte);
}

if ('SpeechRecognition' in window || 'webkitSpeechRecognition' in window) {
  const Reco = window.SpeechRecognition || window.webkitSpeechRecognition;
  reco = new Reco();
  reco.lang = 'fr-FR';
  reco.interimResults = false;

  function arreterEcoute() {
    ecouteActive = false;
    try { reco.stop(); } catch (e) { /* session déjà terminée */ }
  }

  function demarrerEcouteVocale() {
    if (!vocalActif || ecouteActive || microBloque) return; // idempotent : jamais deux start()
    modeActuel = 'vocal';
    ouvrirModeVocal();
    afficherEtatVocal('ecoute', 'Dashle écoute...');
    btnVocal.classList.add('ecoute');
    btnVocal.classList.remove('parle');
    afficherStatutVocal("🎧 Je t'écoute...");
    arreterLecture(); // Dashle se tait dès que tu reprends la parole
    try {
      reco.start();
      ecouteActive = true;
    } catch (e) {
      ecouteActive = false;
      afficherStatutVocal('🎙️ Micro indisponible (' + ((e && e.name) || 'erreur') + ').');
    }
  }

  // Dictée simple (un seul message, on garde le contrôle avant l'envoi)
  btnMicro.onclick = function() {
  if (vocalActif && !vocalReduit) return;

  microBloque = false;
  modeActuel = 'dictee';
  btnMicro.classList.add('actif');

  const lancerMicro = function() {
    try {
      reco.start();
      ecouteActive = true;
    } catch (e) {
      ecouteActive = false;
      btnMicro.classList.remove('actif');
      afficherStatutVocal(
        '🎙️ Micro indisponible (' +
        ((e && e.name) || 'erreur') +
        ').'
      );
    }
  };

    if (ecouteActive) {
  arreterEcoute();

  setTimeout(function() {
    if (!ecouteActive) {
      lancerMicro();
    }
  }, 600);
} else {
  lancerMicro();
}
};
  btnVocal.onclick = function() {

    if (vocalActif && vocalReduit) {
    vocalReduit = false;
    modeActuel = 'vocal';
    btnVocal.classList.add('vocal-on');
    ouvrirModeVocal();
    demarrerEcouteVocale();
    return;
  }
    vocalActif = !vocalActif;
    microBloque = false;
    if (vocalActif) {
      btnVocal.classList.add('vocal-on');
      ouvrirModeVocal();
      if (ecouteActive) {
        // onend relancera l'écoute en mode vocal : on ne fait pas stop()+start() collés
        arreterEcoute();
      } else {
        demarrerEcouteVocale();
      }
    } else {
      btnVocal.classList.remove('vocal-on', 'ecoute', 'parle');
      fermerModeVocal();
      afficherEtatVocal('attente', 'En attente');
      afficherStatutVocal('');
      arreterEcoute();
      arreterLecture();
    }
  };

  reco.onresult = function(e) {
    const resultat = e.results[0] && e.results[0][0];
    const transcript = resultat ? resultat.transcript.trim() : '';
    if (!transcript) return;
    champ.value = transcript;
    champ.style.height = 'auto';
    if (modeActuel === 'vocal') {
      afficherEtatVocal('reflexion', 'Dashle réfléchit...');
      afficherStatutVocal('');
      form.requestSubmit();
    }
  };

  reco.onend = function() {
    ecouteActive = false;
    btnMicro.classList.remove('actif');
    btnVocal.classList.remove('ecoute');
    // En mode vocal, l'écoute reprend dès que Dashle a fini de répondre.
    if (vocalActif && !vocalReduit && modeActuel === 'vocal' && !reponseEnCours) {
  setTimeout(demarrerEcouteVocale, 400);
}

  reco.onerror = function(e) {
    ecouteActive = false;
    btnMicro.classList.remove('actif');
    btnVocal.classList.remove('ecoute');
    if (e.error === 'not-allowed' || e.error === 'service-not-allowed') {
      microBloque = true;
      afficherEtatVocal('attente', 'Micro refusé');
      afficherStatutVocal('🎙️ Micro refusé : autorise le microphone dans le navigateur (et utilise localhost ou https).');
    } else if (vocalActif && e.error !== 'aborted' && e.error !== 'no-speech') {
      afficherEtatVocal('attente', 'En attente du micro...');
      afficherStatutVocal("🎧 Petit souci d'écoute, je réessaie...");
      setTimeout(demarrerEcouteVocale, 900);
    }
  };

  // Exposées pour le handler d'envoi plus bas
  window._dashleVocal = {
    estActif: function() { return vocalActif; },
    reprendreEcoute: demarrerEcouteVocale,
    marquerEnvoi: function() { reponseEnCours = true; },
    marquerFin: function() {
      reponseEnCours = false;
      if (!vocalActif || microBloque || champ.disabled) return; // envoi bloqué : on ne relance pas
      if (lectureActuelle || ('speechSynthesis' in window && window.speechSynthesis.speaking)) return; // le TTS relancera l'écoute
      setTimeout(demarrerEcouteVocale, 400);
    },
    marquerParle: function() {
      ouvrirModeVocal();
      afficherEtatVocal('parle', 'Dashle parle...');
      btnVocal.classList.add('parle');
      btnVocal.classList.remove('ecoute');
      afficherStatutVocal('🗣️ Dashle répond...');
    }
  };
} else {
  // Le navigateur ne sait pas transcrire la voix : on le dit clairement au lieu de
  // masquer les boutons en silence.
  btnMicro.disabled = true;
  btnVocal.disabled = true;
  btnMicro.style.opacity = '0.45';
  btnVocal.style.opacity = '0.45';
  const messageVocal = 'Reconnaissance vocale non prise en charge par ce navigateur : utilise Chrome ou Edge.';
  btnMicro.title = messageVocal;
  btnVocal.title = messageVocal;
}

let fichierImage = null;
function effacerApercuFichier() {
  if (apercuMedia.dataset.url) URL.revokeObjectURL(apercuMedia.dataset.url);
  apercuMedia.removeAttribute('src');
  delete apercuMedia.dataset.url;
  apercuFichier.classList.remove('visible');
  fichierImage = null;
  inputImage.value = '';
}

function afficherApercuFichier(fichier) {
  if (!fichier) { effacerApercuFichier(); return; }
  if (apercuMedia.dataset.url) URL.revokeObjectURL(apercuMedia.dataset.url);
  const url = URL.createObjectURL(fichier);
  if (fichier.type.startsWith('video/')) {
    const lecteur = document.createElement('video');
    lecteur.id = 'apercu-fichier-media';
    lecteur.controls = true;
    lecteur.muted = true;
    lecteur.playsInline = true;
    lecteur.setAttribute('aria-label', 'Aperçu vidéo de ' + fichier.name);
    apercuMedia.replaceWith(lecteur);
    apercuMedia = lecteur;
  } else if (apercuMedia.tagName !== 'IMG') {
    const image = document.createElement('img');
    image.id = 'apercu-fichier-media';
    image.alt = 'Aperçu du fichier sélectionné';
    apercuMedia.replaceWith(image);
    apercuMedia = image;
  }
  apercuMedia.dataset.url = url;
  apercuMedia.src = url;
  apercuMedia.alt = 'Aperçu de ' + fichier.name;
  apercuNom.textContent = fichier.name;
  apercuType.textContent = (fichier.type || 'Type inconnu') + ' · ' + Math.ceil(fichier.size / 1024) + ' Ko';
  apercuFichier.classList.add('visible');
}

inputImage.addEventListener('change', function(e) {
  fichierImage = e.target.files[0] || null;
  afficherApercuFichier(fichierImage);
});
document.getElementById('retirer-fichier').addEventListener('click', effacerApercuFichier);
document.getElementById('reduire-vocal').addEventListener('click', function() {
  vocalReduit = true;
  fermerModeVocal();
});
document.getElementById('fermer-vocal').addEventListener('click', function() {
  vocalActif = false;
  btnVocal.classList.remove('vocal-on', 'ecoute', 'parle');
  try { reco && reco.stop(); } catch (e) {}
  afficherStatutVocal('');
  fermerModeVocal();
  afficherEtatVocal('attente', 'En attente');
});

function bloquerEnvoi(dureeSecondes) {
  champ.disabled = true;
  btnEnvoyer.disabled = true;
  let restant = dureeSecondes;
  const placeholderOriginal = champ.placeholder;
  champ.placeholder = 'Patiente ' + restant + 's...';
  const interval = setInterval(function() {
    restant--;
    if (restant <= 0) {
      clearInterval(interval);
      champ.disabled = false;
      btnEnvoyer.disabled = false;
      champ.placeholder = placeholderOriginal;
    } else {
      champ.placeholder = 'Patiente ' + restant + 's...';
    }
  }, 1000);
}

// Toujours descendre en bas au chargement
chat.scrollTop = chat.scrollHeight;

// Envoi via Entrée (sans Maj)
champ.addEventListener('keydown', function(e) {
  if (e.key === 'Enter' && !e.shiftKey) {
    e.preventDefault();
    form.requestSubmit();
  }
});

function ajouterMessage(texte, classe) {
  const div = document.createElement('div');
  div.className = 'msg ' + classe;
  div.textContent = texte;
  const enveloppe = document.createElement('div');
  enveloppe.className = 'message-wrap ' + classe;
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
  enveloppe.innerHTML = '<div class="actions-reponse"><button type="button" class="action-copier" title="Copier">📋</button><button type="button" class="action-feedback" data-valeur="positif" title="J&#x27;aime">👍</button><button type="button" class="action-feedback" data-valeur="negatif" title="Je n&#x27;aime pas">👎</button><button type="button" class="action-partager" title="Partager">🔗</button><button type="button" class="action-regenerer" title="Régénérer">🔄</button><button type="button" class="action-lire" title="Lecture / pause">▶</button><button type="button" class="action-stop" title="Arrêter">⏹</button><span class="lecture-etat"></span></div>';
  enveloppe.insertBefore(message, enveloppe.firstChild);
  chat.appendChild(enveloppe);
  chat.scrollTop = chat.scrollHeight;
  return enveloppe;
}

let lectureActuelle = null;
let utteranceActuelle = null;
let voixDisponibles = [];

function chargerVoix() {
  if ('speechSynthesis' in window) voixDisponibles = window.speechSynthesis.getVoices();
}

function choisirVoixFrancaise() {
  const francaises = voixDisponibles.filter(function(voix) {
    return voix.lang && voix.lang.toLowerCase().startsWith('fr');
  });
  const marqueursFeminins = /female|femme|woman|amelie|audrey|claire|julie|marie|sophie|hortense|celine|victoria|eloquence/i;
  return francaises.find(function(voix) { return voix.name === preferencesVocales.voix_nom; }) ||
    francaises.find(function(voix) { return marqueursFeminins.test(voix.name); }) ||
    francaises[0] || voixDisponibles[0] || null;
}

chargerVoix();
if ('speechSynthesis' in window) window.speechSynthesis.onvoiceschanged = chargerVoix;

function arreterLecture() {
  if ('speechSynthesis' in window) window.speechSynthesis.cancel();
  if (lectureActuelle) {
    lectureActuelle.classList.remove('actif');
    lectureActuelle.textContent = '▶';
    lectureActuelle.closest('.actions-reponse').querySelector('.lecture-etat').textContent = '';
  }
  lectureActuelle = null;
  utteranceActuelle = null;
}

function lireReponse(bouton) {
  if (!('speechSynthesis' in window)) {
    bouton.closest('.actions-reponse').querySelector('.lecture-etat').textContent = 'Voix indisponible';
    return;
  }
  if (preferencesVocales.voix_active === false) {
    bouton.closest('.actions-reponse').querySelector('.lecture-etat').textContent = 'Voix désactivée';
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
  utteranceActuelle = new SpeechSynthesisUtterance(texte);
  utteranceActuelle.lang = 'fr-FR';
  const voix = choisirVoixFrancaise();
  if (voix) utteranceActuelle.voice = voix;
  utteranceActuelle.rate = Number(preferencesVocales.voix_vitesse) || 1;
  utteranceActuelle.pitch = Number(preferencesVocales.voix_tonalite) || 1;
  utteranceActuelle.volume = Number(preferencesVocales.voix_volume) || 1;
  lectureActuelle = bouton;
  bouton.classList.add('actif');
  bouton.textContent = '⏸';
  etat.textContent = 'Lecture';
  utteranceActuelle.onend = arreterLecture;
  utteranceActuelle.onerror = function() { etat.textContent = 'Erreur audio'; arreterLecture(); };
  window.speechSynthesis.speak(utteranceActuelle);
}

async function partagerConversation(conversationId) {
  const res = await fetch('/partager/' + conversationId, { method:'POST', headers:{'X-CSRF-Token': csrfToken} });
  const data = await res.json();
  if (data.url) {
    try { await navigator.clipboard.writeText(data.url); } catch (e) {}
    alert('Lien de partage copié : ' + data.url);
  }
}

document.getElementById('recherche-conversations').addEventListener('input', function() {
  const terme = this.value.toLowerCase().trim();
  document.querySelectorAll('.ligne-conversation').forEach(function(ligne) {
    ligne.style.display = !terme || ligne.dataset.titre.includes(terme) ? 'flex' : 'none';
  });
});

chat.addEventListener('click', async function(e) {
  const bouton = e.target.closest('button');
  if (!bouton) return;
  const enveloppe = bouton.closest('.message-wrap');
  const message = enveloppe && enveloppe.querySelector('.msg');
  if (!message) return;
  if (bouton.classList.contains('action-copier')) {
    await navigator.clipboard.writeText(message.textContent);
    bouton.classList.add('actif');
  } else if (bouton.classList.contains('action-lire')) {
    lireReponse(bouton);
  } else if (bouton.classList.contains('action-stop')) {
    arreterLecture();
  } else if (bouton.classList.contains('action-feedback')) {
    const corps = 'message_id=' + encodeURIComponent(message.dataset.messageId) + '&valeur=' + bouton.dataset.valeur;
    await fetch('/feedback', { method:'POST', headers:{'Content-Type':'application/x-www-form-urlencoded','X-CSRF-Token':csrfToken}, body:corps });
    bouton.classList.add('actif');
  } else if (bouton.classList.contains('action-regenerer')) {
    const res = await fetch('/regenerer/' + message.dataset.messageId, { method:'POST', headers:{'X-CSRF-Token':csrfToken} });
    const data = await res.json();
    if (data.reponse) ajouterReponse(data.reponse, data.message_id);
  } else if (bouton.classList.contains('action-partager')) {
    partagerConversation({{ conversation_id }});
  }
});

function afficherReflexion() {
  const div = document.createElement('div');
  div.className = 'reflexion';
  div.id = 'reflexion-active';
  div.innerHTML = '<div class="dot"></div>';
  chat.appendChild(div);
  chat.scrollTop = chat.scrollHeight;
}

function retirerReflexion() {
  const el = document.getElementById('reflexion-active');
  if (el) el.remove();
}

form.addEventListener('submit', async function(e) {
  e.preventDefault();
  const texte = champ.value.trim();
  if (!texte && !fichierImage) return;

  if (fichierImage) {
    ajouterMessage(texte || '📷 Image envoyée', 'user');
    champ.value = '';
    champ.style.height = 'auto';
    afficherReflexion();
    if (window._dashleVocal) window._dashleVocal.marquerEnvoi();
    const formData = new FormData();
    formData.append('message', texte);
    formData.append('image', fichierImage);
    try {
      const res = await fetch("{{ url_for('repondre_image') }}", { method: 'POST', headers: { 'X-CSRF-Token': csrfToken }, body: formData });
      const data = await res.json();
      retirerReflexion();
      ajouterReponse(data.reponse, data.message_id);
    } catch (err) {
      retirerReflexion();
      ajouterMessage("Erreur d'envoi de l'image. Réessaie.", 'bot');
    }
    fichierImage = null;
    effacerApercuFichier();
    if (window._dashleVocal) window._dashleVocal.marquerFin();
    return;
  }
  if (!texte) return;
  if (window._dashleVocal) window._dashleVocal.marquerEnvoi();

  ajouterMessage(texte, 'user');
  champ.value = '';
  champ.style.height = 'auto';
  afficherReflexion();

  try {
    const res = await fetch("{{ url_for('repondre_flux') }}", {
      method: 'POST',
      headers: { 'Content-Type': 'application/x-www-form-urlencoded', 'X-CSRF-Token': csrfToken },
      body: 'message=' + encodeURIComponent(texte)
    });
    retirerReflexion();
    if (!res.ok || !res.body) throw new Error('Flux indisponible');
    const reponseElement = ajouterReponse('', '');
    const messageElement = reponseElement.querySelector('.msg');
    const lecteur = res.body.getReader();
    const decodeur = new TextDecoder();
    let tampon = '';
    let reponseTexte = '';
    let messageId = null;
    while (true) {
      const morceau = await lecteur.read();
      if (morceau.done) break;
      tampon += decodeur.decode(morceau.value, {stream:true});
      const lignes = tampon.split('\\n');
      tampon = lignes.pop();
      for (const ligne of lignes) {
        if (!ligne.startsWith('data:')) continue;
        const evenement = JSON.parse(ligne.slice(5).trim());
        if (evenement.morceau) {
          reponseTexte += evenement.morceau;
          messageElement.textContent = reponseTexte;
          chat.scrollTop = chat.scrollHeight;
        }
        if (evenement.termine) messageId = evenement.message_id;
      }
    }
    messageElement.dataset.messageId = messageId || '';
    const data = {reponse: reponseTexte};

    const vocal = window._dashleVocal;
    const enModeVocal = vocal && vocal.estActif();

    if (enModeVocal) {
      vocal.marquerParle();
      const boutonLecture = reponseElement.querySelector('.action-lire');
      lireReponse(boutonLecture);
      if (utteranceActuelle) {
        const reprise = utteranceActuelle.onend;
        utteranceActuelle.onend = function() { reprise(); vocal.reprendreEcoute(); };
      }
    }

    if (data.reponse && data.reponse.toLowerCase().includes('quota')) {
      bloquerEnvoi(30);
    }
  } catch (err) {
    retirerReflexion();
    ajouterMessage("Erreur de connexion au serveur. Réessaie.", 'bot');
    if (window._dashleVocal && window._dashleVocal.estActif()) {
      setTimeout(window._dashleVocal.reprendreEcoute, 1000);
    }
  }
});

// Textarea qui grandit avec le texte
champ.addEventListener('input', function() {
  this.style.height = 'auto';
  this.style.height = Math.min(this.scrollHeight, 100) + 'px';
});
</script>

</body>
</html>
"""

SHARE_PAGE = """
<!doctype html><html lang="fr"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Dashle - {{ titre }}</title>
<style>body{font-family:Segoe UI,sans-serif;background:#f4f8f6;color:#14251f;margin:0}.partage{max-width:760px;margin:0 auto;padding:28px 18px}.marque{color:#10A37F;font-weight:700}.message{padding:12px 16px;margin:12px 0;border-radius:14px;white-space:pre-wrap;line-height:1.45;background:#fff;box-shadow:0 2px 10px #1231}.user{margin-left:15%;background:#e2f7ed}.bot{margin-right:15%}</style></head>
<body><main class="partage"><div class="marque">Dashle</div><h1>{{ titre }}</h1>{% for m in messages %}<div class="message {{ m.auteur }}">{{ m.texte }}</div>{% endfor %}</main></body></html>
"""

SETTINGS_PAGE = """
<!doctype html><html lang="fr"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Dashle - Paramètres</title>
<style>:root{font-family:Segoe UI,sans-serif;color:#17251f;background:#f4f8f6}*{box-sizing:border-box}body{margin:0}.page{max-width:760px;margin:auto;padding:24px 18px 50px}.bar{display:flex;justify-content:space-between;align-items:center;margin-bottom:22px}.bar a{color:#10A37F;text-decoration:none;font-weight:600}.carte{background:#fff;border:1px solid #dceae4;border-radius:14px;padding:18px;margin:12px 0}.carte h2{font-size:15px;margin:0 0 14px;color:#10A37F}label{display:flex;justify-content:space-between;gap:14px;align-items:center;padding:10px 0;border-top:1px solid #edf2f0}label:first-of-type{border-top:0}select,input[type=checkbox],input[type=range]{accent-color:#10A37F}select{max-width:100%;padding:7px;border:1px solid #dceae4;border-radius:7px}input[type=range]{width:160px}button{border:0;border-radius:9px;background:#10A37F;color:#fff;padding:10px 14px;cursor:pointer}.secondaire{background:#e5f3ed;color:#087355}.note{color:#71837b;font-size:13px}</style></head>
<body><main class="page"><div class="bar"><div><strong>Dashle</strong><h1>Paramètres</h1></div><a href="{{ url_for('accueil') }}">Retour au chat</a></div>{% if erreur %}<p class="note">{{ erreur }}</p>{% endif %}{% if succes %}<p class="note">{{ succes }}</p>{% endif %}
<form method="post"><input type="hidden" name="csrf_token" value="{{ csrf_token }}"><section class="carte"><h2>Compte</h2><p>{{ utilisateur }}</p><p class="note">La modification de l'adresse e-mail et la récupération de compte ne sont pas encore disponibles.</p></section>
<section class="carte"><h2>Apparence</h2><label>Thème<select name="theme"><option value="clair" {% if preferences.theme == 'clair' %}selected{% endif %}>Clair</option><option value="sombre" {% if preferences.theme == 'sombre' %}selected{% endif %}>Sombre</option></select></label></section>
<section class="carte"><h2>Voix</h2><label>Voix activée<input type="checkbox" name="voix_active" {% if preferences.voix_active %}checked{% endif %}></label><label>Voix française<select id="voix-select" name="voix_nom" data-selection="{{ preferences.voix_nom }}"><option value="">Automatique</option></select></label><label>Vitesse<input type="range" name="voix_vitesse" min="0.6" max="1.4" step="0.05" value="{{ preferences.voix_vitesse }}"><output id="vitesse-valeur">{{ preferences.voix_vitesse }}</output></label><label>Tonalité<input type="range" name="voix_tonalite" min="0.7" max="1.3" step="0.05" value="{{ preferences.voix_tonalite }}"><output id="tonalite-valeur">{{ preferences.voix_tonalite }}</output></label><label>Volume<input type="range" name="voix_volume" min="0.2" max="1" step="0.05" value="{{ preferences.voix_volume }}"><output id="volume-valeur">{{ preferences.voix_volume }}</output></label><button type="button" class="secondaire" id="tester-voix">▶ Tester la voix</button><p class="note">Dashle privilégie automatiquement une voix féminine française disponible sur ton navigateur. La lecture automatique reste désactivée par défaut.</p></section>
<section class="carte"><h2>Conversations et confidentialité</h2><label>Conserver l'historique<input type="checkbox" name="conserver_historique" {% if preferences.conserver_historique %}checked{% endif %}></label><p class="note">Les conversations partagées utilisent un lien révocable et ne montrent pas les informations du compte.</p></section>
<section class="carte"><h2>Sécurité</h2><p class="note">Les mots de passe sont hachés. La gestion avancée des sessions et le changement de mot de passe restent à implémenter.</p></section><button type="submit">Enregistrer</button></form><script>const selectVoix=document.getElementById('voix-select');let voixParametres=[];function remplirVoix(){voixParametres='speechSynthesis' in window ? speechSynthesis.getVoices().filter(v=>v.lang&&v.lang.toLowerCase().startsWith('fr')):[];selectVoix.innerHTML='<option value="">Automatique</option>';voixParametres.forEach(v=>{const option=document.createElement('option');option.value=v.name;option.textContent=v.name+' ('+v.lang+')';option.selected=v.name===selectVoix.dataset.selection;selectVoix.appendChild(option);});}remplirVoix();if('speechSynthesis' in window)speechSynthesis.onvoiceschanged=remplirVoix;function reglerSorties(){document.getElementById('vitesse-valeur').value=document.querySelector('[name=voix_vitesse]').value;document.getElementById('tonalite-valeur').value=document.querySelector('[name=voix_tonalite]').value;document.getElementById('volume-valeur').value=document.querySelector('[name=voix_volume]').value;}document.querySelectorAll('input[type=range]').forEach(i=>i.addEventListener('input',reglerSorties));document.getElementById('tester-voix').addEventListener('click',()=>{if(!('speechSynthesis' in window)){return;}speechSynthesis.cancel();const u=new SpeechSynthesisUtterance('Bonjour, je suis Dashle.');u.lang='fr-FR';u.voice=voixParametres.find(v=>v.name===selectVoix.value)||voixParametres[0]||null;u.rate=Number(document.querySelector('[name=voix_vitesse]').value);u.pitch=Number(document.querySelector('[name=voix_tonalite]').value);u.volume=Number(document.querySelector('[name=voix_volume]').value);speechSynthesis.speak(u);});</script></main></body></html>
"""

SETTINGS_PAGE = SETTINGS_PAGE.replace(
  "Retour au chat</a>",
  "Retour au chat</a> <a href=\"{{ url_for('securite') }}\">Sécurité</a>",
)

SECURITY_PAGE = """
<!doctype html><html lang="fr"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Dashle - Sécurité</title>
<style>:root{font-family:Segoe UI,sans-serif;color:#17251f;background:#f4f8f6}*{box-sizing:border-box}body{margin:0}.page{max-width:620px;margin:auto;padding:24px 18px 50px}.bar{display:flex;justify-content:space-between;align-items:center;margin-bottom:22px}.bar a{color:#10A37F;text-decoration:none;font-weight:600}.carte{background:#fff;border:1px solid #dceae4;border-radius:14px;padding:18px;margin:12px 0}.carte h2{font-size:15px;margin:0 0 14px;color:#10A37F}label{display:block;margin-top:12px;font-size:14px}input{display:block;width:100%;margin-top:5px;padding:10px;border:1px solid #dceae4;border-radius:8px}button{border:0;border-radius:9px;background:#10A37F;color:#fff;padding:10px 14px;margin-top:16px;cursor:pointer}.danger{background:#b42318}.note{color:#71837b;font-size:13px}.message{padding:10px;border-radius:8px;background:#e5f3ed;color:#087355}</style></head>
<body><main class="page"><div class="bar"><div><strong>Dashle</strong><h1>Sécurité</h1></div><a href="{{ url_for('parametres') }}">Retour aux paramètres</a></div>{% if erreur %}<p class="note">{{ erreur }}</p>{% endif %}{% if succes %}<p class="message">{{ succes }}</p>{% endif %}
<section class="carte"><h2>Modifier le mot de passe</h2><form method="post" action="{{ url_for('changer_mot_de_passe') }}"><input type="hidden" name="csrf_token" value="{{ csrf_token }}"><label>Ancien mot de passe<input type="password" name="ancien_password" required autocomplete="current-password"></label><label>Nouveau mot de passe<input type="password" name="nouveau_password" minlength="8" required autocomplete="new-password"></label><label>Confirmation<input type="password" name="confirmation_password" minlength="8" required autocomplete="new-password"></label><button type="submit">Modifier le mot de passe</button></form></section>
<section class="carte"><h2>Supprimer le compte</h2><p class="note">Cette action supprime définitivement le compte, les conversations, les préférences et la mémoire associée.</p><form method="post" action="{{ url_for('supprimer_compte') }}"><input type="hidden" name="csrf_token" value="{{ csrf_token }}"><label>Écris supprimer pour confirmer<input type="text" name="confirmation" required></label><button class="danger" type="submit">Supprimer définitivement</button></form></section></main></body></html>
"""

AUTH_PAGE = """
<!doctype html><html lang="fr"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Dashle — {{ titre }}</title>
<style>body{font-family:Segoe UI,sans-serif;background:#f5f7f6;margin:0;display:grid;place-items:center;min-height:100vh}.carte{width:min(360px,90vw);padding:28px;background:#fff;border-radius:14px;box-shadow:0 4px 18px #0002}h1{color:#10A37F;margin-top:0}label,input,button{display:block;width:100%;box-sizing:border-box}label{margin-top:14px}input{padding:10px;margin-top:5px;border:1px solid #ddd;border-radius:8px}button{margin-top:20px;padding:11px;border:0;border-radius:8px;background:#10A37F;color:#fff;cursor:pointer}.erreur{color:#b00020}</style></head>
<body><main class="carte"><h1>Dashle</h1><h2>{{ titre }}</h2>{% if erreur %}<p class="erreur">{{ erreur }}</p>{% endif %}<form method="post"><input type="hidden" name="csrf_token" value="{{ csrf_token }}"><label>E-mail<input name="email" type="email" required maxlength="254" autocomplete="email"></label><label>Mot de passe<input name="password" type="password" required minlength="8" autocomplete="{{ autocomplete }}"></label><button type="submit">{{ action }}</button></form><p>{{ texte_lien }} <a href="{{ url_for(lien) }}">{{ libelle_lien }}</a></p></main></body></html>
"""


def _conv_courante(user_id):
    """Renvoie l'identifiant d'une conversation appartenant à l'utilisateur."""
    conversation_id = session.get("conversation_id")
    with session_base() as db:
        conversation = db.query(Conversation).filter_by(id=conversation_id, user_id=user_id).one_or_none()
        if conversation is None:
            conversation = Conversation(user_id=user_id)
            db.add(conversation)
            db.flush()
            conversation_id = conversation.id
    session["conversation_id"] = conversation_id
    return conversation_id


def _liste_conversations(user_id):
    with session_base() as db:
        conversations = db.query(Conversation).filter_by(user_id=user_id, archivee=False).order_by(Conversation.updated_at.desc()).all()
        return [{"id": conv.id, "titre": conv.title} for conv in conversations]


def _messages_conversation(user_id, conversation_id):
    with session_base() as db:
        conversation = db.query(Conversation).filter_by(id=conversation_id, user_id=user_id).one_or_none()
        if conversation is None:
            return []
        return [{"id": msg.id, "auteur": msg.auteur, "texte": msg.texte,
             "date": msg.created_at.isoformat()} for msg in conversation.messages]


def _resume_conversation(user_id, conversation_id):
    with session_base() as db:
        conversation = db.query(Conversation).filter_by(id=conversation_id, user_id=user_id).one_or_none()
        return conversation.resume if conversation is not None else ""


def _actualiser_resume(user_id, conversation_id):
    historique = _messages_conversation(user_id, conversation_id)
    if len(historique) < 24 or (len(historique) - 24) % 12 != 0:
        return
    resume = _resume_conversation(user_id, conversation_id)
    nouveau_resume = resumer_conversation(historique, resume)
    if nouveau_resume and nouveau_resume != resume:
        with session_base() as db:
            conversation = db.query(Conversation).filter_by(id=conversation_id, user_id=user_id).one_or_none()
            if conversation is not None:
                conversation.resume = nouveau_resume


def _preferences(user_id):
    with session_base() as db:
        preferences = db.query(UserPreference).filter_by(user_id=user_id).one_or_none()
        if preferences is None:
            preferences = UserPreference(user_id=user_id)
            db.add(preferences)
            db.flush()
        return {
            "theme": preferences.theme,
            "voix_active": preferences.voix_active,
            "lecture_automatique": preferences.lecture_automatique,
            "conserver_historique": preferences.conserver_historique,
          "voix_nom": preferences.voix_nom,
          "voix_vitesse": preferences.voix_vitesse,
          "voix_tonalite": preferences.voix_tonalite,
          "voix_volume": preferences.voix_volume,
        }


def ajouter_message(user_id, conversation_id, texte, auteur):
    with session_base() as db:
        conversation = db.query(Conversation).filter_by(id=conversation_id, user_id=user_id).one_or_none()
        if conversation is None:
            raise LookupError("Conversation introuvable")
        message = Message(conversation_id=conversation.id, auteur=auteur, texte=texte)
        db.add(message)
        db.flush()
        if auteur == "user" and conversation.title == "Nouvelle conversation":
            conversation.title = texte[:48] or conversation.title
        conversation.updated_at = datetime.utcnow()
        return message.id


@app.route("/")
def accueil():
    user_id = session["user_id"]
    conversation_id = _conv_courante(user_id)
    return render_template_string(PAGE, conversations=_liste_conversations(user_id),
                    messages=_messages_conversation(user_id, conversation_id),
                    utilisateur={"email": session["user_email"]}, enumerate=enumerate,
                    csrf_token=jeton_csrf(), preferences=_preferences(user_id),
                    conversation_id=conversation_id)


@app.route("/nouvelle", methods=["POST"])
def nouvelle_conv():
    user_id = session["user_id"]
    with session_base() as db:
        conversation = Conversation(user_id=user_id)
        db.add(conversation)
        db.flush()
        session["conversation_id"] = conversation.id
    return redirect(url_for("accueil"))


@app.route("/conv/<int:i>")
def charger_conv(i):
    with session_base() as db:
        if db.query(Conversation).filter_by(id=i, user_id=session["user_id"]).one_or_none():
            session["conversation_id"] = i
    return redirect(url_for("accueil"))

@app.route("/supprimer_conv/<int:i>", methods=["POST"])
def supprimer_conv(i):
    with session_base() as db:
        conversation = db.query(Conversation).filter_by(id=i, user_id=session["user_id"]).one_or_none()
        if conversation:
            db.delete(conversation)
    if session.get("conversation_id") == i:
        session.pop("conversation_id", None)
    return redirect(url_for("accueil"))


@app.route("/archiver_conv/<int:i>", methods=["POST"])
def archiver_conv(i):
    with session_base() as db:
        conversation = db.query(Conversation).filter_by(id=i, user_id=session["user_id"]).one_or_none()
        if conversation is None:
            return jsonify({"erreur": "Conversation introuvable."}), 404
        conversation.archivee = True
    if session.get("conversation_id") == i:
        session.pop("conversation_id", None)
    return redirect(url_for("accueil"))


@app.route("/renommer/<int:i>", methods=["POST"])
def renommer_conv(i):
    titre = request.form.get("titre", "").strip()[:120]
    if not titre:
        return jsonify({"erreur": "Le titre est vide."}), 400
    with session_base() as db:
        conversation = db.query(Conversation).filter_by(id=i, user_id=session["user_id"]).one_or_none()
        if conversation is None:
            return jsonify({"erreur": "Conversation introuvable."}), 404
        conversation.title = titre
    return redirect(url_for("accueil"))


@app.route("/rechercher")
def rechercher():
    terme = request.args.get("q", "").strip().lower()
    user_id = session["user_id"]
    with session_base() as db:
        conversations = db.query(Conversation).filter(Conversation.user_id == user_id).order_by(
            Conversation.updated_at.desc()
        ).all()
        resultats = []
        for conversation in conversations:
            if not terme or terme in conversation.title.lower() or any(
                terme in message.texte.lower() for message in conversation.messages
            ):
                resultats.append({"id": conversation.id, "titre": conversation.title})
    return jsonify({"resultats": resultats})


@app.route("/feedback", methods=["POST"])
def feedback():
    message_id = request.form.get("message_id", type=int)
    valeur = request.form.get("valeur", "").strip().lower()
    if valeur not in {"positif", "negatif"} or not message_id:
        return jsonify({"erreur": "Retour invalide."}), 400
    with session_base() as db:
        message = db.query(Message).join(Conversation).filter(
            Message.id == message_id, Conversation.user_id == session["user_id"]
        ).one_or_none()
        if message is None or message.auteur != "bot":
            return jsonify({"erreur": "Réponse introuvable."}), 404
        retour = db.query(MessageFeedback).filter_by(
            user_id=session["user_id"], message_id=message_id
        ).one_or_none()
        if retour is None:
            db.add(MessageFeedback(user_id=session["user_id"], message_id=message_id, valeur=valeur))
        else:
            retour.valeur = valeur
    return jsonify({"ok": True, "valeur": valeur})


@app.route("/regenerer/<int:message_id>", methods=["POST"])
def regenerer(message_id):
    user_id = session["user_id"]
    with session_base() as db:
        message = db.query(Message).join(Conversation).filter(
            Message.id == message_id, Message.auteur == "bot", Conversation.user_id == user_id
        ).one_or_none()
        if message is None:
            return jsonify({"erreur": "Réponse introuvable."}), 404
        conversation_id = message.conversation_id
        historique = [{"auteur": item.auteur, "texte": item.texte} for item in db.query(Message).filter(
            Message.conversation_id == conversation_id, Message.id < message_id
        ).order_by(Message.id).all()]
        dernier_user = next((item["texte"] for item in reversed(historique) if item["auteur"] == "user"), "")
    if not dernier_user:
        return jsonify({"erreur": "Aucun message utilisateur à régénérer."}), 400
    reponse = traiter_message(dernier_user, historique, user_id, _resume_conversation(user_id, conversation_id))
    nouveau_message_id = ajouter_message(user_id, conversation_id, reponse, "bot")
    return jsonify({"reponse": reponse, "message_id": nouveau_message_id})


@app.route("/partager/<int:i>", methods=["POST"])
def creer_partage(i):
    with session_base() as db:
        conversation = db.query(Conversation).filter_by(id=i, user_id=session["user_id"]).one_or_none()
        if conversation is None:
            return jsonify({"erreur": "Conversation introuvable."}), 404
        lien = db.query(ShareLink).filter_by(conversation_id=i, actif=True).one_or_none()
        if lien is None:
            lien = ShareLink(conversation_id=i, token=secrets.token_urlsafe(32))
            db.add(lien)
            db.flush()
        return jsonify({"url": url_for("partage", token=lien.token, _external=True)})


@app.route("/partager/<int:i>/desactiver", methods=["POST"])
def desactiver_partage(i):
    with session_base() as db:
        lien = db.query(ShareLink).join(Conversation).filter(
            ShareLink.conversation_id == i, Conversation.user_id == session["user_id"], ShareLink.actif.is_(True)
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
        conversation = db.query(Conversation).filter_by(id=lien.conversation_id).one_or_none()
        if conversation is None:
            return "Conversation introuvable.", 404
        messages = [{"auteur": message.auteur, "texte": message.texte} for message in conversation.messages]
        titre = conversation.title
    return render_template_string(SHARE_PAGE, titre=titre, messages=messages)


@app.route("/parametres", methods=["GET", "POST"])
def parametres():
    user_id = session["user_id"]
    if request.method == "POST":
        def nombre_parametre(nom, minimum, maximum, valeur_defaut):
            try:
                return max(minimum, min(maximum, float(request.form.get(nom, valeur_defaut))))
            except (TypeError, ValueError):
                return valeur_defaut

        with session_base() as db:
            preferences = db.query(UserPreference).filter_by(user_id=user_id).one_or_none()
            if preferences is None:
                preferences = UserPreference(user_id=user_id)
                db.add(preferences)
            preferences.theme = request.form.get("theme", "clair") if request.form.get("theme") in {"clair", "sombre"} else "clair"
            preferences.voix_active = request.form.get("voix_active") == "on"
            preferences.lecture_automatique = False
            preferences.conserver_historique = request.form.get("conserver_historique") == "on"
            preferences.voix_nom = request.form.get("voix_nom", "")[:160]
            preferences.voix_vitesse = nombre_parametre("voix_vitesse", 0.6, 1.4, 1.0)
            preferences.voix_tonalite = nombre_parametre("voix_tonalite", 0.7, 1.3, 1.0)
            preferences.voix_volume = nombre_parametre("voix_volume", 0.2, 1.0, 1.0)
        return redirect(url_for("parametres"))
    return render_template_string(SETTINGS_PAGE, utilisateur=session["user_email"], preferences=_preferences(user_id), csrf_token=jeton_csrf(), erreur=request.args.get("erreur"), succes=request.args.get("succes"))


@app.route("/securite")
def securite():
    return render_template_string(
        SECURITY_PAGE,
        csrf_token=jeton_csrf(),
        erreur=request.args.get("erreur"),
        succes=request.args.get("succes"),
    )


@app.route("/repondre", methods=["POST"])
def repondre():
    """Endpoint appelé en AJAX : ne renvoie que du JSON, pas de rechargement de page."""
    user_id = session["user_id"]
    conversation_id = _conv_courante(user_id)
    message = request.form.get("message", "").strip()

    if not message:
        return jsonify({"reponse": ""})

    historique = _messages_conversation(user_id, conversation_id)
    resume = _resume_conversation(user_id, conversation_id)
    ajouter_message(user_id, conversation_id, message, "user")
    reponse = traiter_message(message, historique + [{"auteur": "user", "texte": message}], user_id, resume)
    message_id = ajouter_message(user_id, conversation_id, reponse, "bot")
    _actualiser_resume(user_id, conversation_id)

    return jsonify({"reponse": reponse, "message_id": message_id})


@app.route("/repondre_flux", methods=["POST"])
def repondre_flux():
    """Diffuse une réponse texte et persiste le message complet à la fin."""
    user_id = session["user_id"]
    conversation_id = _conv_courante(user_id)
    message = request.form.get("message", "").strip()
    if not message:
        return jsonify({"reponse": ""})

    historique = _messages_conversation(user_id, conversation_id)
    resume = _resume_conversation(user_id, conversation_id)
    ajouter_message(user_id, conversation_id, message, "user")

    @stream_with_context
    def generer():
        morceaux = []
        for morceau in streamer_message(message, historique + [{"auteur": "user", "texte": message}], user_id, resume):
            morceaux.append(morceau)
            yield "data: " + json.dumps({"morceau": morceau}, ensure_ascii=False) + "\n\n"
        reponse = "".join(morceaux).strip()
        message_id = ajouter_message(user_id, conversation_id, reponse, "bot")
        _actualiser_resume(user_id, conversation_id)
        yield "data: " + json.dumps({"termine": True, "message_id": message_id}, ensure_ascii=False) + "\n\n"

    return Response(generer(), mimetype="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

@app.route("/repondre_image", methods=["POST"])
def repondre_image():
    user_id = session["user_id"]
    conversation_id = _conv_courante(user_id)
    historique = _messages_conversation(user_id, conversation_id)
    resume = _resume_conversation(user_id, conversation_id)
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
            with Image.open(io.BytesIO(image_bytes)) as image:
                image.verify()
        except Exception:
          return jsonify({"reponse": "Le fichier envoyé n'est pas une image valide."}), 400

    image_b64 = base64.b64encode(image_bytes).decode("utf-8")
    type_media = "Vidéo" if mime_type.startswith("video/") else "Image"
    ajouterMessage_texte = message or f"[{type_media} envoyée]"
    ajouter_message(user_id, conversation_id, ajouterMessage_texte, "user")
    reponse = traiter_message_image(message, image_b64, mime_type, historique, resume)
    message_id = ajouter_message(user_id, conversation_id, reponse, "bot")
    _actualiser_resume(user_id, conversation_id)

    return jsonify({"reponse": reponse, "message_id": message_id})


@app.errorhandler(413)
def fichier_trop_volumineux(_erreur):
  return jsonify({"reponse": "Le fichier est trop volumineux (maximum : 8 Mo)."}), 413


@app.route("/inscription", methods=["GET", "POST"])
def inscription():
    erreur = None
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        if "@" not in email or len(email) > 254:
            erreur = "Indique une adresse e-mail valide."
        elif len(password) < 8:
            erreur = "Le mot de passe doit contenir au moins 8 caractères."
        else:
            try:
                with session_base() as db:
                    user = User(email=email, password_hash=generate_password_hash(password))
                    db.add(user)
                    db.flush()
                    session["user_id"] = user.id
                    session["user_email"] = user.email
                return redirect(url_for("accueil"))
            except IntegrityError:
                erreur = "Cette adresse e-mail est déjà utilisée."
    return render_template_string(AUTH_PAGE, titre="Créer un compte", action="S'inscrire", erreur=erreur,
                                  lien="connexion", texte_lien="Déjà un compte ?", libelle_lien="Se connecter",
                                  autocomplete="new-password", csrf_token=jeton_csrf())


@app.route("/connexion", methods=["GET", "POST"])
def connexion():
    erreur = None
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        with session_base() as db:
            user = db.query(User).filter_by(email=email).one_or_none()
            if user and check_password_hash(user.password_hash, request.form.get("password", "")):
                session.clear()
                session["user_id"] = user.id
                session["user_email"] = user.email
                return redirect(url_for("accueil"))
        erreur = "Adresse e-mail ou mot de passe incorrect."
    return render_template_string(AUTH_PAGE, titre="Connexion", action="Se connecter", erreur=erreur,
                                  lien="inscription", texte_lien="Pas encore de compte ?", libelle_lien="S'inscrire",
                                  autocomplete="current-password", csrf_token=jeton_csrf())


@app.route("/mot-de-passe", methods=["POST"])
def changer_mot_de_passe():
    ancien = request.form.get("ancien_password", "")
    nouveau = request.form.get("nouveau_password", "")
    confirmation = request.form.get("confirmation_password", "")
    if len(nouveau) < 8 or nouveau != confirmation:
        return redirect(url_for("securite", erreur="Le nouveau mot de passe est invalide."))
    with session_base() as db:
        user = db.query(User).filter_by(id=session["user_id"]).one_or_none()
        if user is None or not check_password_hash(user.password_hash, ancien):
            return redirect(url_for("securite", erreur="L'ancien mot de passe est incorrect."))
        user.password_hash = generate_password_hash(nouveau)
    return redirect(url_for("securite", succes="Mot de passe modifié."))


@app.route("/compte/supprimer", methods=["POST"])
def supprimer_compte():
    confirmation = request.form.get("confirmation", "").strip().lower()
    if confirmation != "supprimer":
        return redirect(url_for("securite", erreur="Écris supprimer pour confirmer."))
    with session_base() as db:
        user = db.query(User).filter_by(id=session["user_id"]).one_or_none()
        if user is not None:
            db.query(UserMemory).filter_by(user_id=user.id).delete()
            db.query(MessageFeedback).filter_by(user_id=user.id).delete()
            db.query(UserPreference).filter_by(user_id=user.id).delete()
            db.query(ShareLink).filter(ShareLink.conversation_id.in_(
                db.query(Conversation.id).filter_by(user_id=user.id)
            )).delete(synchronize_session=False)
            db.delete(user)
    session.clear()
    return redirect(url_for("inscription"))


@app.route("/deconnexion", methods=["POST"])
def deconnexion():
    session.clear()
    return redirect(url_for("connexion"))


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
