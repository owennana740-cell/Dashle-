"""Contract tests for DASHLE image/video multimodal input."""

import base64
import io
import os
import unittest
from unittest.mock import patch

os.environ["DATABASE_URL"] = "sqlite://"
os.environ["SESSION_COOKIE_SECURE"] = "0"

import web


class MultimodalContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        web.app.config.update(TESTING=True, SESSION_COOKIE_SECURE=False)

    def setUp(self):
        self.client = web.app.test_client()

    def test_missing_media_is_rejected_without_provider_call(self):
        with patch.object(web, "traiter_message_image") as analyse:
            response = self.client.post("/repondre_image", data={"message": "Décris cette image."})
        self.assertEqual(response.status_code, 200)
        self.assertIn("Aucune image reçue", response.get_json()["reponse"])
        analyse.assert_not_called()

    def test_oversized_media_is_rejected_before_provider(self):
        with (
            patch.object(web, "IMAGE_UPLOAD_MAX_BYTES", 4),
            patch.object(web, "traiter_message_image") as analyse,
        ):
            response = self.client.post(
                "/repondre_image",
                data={"image": (io.BytesIO(b"12345"), "image.png")},
            )
        self.assertEqual(response.status_code, 413)
        self.assertEqual(response.get_json()["code"], "image_trop_volumineuse")
        analyse.assert_not_called()

    def test_invalid_media_type_is_rejected_before_provider(self):
        with (
            patch.object(web, "detecter_type_media", return_value=None),
            patch.object(web, "traiter_message_image") as analyse,
        ):
            response = self.client.post(
                "/repondre_image",
                data={"image": (io.BytesIO(b"not-an-image"), "image.png")},
            )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()["code"], "image_invalide")
        analyse.assert_not_called()

    def test_image_is_forwarded_with_detected_mime_and_base64_payload(self):
        captured = {}

        def fake_analyse(message, image_b64, mime_type, historique, resume, user_id=None, **_kwargs):
            captured.update(
                message=message,
                image_b64=image_b64,
                mime_type=mime_type,
                historique=historique,
                resume=resume,
                user_id=user_id,
            )
            return "Analyse multimodale réussie"

        with (
            patch.object(web, "detecter_type_media", return_value="image/png"),
            patch.object(web, "PIL_DISPONIBLE", False),
            patch.object(web, "_quota_image_bloque", return_value=(False, None)),
            patch.object(web, "_consommer_quota_image", return_value=True),
            patch.object(web, "traiter_message_image", side_effect=fake_analyse),
        ):
            response = self.client.post(
                "/repondre_image",
                data={
                    "message": "Que vois-tu ?",
                    "image": (io.BytesIO(b"fake-png"), "photo.png"),
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["reponse"], "Analyse multimodale réussie")
        self.assertEqual(captured["message"], "Que vois-tu ?")
        self.assertEqual(captured["mime_type"], "image/png")
        self.assertEqual(base64.b64decode(captured["image_b64"]), b"fake-png")
        self.assertIsNone(captured["user_id"])

    def test_video_uses_same_multimodal_provider_without_image_quota(self):
        with (
            patch.object(web, "detecter_type_media", return_value="video/mp4"),
            patch.object(web, "traiter_message_image", return_value="Vidéo analysée") as analyse,
            patch.object(web, "_consommer_quota_image") as consommer,
        ):
            response = self.client.post(
                "/repondre_image",
                data={
                    "message": "Résume cette vidéo.",
                    "image": (io.BytesIO(b"fake-video"), "clip.mp4"),
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["reponse"], "Vidéo analysée")
        self.assertEqual(analyse.call_args.args[:3], ("Résume cette vidéo.", base64.b64encode(b"fake-video").decode("utf-8"), "video/mp4"))
        consommer.assert_not_called()

    def test_image_edit_is_not_misrepresented_as_analysis_or_generation(self):
        with (
            patch.object(web, "detecter_type_media", return_value="image/png"),
            patch.object(web.PROVIDER_REGISTRY, "available", return_value=False),
            patch.object(web, "traiter_message_image") as analyse,
        ):
            response = self.client.post(
                "/repondre_image",
                data={
                    "message": "Transforme cette image en style futuriste.",
                    "image": (io.BytesIO(b"fake-png"), "photo.png"),
                },
            )

        self.assertEqual(response.status_code, 501)
        self.assertEqual(response.get_json()["status"], "provider_unavailable")
        self.assertIn("aucun outil d’édition d’image", response.get_json()["reponse"])
        analyse.assert_not_called()


if __name__ == "__main__":
    unittest.main()
