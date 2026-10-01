"""Security regression tests for provider error logging."""

import unittest
from unittest.mock import patch

import requests

import brain


class ProviderLoggingSecurityTests(unittest.TestCase):
    def test_provider_error_body_is_not_logged(self):
        marker = "PROVIDER-ERROR-BODY-SHOULD-NOT-BE-LOGGED"

        class FakeResponse:
            status_code = 500
            text = marker
            headers = {}

            def raise_for_status(self):
                raise requests.HTTPError("provider failure", response=self)

        with (
                patch.object(brain, "CLE_API", "test-key"),
                patch.object(brain, "contexte_temps_reel", return_value=""),
                patch.object(brain, "_plugins_actifs", return_value=[]),
                patch.object(brain, "_instruction_systeme", return_value=""),
                patch.object(brain, "_nom_utilisateur", return_value=""),
                patch.object(brain, "_construire_contents", return_value=[]),
                patch.object(brain, "_gen_config", return_value={}),
                patch.object(brain._session, "post", return_value=FakeResponse()),
                self.assertLogs(brain.logger, level="ERROR") as captured,
        ):
            brain.demander_a_lia("bonjour")

        logs = "\n".join(captured.output)
        self.assertIn("code=500", logs)
        self.assertNotIn(marker, logs)


if __name__ == "__main__":
    unittest.main()