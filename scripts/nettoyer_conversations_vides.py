"""Nettoyage prudent des conversations vides.
Le mode par défaut est --dry-run. --execute exige explicitement la liste des IDs
épinglés côté client afin de ne jamais supprimer un élément dont l'état d'épingle
n'est pas connu du serveur.
"""
import argparse
import json
from datetime import datetime, timedelta
from database import Conversation, session_base


def _lire_ids_epingles(chemin):
    if not chemin:
        return set()
    with open(chemin, encoding="utf-8") as fichier:
        return {int(valeur) for valeur in json.load(fichier)}


def candidats(heures=24, ids_epingles=None):
    ids_epingles = ids_epingles or set()
    seuil = datetime.utcnow() - timedelta(hours=heures)
    with session_base() as db:
        query = (
            db.query(Conversation)
            .filter(
                Conversation.updated_at < seuil,
                Conversation.project_id.is_(None),
                ~Conversation.messages.any(),
            )
            .order_by(Conversation.id.asc())
        )
        if ids_epingles:
            query = query.filter(~Conversation.id.in_(ids_epingles))
        return [conv.id for conv in query.all()]


def main(argv=None):
    parser = argparse.ArgumentParser(description="Nettoie les conversations vides anciennes.")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="Compte les candidats sans supprimer (défaut).")
    mode.add_argument("--execute", action="store_true", help="Supprime les candidats validés.")
    parser.add_argument("--pinned-ids-file", help="JSON contenant les IDs des conversations épinglées.")
    args = parser.parse_args(argv)
    ids_epingles = _lire_ids_epingles(args.pinned_ids_file)
    ids = candidats(ids_epingles=ids_epingles)
    if not args.execute:
        print(f"dry-run: {len(ids)} conversation(s) candidate(s); aucune suppression.")
        return 0
    if not args.pinned_ids_file:
        parser.error("--pinned-ids-file est obligatoire avec --execute.")
    with session_base() as db:
        supprimees = (
            db.query(Conversation)
            .filter(Conversation.id.in_(ids), ~Conversation.messages.any(), Conversation.project_id.is_(None))
            .delete(synchronize_session=False)
        )
    print(f"supprimées: {supprimees}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
