"""Regression tests for international phone normalization on registration."""

import os
import subprocess
import sys
import textwrap
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class RegistrationPhoneTests(unittest.TestCase):
    def run_probe(self, script):
        env = os.environ.copy()
        env["DATABASE_URL"] = "sqlite://"
        env.pop("RENDER", None)
        return subprocess.run(
            [sys.executable, "-B", "-c", textwrap.dedent(script)],
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )

    def test_burkina_number_and_leading_zero(self):
        result = self.run_probe("""
            import web
            assert web._normaliser_telephone("BF", "70125647") == ("+22670125647", "70125647")
            assert web._normaliser_telephone("BF", "070125647") == ("+22670125647", "070125647")
        """)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_other_country_number(self):
        result = self.run_probe("""
            import web
            assert web._normaliser_telephone("FR", "0612345678") == ("+33612345678", "0612345678")
        """)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_rendered_registration_script_is_valid_javascript(self):
        result = self.run_probe(r"""
            import re
            import shutil
            import subprocess
            import web
            client = web.app.test_client()
            page = client.get("/inscription")
            assert page.status_code == 200
            body = page.get_data(as_text=True)
            scripts = re.findall(r"<script>(.*?)</script>", body, flags=re.S)
            assert scripts, "aucun script d'inscription rendu"
            script = next((s for s in scripts if "const indicatifs" in s), None)
            assert script is not None, "script des indicatifs absent"
            node = shutil.which("node")
            assert node, "node requis pour vérifier le JavaScript rendu"
            checked = subprocess.run(
                [node, "--check"],
                input=script,
                text=True,
                capture_output=True,
                check=False,
            )
            assert checked.returncode == 0, checked.stderr
            assert re.search(r'const\s+indicatifs\s*=\s*\{', script)
            assert '"BF":"+226"' in script
        """)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_registration_error_preserves_non_password_fields(self):
        result = self.run_probe("""
            import re
            import web
            client = web.app.test_client()
            page = client.get("/inscription")
            assert page.status_code == 200
            csrf = re.search(rb'name="csrf_token" value="([^"]+)"', page.data).group(1).decode()
            failed = client.post("/inscription", data={
                "csrf_token": csrf,
                "email": "person@example.invalid",
                "nom": "Test Person",
                "pays": "BF",
                "indicatif": "+226",
                "telephone": "7012564",
                "password": "not-reused-in-response",
            })
            assert failed.status_code == 200
            body = failed.get_data(as_text=True)
            assert 'value="person@example.invalid"' in body
            assert 'value="Test Person"' in body
            assert 'value="7012564"' in body
            assert 'name="password"' in body
            assert "not-reused-in-response" not in body
            assert re.search(r'value="BF"\\s+selected', body)
            assert 'value="+226"' in body
        """)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
