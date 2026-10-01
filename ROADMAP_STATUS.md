# DASHLE — ROADMAP STATUS

Dernière mise à jour : 2026-10-01

## État de référence

- Branche principale : `main`
- SHA actuel de `main` : `2bfd7382debac472d18b88cc4557f46d867c7bd7`
- PR #9 D2 vocal : fusionnée
- PR #10 génération d’image Gemini : fusionnée
- PR #11 stabilisation vocale utile : fusionnée
- PR #12 retrait du bouton « Interrompre » : fusionnée

## Phase A

### A4 — Sécurité, première passe
- État : première passe terminée ; audit quotas/rate limiting chat texte encore ouvert
- PR : #16
- Merge commit : `abe0509f515819e98c110172589eefb1d1d71cab`
- CI PR : workflow `Python validation` #214 — succès
- Correction : les erreurs Gemini ne journalisent plus les corps bruts ni `repr(exc)` ; seuls code HTTP/type d’exception sont journalisés
- Test : `tests.test_provider_logging_security` — succès
- Risque résiduel vérifié : aucun quota DASHLE serveur dédié au chat texte n’a été trouvé. Des quotas serveur existent pour images, transcription et certaines fonctions. Ne pas inventer de plafonds texte sans règle produit ; à traiter avec une politique de quota explicite.

### A5 — Intégration / E2E, état actuel
- État : couverture d’intégration backend déjà présente et verte dans la CI ; E2E navigateur réel/Android non exécuté dans cet environnement
- CI #214 conserve verts : génération fichiers/images, conversations, vocal jsdom, sécurité/connecteurs, inscription/téléphones, D2, observabilité, résilience SSE, performance
- Limitation : aucun test matériel Chrome Android, microphone, haut-parleur ou réseau mobile réel n’est déclaré réussi

### A3 — Performance mobile, première passe
- État : première passe terminée ; mesure mobile réelle encore requise
- PR : #15
- Branche : `performance/a3-mobile-baseline`
- Merge commit : `7a0d31e9d8fe928e50626722b7accf3b51ad1ce9`
- CI PR : workflow `Python validation` #211 — succès
- Baseline CI avant optimisation : HTML `/` = 150 215 octets ; JavaScript inline = 105 108 octets ; CSS inline = 34 550 octets ; `marked.min.js` = 46 706 octets ; `purify.min.js` = 22 305 octets
- Optimisation validée : `marked.min.js` et `purify.min.js` chargés avec `defer`, rendu Markdown initial déplacé à `DOMContentLoaded`
- Non-modifié : comportement Markdown streaming, vocal, SSE, fournisseurs IA, données, secrets
- Limitation : la CI ne mesure pas FCP/LCP/INP réels ni un téléphone Android/4G physique

### A2 — Fiabilisation SSE
- État : terminée
- PR : #14
- Branche : `sse/a2-provider-cleanup`
- Merge commit : `1a8119e829f197ee7f16f9812571b4526a0f8791`
- CI PR : workflow `Python validation` #205 — succès
- Correction : fermeture explicite de la réponse HTTP Gemini lorsque le générateur SSE est annulé
- Test : `tests.test_sse_resilience` — succès
- Non-modifié : timeout fournisseur de 90 s, logique vocale, données, secrets, fournisseur IA

### A1 — Observabilité
- État : terminée
- PR : #13
- Branche : `observability/a1-request-sse-metrics`
- Merge commit : `bc5503d55350d4821bf874f7e1f1394f97a95c17`
- CI PR : workflow `Python validation` #202 — succès
- Validations CI observées :
  - compilation Python
  - rendu Markdown JavaScript
  - génération fichiers/images
  - conversations paresseuses
  - vocal jsdom
  - sécurité/connecteurs
  - inscription/téléphones
  - vocal D2
  - tests observabilité
  - `git diff --check`

### A1 — Ce qui est maintenant mesuré
- `request_id` corrélé et renvoyé dans `X-Request-ID`
- endpoint
- identifiant utilisateur anonymisé par hachage
- fournisseur et modèle
- durée de requête
- statut HTTP
- durée SSE
- TTFB SSE
- durée de génération d’image
- résultat succès/erreur/annulation pour le flux SSE
- type d’exception pour les erreurs SSE

### A1 — Protection des logs
- aucun contenu de prompt ajouté aux logs d’observabilité
- aucun token, secret, mot de passe ou clé API ajouté
- `X-Request-ID` entrant est normalisé avant réutilisation
- les erreurs SSE journalisent le type d’exception, pas le traceback ni son contenu

## Phase A — prochaines étapes

1. A2 — fiabilisation SSE
2. A3 — performance mobile réelle
3. A4 — audit sécurité
4. A5 — tests intégration/E2E

## Phase B

