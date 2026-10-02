"""Regression tests for provider cleanup when an SSE generator is cancelled."""

import unittest
from unittest.mock import patch

import brain


class SseResilienceTests(unittest.TestCase):
    def test_stream_generator_closes_provider_response_when_cancelled(self):
        class FakeResponse:
            def __init__(self):
                self.closed = False
                self.encoding = None

            def raise_for_status(self):
                return None

            def iter_lines(self, decode_unicode=True):
                yield 'data: {"candidates":[{"content":{"parts":[{"text":"bonjour"}]}}]}'
                yield 'data: [DONE]'

            def close(self):
                self.closed = True

        response = FakeResponse()
        with patch.object(brain, "CLE_API", "test-key"), \
                patch.object(brain, "preflight_connector", return_value=None), \
                patch.object(brain, "_reglages_reponse", return_value=("", 1000, "free", "")), \
                patch.object(brain, "contexte_temps_reel", return_value=""), \
                patch.object(brain, "_plugins_actifs", return_value=[]), \
                patch.object(brain, "_memoire_pertinente", return_value=""), \
                patch.object(brain, "_instruction_systeme", return_value=""), \
                patch.object(brain, "_construire_contents", return_value=[]), \
                patch.object(brain, "_gen_config", return_value={}), \
                patch.object(brain._session, "post", return_value=response):
            flux = brain.streamer_a_lia("bonjour", [], user_id=1)
            self.assertEqual(next(flux), "bonjour")
            flux.close()

        self.assertTrue(response.closed)


if __name__ == "__main__":
    unittest.main()
