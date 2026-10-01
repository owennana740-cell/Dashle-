"""Regression tests for DASHLE request/SSE observability."""

import os
import unittest
from unittest.mock import patch

os.environ["DATABASE_URL"] = "sqlite://"
os.environ["SESSION_COOKIE_SECURE"] = "0"

import web


class ObservabilityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        web.app.config.update(TESTING=True, SESSION_COOKIE_SECURE=False)

    def test_request_id_and_sse_timing_are_logged_without_prompt_content(self):
        client = web.app.test_client()
        request_id = "obs-test-123"
        prompt = "PROMPT-NE-DOIT-PAS-APPARAITRE-DANS-LES-LOGS"

        with patch.object(web, "streamer_message", return_value=iter(["réponse test"])), \\
                self.assertLogs(web.app.logger, level="INFO") as captured:
            response = client.post(
                "/repondre_flux",
                data={"message": prompt},
                headers={"X-Request-ID": request_id},
                buffered=True,
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers.get("X-Request-ID"), request_id)
        logs = "\n".join(captured.output)
        self.assertIn("dashle.request", logs)
        self.assertIn("dashle.sse", logs)
        self.assertIn(request_id, logs)
        self.assertIn("provider=gemini", logs)
        self.assertIn("duration_ms=", logs)
        self.assertIn("ttfb_ms=", logs)
        self.assertNotIn(prompt, logs)


if __name__ == "__main__":
    unittest.main()
