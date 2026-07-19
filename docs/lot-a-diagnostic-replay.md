# Lot A — Pourquoi la baseline de replay échoue à ~95 %

> Diagnostic. Aucun code de fix. Preuves établies sans dépenser d'API
> (lecture du harnais + table `replays` + statistiques du corpus), à la
> demande de Jordan (« gratuit only »).

## Contexte

Premier `ghost validate` réel sur le skill **16** (`edit-file-modified-since-read`) :
succès **1/22 runs**, avec comme sans skill → verdict « pas de lift mesurable ».
Le problème n'est pas le skill : c'est que **le replay lui-même n'aboutit
presque jamais**. On ne peut mesurer aucun lift tant que la baseline SANS
skill ne réussit pas raisonnablement.

## Méthode

Trois sources, toutes gratuites :

1. Lecture du harnais : `ghost/replay.py`, `ghost/validate.py`.
2. Table `replays` de `~/.ghost/ghost.db` : métriques persistées des 22 runs.
3. Statistiques du corpus (`sessions`, `events`).

Aucun replay live relancé (0 $).

## Étape 0 — Autopsie du cas `1fe94ccd`

| Question | Réponse mesurée |
|---|---|
| **Prompt réinjecté ?** | Le **premier** des **17** tours humains de la session, seul (`validate.py:161-166`, `ORDER BY seq LIMIT 1`). Aucun contexte de conversation. Contenu : `# GHOST WORLD — LE CEO PROPOSE LA NEXT STEP… Mode AUTONOME, ultracode… libère les ports lsof -ti:3000,3001,8000,8001…` |
| **État du worktree ?** | Clone `git clone --local` de `~/Desktop/ghost-world` checkouté sur `hashes[0]~1`. **Pas** de `uv sync`/`npm install`, pas de `node_modules`, **aucune var d'env/secret** (seuls PATH/HOME/LANG/LC_ALL/TERM/TMPDIR/SHELL passent — `replay.py:57`). **Ni réseau, ni MCP `claude_design`, ni ports libres, ni modèles Kimi/GLM** — tout ce que le prompt exige. |
| **Que fait l'agent ?** | ~5-6 tours, ~2-4 lignes changées, **0 commit**, arrêt **propre** (ni erreur, ni timeout, ni budget). *(Transcript verbatim indisponible : le `CLAUDE_CONFIG_DIR` temporaire est supprimé en fin de run — `replay.py:260`. Le récupérer exigerait un replay live payant, non lancé.)* |
| **Pourquoi ✗ ?** | `success = new_commits > 0` (`replay.py:254`). 0 commit → ✗. |

## Étape 1 — Les 4 hypothèses, testées

Distribution des 22 runs de skill 16 (table `replays`) :

| condition | n | ✓ | timeouts | erreurs | commits | tours moy. | coût moy. | lignes moy. |
|---|---|---|---|---|---|---|---|---|
| sans | 11 | **1** | 0 | 1 | 1 | 5.2 | **0,245 $** | 4.0 |
| avec | 11 | **0** | 0 | 2 | 0 | 5.8 | **0,253 $** | 2.0 |

Les 4 cas éligibles de skill 16, tous dans `~/Desktop/ghost-world` :

| case | tours humains | nature du prompt |
|---|---|---|
| `1fe94ccd` | 17 | mission autonome « CEO next step + fix chat » |
| `559fffd7` | 7 | import via **MCP `claude_design`** (réseau + auth) |
| `dc2ac649` | 34 | « CRÉER UNE APP NATIVE (React Native/Expo) » |
| `f07e1693` | 5 | « correctif + **preuve en prod** avec mes accès » |

### Verdicts (classés par impact)

| # | Hypothèse | Verdict | Preuve |
|---|---|---|---|
| **0** | **Cas non-rejouables (racine — domine tout)** | **CONFIRMÉ** | Les 4 cas sont des méga-missions ghost-world (5-34 tours) exigeant réseau, MCP, ports, prod, autres modèles. Le sandbox n'en fournit aucun. La tâche est **impossible dans la boîte**. |
| A | Contexte amputé | **CONFIRMÉ, sévère** | 1 seul prompt injecté vs 5-34 tours réels ; base = `hashes[0]~1`, qui ne correspond pas forcément au 1er prompt. |
| B | Run-budget trop serré | **RÉFUTÉ comme cause active** | 0/22 timeout ; coût moyen **0,245–0,253 $ ≪ 0,60 $**. L'agent s'arrête tout seul, pas au plafond. **Bug latent réel** : coupé-budget/timeout comptés ✗, sans catégorie distincte (`aggregate` ne fait que `sum(success)`, `validate.py:440-449`). |
| C | Environnement cassé | **PLAUSIBLE, secondaire** | Aucun provisioning de deps/env (`sandbox`, `replay.py:106-131`). Mais l'agent s'arrête *avant* d'en avoir besoin ; et ces missions veulent réseau/MCP/prod de toute façon. |
| D | Critère trop strict | **INVERSÉ** | `success = new_commits > 0` = *n'importe quel* commit. Pas trop strict : trop **laxiste**, et mauvaise cible (commit ≠ résolu). L'agent édite 2-4 lignes sans committer → ✗. |

## Le corpus n'a aucun cas court rejouable

Distribution des 29 sessions (avec `cwd`) par nombre de tours humains (>50 car.) :

| tours | sessions |
|---|---|
| 1 | 3 |
| 2-3 | 9 |
| 4-8 | 3 |
| 9+ | 7 |

Et surtout : sur les **3 sessions à 1 tour**, **0 possède un commit extractible**.
→ **Il n'existe aucune session courte + auto-contenue + committante** à rejouer.
Le harnais ne peut donc pas être réparé par le seul réglage du budget ou du
critère : il n'y a pas de tâche propre à rejouer.

## Causes dominantes

1. **Les cas éligibles ne sont pas des tâches rejouables.** `eligible_cases`
   filtre sur « a un commit + un prompt >50 car. + repo/commit présents », mais
   **pas** sur « session courte, auto-contenue, sans réseau/MCP/prod ». Il admet
   donc des méga-missions impossibles à rejouer sous les caps du sandbox.
2. **La mesure est aveugle indépendamment des cas.** Succès = commit-existe
   (≠ résolu) ; coupé-budget/timeout ≠ catégorie. Même avec de bons cas, le
   chiffre resterait douteux.

## Fix proposés (pour Lot B — non codés)

Direction retenue par Jordan : **micro-benchmarks synthétiques**.

- **Micro-benchmarks synthétiques** dérivés des scars : mini-états de repo
  auto-contenus + prompt de tâche + **checker déterministe** (un test qui passe
  ssi la tâche est résolue). Sans réseau/MCP/prod. **Explicitement étiquetés
  « pas du vrai replay »** — c'est un banc, pas une reproduction causale de
  l'historique. Seule voie qui produit une baseline > 50 % sur CE corpus.
- **Critère = tâche résolue** (le checker passe) plutôt que commit-existe.
- **Catégoriser** budget/timeout hors des ✗ (nouveau flag + agrégation séparée).
- **Garde-fou anti-triche** (exigence Lot B) : un skill volontairement nul doit
  donner un lift ~0, jamais positif → à vérifier par un test.
- Reconstruction du contexte + provisioning de l'env : seulement si un jour on
  obtient de vrais cas multi-tours rejouables (pas la priorité ici).

## Gate

Diagnostic livré. Aucun fix implémenté. Direction validée par Jordan
(micro-benchmarks synthétiques) → exécutée au Lot B.
