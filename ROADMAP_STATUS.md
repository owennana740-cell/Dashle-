# DASHLE — ROADMAP STATUS

Dernière mise à jour : 2026-10-01

## État de référence

- Branche principale : `main`
- SHA actuel après A1 : `bc5503d55350d4821bf874f7e1f1394f97a95c17`
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

- PWA : non commencée
- interface mobile : non commencée
- formalisation supplémentaire du vocal : non commencée
- génération d’images : abstraction progressive à évaluer, fournisseur Gemini conservé
- résilience réseau : non commencée

## Phase C

- mémoire améliorée : non commencée
- conversations longues : non commencée
- multimodal : non commencé
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
