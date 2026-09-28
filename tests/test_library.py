"""Private library persistence, filtering, and ownership checks."""

import os
import unittest
import uuid
import importlib.util

os.environ["DATABASE_URL"] = "sqlite://"
os.environ["SESSION_COOKIE_SECURE"] = "0"

import web
from database import LibraryItem, StatisticalAnalysisUsage, User, session_base
from unittest.mock import patch


class LibraryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        web.app.config.update(TESTING=True, SESSION_COOKIE_SECURE=False)

    def setUp(self):
        self.client = web.app.test_client()
        with session_base() as db:
            user = User(email=f"library-{uuid.uuid4().hex}@example.invalid", password_hash="test-only")
            other = User(email=f"other-library-{uuid.uuid4().hex}@example.invalid", password_hash="test-only")
            db.add_all([user, other])
            db.flush()
            self.user_id, self.other_id = user.id, other.id
            private = LibraryItem(user_id=self.user_id, type="pdf", title="Rapport Burkina",
                                  mime_type="application/pdf", content=b"%PDF-test", size_bytes=9)
            foreign = LibraryItem(user_id=self.other_id, type="analyse", title="Secret other",
                                  mime_type="text/plain", content=b"private", size_bytes=7)
            db.add_all([private, foreign])
            db.flush()
            self.item_id = private.id
            self.foreign_item_id = foreign.id
        with self.client.session_transaction() as state:
            state["user_id"] = self.user_id
            state["csrf_token"] = "library-token"

    def test_list_filter_download_delete_and_strict_ownership(self):
        page = self.client.get("/bibliotheque?type=pdf&q=Burkina")
        self.assertEqual(page.status_code, 200)
        self.assertIn(b"Rapport Burkina", page.data)
        self.assertNotIn(b"Secret other", page.data)

        download = self.client.get(f"/bibliotheque/{self.item_id}/telecharger")
        self.assertEqual(download.status_code, 200)
        self.assertEqual(download.data, b"%PDF-test")
        self.assertEqual(self.client.get(
            f"/bibliotheque/{self.foreign_item_id}/telecharger").status_code, 404)
        self.assertEqual(self.client.post(
            f"/bibliotheque/{self.foreign_item_id}/supprimer",
            headers={"X-CSRF-Token": "library-token"}).status_code, 404)
        self.assertEqual(self.client.post(
            f"/bibliotheque/{self.item_id}/supprimer",
            headers={"X-CSRF-Token": "library-token"}).status_code, 302)
        with session_base() as db:
            self.assertIsNone(db.get(LibraryItem, self.item_id))
            self.assertIsNotNone(db.get(LibraryItem, self.foreign_item_id))

    def test_generated_item_respects_user_quota(self):
        ok = web._enregistrer_element_bibliotheque(
            self.user_id, "analyse", "Résultats", "text/plain", "résumé"
        )
        self.assertTrue(ok)
        self.assertFalse(web._enregistrer_element_bibliotheque(
            self.user_id, "analyse", "Trop gros", "text/plain",
            b"x" * (web.MAX_LIBRARY_ITEM_BYTES + 1),
        ))
        self.assertFalse(web._enregistrer_element_bibliotheque(
            self.other_id + 100000, "analyse", "Foreign", "text/plain", "x"
        ))

    @unittest.skipUnless(importlib.util.find_spec("reportlab"), "reportlab absent from local test environment")
    def test_requested_pdf_is_saved_automatically(self):
        with patch.object(web, "traiter_message", return_value="PDF body"):
            response = self.client.post(
                "/telecharger-pdf-temps-reel",
                data={"sujet": "Le gouvernement du Burkina Faso"},
                headers={"X-CSRF-Token": "library-token"},
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.mimetype, "application/pdf")
        with session_base() as db:
            item = db.query(LibraryItem).filter_by(user_id=self.user_id, type="pdf").one()
            self.assertIn(b"PDF", item.content)

    def test_statistical_analysis_and_chart_are_saved_automatically(self):
        with session_base() as db:
            user = db.get(User, self.user_id)
            user.subscription_level = "pro"
        chart = "data:image/svg+xml;base64," + __import__("base64").b64encode(
            b"<svg xmlns='http://www.w3.org/2000/svg'></svg>"
        ).decode("ascii")
        with patch.object(web, "analyser_fichier", return_value=("known calculations", {
                "lignes": 2, "colonnes": 1, "graphique": chart,
        })), patch.object(web, "traiter_message", return_value="Interpretation"):
            response = self.client.post(
                "/statistiques",
                data={"fichier": (__import__("io").BytesIO(b"x,y\n1,2"), "sales.csv"), "question": "Analyse"},
                headers={"X-CSRF-Token": "library-token"},
                content_type="multipart/form-data",
            )
        self.assertEqual(response.status_code, 200)
        with session_base() as db:
            items = db.query(LibraryItem).filter_by(user_id=self.user_id).all()
            self.assertTrue({"analyse", "graphique"}.issubset({item.type for item in items}))
            self.assertEqual(db.query(StatisticalAnalysisUsage).filter_by(user_id=self.user_id).count(), 1)


if __name__ == "__main__":
    unittest.main()
