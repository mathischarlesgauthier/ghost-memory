# Ghost Brain

Ce que Claude fait bien du premier coup est générique et sans valeur. Ce qui
t'a coûté des échecs est propriétaire. Ghost Brain fouille ton historique
Claude Code, en extrait les **cicatrices** — boucles d'échec, corrections
humaines, séquences répétées — et les distille en skills réutilisables,
rechargés dans Claude Code. Personne d'autre n'a tes traces.

## Installer

```bash
uv tool install ghost-brain
```

## Trois commandes

```bash
ghost doctor    # vérifie ton installation (chaque ✗ dit quoi faire)
ghost ingest    # ton historique Claude Code → base locale ~/.ghost/ghost.db
ghost scan      # les cicatrices : boucles d'échec, corrections, répétitions
```

Puis, quand tu veux transformer un candidat en skill vendable et le déployer :

```bash
ghost show <id>       # l'évidence brute d'un candidat
ghost distill <id>    # candidat → SKILL.md (un appel LLM, section Pièges prouvée)
ghost keep <id>       # valide un candidat
ghost deploy          # pousse les validés dans ~/.claude/skills/ (Claude Code les charge)
```

Et pour tout automatiser : `ghost run` (ingest → scan → distille les nouveaux,
sous plafond de dépense).

## Transparence et contrôle

Ghost Brain **n'installe aucun hook** : `ghost deploy` place des `SKILL.md` que
Claude Code découvre nativement. Rien n'est silencieux.

```bash
ghost why           # quels skills étaient injectables au dernier prompt, et pourquoi
ghost disable <id>  # un skill n'est plus jamais injecté
ghost uninstall     # retire tous les skills déployés
```

## Mesure (ce qui distingue Ghost Brain d'un catalogue)

```bash
ghost watch            # signal précoce : sessions exposées vs baseline (0 inférence)
ghost validate <id>    # chiffre CAUSAL : rejoue l'histoire avec/sans le skill
```

`ghost validate` ne ment pas : distributions qui se recouvrent → « pas de lift
mesurable », c'est un résultat.

## Vie privée

Tout est local (`~/.ghost/`). La distillation envoie des traces **redactées**
(clés, tokens, chemins home, emails masqués — fail closed) à l'API Anthropic,
avec ta clé. La télémétrie est **désactivée par défaut**, opt-in explicite, et
n'envoie que des comptes agrégés (jamais prompts, code, chemins, ni contenu de
skills) — `ghost telemetry preview` montre exactement ce qui partirait.

## Statut

Alpha. Détecteurs calibrés sur du Python/TS ; ils sortent aussi des candidats
sur du TypeScript/React et du JavaScript. Sur ta stack, `ghost scan` te dira
lui-même s'il trouve quelque chose.
