import json
import os
import tempfile
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent.parent


def nettoyer(texte):
    return texte.strip().lower()


def lire_json(chemin, valeur_par_defaut):
    """Lit un JSON local sans faire tomber l'application s'il est invalide."""
    try:
        with open(chemin, "r", encoding="utf-8") as fichier:
            return json.load(fichier)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return valeur_par_defaut


def ecrire_json_atomiquement(chemin, donnees):
    """Écrit d'abord un fichier temporaire puis le remplace en une opération."""
    chemin = Path(chemin)
    chemin.parent.mkdir(parents=True, exist_ok=True)
    descripteur, temporaire = tempfile.mkstemp(
        dir=chemin.parent, prefix=f".{chemin.name}.", suffix=".tmp"
    )
    try:
        with os.fdopen(descripteur, "w", encoding="utf-8") as fichier:
            json.dump(donnees, fichier, indent=4, ensure_ascii=False)
            fichier.flush()
            os.fsync(fichier.fileno())
        os.replace(temporaire, chemin)
    except Exception:
        try:
            os.unlink(temporaire)
        except FileNotFoundError:
            pass
        raise
