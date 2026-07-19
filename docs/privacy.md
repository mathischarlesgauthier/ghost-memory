# Vie privée

Ghost Memory lit ton historique de code. Voici exactement ce qui reste chez toi
et ce qui part, quand, et sous quelle forme.

## Local par défaut

Tout vit dans `~/.ghost/` (dossier en `0700`, base en `0600`) :

- `ghost.db` — ton historique ingéré ;
- `skills/<slug>/SKILL.md` — les skills distillés ;
- `api_key` — ta clé Anthropic (`0600`), jamais commitée.

`ingest`, `scan`, `show`, `skills`, `deploy`, `bench` (le grader) ne sortent
**rien** sur le réseau. Seuls `distill`/`validate`/`bench` (les runs d'agent)
appellent l'API Anthropic, avec **ta** clé.

## Rédaction avant tout envoi — fail closed

Avant qu'une trace parte vers l'API pour distillation, elle passe par une
rédaction *fail closed* : dans le doute, on masque. On logge des **comptes** de
rédactions, jamais les valeurs. La sur-rédaction est acceptée par contrat ; la
sous-rédaction est un bug.

Sont masqués : clés d'API et tokens (Anthropic, GitHub, Slack, AWS, Google,
Stripe, Notion…), clés privées, JWT, en-têtes `Authorization`, secrets dans les
URLs et les `postgres://user:pass@`, variables `*_KEY/TOKEN/SECRET/PASSWORD`,
emails, IPv4, ton chemin home, et toute chaîne littérale de ta deny-list
(`~/.ghost/deny.txt`).

Exemple — ce que tu as dans ta trace :

```
export DATABASE_KEY=sk-live-abc123def456  # dans /Users/toi/projet/app.py
```

Ce qui part réellement :

```
export DATABASE_KEY=<redacted:env_secret>  # dans ~/projet/app.py
```

Et ce qui est loggé (comptes seulement) :

```
redactions {env_secret: 1, home_path: 1}
```

> Une deny-list présente mais illisible **interrompt** l'envoi
> (`RedactionError`) au lieu d'être ignorée en silence.

## Télémétrie — désactivée par défaut, opt-in

La télémétrie est **off** par défaut. `ghost telemetry on <endpoint>` l'active
explicitement. Même activée, elle n'envoie que des **comptes agrégés** via une
allowlist stricte (verbes de commande + classes d'erreur d'ensembles fixes) —
**jamais** ton code, tes chemins, tes prompts, ni le contenu de tes skills.
L'envoi exige HTTPS.

Avant d'envoyer quoi que ce soit :

```bash
ghost telemetry preview   # affiche le payload EXACT qui partirait, sans l'envoyer
```

`ghost telemetry off` désactive ; `ghost telemetry status` montre l'état.

## Déploiement transparent

`ghost deploy` **n'installe aucun hook**. Il écrit des `SKILL.md` que Claude Code
découvre nativement, confinés à `~/.claude/skills/<slug>/` (ou le
`.claude/skills/` du projet). `ghost why` montre ce qui était injectable ;
`ghost uninstall` retire tout ce que Ghost a déployé.
