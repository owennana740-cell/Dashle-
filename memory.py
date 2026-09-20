from core.utils import BASE_DIR, ecrire_json_atomiquement, lire_json

fichier = BASE_DIR / "memoire.json"

def charger_memoire():
    memoire = lire_json(fichier, {})
    return memoire if isinstance(memoire, dict) else {}

def sauvegarder_memoire(donnees):
    ecrire_json_atomiquement(fichier, donnees)

def retenir(cle, valeur, user_id=None):
    if user_id is not None:
        from database import UserMemory, session_base
        with session_base() as session:
            souvenir = session.query(UserMemory).filter_by(user_id=user_id, cle=cle).one_or_none()
            if souvenir:
                souvenir.valeur = valeur
            else:
                session.add(UserMemory(user_id=user_id, cle=cle, valeur=valeur))
        return
    donnees = charger_memoire()
    donnees[cle] = valeur
    sauvegarder_memoire(donnees)

def se_souvenir(cle, user_id=None):
    if user_id is not None:
        from database import UserMemory, session_base
        with session_base() as session:
            souvenir = session.query(UserMemory).filter_by(user_id=user_id, cle=cle).one_or_none()
            return souvenir.valeur if souvenir else "Je ne connais pas encore cette information."
    donnees = charger_memoire()
    return donnees.get(cle, "Je ne connais pas encore cette information.")

def se_souvenir_tout(user_id=None):
    if user_id is not None:
        from database import UserMemory, session_base
        with session_base() as session:
            return {souvenir.cle: souvenir.valeur for souvenir in session.query(UserMemory).filter_by(user_id=user_id)}
    return charger_memoire()
