"""Regression checks for the DASHLE PWA cache contract."""

import json
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class PwaAssetTests(unittest.TestCase):
    def test_manifest_and_service_worker_keep_static_cache_contract(self):
        manifest = json.loads((ROOT / "static/manifest.json").read_text(encoding="utf-8"))
        worker = (ROOT / "static/service-worker.js").read_text(encoding="utf-8")

        self.assertEqual(manifest["display"], "standalone")
        self.assertIn('"/static/vendor/marked.min.js"', worker)
        self.assertIn('"/static/vendor/purify.min.js"', worker)
        self.assertIn("dashle-static-v5", worker)
        self.assertNotIn("'/repondre_flux'", worker)
        self.assertNotIn("'/repondre'", worker)


if __name__ == "__main__":
    unittest.main()
