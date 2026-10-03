"""Tests des contrats/providers réels avec réseau simulé uniquement au niveau du test."""
import base64
import io
import unittest
from unittest.mock import patch

import web

from tool_router import detect_tool_intent
from tool_providers import (
    GeminiImageEditingProvider,
    GeminiVideoGenerationProvider,
    GeminiWebSearchProvider,
    ProviderUnavailable,
)

PNG = base64.b64encode(b"fake-image").decode("ascii")


class ToolRoutingTests(unittest.TestCase):
    def test_natural_image_generation(self):
        self.assertEqual(
            detect_tool_intent("Crée-moi une ville futuriste avec des voitures volantes.").name,
            "image_generation",
        )

    def test_natural_video_generation(self):
        self.assertEqual(
            detect_tool_intent("Fais une courte vidéo où une voiture volante traverse cette ville.").name,
            "video_generation",
        )

    def test_natural_web_search(self):
        self.assertEqual(
            detect_tool_intent("Cherche sur Internet les dernières informations sur le climat.").name,
            "web_search",
        )

    def test_image_edit_requires_supplied_image(self):
        self.assertEqual(
            detect_tool_intent("Transforme cette photo en style futuriste.", has_image=True).name,
            "image_editing",
        )
        self.assertEqual(
            detect_tool_intent("Transforme cette photo en style futuriste.").name,
            "chat",
        )

    def test_video_analysis_requires_supplied_video(self):
        self.assertEqual(
            detect_tool_intent("Analyse cette vidéo et explique ce qu'il se passe.", has_video=True).name,
            "video_analysis",
        )

    def test_pdf_is_preserved(self):
        self.assertEqual(
            detect_tool_intent("Transforme cette réponse en PDF.").name,
            "pdf_generation",
        )


