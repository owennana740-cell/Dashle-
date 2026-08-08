import os
import io
from flask import Flask, request, render_template_string, redirect, url_for, session, jsonify
from app import traiter_message
from history import charger_conversations, sauvegarder_conversations

try:
    from PIL import Image
    PIL_DISPONIBLE = True
except Exception:
    PIL_DISPONIBLE = False

app = Flask(__name__)
app.secret_key = "dashle-secret-key"  # change si tu veux, sert juste à garder ta session:

PAGE = """
<!DOCTYPE html>
<html lang="fr">
<head>
<meta charset="UTF-8">
<title>Dashle</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
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
  #sidebar a { display:block; padding:12px 18px; text-decoration:none; color:#111; border-bottom:1px solid #eee; }
  #sidebar a.nouvelle { color:#10A37F; font-weight:bold; }

  #chat { flex:1; overflow-y:auto; padding: 14px; }
  .msg { max-width: 80%; padding: 10px 14px; border-radius: 14px; margin-bottom: 10px; white-space: pre-wrap; line-height:1.4; }
  .user { background:#DCF8C6; margin-left:auto; }
  .bot { background:#f0f0f0; margin-right:auto; }

  /* Indicateur "Dashle réfléchit" : point qui pulse */
  .reflexion { display:flex; align-items:center; gap:6px; padding: 10px 14px; }
  .dot { width:10px; height:10px; border-radius:50%; background:#10A37F; animation: pulse 0.9s infinite ease-in-out; }
  @keyframes pulse { 0%,100% { transform: scale(0.7); opacity:0.5; } 50% { transform: scale(1); opacity:1; } }

  form.bas { display:flex; gap:8px; padding:10px; border-top:1px solid #eee; align-items:center; }
  form.bas textarea { flex:1; resize:none; border:1px solid #ddd; border-radius:18px; padding:10px 14px; font-size:15px; max-height:100px; }
  form.bas textarea:focus { outline: none; }
  form.bas button.envoyer { background:#10A37F; color:white; border:none; border-radius:50%; width:38px; height:38px; font-size:16px; flex-shrink:0; }
</style>
</head>
<body>

<header>
  <button onclick="document.getElementById('sidebar').style.display='block';document.getElementById('voile').style.display='block';">&#9776;</button>
  <img src="{{ url_for('static', filename='logo.png') }}" alt="Dashle" style="height:28px; margin-left:8px; border-radius:50%;">
  <div class="titre">Dashle</div>
  <a href="{{ url_for('nouvelle_conv') }}" style="color:white;text-decoration:none;font-size:22px;">+</a>
</header>

<div id="voile" onclick="document.getElementById('sidebar').style.display='none';this.style.display='none';"></div>
<div id="sidebar">
  <h2>Dashle</h2>
  <a class="nouvelle" href="{{ url_for('nouvelle_conv') }}">+ Nouvelle conversation</a>
  {% for i, conv in enumerate(conversations) %}
    <a href="{{ url_for('charger_conv', i=i) }}">{{ conv.titre }}</a>
  {% endfor %}
</div>

<div id="chat">
  {% for m in messages %}
    <div class="msg {{ 'user' if m.auteur == 'user' else 'bot' }}">{{ m.texte }}</div>
  {% endfor %}
</div>

<form class="bas" id="form-message" autocomplete="off">
  <textarea id="message" name="message" rows="1" placeholder="Écris à Dashle..." required></textarea>
  <button class="envoyer" type="submit" id="btn-envoyer">&#10148;</button>
</form>

<script>
const chat = document.getElementById('chat');
const form = document.getElementById('form-message');
const champ = document.getElementById('message');
const btnEnvoyer = document.getElementById('btn-envoyer');

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
  if (!texte) return;

  ajouterMessage(texte, 'user');
  champ.value = '';
  champ.style.height = 'auto';
  afficherReflexion();

  try {
    const res = await fetch("{{ url_for('repondre') }}", {
      method: 'POST',
      headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
      body: 'message=' + encodeURIComponent(texte)
    });
    const data = await res.json();
    retirerReflexion();
    ajouterMessage(data.reponse, 'bot');
    if (data.reponse && data.reponse.toLowerCase().includes('quota')) {
      bloquerEnvoi(30);
    }
  } catch (err) {
    retirerReflexion();
    ajouterMessage("Erreur de connexion au serveur. Réessaie.", 'bot');
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


def _conv_courante():
    """Récupère la conversation active (dict avec 'titre' et 'messages')."""
    conversations = charger_conversations()
    index = session.get("index", 0)
    if not conversations:
        conversations = [{"titre": "Nouvelle conversation", "messages": []}]
        sauvegarder_conversations(conversations)
        index = 0
        session["index"] = 0
    if index >= len(conversations):
        index = 0
        session["index"] = 0
    return conversations, index


def ajouter_message(conversations, index, texte, auteur):
    conversations[index]["messages"].append({"auteur": auteur, "texte": texte})
    sauvegarder_conversations(conversations)
    return index


@app.route("/")
def accueil():
    conversations, index = _conv_courante()
    messages = conversations[index]["messages"]
    return render_template_string(PAGE, conversations=conversations, messages=messages, enumerate=enumerate)


@app.route("/nouvelle")
def nouvelle_conv():
    conversations = charger_conversations()
    conversations.append({"titre": f"Conversation {len(conversations)+1}", "messages": []})
    sauvegarder_conversations(conversations)
    session["index"] = len(conversations) - 1
    return redirect(url_for("accueil"))


@app.route("/conv/<int:i>")
def charger_conv(i):
    conversations = charger_conversations()
    if 0 <= i < len(conversations):
        session["index"] = i
    return redirect(url_for("accueil"))


@app.route("/repondre", methods=["POST"])
def repondre():
    """Endpoint appelé en AJAX : ne renvoie que du JSON, pas de rechargement de page."""
    conversations, index = _conv_courante()
    message = request.form.get("message", "").strip()

    if not message:
        return jsonify({"reponse": ""})

    ajouter_message(conversations, index, message, "user")
    reponse = traiter_message(message)
    ajouter_message(conversations, index, reponse, "bot")

    return jsonify({"reponse": reponse})

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)