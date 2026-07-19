# Lot C — Premier kilomètre

Objectif : un inconnu, machine vierge → `uv tool install` → `ghost init` → il voit
ses candidats, sans aide externe, sans lire de doc, et **aucune commande ne plante**.

## Ce qui a changé

1. **`ghost init`** — onboarding interactif : PATH (détection + `uv tool update-shell`),
   clé API (opt-in, ping de validation *sans coût token* via GET /v1/models,
   écrite chmod 600), détection Claude Code + historique, puis premier scan guidé
   qui affiche les candidats. Conçu pour ne jamais planter sur une machine vierge.
2. **Cohérence des identifiants** — `validate`/`bench`/`disable`/`enable` (skills)
   et `show`/`distill`/`keep`/`reject` (candidats) acceptent **au choix** un id de
   skill, un id de candidat, ou un slug. Résolution intelligente avec note
   explicative ; message actionnable si ambigu. Plus jamais « not a valid int ».
3. **Déduplication** — `distill` refuse de créer un doublon (skill déjà actif pour
   le candidat) sans `--force` ; avec `--force`, l'ancien est désactivé. `ghost
   skills` signale les doublons (⚠DOUBLON). `deploy` ne pousse déjà que le skill
   le plus récent par candidat.
4. **Machine vierge** — toute commande sur un HOME sans historique donne une
   consigne, jamais une trace.

## Transcript — conteneur neuf (python:3.12-slim, HOME vierge)

`pip install .` du working-tree, puis :

```
$ ghost init --yes            # pas de clé, pas d'historique
✓ `ghost` est sur le PATH.
• pas de clé API — ingest/scan marchent sans, mais distill/validate/bench en auront besoin.
• aucun historique Claude Code (/home/dev/.claude/projects absent).
    → installe Claude Code (code.claude.com) et code un peu.
Prochaine étape : ghost run (ingest + scan + distille).
[exit 0]                      # ← aucune erreur

$ ghost skills                # base vide
aucun skill distillé pour l'instant — lance `ghost run` ... [exit 0]

$ ghost scan                  # base vide
base vide — lance d'abord `ghost ingest` ... [exit 0]

$ ghost validate pas-un-skill # résolution
« pas-un-skill » inconnu — donne un id de skill, un slug, ou un id de candidat. [exit 1]
```

Aucune commande ne plante avec une traceback ; chaque absence donne une consigne.

## Transcript — base réelle (résolution + dédup)

```
$ ghost skills
 3   291  edit-stale-read-recovery       SKILL ⚠DOUBLON  kept  oui
 16  291  edit-file-modified-since-read  SKILL ⚠DOUBLON  kept  oui
⚠ 1 candidat(s) avec doublon : 291. `ghost distill 291 --force` régénère et désactive l'ancien.

$ ghost show 16
16 est un skill (edit-file-modified-since-read) → candidat 291
[FAILURE_LOOP] Edit|File has been modified since read ...

$ ghost validate 291
291 est un candidat avec plusieurs skills (16:…, 3:…) — donne l'id du skill voulu. [exit 1]

$ ghost distill 291           # sans --force
candidat 291 a déjà un skill actif : 3:…, 16:…. Relance avec --force pour redistiller. [exit 1]
```

Tests : 94 au total (13 nouveaux pour résolution/dédup/onboarding). ruff + mypy --strict clean.