class ProviderParsingTests(unittest.TestCase):
    def test_web_search_extracts_answer_and_citations(self):
        payload = {
            "steps": [{
                "type": "model_output",
                "content": [{
                    "type": "text",
                    "text": "Réponse actuelle.",
                    "annotations": [{
                        "type": "url_citation", "url": "https://example.com", "title": "Example",
                        "start_index": 0, "end_index": 8
                    }]
                }]
            }]
        }
        fake = type("R", (), {"ok": True, "status_code": 200, "json": lambda self: payload})()
        with patch("tool_providers.requests.request", return_value=fake):
            results = GeminiWebSearchProvider(lambda: "server-secret").search("actualité")
        self.assertEqual(results[0].answer, "Réponse actuelle.")
        self.assertEqual(results[0].url, "https://example.com")
        self.assertNotIn("server-secret", str(results))

    def test_image_edit_sends_image_and_prompt_server_side(self):
        payload = {"output_image": {"data": PNG, "mime_type": "image/png"}}
        fake = type("R", (), {"ok": True, "status_code": 200, "json": lambda self: payload})()
        with patch("tool_providers.requests.request", return_value=fake) as call:
            artifact = GeminiImageEditingProvider(lambda: "server-secret").edit(
                b"fake-image", "image/png", "Transforme cette photo en style futuriste."
            )
        self.assertEqual(artifact.data, b"fake-image")
        sent = call.call_args.kwargs["json"]
        self.assertNotIn("server-secret", str(sent))
        self.assertEqual(sent["input"][0]["type"], "image")
        self.assertEqual(sent["input"][1]["type"], "text")

    def test_video_provider_creates_long_running_job(self):
        payload = {"name": "operations/video-123"}
        fake = type("R", (), {"ok": True, "status_code": 200, "json": lambda self: payload})()
        with patch("tool_providers.requests.request", return_value=fake) as call:
            job = GeminiVideoGenerationProvider(lambda: "server-secret").create("Une ville futuriste")
        self.assertEqual(job.status, "queued")
        self.assertIsNone(job.progress)
        self.assertIn(":predictLongRunning", call.call_args.args[1])
        self.assertNotIn("server-secret", str(call.call_args.kwargs["json"]))

    def test_missing_key_is_unavailable(self):
        with self.assertRaises(ProviderUnavailable):
            GeminiWebSearchProvider(lambda: None).search("test")

    def test_provider_error_never_contains_key(self):
        fake = type("R", (), {
            "ok": False, "status_code": 403,
            "json": lambda self: {"error": {"message": "forbidden"}}
        })()
        with patch("tool_providers.requests.request", return_value=fake):
            with self.assertRaises(ProviderUnavailable) as ctx:
                GeminiWebSearchProvider(lambda: "server-secret").search("test")
        self.assertNotIn("server-secret", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()


class ProviderRouteTests(unittest.TestCase):
    def setUp(self):
        web.app.config.update(TESTING=True, SESSION_COOKIE_SECURE=False)
        self.client = web.app.test_client()
        with self.client.session_transaction() as state:
            state["csrf_token"] = "tool-test"

    def test_web_search_route_renders_real_provider_sources(self):
        from tool_providers import WebSearchResult
        class FakeWeb:
            def search(self, query, *, timeout_s):
                return [WebSearchResult(
                    title="Source officielle", url="https://example.com/source",
                    snippet="Extrait", source="Example", answer="Réponse fraîche."
                )]
        with patch.object(web.PROVIDER_REGISTRY, "available", return_value=True),              patch.object(web.PROVIDER_REGISTRY, "get", return_value=FakeWeb()):
            response = self.client.post(
                "/repondre", data={"message": "Cherche sur Internet les dernières informations sur DASHLE."}
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["status"], "web_search")
        self.assertEqual(response.json["sources"][0]["url"], "https://example.com/source")
        self.assertEqual(response.json["reponse"], "Réponse fraîche.")

    def test_video_sse_launches_background_job_without_blocking_chat(self):
        with patch.object(web.PROVIDER_REGISTRY, "available", return_value=True), patch.object(web, "_lancer_job_video", return_value="job-test-123"):
            response = self.client.post(
                "/repondre_flux", data={"message": "Fais une vidéo d'une ville futuriste."}
            )
            body = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn('"type": "video"', body)
        self.assertIn('"step": "en_attente"', body)
        self.assertIn('"job_id": "job-test-123"', body)
        self.assertIn('"tool_job_id": "job-test-123"', body)

    def test_video_job_flux_renders_completed_artifact(self):
        from database import VideoGenerationJob, session_base
        from datetime import datetime, timedelta
        with self.client.session_transaction() as state:
            state["user_id"] = 999
        now = datetime.utcnow()
        with session_base() as db:
            db.add(VideoGenerationJob(
                id="job-progress-test", user_id=999, visitor_key=None, conversation_id=None,
                provider="gemini", tool_type="video_generation", prompt="test", status="completed",
                progress=100.0, status_message="Génération terminée", provider_job_id="operations/test",
                result_mime_type="video/mp4", result_filename="video.mp4", result_data=b"video",
                result_size_bytes=5, expires_at=now + timedelta(hours=1),
                retention_until=now + timedelta(days=7), completed_at=now,
            ))
        try:
            response = self.client.get("/api/outils/jobs/job-progress-test/flux", headers={"X-CSRF-Token": "tool-test"})
            body = response.get_data(as_text=True)
            self.assertEqual(response.status_code, 200, f"status={response.status_code} location={response.location!r}")
            self.assertIn('"event": "action_completed"', body)
            self.assertIn('"mime_type": "video/mp4"', body)
            self.assertIn('/api/outils/jobs/job-progress-test/result', body)
        finally:
            with session_base() as db:
                db.query(VideoGenerationJob).filter_by(id="job-progress-test").delete()

    def test_image_edit_route_passes_source_image_to_provider(self):
        from tool_providers import ImageArtifact
        class FakeEdit:
            def edit(self, image_bytes, mime_type, prompt, *, timeout_s):
                assert image_bytes == b"fake-image"
                assert mime_type == "image/png"
                return ImageArtifact(b"edited-image", "image/png", "edited.png")
        with patch.object(web.PROVIDER_REGISTRY, "available", return_value=True),              patch.object(web.PROVIDER_REGISTRY, "get", return_value=FakeEdit()),              patch.object(web, "_consommer_quota_image", return_value=True),              patch.object(web, "_enregistrer_element_bibliotheque", return_value=False),              patch.object(web, "detecter_type_media", return_value="image/png"):
            response = self.client.post(
                "/repondre_image",
                data={"message": "Transforme cette photo en style futuriste.",
                      "image": (io.BytesIO(b"fake-image"), "photo.png")},
                content_type="multipart/form-data",
                headers={"X-CSRF-Token": "tool-test"},
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["status"], "success")
        self.assertEqual(response.json["artifact"]["filename"], "edited.png")
