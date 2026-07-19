# Les commandes de Ghost Memory

Toutes acceptent `--db` (défaut `~/.ghost/ghost.db`). Les commandes qui prennent
un identifiant acceptent **au choix** un id de skill, un id de candidat, ou un
slug — Ghost devine et te le dit.

## Démarrer

### `ghost init`
Onboarding guidé. Vérifie le PATH, enregistre ta clé API (opt-in, chmod 600,
validée par un ping sans coût), détecte Claude Code + ton historique, puis lance
un premier scan et affiche tes candidats. Ne plante jamais sur une machine
vierge : chaque absence donne une consigne.

```
$ ghost init
✓ `ghost` est sur le PATH.
✓ clé validée et enregistrée (chmod 600).
✓ historique Claude Code : 29 session(s).
Premier scan… ✓ 29 fichier(s) ingéré(s), N candidat(s) détecté(s).
```

### `ghost doctor`
Diagnostic d'installation. Chaque `✗` dit quoi faire (PATH, CLI claude,
historique, base). Sort en erreur s'il manque quelque chose.

## La boucle

### `ghost ingest`
Ingère `~/.claude/projects/**/*.jsonl` dans `~/.ghost/ghost.db`. Idempotent
(mtime/sha), streaming. `--rebuild` vide les tables brutes et ré-ingère (refuse
si des fichiers ont disparu du disque, pour ne pas perdre de sessions).

### `ghost scan`
Détecte les **cicatrices** : `FAILURE_LOOP` (boucles d'échec), `HUMAN_OVERRIDE`
(corrections humaines), `REPEATED_SEQUENCE` (séquences répétées). Écrit des
candidats. Le `status` de triage survit aux re-scans.

### `ghost show <id>`
Dump l'évidence brute d'un candidat : signature, score, occurrences, events
bruts avec leur `src_file:src_line`. Accepte id candidat / id skill / slug.

### `ghost distill <id>`
Candidat → `SKILL.md` (`~/.ghost/skills/<slug>/`). Un appel LLM, section
**Pièges** où chaque piège cite l'échec qui le prouve, plus une auto-critique.
Verdict `SKILL` ou `SKIP`. Refuse de créer un doublon si le candidat a déjà un
skill actif — `--force` régénère (et désactive l'ancien).

### `ghost keep <id>` / `ghost reject <id>`
Valide (déployable) ou rejette (survit aux re-scans, jamais re-proposé) un
candidat.

### `ghost skills`
Liste les skills distillés : verdict, coût, statut, déploiement. Signale les
**doublons** (⚠DOUBLON) et les skills désactivés.

### `ghost deploy`
Pousse les skills des candidats `kept` dans `~/.claude/skills/` (global) ou le
`.claude/skills/` du projet concerné. Ne pousse qu'**un** skill par candidat (le
plus récent). `--dry-run` montre sans écrire ; `--force-global <slug>` force un
skill mono-projet en global.

### `ghost run`
La boucle complète : `ingest` → `scan` → distille les nouveaux candidats sous un
plafond de dépense (`--budget`, défaut 2 $ ; `--top`, défaut 10). Pas de
déploiement automatique : le triage reste humain.

## Mesure

### `ghost bench <skill>`
Mesure le lift sur des **micro-benchmarks synthétiques** : des mini-tâches
auto-contenues, sans réseau ni MCP ni secret, avec un grader déterministe. La
baseline sans skill réussit vraiment (>50 %) → un lift veut dire quelque chose.
Étiqueté « synthétique » (ce n'est pas un replay de ton historique). Voir
[Comment ça marche](how-it-works.md).

### `ghost validate <skill>`
Rejoue des tâches de ton historique avec/sans le skill (quand des cas courts et
auto-contenus existent). Protocole alterné, budget dur, reprise possible. Runs
coupés par le budget/timeout = catégorie distincte, jamais comptés comme échec.

### `ghost watch`
Signal précoce sans inférence : sessions exposées à un skill vs baseline.

## Transparence & contrôle

### `ghost why`
Quels skills Ghost étaient injectables au dernier prompt, et pourquoi (déclenchés
par leur description).

### `ghost disable <id>` / `ghost enable <id>`
Retire un skill déployé (plus jamais injecté) / le réactive.

### `ghost uninstall`
Retire tous les `SKILL.md` déployés par Ghost Memory. Aucun hook n'a été installé.

### `ghost telemetry {status,on,off,preview,send}`
État / opt-in / opt-out / **aperçu du payload exact** / envoi. Voir
[Vie privée](privacy.md).

## Inspection

### `ghost stats`
Sessions, events, top 10 outils avec taux d'erreur, plage de dates.
