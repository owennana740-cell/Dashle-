from core.utils import BASE_DIR, ecrire_json_atomiquement, lire_json

FICHIER = BASE_DIR / "data" / "knowledge.json"

def apprendre(question, reponse):
    connaissances = lire_json(FICHIER, {})

    connaissances[question.lower()] = reponse

    ecrire_json_atomiquement(FICHIER, connaissances)
