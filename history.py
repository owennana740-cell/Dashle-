import json
import os

FICHIER = "conversations.json"
def charger_conversations():
    if os.path.exists(FICHIER):
        with open(FICHIER, "r", encoding="utf-8") as f:
            try:
                return json.load(f)
            except json.JSONDecodeError:
                return []
    return []

def sauvegarder_conversations(conversations):
    with open(FICHIER, "w", encoding="utf-8") as f:
        json.dump(conversations, f, indent=4, ensure_ascii=False)