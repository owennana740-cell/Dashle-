"""Project ownership, plan limits, files, and history preference checks."""

import io
import os
import unittest
import uuid

from openpyxl import Workbook

os.environ["DATABASE_URL"] = "sqlite://"
os.environ["SESSION_COOKIE_SECURE"] = "0"

import web
from database import (
    Conversation,
    Project,
    ProjectFile,
    User,
    UserPreference,
    session_base,
)


class ProjectRouteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        web.app.config.update(TESTING=True, SESSION_COOKIE_SECURE=False)

    def setUp(self):
        self.client = web.app.test_client()
        self.email = f"project-{uuid.uuid4().hex}@example.invalid"
        with session_base() as db:
            user = User(email=self.email, password_hash="test-only")
            db.add(user)
            db.flush()
            self.user_id = user.id
            self.project = Project(
                user_id=user.id,
                name="Private project",
                instructions="Use the project brief.",
            )
            db.add(self.project)
            db.flush()
            self.project_id = self.project.id
        with self.client.session_transaction() as state:
            state["user_id"] = self.user_id
            state["user_email"] = self.email
            state["csrf_token"] = "project-test-token"

    def post_headers(self):
        return {"X-CSRF-Token": "project-test-token"}

    def test_free_project_limit_and_owner_scoping(self):
        other_email = f"other-{uuid.uuid4().hex}@example.invalid"
        with session_base() as db:
            other = User(email=other_email, password_hash="test-only")
            db.add(other)
            db.flush()
            other_id = other.id
            other_project = Project(user_id=other_id, name="Other private project")
            db.add(other_project)

        page = self.client.get("/projets")
        self.assertEqual(page.status_code, 200)
        self.assertIn(b"Private project", page.data)
        self.assertNotIn(b"Other private project", page.data)

        limited = self.client.post(
            "/projets/creer",
            data={"name": "Second project", "instructions": ""},
            headers=self.post_headers(),
        )
        self.assertEqual(limited.status_code, 302)
        with session_base() as db:
            self.assertEqual(db.query(Project).filter_by(user_id=self.user_id).count(), 1)
            self.assertEqual(db.query(Project).filter_by(user_id=other_id).count(), 1)

        denied = self.client.post(
            f"/projets/{other_project.id}/modifier",
            data={"name": "Hijacked", "instructions": ""},
            headers=self.post_headers(),
        )
        self.assertEqual(denied.status_code, 404)

    def test_project_file_upload_and_download_are_account_scoped(self):
        content = b"city,temperature\nOuagadougou,31\n"
        response = self.client.post(
            f"/projets/{self.project_id}/fichiers",
            data={"file": (io.BytesIO(content), "weather.csv")},
            headers=self.post_headers(),
            content_type="multipart/form-data",
        )
        self.assertEqual(response.status_code, 302)
        with session_base() as db:
            saved = db.query(ProjectFile).filter_by(project_id=self.project_id).one()
            file_id = saved.id
            self.assertEqual(saved.content, content)
            self.assertIn("Ouagadougou", saved.extracted_text)

        download = self.client.get(
            f"/projets/{self.project_id}/fichiers/{file_id}/telecharger"
        )
        self.assertEqual(download.status_code, 200)
        self.assertEqual(download.data, content)

        foreign_download = self.client.get(
            f"/projets/999999/fichiers/{file_id}/telecharger"
        )
        self.assertEqual(foreign_download.status_code, 404)

    def test_project_accepts_and_extracts_xlsx_documents(self):
        workbook = Workbook()
        sheet = workbook.active
        sheet.title = "Budget"
        sheet.append(["Région", "Montant"])
        sheet.append(["Centre", 250000])
        output = io.BytesIO()
        workbook.save(output)

        response = self.client.post(
            f"/projets/{self.project_id}/fichiers",
            data={"file": (io.BytesIO(output.getvalue()), "budget.xlsx")},
            headers=self.post_headers(),
            content_type="multipart/form-data",
        )
        self.assertEqual(response.status_code, 302)
        with session_base() as db:
            saved = db.query(ProjectFile).filter_by(project_id=self.project_id).one()
            self.assertEqual(
                saved.mime_type,
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )
            self.assertIn("Centre", saved.extracted_text)
            self.assertIn("250000", saved.extracted_text)

    def test_deleting_project_keeps_conversation_and_removes_project_files(self):
        with session_base() as db:
            conversation = Conversation(user_id=self.user_id, project_id=self.project_id)
            db.add(conversation)
            db.add(ProjectFile(
                project_id=self.project_id,
                filename="brief.txt",
                mime_type="text/plain",
                size_bytes=5,
                content=b"brief",
                extracted_text="brief",
            ))
            db.flush()
            conversation_id = conversation.id

        response = self.client.post(
            f"/projets/{self.project_id}/supprimer", headers=self.post_headers()
        )
        self.assertEqual(response.status_code, 302)
        with session_base() as db:
            conversation = db.get(Conversation, conversation_id)
            self.assertIsNotNone(conversation)
            self.assertIsNone(conversation.project_id)
            self.assertEqual(db.query(ProjectFile).filter_by(project_id=self.project_id).count(), 0)

    def test_project_conversation_respects_disabled_history(self):
        with session_base() as db:
            db.add(UserPreference(user_id=self.user_id, conserver_historique=False))

        response = self.client.post(
            f"/projets/{self.project_id}/conversation", headers=self.post_headers()
        )
        self.assertEqual(response.status_code, 302)
        with self.client.session_transaction() as state:
            self.assertEqual(state["projet_temporaire_id"], self.project_id)
            self.assertNotIn("conversation_id", state)
        with session_base() as db:
            self.assertEqual(db.query(Conversation).filter_by(user_id=self.user_id).count(), 0)

        with web.app.test_request_context("/"):
            from flask import session
            session["projet_temporaire_id"] = self.project_id
            instructions, files = web._contexte_projet_pour_conversation(self.user_id)
        self.assertEqual(instructions, "Use the project brief.")
        self.assertEqual(files, "")

    def test_project_conversation_is_created_for_saved_history(self):
        response = self.client.post(
            f"/projets/{self.project_id}/conversation", headers=self.post_headers()
        )
        self.assertEqual(response.status_code, 302)
        with self.client.session_transaction() as state:
            conversation_id = state["conversation_id"]
            self.assertNotIn("projet_temporaire_id", state)
        with session_base() as db:
            conversation = db.get(Conversation, conversation_id)
            self.assertEqual(conversation.user_id, self.user_id)
            self.assertEqual(conversation.project_id, self.project_id)

    def test_sync_chat_passes_project_instructions_and_files_to_model(self):
        with session_base() as db:
            conversation = Conversation(user_id=self.user_id, project_id=self.project_id)
            db.add(conversation)
            db.add(UserPreference(user_id=self.user_id, conserver_historique=False))
            db.add(ProjectFile(
                project_id=self.project_id,
                filename="brief.txt",
                mime_type="text/plain",
                size_bytes=17,
                content=b"Internal project brief",
                extracted_text="Internal project brief",
            ))
            db.flush()
            conversation_id = conversation.id
        with self.client.session_transaction() as state:
            state["conversation_id"] = conversation_id

        captured = {}

        def fake_answer(*_args, **kwargs):
            captured.update(kwargs)
            return "project answer"

        from unittest.mock import patch
        with patch.object(web, "traiter_message", side_effect=fake_answer):
            response = self.client.post(
                "/repondre", data={"message": "question"}, headers=self.post_headers()
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(captured["instructions_projet"], "Use the project brief.")
        self.assertIn("Internal project brief", captured["fichiers_projet"])

    def test_sse_chat_passes_project_context_to_streamer(self):
        with session_base() as db:
            conversation = Conversation(user_id=self.user_id, project_id=self.project_id)
            db.add(conversation)
            db.flush()
            conversation_id = conversation.id
        with self.client.session_transaction() as state:
            state["conversation_id"] = conversation_id

        captured = {}

        def fake_stream(*_args, **kwargs):
            captured.update(kwargs)
            yield "project stream"

        from unittest.mock import patch
        with patch.object(web, "streamer_message", side_effect=fake_stream), \
                patch.object(web, "_actualiser_resume_en_arriere_plan"):
            response = self.client.post(
                "/repondre_flux",
                data={"message": "question"},
                headers=self.post_headers(),
                buffered=True,
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(captured["instructions_projet"], "Use the project brief.")


if __name__ == "__main__":
    unittest.main()
