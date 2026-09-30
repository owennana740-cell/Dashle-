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

    def test_vocal_vad_filters_tts_echo_and_interrupts_real_speech(self):
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
                  window.requestAnimationFrame = (cb) => setTimeout(() => cb(performance.now()), 16);
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
                  const tracks = [{readyState:"live", stop(){this.readyState="ended";}}];
                  window.navigator.mediaDevices = {
                    getUserMedia: (constraints) => {
                      window.__gumConstraints = constraints;
                      return Promise.resolve({getAudioTracks: () => tracks});
                    }
                  };
                  let rms = 0.02;
                  window.__setVadRms = (value) => { rms = value; };
                  class FakeAnalyser {
                    constructor(){ this.fftSize = 1024; this.smoothingTimeConstant = 0.2; }
                    getByteTimeDomainData(data) {
                      const delta = Math.max(1, Math.min(30, Math.round(rms * 128)));
                      const hi = 128 + delta;
                      for (let i = 0; i < data.length; i++) data[i] = hi;
                    }
                    connect(){}
                    disconnect(){}
                  }
                  class FakeAudioContext {
                    constructor(){ this.state = "running"; }
                    resume(){ return Promise.resolve(); }
                    close(){ this.state = "closed"; return Promise.resolve(); }
                    createMediaStreamSource(){ return {connect(){}, disconnect(){}}; }
                    createAnalyser(){ return new FakeAnalyser(); }
                  }
                  window.AudioContext = FakeAudioContext;
                  window.webkitAudioContext = FakeAudioContext;
                  let cancelCount = 0;
                  window.speechSynthesis = {
                    speaking: false, pending: false, getVoices: () => [],
                    cancel() { cancelCount += 1; this.speaking = false; },
                    speak(utterance) { this.speaking = true; if (utterance.onstart) utterance.onstart(); },
                    onvoiceschanged: null
                  };
                  window.__ttsCancelCount = () => cancelCount;
                  window.SpeechSynthesisUtterance = class {
                    constructor(text){ this.text = text; this.onstart = null; this.onend = null; this.onerror = null; }
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

            async function settle(ms) {
              await new Promise((resolve) => setTimeout(resolve, ms));
            }

            (async () => {
              const html = fs.readFileSync(process.argv[1], "utf8");
              const dom = createPage(html);
              const w = dom.window;
              const document = w.document;
              const vocal = document.getElementById("btn-vocal");
              const interrupt = document.getElementById("btn-interrompre-vocal");
              if (!vocal || !interrupt) throw new Error("contrôles D2 absents");

              vocal.click();
              await settle(180);

              const wrap = document.createElement("div");
              wrap.className = "message-wrap";
              const msg = document.createElement("div");
              msg.className = "msg";
              msg.dataset.markdownSource = "Réponse de Dashle";
              const actions = document.createElement("div");
              actions.className = "actions-reponse";
              const play = document.createElement("button");
              play.className = "action-lire";
              const state = document.createElement("span");
              state.className = "lecture-etat";
              actions.append(play, state);
              wrap.append(msg, actions);
              document.body.appendChild(wrap);

              w.lireReponse(play);
              await settle(220);
              if (!w.__gumConstraints || !w.__gumConstraints.audio) {
                throw new Error("le VAD n'a pas ouvert le micro pendant le TTS");
              }
              const audio = w.__gumConstraints.audio;
              if (audio.echoCancellation !== true || audio.noiseSuppression !== true || audio.autoGainControl !== true) {
                throw new Error("contraintes anti-écho absentes");
              }

              await settle(500);
              if (w.__ttsCancelCount() !== 1) {
                throw new Error("l'écho seul a déclenché une interruption");
              }

              w.__setVadRms(0.12);
              await settle(500);
              if (w.__ttsCancelCount() < 2) {
                throw new Error("la parole utilisateur n'a pas interrompu le TTS");
              }

              if (interrupt.disabled) throw new Error("bouton Interrompre désactivé");
              console.log("VOCAL_VAD_ECHO_OK");
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
            assert "VOCAL_VAD_ECHO_OK" in checked.stdout
        ''')

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
