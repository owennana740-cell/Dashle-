"""Tests des contrats/providers réels avec réseau simulé uniquement au niveau du test."""
import base64
import unittest
from unittest.mock import patch

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
