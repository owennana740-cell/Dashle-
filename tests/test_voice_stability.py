import re
from pathlib import Path
import unittest

SW = Path(__file__).resolve().parents[1] / "static" / "service-worker.js"

WEB = Path(__file__).resolve().parents[1] / "web.py"


class TestVoiceStability(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = WEB.read_text(encoding="utf-8")

    def test_vocal_uses_phrase_by_phrase_recognition(self):
        block = self.source[self.source.index("function demarrerEcouteVocale()"):
                           self.source.index("function planifierRelanceReco()")]
        self.assertIn("reco.interimResults = false;", block)
        self.assertIn("reco.continuous = false;", block)
        self.assertIn("arreterVAD();", block)
        self.assertIn("armerWatchdog();", block)

    def test_dictation_settings_remain_unchanged(self):
        block = self.source[self.source.index("btnMicro.onclick"):
                           self.source.index("btnVocal.onclick")]
        self.assertIn("reco.interimResults = false;", block)
        self.assertIn("reco.continuous = false;", block)

    def test_result_and_end_logs_are_client_side_and_complete(self):
        result = self.source[self.source.index("reco.onresult = function(e)"):
                             self.source.index("reco.onend = function()")]
        end = self.source[self.source.index("reco.onend = function()"):
                           self.source.index("reco.onerror = function(e)")]
        self.assertIn("transcript: transcript", result)
        self.assertIn("confidence: confidence", result)
        self.assertIn("isFinal: Boolean", result)
        self.assertIn("dureeEcouteMs", end)
        self.assertIn("hasTranscription", end)
        self.assertIn("transcriptionLength", end)
        self.assertIn("dernierModeReconnaissance", end)

    def test_error_handler_is_attached(self):
        block = self.source[self.source.index("reco.onerror = function(e)"):
                           self.source.index("window._dashleVocal")]
        self.assertIn("e && e.error", block)
        self.assertIn("e && e.message", block)
        self.assertIn("planifierRelanceReco();", block)

    def test_no_legacy_immediate_finish_symbols_remain(self):
        self.assertNotIn("nbFinsImmediatesVocal", self.source)
        self.assertNotIn("DUREE_FIN_IMMEDIATE_VOCAL_MS", self.source)
        self.assertNotIn("MAX_FINS_IMMEDIATES_VOCAL", self.source)

    def test_visible_diagnostic_handlers_and_version(self):
        for event in ("onaudiostart", "onsoundstart", "onspeechstart", "onspeechend", "onsoundend", "onaudioend", "onnomatch"):
            self.assertIn("reco." + event, self.source)
        self.assertIn("DASHLE_VOCAL_DIAG_KEY", self.source)
        self.assertIn("build_commit_short", self.source)
        self.assertIn("build_date", self.source)

    def test_retry_backoff_and_three_immediate_failures_exist(self):
        self.assertIn("const DELAI_RELANCE_RECO_INITIAL = 300;", self.source)
        self.assertIn("const DELAI_RELANCE_RECO_MAX = 2000;", self.source)
        self.assertIn("const MAX_FINS_SANS_TRANSCRIPTION_VOCAL = 3;", self.source)
        self.assertIn("Je n'arrive pas à t'entendre, réessaie", self.source)
        self.assertIn("fin sans transcription", self.source)
        self.assertIn("error: e && e.error", self.source)

    def test_service_worker_cache_is_versioned(self):
        sw = SW.read_text(encoding="utf-8")
        self.assertIn("dashle-static-v4", sw)

if __name__ == "__main__":
    unittest.main()
