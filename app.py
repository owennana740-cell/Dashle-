from dotenv import load_dotenv
load_dotenv()
from brain import reflechir, demander_a_lia_image
from memory import retenir, se_souvenir
from learn import apprendre


def traiter_message(message):
    message_lower = message.lower()

    if message_lower.startswith("retiens que"):
        texte = message[len("retiens que"):].strip()

        if "mon nom est" in texte.lower():
            valeur = texte.lower().replace("mon nom est", "").strip()
            retenir("nom", valeur)
            return "D'accord, j'ai retenu ton nom."
        else:
            retenir("information", texte)
            return "D'accord, j'ai enregistré cette information."

    elif message_lower.startswith("apprends que"):
        contenu = message[len("apprends que"):].strip()
        if "=" in contenu:
            mot_cle, reponse = contenu.split("=", 1)
            apprendre(mot_cle.strip().lower(), reponse.strip())
            return "J'ai appris ça, merci !"
        else:
            return "Utilise le format : apprends que question = réponse"

    elif "quel est mon nom" in message_lower or "mon nom" in message_lower:
        return se_souvenir("nom")

    elif "que retiens" in message_lower:
        return se_souvenir("information")

    else:
        return reflechir(message)


# Ce bloc ne s'exécute QUE si tu lances app.py directement (mode console).
# Il ne se déclenche pas quand interface.py importe traiter_message.
def traiter_message_image(message, image_b64, mime_type):
    return demander_a_lia_image(message, image_b64, mime_type)


if __name__ == "__main__":
    print("Bonjour, je suis Dashle, ton IA personnelle.")
    while True:
        message = input("Toi : ").strip()
        if message.lower() == "quitter":
            print("Dashle : À bientôt !")
            break
        print("Dashle :", traiter_message(message))