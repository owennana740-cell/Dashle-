"""Regression tests for the vocal view state machine."""

import os
import subprocess
import shutil
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
        ''')

    def test_transcription_results_deduplicate_and_tts_is_single_flight(self):
        result = self.run_probe(r'''
            import shutil
            import subprocess
            import tempfile
            import web

            node = shutil.which('node')
            assert node, 'node requis'
            html = web.app.test_client().get('/').get_data(as_text=True)
            with tempfile.NamedTemporaryFile('w', suffix='.html', encoding='utf-8', delete=False) as handle:
                handle.write(html)
                path = handle.name
            script = r"""
            const fs = require('fs');
            const {JSDOM} = require('jsdom');
            const instances = [];
            const spoken = [];
            const dom = new JSDOM(fs.readFileSync(process.argv[1], 'utf8'), {
              url: 'https://dashle.test/', runScripts: 'dangerously',
              beforeParse(window) {
                window.requestAnimationFrame = (cb) => setTimeout(() => cb(performance.now()), 0);
                window.cancelAnimationFrame = (id) => clearTimeout(id);
                window.matchMedia = () => ({matches:false, addEventListener(){}, removeEventListener(){}});
                window.scrollTo = () => {};
                window.fetch = () => Promise.resolve(new Response(''));
                window.EventSource = class {};
                window.ResizeObserver = class { observe(){} disconnect(){} };
                window.IntersectionObserver = class { observe(){} disconnect(){} };
                window.MediaRecorder = class {};
                window.navigator.mediaDevices = {getUserMedia: () => Promise.reject(new Error('no-microphone'))};
                window.speechSynthesis = {
                  speaking:false, paused:false, pending:false, getVoices:()=>[],
                  cancel(){this.speaking=false;}, pause(){}, resume(){},
                  speak(u){this.speaking=true; spoken.push(u.text);}
                };
                window.SpeechSynthesisUtterance = function(text){this.text=text;};
                window.SpeechRecognition = class {
                  constructor(){instances.push(this);}
                  start(){this.onstart&&this.onstart();}
                  stop(){this.onend&&this.onend();}
                  abort(){this.onend&&this.onend();}
                };
                window.webkitSpeechRecognition = window.SpeechRecognition;
              }
            });
            const document = dom.window.document;
            document.getElementById('btn-vocal').click();
            const reco = instances[0];
            if (!reco) throw new Error('SpeechRecognition absent');
            if (reco.continuous !== false) throw new Error('continuous=false attendu en sessions courtes');
            if (reco.interimResults !== false) throw new Error('interimResults=false attendu en sessions courtes');

            const make = (text, final) => {
              const r=[{transcript:text, confidence:1}];
              r.isFinal=final;
              return r;
            };

            // Séquence observée : un même final revient avec un nouvel index.
            reco.onresult({resultIndex:0, results:[make('je veux', true)]});
            reco.onresult({resultIndex:1, results:[
              make('je veux', true),
              make('je veux', true)
            ]});
            reco.onresult({resultIndex:2, results:[
              make('je veux', true),
              make('je veux', true),
              make('que tu m aides', true)
            ]});
            const texteFinal = dom.window.eval('transcriptionFinaleVocale');
            if (texteFinal !== 'je veux que tu m aides') {
              throw new Error('déduplication vocale incorrecte: ' + texteFinal);
            }

            const wraps = document.createElement('div');
            wraps.innerHTML =
              '<div class="message-wrap" data-message-id="msg-1"><div class="msg">Première réponse</div><div class="actions-reponse"><button class="lecture-reponse"></button><span class="lecture-etat"></span></div></div>' +
              '<div class="message-wrap" data-message-id="msg-2"><div class="msg">Deuxième réponse</div><div class="actions-reponse"><button class="lecture-reponse"></button><span class="lecture-etat"></span></div></div>';
            document.body.appendChild(wraps);
            const boutons = document.querySelectorAll('.lecture-reponse');

            dom.window.lireReponse(boutons[0], 'Première réponse', true);
            dom.window.lireReponse(boutons[0], 'Première réponse', true);
            if (spoken.length !== 1) throw new Error('TTS relancé pour le même message');

            dom.window.lireReponse(boutons[1], 'Deuxième réponse', true);
            if (spoken.length !== 2) throw new Error('TTS absent pour un nouveau message');

            // Le contrôle utilisateur de relecture reste disponible.
            dom.window.lireReponse(boutons[0], 'Première réponse', false);
            if (spoken.length !== 3) throw new Error('relecture manuelle bloquée');

            if (instances.length !== 1) throw new Error('plusieurs instances SpeechRecognition créées');

            // Interruption pendant TTS : couper la voix puis relancer une seule écoute.
            dom.window.speechSynthesis.speaking = true;
            dom.window.__ttsCancelCount = () => 0;
            const avantStart = reco.start;
            let startCount = 0;
            reco.start = () => { startCount += 1; avantStart(); };
            dom.window.interrompreDashle();
            await new Promise((resolve) => setTimeout(resolve, 180));
            if (dom.window.speechSynthesis.speaking) throw new Error('TTS non interrompu');
            if (startCount !== 1) throw new Error('reprise SpeechRecognition multiple ou absente');

            // Trois fins sans transcription arrêtent le mode vocal avec le message attendu.
            dom.window.__dashleTestDisableAudioFallback = true;
            reco.onend();
            reco.onend();
            reco.onend();
            if (dom.window._dashleVocal && dom.window._dashleVocal.estActif()) throw new Error('vocal encore actif après 3 fins sans transcription');
            if (!document.body.textContent.includes("Je n'arrive pas à t'entendre, réessaie")) {
              throw new Error('message des 3 fins sans transcription absent');
            }

            console.log('VOCAL_RESULT_SEQUENCE_OK');
            """;
            checked = subprocess.run([node, '-e', script, path], cwd=ROOT, text=True, capture_output=True, check=False)
            assert checked.returncode == 0, checked.stderr
            assert 'VOCAL_RESULT_SEQUENCE_OK' in checked.stdout
        ''')

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
              const orb = document.getElementById("orbe-dashle");
              if (!vocal || interrupt || !orb) throw new Error("UI d'interruption incorrecte");
              if (orb.getAttribute("aria-label") !== "Appuie pour interrompre Dashle") throw new Error("aria-label orbe absent");

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

              const avantOrbe = w.__ttsCancelCount();
              w.speechSynthesis.speaking = true;
              orb.click();
              await settle(180);
              if (w.__ttsCancelCount() <= avantOrbe) throw new Error("appui sur l'orbe n'interrompt pas Dashle");
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

    def test_new_text_submission_aborts_previous_sse_and_tts(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("Node.js indisponible")
        import tempfile
        import web
        html = web.app.test_client().get("/").get_data(as_text=True)
        with tempfile.NamedTemporaryFile("w", suffix=".html", encoding="utf-8", delete=False) as handle:
            handle.write(html)
            path = handle.name
        script = r"""
          const fs = require("fs");
          const {JSDOM} = require("jsdom");
          const html = fs.readFileSync(process.argv[1], "utf8");
          const dom = new JSDOM(html, {
            runScripts:"dangerously",
            url:"http://localhost/",
            beforeParse(window) {
              window.matchMedia = () => ({matches:false, addEventListener(){}, removeEventListener(){}});
              window.requestAnimationFrame = (cb) => setTimeout(() => cb(Date.now()), 0);
              window.cancelAnimationFrame = (id) => clearTimeout(id);
              window.scrollTo = () => {};
            }
          });
          const w = dom.window;
          let fetches = 0;
          w.fetch = (url, options) => {
            fetches += 1;
            let reader;
            const body = {
              getReader() {
                reader = {
                  read() {
                    return new Promise((resolve, reject) => {
                      reader.resolve = resolve;
                      reader.reject = reject;
                      if (fetches === 2) setTimeout(() => reader.resolve({done:true, value:undefined}), 40);
                    });
                  }
                };
                if (options && options.signal) {
                  options.signal.addEventListener("abort", () => {
                    if (reader.reject) reader.reject(Object.assign(new Error("aborted"), {name:"AbortError"}));
                  });
                }
                return reader;
              }
            };
            return Promise.resolve({ok:true, body});
          };
          const form = w.document.getElementById("form-message");
          const champ = w.document.getElementById("message");
          if (!form || !champ) throw new Error("formulaire absent");
          const botInitial = w.document.querySelectorAll(".message-wrap.bot .msg").length;
          champ.value = "premier";
          form.dispatchEvent(new w.Event("submit", {bubbles:true,cancelable:true}));
          setTimeout(() => {
            if (!fetches || !w.document.querySelector(".message-wrap.bot .msg")) throw new Error("première réponse non démarrée");
            w.arreterGeneration();
            if (w.document.querySelectorAll(".message-wrap.bot .msg").length !== botInitial) {
              throw new Error("arreterGeneration laisse un message fantôme");
            }
            champ.value = "second";
            form.dispatchEvent(new w.Event("submit", {bubbles:true,cancelable:true}));
          }, 100);
          setTimeout(() => {
            if (fetches !== 2) throw new Error("le nouveau message n'a pas pris la main");
            const botFinal = w.document.querySelectorAll(".message-wrap.bot .msg").length;
            if (botFinal !== botInitial + 1) throw new Error("message fantôme ou double réponse: initial=" + botInitial + " final=" + botFinal);
            if (w.document.querySelectorAll(".message-wrap.user .msg").length < 2) throw new Error("nouveau message absent");
            console.log("TEXT_RESUBMIT_ABORT_OK");
          }, 300);
          setTimeout(() => process.exit(0), 360);
        """
        checked = subprocess.run([node, "-e", script, str(path)], cwd=ROOT, text=True, capture_output=True, check=False)
        assert checked.returncode == 0, checked.stderr
        assert "TEXT_RESUBMIT_ABORT_OK" in checked.stdout

    def test_vocal_diagnostic_and_build_metadata_are_local_and_hidden_by_default(self):
        source = Path(ROOT / "web.py").read_text(encoding="utf-8")
        required = [
            'id="diagnostic-vocal" hidden',
            "dashle_vocal_diagnostic_v1",
            "dashle_vocal_diag_open",
            "navigator.clipboard.writeText",
            "diagnostic-vocal-effacer",
            "appuis >= 5",
            "BUILD_COMMIT = os.environ.get",
            "BUILD_COMMIT_SHORT = BUILD_COMMIT[:12]",
            "BUILD_DATE =",
            "build_commit_short=BUILD_COMMIT_SHORT",
            "build_commit=BUILD_COMMIT",
            "build_date=BUILD_DATE",
        ]
        for item in required:
            self.assertIn(item, source, item)
        self.assertNotIn("ALLOW_EPHEMERAL_DB", source)
        self.assertNotIn("EPHEMERAL_DB_MODE", source)

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
            'id="orbe-dashle"',
            'aria-label="Appuie pour interrompre Dashle"',
            "if (reponseEnCours || requeteActiveController)",
            "window.speechSynthesis.cancel()",
        ]
        for marker in required:
            self.assertIn(marker, source, marker)


    def test_network_loss_aborts_sse_and_preserves_prompt_without_retry(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("Node.js indisponible")
        import tempfile
        import web

        html = web.app.test_client().get("/").get_data(as_text=True)
        with tempfile.NamedTemporaryFile("w", suffix=".html", encoding="utf-8", delete=False) as handle:
            handle.write(html)
            path = handle.name

        script = r"""
          const fs = require("fs");
          const {JSDOM} = require("jsdom");
          const html = fs.readFileSync(process.argv[1], "utf8");
          const dom = new JSDOM(html, {
            runScripts: "dangerously",
            url: "https://dashle.test/",
            beforeParse(window) {
              window.matchMedia = () => ({matches:false, addEventListener(){}, removeEventListener(){}});
              window.requestAnimationFrame = (cb) => setTimeout(() => cb(Date.now()), 0);
              window.cancelAnimationFrame = (id) => clearTimeout(id);
              window.scrollTo = () => {};
            }
          });
          const w = dom.window;
          let fetches = 0;
          let aborted = false;
          w.fetch = (url, options) => {
            fetches += 1;
            let rejectRead;
            const body = {
              getReader() {
                return {
                  read() {
                    return new Promise((resolve, reject) => {
                      rejectRead = reject;
                    });
                  }
                };
              }
            };
            if (options && options.signal) {
              options.signal.addEventListener("abort", () => {
                aborted = true;
                if (rejectRead) rejectRead(Object.assign(new Error("aborted"), {name:"AbortError"}));
              });
            }
            return Promise.resolve({ok:true, body});
          };

          const form = w.document.getElementById("form-message");
          const champ = w.document.getElementById("message");
          if (!form || !champ) throw new Error("formulaire absent");
          const placeholderInitial = champ.placeholder;
          champ.value = "question à reprendre";
          form.dispatchEvent(new w.Event("submit", {bubbles:true,cancelable:true}));

          setTimeout(() => {
            if (fetches !== 1) throw new Error("SSE non démarré");
            w.dispatchEvent(new w.Event("offline"));
          }, 80);

          setTimeout(() => {
            if (!aborted) throw new Error("le SSE n'a pas été annulé à la perte réseau");
            if (fetches !== 1) throw new Error("un retry automatique dangereux a été lancé");
            if (champ.value !== "question à reprendre") throw new Error("le texte n'a pas été conservé");
            if (w.document.querySelectorAll(".message-wrap.bot .msg").length !== 0) {
              throw new Error("une réponse fantôme reste après la coupure réseau");
            }
            if (!champ.placeholder.includes("Connexion perdue")) {
              throw new Error("le champ n'indique pas la perte réseau");
            }
            w.dispatchEvent(new w.Event("online"));
          }, 180);

          setTimeout(() => {
            if (champ.placeholder !== placeholderInitial) throw new Error("le placeholder initial n'est pas restauré");
            console.log("NETWORK_LOSS_SSE_SAFE_OK");
          }, 260);

          setTimeout(() => process.exit(0), 320);
        """
        checked = subprocess.run(
            [node, "-e", script, str(path)],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        assert checked.returncode == 0, checked.stderr
        assert "NETWORK_LOSS_SSE_SAFE_OK" in checked.stdout
