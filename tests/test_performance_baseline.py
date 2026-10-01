"""Server-rendered performance baseline for the DASHLE home page."""

import os
import re
import unittest
from pathlib import Path

os.environ["DATABASE_URL"] = "sqlite://"
os.environ["SESSION_COOKIE_SECURE"] = "0"

import web


ROOT = Path(__file__).resolve().parents[1]


class PerformanceBaselineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        web.app.config.update(TESTING=True, SESSION_COOKIE_SECURE=False)

    def test_home_page_emits_reproducible_performance_baseline(self):
        response = web.app.test_client().get("/")
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        inline_script_bytes = sum(len(match.group(1).encode("utf-8")) for match in re.finditer(r"<script(?:\\s[^>]*)?>(.*?)</script>", html, re.DOTALL))
        inline_style_bytes = sum(len(match.group(1).encode("utf-8")) for match in re.finditer(r"<style(?:\\s[^>]*)?>(.*?)</style>", html, re.DOTALL))
        vendor_sizes = {
            "marked": (ROOT / "static/vendor/marked.min.js").stat().st_size,
            "purify": (ROOT / "static/vendor/purify.min.js").stat().st_size,
        }
        script_tags = len(re.findall(r"<script(?:\\s|>)", html))

        print(f"DASHLE_PERF_HOME_HTML_BYTES={len(response.data)}")
        print(f"DASHLE_PERF_INLINE_SCRIPT_BYTES={inline_script_bytes}")
        print(f"DASHLE_PERF_INLINE_STYLE_BYTES={inline_style_bytes}")
        print(f"DASHLE_PERF_SCRIPT_TAGS={script_tags}")
        print(f"DASHLE_PERF_MARKED_BYTES={vendor_sizes['marked']}")
        print(f"DASHLE_PERF_PURIFY_BYTES={vendor_sizes['purify']}")

        self.assertIn('/static/vendor/marked.min.js', html)
        self.assertIn('/static/vendor/purify.min.js', html)
        self.assertEqual(html.count('/static/vendor/marked.min.js'), 1)
        self.assertEqual(html.count('/static/vendor/purify.min.js'), 1)
        self.assertRegex(html, r"<script defer src=\"[^\"]*vendor/marked\\.min\\.js\"></script>")
        self.assertRegex(html, r"<script defer src=\"[^\"]*vendor/purify\\.min\\.js\"></script>")
        self.assertIn("document.addEventListener('DOMContentLoaded', afficherMarkdownInitial)", html)


if __name__ == "__main__":
    unittest.main()
