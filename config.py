import os

from dotenv import load_dotenv


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
# Charger .env avant de lire GEMINI_API_KEY rend le démarrage fiable quel que
# soit le dossier depuis lequel l'application est lancée.
load_dotenv(os.path.join(BASE_DIR, ".env"))

CLE_API = os.environ.get("GEMINI_API_KEY")
