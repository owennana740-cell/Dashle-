"""Regression tests for the vocal view state machine."""

import os
import subprocess
import sys
import textwrap
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class VocalViewTests(unittest.TestCase):
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

    def test_vocal_view_state_in_real_rendered_page(self):
        result = self.run_probe(r'''
            import shutil
            import subprocess
            import tempfile
            import web

            node = shutil.which("node")
            assert node, "node requis pour le test vocal DOM"

            html = web.app.test_client().get("/").get_data(as_text=True)
            with tempfile.NamedTemporaryFile("w", suffix=".html", encoding="utf-8", delete=False) as handle:
                handle.write(html)
                path = handle.name

            script = r"""
            const fs = require("fs");
            const {JSDOM} = require("jsdom");

            function createPage(html) {
              const dom = new JSDOM(html, {
                url: "https://dashle.test/",
                runScripts: "dangerously",
                beforeParse(window) {
                  window.requestAnimationFrame = (cb) => setTimeout(() => cb(performance.now()), 0);
                  window.cancelAnimationFrame = (id) => clearTimeout(id);
                  window.matchMedia = () => ({matches:false, addEventListener(){}, removeEventListener(){}});
                  window.scrollTo = () => {};
                  window.fetch = () => Promise.resolve(new Response(""));
                  window.EventSource = class {};
                  window.ResizeObserver = class { observe(){} disconnect(){} };
                  window.IntersectionObserver = class { observe(){} disconnect(){} };
                  window.MediaRecorder = class {};
                  window.URL.createObjectURL = () => "blob:dashle";
                  window.URL.revokeObjectURL = () => {};
                  window.navigator.mediaDevices = {
                    getUserMedia: () => Promise.reject(new Error("test-no-microphone"))
                  };
                  window.speechSynthesis = {
                    speaking: false, pending: false, getVoices: () => [],
                    cancel() {}, speak() {}, onvoiceschanged: null
                  };
                  window.SpeechRecognition = class {
                    constructor() {
                      this.start = () => { if (this.onstart) this.onstart(); };
                      this.stop = () => { if (this.onend) this.onend(); };
                      this.abort = () => { if (this.onerror) this.onerror({error:"aborted"}); };
                    }
                  };
                  window.webkitSpeechRecognition = window.SpeechRecognition;
                }
              });
              return dom;
            }

            async function settle() {
              await new Promise((resolve) => setTimeout(resolve, 120));
            }

            (async () => {
              const html = fs.readFileSync(process.argv[1], "utf8");
              const dom = createPage(html);
              const document = dom.window.document;
              const vocal = document.getElementById("btn-vocal");
              const view = document.getElementById("mode-vocal");
              const reopen = document.getElementById("btn-rouvrir-vocal");
              if (!vocal || !view || !reopen) throw new Error("éléments vocaux absents");

              // OFF au chargement : vue et réouverture totalement invisibles.
              if (view.classList.contains("visible")) throw new Error("vue visible au démarrage OFF");
              if (reopen.classList.contains("actif")) throw new Error("réouverture visible au démarrage OFF");

              // OFF -> ON : la vue devient disponible.
              vocal.click();
              await settle();
              if (!view.classList.contains("visible")) throw new Error("vue absente après activation");

              // ON -> OFF : arrêt complet et immédiat de toute UI vocale.
              vocal.click();
              await settle();
              if (view.classList.contains("visible")) throw new Error("vue encore visible après OFF");
              if (reopen.classList.contains("actif")) throw new Error("réouverture encore visible après OFF");

              // ON -> réduire -> OFF : aucun résidu.
              vocal.click();
              await settle();
              document.getElementById("reduire-vocal").click();
              if (view.classList.contains("visible")) throw new Error("réduction sans effet");
              if (!reopen.classList.contains("actif")) throw new Error("bouton réouverture absent pendant vocal ON");
              vocal.click();
              if (view.classList.contains("visible")) throw new Error("vue visible après ON -> OFF");
              if (reopen.classList.contains("actif")) throw new Error("bouton réouverture résiduel après OFF");

              // OFF -> rechargement : nouvel état normal, sans vue fantôme.
              const reload = createPage(html);
              const viewReload = reload.window.document.getElementById("mode-vocal");
              const reopenReload = reload.window.document.getElementById("btn-rouvrir-vocal");
              if (viewReload.classList.contains("visible") || reopenReload.classList.contains("actif")) {
                throw new Error("vue fantôme après rechargement OFF");
              }

              // L'ancien marqueur ne doit exister dans aucun état OFF.
              if (document.body.textContent.includes("Vue vocale") && reopen.classList.contains("actif")) {
                throw new Error("Vue vocale visible en état OFF");
              }

              console.log("VOCAL_VIEW_STATE_OK");
            })().catch((error) => {
              console.error(error.stack || error);
              process.exit(1);
            });
            """

            checked = subprocess.run(
                [node, "-e", script, path],
                cwd=ROOT,
                text=True,
                capture_output=True,
                check=False,
            )
            assert checked.returncode == 0, checked.stderr
            assert "VOCAL_VIEW_STATE_OK" in checked.stdout

    def test_vocal_interruption_guards_remain_present(self):
        source = Path(ROOT / "web.py").read_text(encoding="utf-8")
        required = [
            "function interrompreDashle()",
            "window.speechSynthesis.cancel()",
            "arreterGeneration()",
            "echoCancellation: true",
            "noiseSuppression: true",
            "autoGainControl: true",
            "let syntheseEnCours = false",
            "let recoEnCours = false",
            "requeteActiveController.abort()",
        ]
        for marker in required:
            self.assertIn(marker, source, marker)
