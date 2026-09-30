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

    def test_rendered_pages_have_valid_inline_javascript_and_registration_dom(self):
        result = self.run_probe(r'''
            import re
            import shutil
            import subprocess
            import tempfile
            from html.parser import HTMLParser
            import web

            class InlineScripts(HTMLParser):
                def __init__(self):
                    super().__init__()
                    self.items = []
                    self.current = False
                    self.attrs = {}
                    self.buf = []
                def handle_starttag(self, tag, attrs):
                    if tag == "script":
                        self.current = True
                        self.attrs = dict(attrs)
                        self.buf = []
                def handle_endtag(self, tag):
                    if tag == "script" and self.current:
                        self.items.append((self.attrs, "".join(self.buf)))
                        self.current = False
                def handle_data(self, data):
                    if self.current:
                        self.buf.append(data)

            client = web.app.test_client()
            pages = {path: client.get(path) for path in ("/", "/connexion", "/inscription")}
            assert all(r.status_code == 200 for r in pages.values())
            node = shutil.which("node")
            assert node, "node requis pour vérifier les JavaScript rendus"

            for path, response in pages.items():
                parser = InlineScripts()
                parser.feed(response.get_data(as_text=True))
                assert parser.items, f"aucun script trouvé sur {path}"
                for attrs, script in parser.items:
                    script_type = (attrs.get("type") or "").lower()
                    if script_type in {"application/json", "application/ld+json"}:
                        continue
                    checked = subprocess.run([node, "--check"], input=script, text=True, capture_output=True, check=False)
                    assert checked.returncode == 0, f"{path}: {checked.stderr}"

            inscription = pages["/inscription"].get_data(as_text=True)
            with tempfile.NamedTemporaryFile("w", suffix=".html", encoding="utf-8", delete=False) as handle:
                handle.write(inscription)
                html_path = handle.name

            dom_script = r"""
                const fs = require("fs");
                const {JSDOM} = require("jsdom");
                const dom = new JSDOM(fs.readFileSync(process.argv[1], "utf8"), {runScripts: "dangerously"});
                const document = dom.window.document;
                const data = document.getElementById("donnees-indicatifs");
                if (!data) throw new Error("donnees-indicatifs absent");
                const rows = JSON.parse(data.textContent);
                const bf = rows.find((row) => row[0] === "BF");
                if (!bf || bf[2] !== "226") throw new Error("Burkina Faso +226 absent");
                const country = document.getElementById("pays");
                const indicator = document.getElementById("indicatif");
                const hidden = document.getElementById("indicatif-envoye");
                if (!country || !indicator || !hidden) throw new Error("champs téléphone absents");
                country.value = "BF";
                country.dispatchEvent(new dom.window.Event("change"));
                if (indicator.options[0].textContent !== "+226") throw new Error("indicatif visible incorrect");
                if (hidden.value !== "+226") throw new Error("indicatif caché incorrect");
            """
            checked = subprocess.run([node, "-e", dom_script, html_path], text=True, capture_output=True, check=False)
            assert checked.returncode == 0, checked.stderr
        ''')        self.assertEqual(result.returncode, 0, result.stderr)

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
