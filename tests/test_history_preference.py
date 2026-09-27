"""Checks that the history preference controls future chat persistence."""

import io
import json
import os
import threading
import unittest
import uuid
from unittest.mock import patch

os.environ["DATABASE_URL"] = "sqlite://"
os.environ["SESSION_COOKIE_SECURE"] = "0"

import web
from database import Conversation, Message, User, UserPreference, session_base


class HistoryPreferenceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        web.app.config.update(TESTING=True, SESSION_COOKIE_SECURE=False)

    def setUp(self):
        self.client = web.app.test_client()
        self.email = f"history-{uuid.uuid4().hex}@example.invalid"
        with session_base() as db:
            self.user = User(email=self.email, password_hash="test-only")
            db.add(self.user)
            db.flush()
            self.user_id = self.user.id
            self.conversation = Conversation(user_id=self.user_id, title="Saved conversation")
            db.add(self.conversation)
            db.flush()
            self.conversation_id = self.conversation.id
            db.add_all([
                Message(conversation_id=self.conversation_id, auteur="user", texte="old question"),
                Message(conversation_id=self.conversation_id, auteur="bot", texte="old answer"),
                UserPreference(user_id=self.user_id, conserver_historique=False),
            ])
            db.flush()
            self.old_bot_id = db.query(Message).filter_by(
                conversation_id=self.conversation_id, auteur="bot"
            ).one().id
        with self.client.session_transaction() as state:
            state["user_id"] = self.user_id
            state["user_email"] = self.email
            state["conversation_id"] = self.conversation_id
            state["csrf_token"] = "history-test-token"

    def post_headers(self):
        return {"X-CSRF-Token": "history-test-token"}

    def message_count(self):
        with session_base() as db:
            return db.query(Message).join(Conversation).filter(
                Conversation.user_id == self.user_id
            ).count()

    def test_home_and_new_chat_do_not_create_or_delete_saved_conversations(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"old answer", response.data)
        self.assertIn(b"nouveaux", response.data)

        response = self.client.post("/nouvelle", headers=self.post_headers())
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.client.get("/").status_code, 200)
        with self.client.session_transaction() as state:
            self.assertNotIn("conversation_id", state)
        self.assertEqual(self.message_count(), 2)
        with session_base() as db:
            self.assertEqual(db.query(Conversation).filter_by(user_id=self.user_id).count(), 1)

    def test_disabled_history_does_not_transfer_visitor_chat_into_sql(self):
        with self.client.session_transaction() as state:
            state["transfert_en_attente"] = [
                {"auteur": "user", "texte": "temporary visitor message"}
            ]
        response = self.client.post(
            "/transferer_conversation", headers=self.post_headers()
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["transfere"], 0)
        with self.client.session_transaction() as state:
            self.assertEqual(
                state["transfert_en_attente"][0]["texte"], "temporary visitor message"
            )
        self.assertEqual(self.message_count(), 2)

    def test_disabled_sse_uses_page_context_without_persisting_messages(self):
        captured = []

        def fake_stream(message, history, user_id, resume):
            captured.append((message, history, user_id))
            yield "temporary answer"

        context = [
            {"auteur": "user", "texte": "old question"},
            {"auteur": "bot", "texte": "old answer"},
            {"auteur": "user", "texte": "follow-up"},
        ]
        with patch.object(web, "streamer_message", fake_stream), \
                patch.object(web, "_actualiser_resume_en_arriere_plan") as schedule_resume:
            response = self.client.post(
                "/repondre_flux",
                data={"message": "follow-up", "historique": json.dumps(context)},
                headers=self.post_headers(),
                buffered=True,
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(captured[0][0], "follow-up")
        self.assertEqual(captured[0][1], context)
        self.assertEqual(captured[0][2], self.user_id)
        events = [json.loads(line[6:]) for line in response.get_data(as_text=True).splitlines() if line.startswith("data: ")]
        final = next(event for event in events if event.get("termine"))
        self.assertIsNone(final["message_id"])
        schedule_resume.assert_not_called()
        self.assertEqual(self.message_count(), 2)

    def test_disabled_sync_image_and_regeneration_do_not_persist(self):
        with patch.object(web, "traiter_message", return_value="temporary text answer"):
            response = self.client.post(
                "/repondre",
                data={"message": "temporary question", "historique": "[]"},
                headers=self.post_headers(),
            )
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.get_json()["message_id"])
        self.assertEqual(self.message_count(), 2)
        with self.client.session_transaction() as state:
            self.assertNotIn("historique_visiteur", state)

        with patch.object(web, "detecter_type_media", return_value="image/png"), \
                patch.object(web, "PIL_DISPONIBLE", False), \
                patch.object(web, "traiter_message_image", return_value="temporary image answer"):
            response = self.client.post(
                "/repondre_image",
                data={
                    "message": "describe this",
                    "historique": json.dumps([{"auteur": "user", "texte": "old question"}]),
                    "image": (io.BytesIO(b"test image bytes"), "image.png"),
                },
                headers=self.post_headers(),
            )
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.get_json()["message_id"])
        self.assertEqual(self.message_count(), 2)
        with self.client.session_transaction() as state:
            self.assertNotIn("historique_visiteur", state)

        with patch.object(web, "traiter_message", return_value="temporary regenerated answer"):
            response = self.client.post(
                f"/regenerer/{self.old_bot_id}", headers=self.post_headers()
            )
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.get_json()["message_id"])
        self.assertEqual(self.message_count(), 2)

    def test_enabled_sse_still_persists_user_and_assistant_messages(self):
        with session_base() as db:
            preference = db.query(UserPreference).filter_by(user_id=self.user_id).one()
            preference.conserver_historique = True

        def fake_stream(message, history, user_id, resume):
            yield "saved answer"

        with patch.object(web, "streamer_message", fake_stream), \
                patch.object(web, "_actualiser_resume_en_arriere_plan"):
            response = self.client.post(
                "/repondre_flux",
                data={"message": "saved question"},
                headers=self.post_headers(),
                buffered=True,
            )
        events = [json.loads(line[6:]) for line in response.get_data(as_text=True).splitlines() if line.startswith("data: ")]
        final = next(event for event in events if event.get("termine"))
        self.assertIsNotNone(final["message_id"])
        self.assertEqual(self.message_count(), 4)

    def test_sse_does_not_wait_for_background_summary(self):
        with session_base() as db:
            preference = db.query(UserPreference).filter_by(user_id=self.user_id).one()
            preference.conserver_historique = True

        summary_started = threading.Event()
        release_summary = threading.Event()

        def slow_summary(*_args):
            summary_started.set()
            release_summary.wait(timeout=5)

        try:
            with patch.object(web, "_actualiser_resume", side_effect=slow_summary), \
                    patch.object(web, "streamer_message", return_value=iter(["saved answer"])):
                response = self.client.post(
                    "/repondre_flux",
                    data={"message": "saved question"},
                    headers=self.post_headers(),
                    buffered=True,
                )
                self.assertTrue(summary_started.wait(timeout=1))
            self.assertEqual(response.status_code, 200)
            events = [
                json.loads(line[6:])
                for line in response.get_data(as_text=True).splitlines()
                if line.startswith("data: ")
            ]
            self.assertTrue(any(event.get("termine") for event in events))
            self.assertEqual(self.message_count(), 4)
        finally:
            release_summary.set()

    def test_conversation_context_fetch_can_be_limited_to_recent_messages(self):
        with session_base() as db:
            db.add_all([
                Message(
                    conversation_id=self.conversation_id,
                    auteur="user",
                    texte=f"question {index}",
                )
                for index in range(30)
            ])

        recent = web._messages_conversation(
            self.user_id, self.conversation_id, limite=5
        )
        full = web._messages_conversation(self.user_id, self.conversation_id)
        self.assertEqual(len(recent), 5)
        self.assertEqual([message["texte"] for message in recent], [
            "question 25", "question 26", "question 27", "question 28", "question 29"
        ])
        self.assertEqual(len(full), 32)

    def test_summary_uses_recent_messages_without_loading_or_deleting_old_history(self):
        with session_base() as db:
            db.add_all([
                Message(
                    conversation_id=self.conversation_id,
                    auteur="user",
                    texte=f"summary question {index}",
                )
                for index in range(58)
            ])

        captured = {}

        def fake_summary(history, previous, user_id=None):
            captured["history"] = history
            captured["previous"] = previous
            return "updated summary"

        with patch.object(web, "resumer_conversation", side_effect=fake_summary):
            web._actualiser_resume(self.user_id, self.conversation_id)

        self.assertEqual(len(captured["history"]), 40)
        self.assertEqual(captured["history"][0]["texte"], "summary question 18")
        self.assertEqual(captured["history"][-1]["texte"], "summary question 57")
        self.assertEqual(len(web._messages_conversation(self.user_id, self.conversation_id)), 60)
        with session_base() as db:
            conversation = db.query(Conversation).filter_by(id=self.conversation_id).one()
            self.assertEqual(conversation.resume, "updated summary")

    def test_visitor_sse_still_keeps_its_temporary_session_history(self):
        client = web.app.test_client()

        def fake_stream(message, history, user_id, resume):
            yield "visitor answer"

        with patch.object(web, "streamer_message", fake_stream):
            response = client.post(
                "/repondre_flux", data={"message": "visitor question"}, buffered=True
            )
        self.assertEqual(response.status_code, 200)
        with client.session_transaction() as state:
            self.assertEqual(state["historique_visiteur"][-1]["texte"], "visitor question")
        confirmed = client.post(
            "/confirmer_message", json={"reponse": "visitor answer"}
        )
        self.assertEqual(confirmed.status_code, 200)
        with client.session_transaction() as state:
            self.assertEqual(state["historique_visiteur"][-1]["texte"], "visitor answer")


if __name__ == "__main__":
    unittest.main()
