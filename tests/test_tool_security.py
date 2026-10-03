"""Régressions de sécurité des providers : aucun secret fournisseur dans le code client."""
import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class ToolSecretSecurityTests(unittest.TestCase):
    def test_no_high_confidence_provider_secret_in_tracked_source(self):
        patterns = [
            re.compile(r"AIza[0-9A-Za-z_-]{20,}"),
            re.compile(r"sk_live_[0-9A-Za-z]{16,}"),
            re.compile(r"github_pat_[0-9A-Za-z_]{20,}"),
            re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
        ]
        excluded = {".git"}
        for path in ROOT.rglob("*"):
            if not path.is_file() or any(part in excluded for part in path.parts):
                continue
            if "build" in path.parts or path.suffix in {".png", ".jpg", ".jpeg", ".gif", ".webp", ".apk", ".aab", ".zip"}:
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            for pattern in patterns:
                self.assertIsNone(pattern.search(text), f"Secret-like literal found in {path}")

    def test_provider_code_reads_key_server_side_only(self):
        provider = (ROOT / "tool_providers.py").read_text(encoding="utf-8")
        self.assertIn("api_key_getter", provider)
        self.assertIn("x-goog-api-key", provider)
        self.assertNotIn("localStorage", provider)
        self.assertNotIn("sessionStorage", provider)

    def test_frontend_does_not_reference_provider_key_variables(self):
        source = (ROOT / "web.py").read_text(encoding="utf-8")
        self.assertNotIn("process.env.GEMINI_API_KEY", source)
        self.assertNotIn("import.meta.env.GEMINI_API_KEY", source)
        self.assertNotIn("GEMINI_API_KEY=", source)


if __name__ == "__main__":
    unittest.main()
