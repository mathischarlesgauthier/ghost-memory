# Ghost Memory

Ton agent oublie tout entre deux sessions. Ce que Claude fait bien du premier
coup est générique et sans valeur ; ce qui t'a coûté des échecs est propriétaire.
**Ghost Memory lit ton historique Claude Code, en extrait ce que tu lui as déjà
appris, et le lui réinjecte au bon moment** — classé par effet mesuré sur du vrai
code, jamais par nombre de téléchargements. Personne d'autre n'a tes traces.

## Installer

Pas (encore) sur PyPI — installe depuis GitHub :

```bash
uv tool install git+https://github.com/mathischarlesgauthier/ghost-memory
```

Sans `uv` ? `pip install git+https://github.com/mathischarlesgauthier/ghost-memory`.

## Démarrer

```bash
ghost init      # onboarding : PATH, clé API, détection Claude Code, premier scan
```

`ghost init` te guide jusqu'à voir **tes** candidats. Il ne plante jamais : si
quelque chose manque (clé, historique), il te dit quoi faire.

## Les trois commandes à connaître

```bash
ghost run       # tout d'un coup : ingère l'historique, scanne, distille les nouveaux
ghost skills    # ce qui a été distillé — triage, coût, doublons
ghost deploy    # pousse les skills validés dans ~/.claude/skills/ (Claude Code les charge)
```

Le reste est documenté :

- **[Toutes les commandes](docs/commands.md)** — à quoi sert chacune, exemple, sortie.
- **[Comment ça marche](docs/how-it-works.md)** — cicatrices → distillation → SKIP → lift.
- **[Vie privée](docs/privacy.md)** — local par défaut, rédaction avant tout envoi, télémétrie opt-in.
- **[FAQ](docs/faq.md)** — `ghost` introuvable, pas de candidats, pas de lift.

## Transparence

Ghost Memory **n'installe aucun hook** : `ghost deploy` place des `SKILL.md` que
Claude Code découvre nativement. `ghost why` dit ce qui était injectable au
dernier prompt ; `ghost disable <id>` retire un skill ; `ghost uninstall` retire
tout. Rien n'est silencieux.

## Ce qui distingue Ghost Memory d'un catalogue

Un skill n'est gardé que s'il tient debout, et son effet se **mesure** :

```bash
ghost bench <skill>      # micro-benchmark synthétique : baseline qui MARCHE, puis lift
ghost validate <skill>   # replay de ton historique avec/sans le skill (quand des cas s'y prêtent)
```

La mesure ne ment pas : distributions qui se recouvrent → « pas de lift
mesurable », et c'est un **résultat**, pas un bug. Voir [Comment ça marche](docs/how-it-works.md).

## Statut

Alpha. Détecteurs calibrés sur Python/TS ; ils sortent aussi des candidats sur
TypeScript/React et JavaScript. Sur ta stack, `ghost scan` te dira lui-même s'il
trouve quelque chose. Tout est local (`~/.ghost/`).
