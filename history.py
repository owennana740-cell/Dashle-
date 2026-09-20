from core.utils import BASE_DIR, ecrire_json_atomiquement, lire_json

FICHIER = BASE_DIR / "conversations.json"


def charger_conversations():
    conversations = lire_json(FICHIER, [])
    return conversations if isinstance(conversations, list) else []


def sauvegarder_conversations(conversations):
    ecrire_json_atomiquement(FICHIER, conversations)
