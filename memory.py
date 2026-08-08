import json
import os

fichier = "memoire.json"

def charger_memoire():
    if os.path.exists(fichier):
        with open(fichier, "r") as f:
            return json.load(f)
    return {}

def sauvegarder_memoire(donnees):
    with open(fichier, "w") as f:
        json.dump(donnees, f, indent=4)

def retenir(cle, valeur):
    donnees = charger_memoire()
    donnees[cle] = valeur
    sauvegarder_memoire(donnees)

def se_souvenir(cle):
    donnees = charger_memoire()
    return donnees.get(cle, "Je ne connais pas encore cette information.")

def se_souvenir_tout():
    return charger_memoire()