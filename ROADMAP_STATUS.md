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
