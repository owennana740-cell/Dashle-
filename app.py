from dotenv import load_dotenv
load_dotenv()
from brain import reflechir, demander_a_lia_image, streamer_a_lia
from memory import memoire_active, retenir, se_souvenir
from learn import apprendre


def traiter_message(
    message, historique=None, user_id=None, resume="",
    instructions_projet="", fichiers_projet="",
):
    message_lower = message.lower()

    if message_lower.startswith("retiens que"):
        if user_id is None:
            return "La mémoire personnelle est disponible après connexion. Connecte-toi ou crée un compte pour enregistrer ce souvenir."
        if not memoire_active(user_id):
            return "La mémoire est désactivée dans tes paramètres. Réactive-la pour enregistrer ce souvenir."
        texte = message[len("retiens que"):].strip()
        if "mon nom est" in texte.lower():
            valeur = texte.lower().replace("mon nom est", "").strip()
            retenir("nom", valeur, user_id)
            return "D'accord, j'ai retenu ton nom."
        else:
            retenir("information", texte, user_id)
            return "D'accord, j'ai enregistre cette information."

    elif message_lower.startswith("apprends que"):
        if user_id is None:
            return "Les visiteurs ne peuvent pas ajouter de connaissances partagées. Connecte-toi pour enregistrer une information dans ta mémoire personnelle."
        if not memoire_active(user_id):
            return "La mémoire est désactivée dans tes paramètres. Réactive-la pour enregistrer cette information."
        contenu = message[len("apprends que"):].strip()
        if "=" in contenu:
            mot_cle, reponse = contenu.split("=", 1)
            if user_id is None:
                apprendre(mot_cle.strip().lower(), reponse.strip())
            else:
                retenir(mot_cle.strip().lower(), reponse.strip(), user_id)
            return "J'ai appris ca, merci !"
        else:
            return "Utilise le format : apprends que question = reponse"

    elif "quel est mon nom" in message_lower or "mon nom" in message_lower:
        if user_id is None:
            return "Je n’ai pas de mémoire personnelle pour les visiteurs. Connecte-toi pour retrouver les souvenirs liés à ton compte."
        return se_souvenir("nom", user_id)

    elif "que retiens" in message_lower:
        if user_id is None:
            return "Je n’ai pas de mémoire personnelle pour les visiteurs. Connecte-toi pour retrouver les souvenirs liés à ton compte."
        return se_souvenir("information", user_id)

    else:
        return reflechir(
            message, historique, user_id, resume,
            instructions_projet, fichiers_projet,
        )


def traiter_message_image(
    message, image_b64, mime_type, historique=None, resume="", user_id=None,
    instructions_projet="", fichiers_projet="",
):
    return demander_a_lia_image(
        message, image_b64, mime_type, historique, resume, user_id,
        instructions_projet, fichiers_projet,
    )


def streamer_message(
    message, historique=None, user_id=None, resume="",
    instructions_projet="", fichiers_projet="",
):
    """Diffuse une reponse IA tout en gardant les commandes locales synchrones."""
    message_lower = message.lower()
    est_local = (
        message_lower.startswith("retiens que")
        or message_lower.startswith("apprends que")
        or "quel est mon nom" in message_lower
        or "mon nom" in message_lower
        or "que retiens" in message_lower
    )
    if est_local:
        yield traiter_message(
            message, historique, user_id, resume,
            instructions_projet, fichiers_projet,
        )
        return
    yield from streamer_a_lia(
        message, historique, resume, user_id=user_id,
        instructions_projet=instructions_projet,
        fichiers_projet=fichiers_projet,
    )


if __name__ == "__main__":
    print("Bonjour, je suis Dashle, ton IA personnelle.")
    while True:
        message = input("Toi : ").strip()
        if message.lower() == "quitter":
            print("Dashle : A bientot !")
            break
