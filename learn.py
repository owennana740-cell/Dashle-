import json
import os

FICHIER = "data/knowledge.json"

def apprendre(question, reponse):
    if os.path.exists(FICHIER):
        with open(FICHIER, "r", encoding="utf-8") as f:
            connaissances = json.load(f)
    else:
        connaissances = {}

    connaissances[question.lower()] = reponse

    with open(FICHIER, "w", encoding="utf-8") as f:
        json.dump(connaissances, f, indent=4, ensure_ascii=False)