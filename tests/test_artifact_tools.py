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
import brain
import artifact_tools
from artifact_tools import (
    detecter_demande_image,
    detecter_demande_pdf,
    demande_pdf_sans_sujet,
    demande_illustration_pedagogique,
    rendre_pdf,
    structurer_document,
)
from database import LibraryItem, User, VoiceTranscriptionUsage, session_base


FAKE_DOCUMENT_STRUCTURE = {"title":"Document test","author":"DASHLE","language":"fr","orientation":"portrait","footer":"DASHLE","sections":[{"heading":"Contenu","paragraphs":["Réponse JSON de test."],"bullets":[]}]}

PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


class ArtifactToolsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        web.app.config.update(TESTING=True, SESSION_COOKIE_SECURE=False)
        brain.CLE_API = "test-gemini-key"
        web.CLE_API = "test-gemini-key"
        artifact_tools.CLE_API = "test-gemini-key"

    def setUp(self):
        from database import ImageGenerationUsage
        with session_base() as db:
            db.query(ImageGenerationUsage).delete(synchronize_session=False)
        self._gemini_json = patch.object(artifact_tools, "_json_from_gemini", return_value=FAKE_DOCUMENT_STRUCTURE)
        self._gemini_json.start()
        self._image_web = patch.object(web, "generer_image", return_value=(PNG_1X1, "image/png"))
        self._image_web.start()
        self._image_tool = patch.object(artifact_tools, "generer_image", return_value=(PNG_1X1, "image/png"))
        self._image_tool.start()
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
        self._gemini_json.stop()
        self._image_web.stop()
        self._image_tool.stop()
        with session_base() as db:
            db.query(LibraryItem).filter(
                LibraryItem.user_id.in_([self.user_id, self.other_id])
            ).delete(synchronize_session=False)
            from database import ImageGenerationUsage
            db.query(ImageGenerationUsage).delete(synchronize_session=False)
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
        self.assertTrue(detecter_demande_image("Je veux que tu me génères. L'image d'une ville futuriste avec des voitures volantes."))
        self.assertTrue(detecter_demande_image("Peux-tu me créer une image d'une ville futuriste ?"))
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
        with patch.object(web, "generer_image", side_effect=RuntimeError("provider failure")), patch.object(artifact_tools, "generer_image", side_effect=RuntimeError("provider failure")):
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

    def test_transcription_audio_uses_mocked_gemini(self):
        class FakeResponse:
            status_code = 200
            def json(self):
                return {"candidates": [{"content": {"parts": [{"text": "Bonjour Dashle"}]}}]}
        with patch.object(web.requests, "post", return_value=FakeResponse()) as appel:
            response = self.client.post(
                "/api/transcrire",
                data={"audio": (io.BytesIO(b"fake-audio"), "test.webm", "audio/webm")},
                headers={"X-CSRF-Token": "artifact-token", "X-Dashle-Audio-Duration-Ms": "1000"},
                content_type="multipart/form-data",
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["texte"], "Bonjour Dashle")
        appel.assert_called_once()
        with session_base() as db:
            usage = db.query(VoiceTranscriptionUsage).filter_by(user_id=self.user_id).one()
            self.assertEqual(usage.count, 1)

    def test_transcription_audio_rejette_un_fichier_trop_gros(self):
        response = self.client.post(
            "/api/transcrire",
            data={"audio": (io.BytesIO(b"x" * (5 * 1024 * 1024 + 1)), "test.webm", "audio/webm")},
            headers={"X-CSRF-Token": "artifact-token"},
            content_type="multipart/form-data",
        )
        self.assertEqual(response.status_code, 413)

    def test_sse_action_failure_has_no_completed_event(self):
        with patch.object(web, "_quota_image_bloque", return_value=(True, {"message": "provider failure"})):
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


    def test_web_contains_voice_deduplication_guards(self):
        source = open("web.py", encoding="utf-8").read()
        self.assertIn("transcriptionFinaleVocale.trim()", source)
        self.assertIn("resultIndex repart à zéro", source)
        self.assertIn("ne pas dupliquer les finals", source)

    def test_web_does_not_add_pdf_button_to_normal_responses(self):
        source = open("web.py", encoding="utf-8").read()
        self.assertNotIn('class="action-pdf" title="Générer en PDF"', source)

    def test_sse_image_contract_contains_renderable_artifact_and_frontend_consumer(self):
        event = web._evenement_action(
            "action_completed", "abc123", "image", "termine",
            "Image générée.", resultats={"artifact": {
                "type": "image", "mime_type": "image/png",
                "filename": "image.png", "data": base64.b64encode(PNG_1X1).decode("ascii")
            }, "message_id": 1, "conversation_id": 1}
        )
        self.assertIn('"event": "action_completed"', event)
        self.assertIn('"type": "image"', event)
        self.assertIn('"mime_type": "image/png"', event)
        self.assertIn('"message_id": 1', event)
        source = open("web.py", encoding="utf-8").read()
        self.assertIn("function finaliserSuiviAction", source)
        self.assertIn("artifact.data", source)
        self.assertIn("new Blob([bytes]", source)
        self.assertIn("image.src = url", source)
        self.assertIn("ouvrirVisionneuseImage", source)
        self.assertIn("navigator.share", source)

    def test_action_event_contains_real_artifact(self):
        event = web._evenement_action(
            "action_completed", "abc123", "image", "termine",
            "Image générée.", resultats={"artifact": {
                "type": "image", "mime_type": "image/png",
                "filename": "image.png", "data": "AAAA"
            }}
        )
        self.assertIn('"artifact"', event)
        self.assertIn('"mime_type": "image/png"', event)
        self.assertIn('"artifact"', event)
        self.assertIn('"mime_type": "image/png"', event)


    def test_image_quota_limits_visitor_free_and_owner(self):
        with patch.object(web, "generer_image", return_value=(PNG_1X1, "image/png")):
            with self.client.session_transaction() as state:
                state.pop("user_id", None)
            for _ in range(2):
                response = self.client.post("/repondre", data={"message": "Crée une image d'une ville."})
                self.assertEqual(response.status_code, 200)
            response = self.client.post("/repondre", data={"message": "Crée une image d'une ville."})
            self.assertEqual(response.status_code, 429)
            self.assertEqual(response.json["quota"]["limite"], 2)

        with self.client.session_transaction() as state:
            state["user_id"] = self.user_id
        with patch.object(web, "generer_image", return_value=(PNG_1X1, "image/png")):
            for _ in range(web.IMAGE_DAILY_LIMITS["free"]):
                response = self.client.post("/repondre", data={"message": "Crée une image d'une ville."},
                                            headers={"X-CSRF-Token": "artifact-token"})
                self.assertEqual(response.status_code, 200)
            response = self.client.post("/repondre", data={"message": "Crée une image d'une ville."},
                                        headers={"X-CSRF-Token": "artifact-token"})
            self.assertEqual(response.status_code, 429)
            self.assertEqual(response.json["quota"]["niveau"], "free")

        with session_base() as db:
            user = db.get(User, self.user_id)
            user.subscription_level = "prime"
            user.subscription_expires_at = None
            from database import ImageGenerationUsage
            db.query(ImageGenerationUsage).filter_by(user_id=self.user_id).delete(synchronize_session=False)
        with patch.object(web, "generer_image", return_value=(PNG_1X1, "image/png")):
            for _ in range(web.IMAGE_DAILY_LIMITS["prime"]):
                response = self.client.post("/repondre", data={"message": "Crée une image d'une ville."},
                                            headers={"X-CSRF-Token": "artifact-token"})
                self.assertEqual(response.status_code, 200)
            response = self.client.post("/repondre", data={"message": "Crée une image d'une ville."},
                                        headers={"X-CSRF-Token": "artifact-token"})
            self.assertEqual(response.status_code, 429)
            self.assertEqual(response.json["quota"]["niveau"], "prime")

        # OWNER_EMAILS reste exempt de quota.
        original = os.environ.get("OWNER_EMAILS")
        os.environ["OWNER_EMAILS"] = "owner-artifact@example.invalid"
        try:
            with session_base() as db:
                user = db.get(User, self.user_id)
                user.email = "owner-artifact@example.invalid"
            with patch.object(web, "generer_image", return_value=(PNG_1X1, "image/png")), patch.object(artifact_tools, "generer_image", return_value=(PNG_1X1, "image/png")):
                for _ in range(web.IMAGE_DAILY_LIMITS["prime"] + 2):
                    response = self.client.post("/repondre", data={"message": "Crée une image d'une ville."},
                                                headers={"X-CSRF-Token": "artifact-token"})
                    self.assertEqual(response.status_code, 200)
        finally:
            if original is None:
                os.environ.pop("OWNER_EMAILS", None)
            else:
                os.environ["OWNER_EMAILS"] = original

    def test_image_quota_does_not_consume_failed_generation(self):
        with patch.object(web, "generer_image", side_effect=RuntimeError("provider failure")):
            response = self.client.post("/repondre", data={"message": "Crée une image d'une ville."},
                                        headers={"X-CSRF-Token": "artifact-token"})
        self.assertEqual(response.status_code, 502)
        from database import ImageGenerationUsage
        with session_base() as db:
            usage = db.query(ImageGenerationUsage).filter_by(user_id=self.user_id).one_or_none()
            self.assertTrue(usage is None or usage.count == 0)

    def test_image_progress_and_viewer_contract(self):
        with patch.object(web, "generer_image", return_value=(PNG_1X1, "image/png")):
            response = self.client.post("/repondre_flux",
                data={"message": "Crée une image d'une ville futuriste."},
                headers={"X-CSRF-Token": "artifact-token"})
        body = response.get_data(as_text=True)
        self.assertIn("Ton idée prend forme", body)
        self.assertIn("Création d'une première ébauche", body)
        self.assertIn("Finitions", body)
        self.assertIn('"event": "action_completed"', body)
        source = open("web.py", encoding="utf-8").read()
        self.assertIn("ouvrirVisionneuseImage", source)
        self.assertIn("navigator.share", source)
        self.assertIn("Télécharger", source)
        self.assertIn("Régénérer", source)


if __name__ == "__main__":
    unittest.main()