### B2 — Interface mobile, première passe
- État : terminée
- PR : #18
- Merge commit : `6e18ca967ea96550a0851e00633415b4cce99b11`
- CI PR : workflow `Python validation` #221 — succès
- `100dvh` avec fallback `100vh`
- safe-area pour la barre de saisie
- `touch-action: manipulation` pour les contrôles interactifs
- Test : `tests.test_mobile_ui` — succès
- Validation matérielle Android réelle : non effectuée

### B1 — PWA, première passe
- État : première passe terminée
- PR : #17
- Merge commit : `f68da7723f7b0bf11512e6f7fe8726306584c897`
- CI PR : workflow `Python validation` #218 — succès
- Service Worker : cache v5 avec `marked.min.js` et `purify.min.js`
- Aucun cache ajouté pour `/repondre_flux` ou `/repondre`
- Test : `tests.test_pwa_assets` — succès
- Reste à valider sur Android réel : installation, retour, reprise, mise à jour du SW et comportement hors-ligne réel

- PWA : première passe terminée
- interface mobile : non commencée
- formalisation supplémentaire du vocal : non commencée
- génération d’images : abstraction progressive à évaluer, fournisseur Gemini conservé
- résilience réseau : non commencée

### B3 — Vocal, audit sans modification
- État : audit effectué ; aucune modification nécessaire dans cette passe
- Tests existants couvrent `continuous=false`, déduplication, TTS single-flight, VAD/anti-écho, interruption et reprise
- Aucune validation matérielle Android/micro/haut-parleur n’est déclarée

### B4 — Génération d’images, audit sans modification
- État : couche `generer_image()` déjà centralisée côté serveur ; Gemini conservé
- Pas de retry automatique ajouté : un retry aveugle d’une génération d’image pourrait augmenter le coût ou créer un doublon fournisseur
- Timeout et validation du résultat image existent déjà

## Phase C

### C1 — Résilience réseau / SSE
- État : implémentation terminée et fusionnée ; validation production externe limitée par la disponibilité du service Render
- PR : #19
- Merge commit : `2bfd7382debac472d18b88cc4557f46d867c7bd7`
- CI PR : workflow `Python validation` #230 — succès
- Comportement ajouté : lors d'une perte réseau pendant une génération SSE active, le contrôleur est annulé, la réponse partielle est supprimée, le texte du tour est conservé dans la zone de saisie et aucun POST n'est rejoué automatiquement
- Retour réseau : le champ est restauré et l'utilisateur peut renvoyer explicitement le message
- Tests : contrat JSDOM de perte réseau + non-régression SSE/vocal — succès
- Sécurité : aucun secret, donnée utilisateur, fournisseur IA ou variable d'environnement modifié
- Limitation production vérifiée : les smoke tests #23 et sa relance échouent avant l'analyse HTML ; les `curl` vers `https://dashle.onrender.com` expirent et Render ne rapporte aucune requête HTTP récente, alors que le deploy `2bfd7382...` est marqué `live`. Aucune conclusion de fonctionnement HTTP public ne doit être tirée de ces smoke tests.

- mémoire améliorée : C2 terminée et fusionnée via PR #21 ; sélection bornée de souvenirs pertinents (chevauchement lexical), injectée dans les chemins synchrone et SSE ; mémoire désactivée respectée ; 6 éléments / 3000 caractères maximum ; CI #235 verte ; aucune migration de base ; la validation production HTTP reste limitée par le problème Render documenté en C1
- conversations longues : C3 validée via PR #23 ; preuve automatisée sur 100 messages et message unique de 100 000 caractères ; historique borné à 24 messages / 24 000 caractères et dernier message courant conservé ; CI #239 verte ; aucun changement de production nécessaire ; smoke production reste limité par Render comme documenté en C1
- multimodal : C4 audit réussi sans modification de code ; analyse image/vidéo existante dans `brain.py`, génération d'image centralisée dans `artifact_tools.py`, cycle SSE `action_started` / progression / `action_completed`, MIME, bibliothèque, téléchargement et erreurs/réseau couverts par les tests existants ; abstraction fournisseur/retry/safety complète non introduite car aucun gain réel démontré à ce stade
- recherche/Web : non commencée
- outils/agents : non commencés

## Phase D

- Android : non commencé
- notifications : non commencées
- partage Android : non commencé
- fichiers/caméra : non commencés
- fonctionnalités natives : non commencées

## Limitations réelles

- A1 ne mesure pas encore les performances réelles Chrome Android, 4G ou appareil physique.
- La synthèse vocale actuelle est exécutée côté navigateur ; sa durée serveur n’est donc pas instrumentée par A1.
- Aucun test matériel microphone/haut-parleur n’est déclaré réussi sans exécution réelle.
- La CI GitHub PR #202 est vérifiée verte. Le workflow de validation existe aussi sur `push/main`, mais l’outil de récupération utilisé pour cette validation expose ici les runs déclenchés par pull request.

## Règle de progression

Chaque étape future doit repartir du `main` vérifié, rester ciblée, préserver les fonctions existantes, ajouter ses tests, attendre une CI réellement verte avant fusion et mettre à jour ce fichier avec des faits vérifiés.
