
        print("Dashle :", traiter_message(message))
from dotenv import load_dotenv
load_dotenv()
from brain import reflechir, demander_a_lia_image, streamer_a_lia
from memory import retenir, se_souvenir
from learn import apprendre


def traiter_message(message, historique=None, user_id=None, resume=""):
    message_lower = message.lower()

    if message_lower.startswith("retiens que"):
        texte = message[len("retiens que"):].strip()

        if "mon nom est" in texte.lower():
            valeur = texte.lower().replace("mon nom est", "").strip()
            retenir("nom", valeur, user_id)
            return "D'accord, j'ai retenu ton nom."
        else:
            retenir("information", texte, user_id)
            return "D'accord, j'ai enregistré cette information."

    elif message_lower.startswith("apprends que"):
        contenu = message[len("apprends que"):].strip()
        if "=" in contenu:
            mot_cle, reponse = contenu.split("=", 1)
            if user_id is None:
                apprendre(mot_cle.strip().lower(), reponse.strip())
            else:
                retenir(mot_cle.strip().lower(), reponse.strip(), user_id)
            return "J'ai appris ça, merci !"
        else:
            return "Utilise le format : apprends que question = réponse"

    elif "quel est mon nom" in message_lower or "mon nom" in message_lower:
        return se_souvenir("nom", user_id)

    elif "que retiens" in message_lower:
        return se_souvenir("information", user_id)

    else:
        return reflechir(message, historique, user_id, resume)


# Ce bloc ne s'exécute QUE si tu lances app.py directement (mode console).
# Il ne se déclenche pas quand interface.py importe traiter_message.
def traiter_message_image(message, image_b64, mime_type, historique=None, resume=""):
    return demander_a_lia_image(message, image_b64, mime_type, historique, resume)


def streamer_message(message, historique=None, user_id=None, resume=""):
    """Diffuse une réponse IA tout en gardant les commandes locales synchrones."""
    message_lower = message.lower()
    est_local = (
        message_lower.startswith("retiens que")
        or message_lower.startswith("apprends que")
        or "quel est mon nom" in message_lower
        or "mon nom" in message_lower
        or "que retiens" in message_lower
    )
    if est_local:
        yield traiter_message(message, historique, user_id, resume)
        return
    yield from streamer_a_lia(message, historique, resume)


if __name__ == "__main__":
    print("Bonjour, je suis Dashle, ton IA personnelle.")
    while True:
        message = input("Toi : ").strip()
        if message.lower() == "quitter":
            print("Dashle : À bientôt !")
            break