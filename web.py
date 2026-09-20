import os
import io
import base64
import secrets
from datetime import datetime
from flask import Flask, request, render_template_string, redirect, url_for, session, jsonify
from werkzeug.security import check_password_hash, generate_password_hash
from sqlalchemy.exc import IntegrityError
from app import traiter_message, traiter_message_image
from database import Conversation, Message, User, initialiser_base, session_base
from voice import generer_audio_web

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


def detecter_type_image(contenu):
    """Détermine le type depuis la signature, même sans Pillow."""
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
    publiques = {"static", "connexion", "inscription"}
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
  body { font-family: 'Segoe UI', sans-serif; margin: 0; background: #fff; height: 100vh; display: flex; flex-direction: column; }
  header { background: #10A37F; color: white; padding: 14px 16px; display: flex; align-items: center; }
  header .titre { font-weight: bold; font-size: 18px; margin-left: 10px; flex: 1; }
  header button { background: none; border: none; color: white; font-size: 20px; cursor: pointer; }

  #voile { display:none; position:fixed; top:0; left:0; width:100%; height:100%; background:rgba(0,0,0,0.3); z-index:5; }
  #sidebar { display:none; position:fixed; top:0; left:0; width:82%; max-width:320px; height:100%; background:#fff; z-index:6; overflow-y:auto; box-shadow:2px 0 8px rgba(0,0,0,0.15); }
  #sidebar h2 { padding: 20px 18px 10px; margin:0; }
  #sidebar a, #sidebar button.nouvelle { display:block; width:100%; padding:12px 18px; text-decoration:none; color:#111; border:0; border-bottom:1px solid #eee; background:#fff; font:inherit; cursor:pointer; }
  #sidebar a.nouvelle, #sidebar button.nouvelle { color:#10A37F; font-weight:bold; }

  #chat { flex:1; overflow-y:auto; padding: 14px; }
  .msg { max-width: 80%; padding: 10px 14px; border-radius: 14px; margin-bottom: 10px; white-space: pre-wrap; line-height:1.4; }
  .user { background:#DCF8C6; margin-left:auto; }
  .bot { background:#f0f0f0; margin-right:auto; }

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

  button.envoyer {
    background:#10A37F; color:white; border:none; border-radius:50%;
    width:36px; height:36px; font-size:15px; flex-shrink:0;
    display:flex; align-items:center; justify-content:center;
    cursor:pointer; transition: background 0.15s ease, opacity 0.15s ease;
  }
  button.envoyer:disabled { opacity:0.5; cursor:default; }
</style>
</head>
<body>

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
  <form action="{{ url_for('nouvelle_conv') }}" method="post" style="margin:0;">
    <input type="hidden" name="csrf_token" value="{{ csrf_token }}">
    <button class="nouvelle" type="submit" style="width:100%;text-align:left;">+ Nouvelle conversation</button>
  </form>
  {% for i, conv in enumerate(conversations) %}
    <div style="display:flex;align-items:center;">
      <a href="{{ url_for('charger_conv', i=conv.id) }}" style="flex:1;">{{ conv.titre }}</a>
      <form action="{{ url_for('supprimer_conv', i=conv.id) }}" method="post" style="margin:0;">
        <input type="hidden" name="csrf_token" value="{{ csrf_token }}">
        <button type="submit" onclick="return confirm('Supprimer cette conversation ?');" aria-label="Supprimer cette conversation" style="color:#c00;padding:8px 12px;border:0;background:none;cursor:pointer;font-size:20px;">&times;</button>
      </form>
    </div>
  {% endfor %}
</div>

<div id="chat">
  {% for m in messages %}
    <div class="msg {{ 'user' if m.auteur == 'user' else 'bot' }}">{{ m.texte }}</div>
  {% endfor %}
</div>

<form class="bas" id="form-message" autocomplete="off">
  <input type="file" id="image-input" accept="image/*" style="display:none;">
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
<div id="statut-vocal"></div>

<script>
const chat = document.getElementById('chat');
const form = document.getElementById('form-message');
const champ = document.getElementById('message');
const btnEnvoyer = document.getElementById('btn-envoyer');
const btnMicro = document.getElementById('btn-micro');
const btnVocal = document.getElementById('btn-vocal');
const statutVocal = document.getElementById('statut-vocal');
const csrfToken = {{ csrf_token|tojson }};

let vocalActif = false;   // mode "conversation vocale en boucle" activé ou non
let modeActuel = 'texte'; // 'texte' | 'dictee' | 'vocal' : d'où vient la dernière écoute

function afficherStatutVocal(texte) {
  statutVocal.textContent = texte;
  statutVocal.classList.toggle('visible', !!texte);
}

if ('SpeechRecognition' in window || 'webkitSpeechRecognition' in window) {
  const Reco = window.SpeechRecognition || window.webkitSpeechRecognition;
  const reco = new Reco();
  reco.lang = 'fr-FR';
  reco.interimResults = false;

  function demarrerEcouteVocale() {
    if (!vocalActif) return;
    modeActuel = 'vocal';
    btnVocal.classList.add('ecoute');
    btnVocal.classList.remove('parle');
    afficherStatutVocal('🎧 Je t\\'écoute...');
    try { reco.start(); } catch (e) { /* déjà démarré, on ignore */ }
  }

  // Dictée simple (un seul message, on garde le contrôle avant l'envoi)
  btnMicro.onclick = function() {
    if (vocalActif) return; // pas de dictée manuelle pendant le mode vocal
    modeActuel = 'dictee';
    btnMicro.classList.add('actif');
    try { reco.start(); } catch (e) {}
  };

  // Toggle du mode conversation vocale en boucle
  btnVocal.onclick = function() {
    vocalActif = !vocalActif;
    if (vocalActif) {
      btnVocal.classList.add('vocal-on');
      try { reco.stop(); } catch (e) {}
      demarrerEcouteVocale();
    } else {
      btnVocal.classList.remove('vocal-on', 'ecoute', 'parle');
      afficherStatutVocal('');
      try { reco.stop(); } catch (e) {}
    }
  };

  reco.onresult = function(e) {
    const transcript = e.results[0][0].transcript;
    champ.value = transcript;
    champ.style.height = 'auto';
    if (modeActuel === 'vocal') {
      afficherStatutVocal('');
      form.requestSubmit();
    }
  };

  reco.onend = function() {
    btnMicro.classList.remove('actif');
    btnVocal.classList.remove('ecoute');
  };

  reco.onerror = function(e) {
    btnMicro.classList.remove('actif');
    btnVocal.classList.remove('ecoute');
    if (vocalActif && e.error !== 'aborted') {
      afficherStatutVocal('🎧 Petit souci d\\'écoute, je réessaie...');
      setTimeout(demarrerEcouteVocale, 900);
    }
  };

  // Exposées pour le handler d'envoi plus bas
  window._dashleVocal = {
    estActif: function() { return vocalActif; },
    reprendreEcoute: demarrerEcouteVocale,
    marquerParle: function() {
      btnVocal.classList.add('parle');
      btnVocal.classList.remove('ecoute');
      afficherStatutVocal('🗣️ Dashle répond...');
    }
  };
} else {
  btnMicro.style.display = 'none';
  btnVocal.style.display = 'none';
}

let fichierImage = null;
document.getElementById('image-input').addEventListener('change', function(e) {
  fichierImage = e.target.files[0] || null;
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
  chat.appendChild(div);
  chat.scrollTop = chat.scrollHeight;
  return div;
}

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
    const formData = new FormData();
    formData.append('message', texte);
    formData.append('image', fichierImage);
    try {
      const res = await fetch("{{ url_for('repondre_image') }}", { method: 'POST', headers: { 'X-CSRF-Token': csrfToken }, body: formData });
      const data = await res.json();
      retirerReflexion();
      ajouterMessage(data.reponse, 'bot');
    } catch (err) {
      retirerReflexion();
      ajouterMessage("Erreur d'envoi de l'image. Réessaie.", 'bot');
    }
    fichierImage = null;
    document.getElementById('image-input').value = '';
    return;
  }
  if (!texte) return;

  ajouterMessage(texte, 'user');
  champ.value = '';
  champ.style.height = 'auto';
  afficherReflexion();

  try {
    const res = await fetch("{{ url_for('repondre') }}", {
      method: 'POST',
      headers: { 'Content-Type': 'application/x-www-form-urlencoded', 'X-CSRF-Token': csrfToken },
      body: 'message=' + encodeURIComponent(texte)
    });
    const data = await res.json();
    retirerReflexion();
    ajouterMessage(data.reponse, 'bot');

    const vocal = window._dashleVocal;
    const enModeVocal = vocal && vocal.estActif();

    if (data.audio) {
      const son = new Audio(data.audio + '?t=' + Date.now());
      if (enModeVocal) vocal.marquerParle();
      son.onended = function() {
        if (enModeVocal) vocal.reprendreEcoute();
      };
      son.play().catch(function(e) {
        console.log('Lecture audio bloquee:', e);
        if (enModeVocal) vocal.reprendreEcoute();
      });
    } else if (enModeVocal) {
      // Pas d'audio généré (ex: gTTS indisponible) : on relance quand même l'écoute
      setTimeout(vocal.reprendreEcoute, 500);
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
        conversations = db.query(Conversation).filter_by(user_id=user_id).order_by(Conversation.updated_at.desc()).all()
        return [{"id": conv.id, "titre": conv.title} for conv in conversations]


def _messages_conversation(user_id, conversation_id):
    with session_base() as db:
        conversation = db.query(Conversation).filter_by(id=conversation_id, user_id=user_id).one_or_none()
        if conversation is None:
            return []
        return [{"auteur": msg.auteur, "texte": msg.texte} for msg in conversation.messages]


def ajouter_message(user_id, conversation_id, texte, auteur):
    with session_base() as db:
        conversation = db.query(Conversation).filter_by(id=conversation_id, user_id=user_id).one_or_none()
        if conversation is None:
            raise LookupError("Conversation introuvable")
        db.add(Message(conversation_id=conversation.id, auteur=auteur, texte=texte))
        if auteur == "user" and conversation.title == "Nouvelle conversation":
            conversation.title = texte[:48] or conversation.title
        conversation.updated_at = datetime.utcnow()


@app.route("/")
def accueil():
    user_id = session["user_id"]
    conversation_id = _conv_courante(user_id)
    return render_template_string(PAGE, conversations=_liste_conversations(user_id),
                                  messages=_messages_conversation(user_id, conversation_id),
                                  utilisateur={"email": session["user_email"]}, enumerate=enumerate,
                                  csrf_token=jeton_csrf())


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


@app.route("/repondre", methods=["POST"])
def repondre():
    """Endpoint appelé en AJAX : ne renvoie que du JSON, pas de rechargement de page."""
    user_id = session["user_id"]
    conversation_id = _conv_courante(user_id)
    message = request.form.get("message", "").strip()

    if not message:
        return jsonify({"reponse": ""})

    historique = _messages_conversation(user_id, conversation_id)
    ajouter_message(user_id, conversation_id, message, "user")
    reponse = traiter_message(message, historique + [{"auteur": "user", "texte": message}], user_id)
    ajouter_message(user_id, conversation_id, reponse, "bot")

    audio_url = None
    if generer_audio_web(reponse):
        audio_url = url_for("static", filename="audio/dashle_voix.mp3")

    return jsonify({"reponse": reponse, "audio": audio_url})

@app.route("/repondre_image", methods=["POST"])
def repondre_image():
    user_id = session["user_id"]
    conversation_id = _conv_courante(user_id)
    message = request.form.get("message", "").strip()
    fichier = request.files.get("image")
    if not fichier:
        return jsonify({"reponse": "Aucune image reçue."})

    image_bytes = fichier.read()
    if not image_bytes:
        return jsonify({"reponse": "L'image reçue est vide."}), 400

    mime_type = detecter_type_image(image_bytes)
    if not mime_type:
        return jsonify({"reponse": "Le fichier envoyé n'est pas une image valide."}), 400

    if PIL_DISPONIBLE:
        try:
            with Image.open(io.BytesIO(image_bytes)) as image:
                image.verify()
        except Exception:
            return jsonify({"reponse": "Le fichier envoyé n'est pas une image valide."}), 400

    image_b64 = base64.b64encode(image_bytes).decode("utf-8")
    ajouterMessage_texte = message or "[Image envoyée]"
    ajouter_message(user_id, conversation_id, ajouterMessage_texte, "user")
    reponse = traiter_message_image(message, image_b64, mime_type)
    ajouter_message(user_id, conversation_id, reponse, "bot")

    return jsonify({"reponse": reponse})


@app.errorhandler(413)
def fichier_trop_volumineux(_erreur):
    return jsonify({"reponse": "L'image est trop volumineuse (maximum : 8 Mo)."}), 413


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


@app.route("/deconnexion", methods=["POST"])
def deconnexion():
    session.clear()
    return redirect(url_for("connexion"))


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
