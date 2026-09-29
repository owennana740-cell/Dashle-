"""Tests des outils de génération de fichiers et d'images Dashle."""
import base64
import io
import os
import unittest
import uuid
from unittest.mock import patch

os.environ["DATABASE_URL"] = "sqlite://"
os.environ["SESSION_COOKIE_SECURE"] = "0"

import web
from artifact_tools import (
    detecter_demande_image,
    detecter_demande_pdf,
    demande_pdf_sans_sujet,
    demande_illustration_pedagogique,
    rendre_pdf,
    structurer_document,
)
from database import LibraryItem, User, session_base


PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


class ArtifactToolsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        web.app.config.update(TESTING=True, SESSION_COOKIE_SECURE=False)

    def setUp(self):
        self.client = web.app.test_client()
        with session_base() as db:
            user = User(
                email=f"artifact-{uuid.uuid4().hex}@example.invalid",
                password_hash="test-only",
            )
            other = User(
                email=f"artifact-other-{uuid.uuid4().hex}@example.invalid",
                password_hash="test-only",
            )
            db.add_all([user, other])
            db.flush()
            self.user_id, self.other_id = user.id, other.id
        with self.client.session_transaction() as state:
            state["user_id"] = self.user_id
            state["csrf_token"] = "artifact-token"

    def tearDown(self):
        with session_base() as db:
            db.query(LibraryItem).filter(
                LibraryItem.user_id.in_([self.user_id, self.other_id])
            ).delete(synchronize_session=False)
            db.query(User).filter(
                User.id.in_([self.user_id, self.other_id])
            ).delete(synchronize_session=False)

    def test_intentions(self):
        self.assertTrue(detecter_demande_pdf("Génère-moi un PDF de 5 pages présentant DASHLE"))
        self.assertTrue(detecter_demande_pdf("Transforme ce texte en PDF"))
        self.assertTrue(detecter_demande_pdf("fais-moi un PDF"))
        self.assertTrue(detecter_demande_pdf("génère un pdf sur le gouvernement burkinabè"))
        self.assertTrue(detecter_demande_pdf("exporte en PDF"))
        self.assertTrue(detecter_demande_pdf("peux-tu me faire un pdf de 5 conseils pour apprendre Python"))
        self.assertTrue(demande_pdf_sans_sujet("tu peux me générer un PDF ?"))
        self.assertFalse(demande_pdf_sans_sujet("génère un PDF sur le gouvernement burkinabè"))
        self.assertFalse(demande_pdf_sans_sujet("fais-moi un PDF de 5 conseils pour apprendre Python"))
        self.assertTrue(detecter_demande_image("Crée une image d'une ville futuriste"))
        self.assertTrue(detecter_demande_image("Fais un schéma du fonctionnement d'un moteur"))
        self.assertFalse(demande_illustration_pedagogique("Quelle est la définition de HTTP ?"))
        self.assertTrue(demande_illustration_pedagogique(
            "Explique-moi comment fonctionne le système solaire et son organisation."
        ))

    def test_structured_pdf_contains_real_content_not_request_sentence(self):
        structure = structurer_document(
            "Génère un PDF sur les règles de sécurité informatique pour débutants, avec 10 règles.",
            contenu_fourni="Introduction\nRègle 1 : utiliser un mot de passe unique.\nConclusion",
        )
        pdf = rendre_pdf(structure)
        self.assertTrue(pdf.startswith(b"%PDF-"))
        self.assertGreater(len(pdf), 500)
        self.assertNotEqual(structure["sections"][0]["paragraphs"][0],
                            "Génère un PDF sur les règles de sécurité informatique pour débutants, avec 10 règles.")

    def test_title_sections_lists_and_table_are_rendered(self):
        structure = {
            "title": "Cours de sécurité",
            "author": "DASHLE",
            "language": "fr",
            "orientation": "portrait",
            "footer": "DASHLE",
            "sections": [{
                "heading": "10 règles",
                "paragraphs": ["Introduction en français."],
                "bullets": ["Règle 1", "Règle 2"],
                "table": {
                    "headers": ["Risque", "Protection"],
                    "rows": [["Hameçonnage", "Vérifier le lien"], ["Mot de passe", "Utiliser un gestionnaire"]],
                },
            }],
        }
        pdf = rendre_pdf(structure)
        self.assertTrue(pdf.startswith(b"%PDF-"))
        self.assertGreater(len(pdf), 800)

    def test_explicit_image_is_real_and_saved_to_correct_user(self):
        with patch.object(web, "generer_image", return_value=(PNG_1X1, "image/png")):
            response = self.client.post(
                "/repondre",
                data={"message": "Génère une image d'une ville futuriste."},
                headers={"X-CSRF-Token": "artifact-token"},
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["artifact"]["type"], "image")
        self.assertEqual(base64.b64decode(response.json["artifact"]["data"]), PNG_1X1)
        with session_base() as db:
            item = db.query(LibraryItem).filter_by(user_id=self.user_id, type="image").one()
            self.assertEqual(item.content, PNG_1X1)
            self.assertIsNone(
                db.query(LibraryItem).filter_by(user_id=self.other_id, type="image").first()
            )

    def test_generated_pdf_is_saved_and_other_user_cannot_download(self):
        structure = {
            "title": "Présentation DASHLE",
            "author": "",
            "language": "fr",
            "orientation": "portrait",
            "footer": "DASHLE",
            "sections": [{
                "heading": "Introduction",
                "paragraphs": ["DASHLE est un assistant personnel."],
                "bullets": [],
            }],
        }
        with patch.object(web, "structurer_document", return_value=structure):
            response = self.client.post(
                "/repondre",
                data={"message": "Génère-moi un PDF de présentation de DASHLE."},
                headers={"X-CSRF-Token": "artifact-token"},
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["artifact"]["type"], "pdf")
        pdf = base64.b64decode(response.json["artifact"]["data"])
        self.assertTrue(pdf.startswith(b"%PDF-"))
        with session_base() as db:
            item = db.query(LibraryItem).filter_by(user_id=self.user_id, type="pdf").one()
            item_id = item.id
        self.assertEqual(
            self.client.get(f"/bibliotheque/{item_id}/telecharger").status_code, 200
        )
        with self.client.session_transaction() as state:
            state["user_id"] = self.other_id
        self.assertEqual(
            self.client.get(f"/bibliotheque/{item_id}/telecharger").status_code, 404
        )

    def test_image_generation_failure_does_not_break_chat(self):
        with patch.object(web, "generer_image", side_effect=RuntimeError("provider failure")):
            response = self.client.post(
                "/repondre",
                data={"message": "Crée un logo pour mon projet."},
                headers={"X-CSRF-Token": "artifact-token"},
            )
        self.assertEqual(response.status_code, 502)
        self.assertIn("pas pu générer", response.json["reponse"])


    def test_sse_image_action_lifecycle(self):
        with patch.object(web, "generer_image", return_value=(PNG_1X1, "image/png")), \
             patch.object(web, "_enregistrer_element_bibliotheque", return_value=True):
            response = self.client.post(
                "/repondre_flux",
                data={"message": "Génère une image d'une ville futuriste."},
                headers={"X-CSRF-Token": "artifact-token"},
            )
        body = response.get_data(as_text=True)
        self.assertIn('"event": "action_started"', body)
        self.assertIn('"event": "action_progress"', body)
        self.assertIn('"event": "action_completed"', body)
        self.assertIn('"type": "image"', body)
        self.assertIn('"saved": true', body)

    def test_sse_pdf_action_lifecycle(self):
        structure = {
            "title": "Conseils Python",
            "author": "DASHLE",
            "language": "fr",
            "orientation": "portrait",
            "footer": "DASHLE",
            "sections": [{
                "heading": "Conseils",
                "paragraphs": ["Apprendre Python progressivement."],
                "bullets": ["Pratiquer", "Lire la documentation"],
            }],
        }
        with patch.object(web, "structurer_document", return_value=structure), \
             patch.object(web, "rendre_pdf", return_value=b"%PDF-test"), \
             patch.object(web, "_enregistrer_element_bibliotheque", return_value=True):
            response = self.client.post(
                "/repondre_flux",
                data={"message": "Génère un PDF de 5 conseils pour apprendre Python."},
                headers={"X-CSRF-Token": "artifact-token"},
            )
        body = response.get_data(as_text=True)
        self.assertIn('"event": "action_started"', body)
        self.assertIn('"message": "Génération du contenu…"', body)
        self.assertIn('"message": "Mise en page du PDF…"', body)
        self.assertIn('"message": "Génération du PDF…"', body)
        self.assertIn('"event": "action_completed"', body)
        self.assertIn('"type": "pdf"', body)

    def test_sse_action_failure_has_no_completed_event(self):
        with patch.object(web, "generer_image", side_effect=RuntimeError("provider failure")):
            response = self.client.post(
                "/repondre_flux",
                data={"message": "Crée un logo pour mon projet."},
                headers={"X-CSRF-Token": "artifact-token"},
            )
        body = response.get_data(as_text=True)
        self.assertIn('"event": "action_failed"', body)
        self.assertNotIn('"event": "action_completed"', body)

    def test_action_event_builder_supports_cancelled(self):
        event = web._evenement_action(
            "action_cancelled", "abc123", "image", "annule", "Action annulée"
        )
        self.assertIn('"event": "action_cancelled"', event)
        self.assertIn('"step": "annule"', event)


if __name__ == "__main__":
    unittest.main()
