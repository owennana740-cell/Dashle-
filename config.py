import os

from dotenv import load_dotenv


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
# Charger .env avant de lire les variables d'environnement rend le démarrage
# fiable quel que soit le dossier depuis lequel l'application est lancée.
load_dotenv(os.path.join(BASE_DIR, ".env"))

# Clé API Gemini — jamais exposée au navigateur ni dans les logs.
CLE_API = os.environ.get("GEMINI_API_KEY")

# Modèle Gemini utilisé pour toutes les requêtes (texte, streaming, résumé).
# Changer GEMINI_MODEL dans .env suffit pour migrer vers un autre modèle.
MODELE_GEMINI = os.environ.get("GEMINI_MODEL", "gemini-3.5-flash-lite")

# Nombre maximal de messages transmis en contexte à chaque appel Gemini.
MAX_MESSAGES_CONTEXTE = int(os.environ.get("GEMINI_MAX_CONTEXTE", "24"))
