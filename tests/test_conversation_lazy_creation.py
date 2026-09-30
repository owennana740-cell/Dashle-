"""Tests dédiés à la création paresseuse et au nettoyage des conversations."""
import contextlib
import io
import os
import unittest
import uuid
from datetime import datetime, timedelta
from unittest.mock import patch

os.environ["DATABASE_URL"] = "sqlite://"
os.environ["SESSION_COOKIE_SECURE"] = "0"

import web
from database import Conversation, Message, User, UserPreference, session_base


class ConversationLazyCreationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        web.app.config.update(TESTING=True, SESSION_COOKIE_SECURE=False)

    def setUp(self):
        self.client = web.app.test_client()
        self.email = f"lazy-{uuid.uuid4().hex}@example.invalid"
        with session_base() as db:
            self.user = User(email=self.email, password_hash="test-only")
            db.add(self.user)
            db.flush()
            db.add(UserPreference(user_id=self.user.id, conserver_historique=True))
            db.flush()
            self.user_id = self.user.id
        with self.client.session_transaction() as state:
            state["user_id"] = self.user_id
            state["user_email"] = self.email
            state["csrf_token"] = "lazy-test-token"

    def tearDown(self):
        with session_base() as db:
            conversation_ids = [
                row.id for row in db.query(Conversation.id).filter_by(user_id=self.user_id).all()
            ]
            if conversation_ids:
                db.query(Message).filter(Message.conversation_id.in_(conversation_ids)).delete(
                    synchronize_session=False
                )
            db.query(Conversation).filter_by(user_id=self.user_id).delete(synchronize_session=False)
            db.query(UserPreference).filter_by(user_id=self.user_id).delete(synchronize_session=False)
            db.query(User).filter_by(id=self.user_id).delete(synchronize_session=False)

    def test_opening_home_does_not_create_empty_conversation(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        with session_base() as db:
            self.assertEqual(db.query(Conversation).filter_by(user_id=self.user_id).count(), 0)

    def test_first_message_creates_exactly_one_conversation_and_titles_from_summary(self):
        with patch.object(web, "streamer_message", return_value=iter(["Réponse du premier échange"])),                 patch.object(web, "resumer_conversation", return_value="Résumé court du projet"):
            response = self.client.post(
                "/repondre_flux",
                data={"message": "Texte collé très long à ne pas utiliser comme titre brut"},
                headers={"X-CSRF-Token": "lazy-test-token"},
                buffered=True,
            )
        self.assertEqual(response.status_code, 200)
        with session_base() as db:
            conversations = db.query(Conversation).filter_by(user_id=self.user_id).all()
            self.assertEqual(len(conversations), 1)
            self.assertEqual(conversations[0].title, "Résumé court du projet")
            self.assertEqual(
                db.query(Message).filter_by(conversation_id=conversations[0].id).count(),
                2,
            )

    def test_plus_on_empty_conversation_does_not_create_another(self):
        with session_base() as db:
            empty = Conversation(user_id=self.user_id)
            db.add(empty)
            db.flush()
            empty_id = empty.id
        with self.client.session_transaction() as state:
            state["conversation_id"] = empty_id
            state.pop("nouvelle_conversation_en_attente", None)
        response = self.client.post("/nouvelle", headers={"X-CSRF-Token": "lazy-test-token"})
        self.assertEqual(response.status_code, 302)
        with session_base() as db:
            self.assertEqual(db.query(Conversation).filter_by(user_id=self.user_id).count(), 1)
        with self.client.session_transaction() as state:
            self.assertEqual(state.get("conversation_id"), empty_id)

    def test_opening_reuses_latest_conversation_without_creating(self):
        with session_base() as db:
            latest = Conversation(user_id=self.user_id, title="Dernière")
            db.add(latest)
            db.flush()
            db.add(Message(conversation_id=latest.id, auteur="user", texte="question"))
            latest_id = latest.id
        with self.client.session_transaction() as state:
            state.pop("conversation_id", None)
            state.pop("nouvelle_conversation_en_attente", None)
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        with self.client.session_transaction() as state:
            self.assertEqual(state.get("conversation_id"), latest_id)
        with session_base() as db:
            self.assertEqual(db.query(Conversation).filter_by(user_id=self.user_id).count(), 1)

    def test_cleanup_dry_run_does_not_delete_old_empty_conversations(self):
        from scripts.nettoyer_conversations_vides import main
        old = datetime.utcnow() - timedelta(hours=48)
        with session_base() as db:
            candidate = Conversation(user_id=self.user_id, created_at=old, updated_at=old)
            db.add(candidate)
            db.flush()
            candidate_id = candidate.id
        with contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(main(["--dry-run"]), 0)
        self.assertIn("dry-run:", output.getvalue())
        with session_base() as db:
            self.assertIsNotNone(db.get(Conversation, candidate_id))


if __name__ == "__main__":
    unittest.main()
