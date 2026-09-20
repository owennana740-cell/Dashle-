"""Fonctions de synthèse vocale utilisables sans démarrer l'interface Tkinter."""

from pathlib import Path


def nettoyer_pour_voix(texte):
    symboles = ["*", "#", "_", "`", "~", ">", "-", "[", "]"]
    propre = texte
    for symbole in symboles:
        propre = propre.replace(symbole, "")
    return propre.strip()


def generer_audio_web(texte):
    """Génère le fichier MP3 lu par le navigateur et renvoie son succès."""
    try:
        from gtts import gTTS

        dossier = Path(__file__).resolve().parent / "static" / "audio"
        dossier.mkdir(parents=True, exist_ok=True)
        gTTS(text=nettoyer_pour_voix(texte), lang="fr").save(dossier / "dashle_voix.mp3")
        return True
    except Exception:
        return False
