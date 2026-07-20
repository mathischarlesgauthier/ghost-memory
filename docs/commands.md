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

### `ghost welcome`
Réaffiche l'écran d'accueil (logo, essence du produit, 3 premières actions). Il
s'affiche aussi **une seule fois**, au tout premier lancement (jamais dans un
pipe ni en `--plain`, jamais ré-affiché ensuite).

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

### `ghost create <lien-github>`
Importe un skill **hébergé sur GitHub** et l'ajoute à tes skills locaux, comme un
distillé maison. Le lien doit pointer vers un fichier de skill existant (page
`/blob/…` ou lien raw) ; il est récupéré (SSRF-safe : github/raw uniquement, https,
taille plafonnée), puis passé au distillateur qui **génère le frontmatter manquant**
(tags, stack, `task_signature`) sans réécrire le corps. L'attribution est lue du
dépôt : `source` depuis l'URL, `license` via l'API GitHub (« unknown » si
indéterminée). Utilise ta clé Anthropic locale (`~/.ghost/api_key`), coût affiché
(~quelques centimes). **Preview avant écriture** : le frontmatter généré, la
signature de tâche et l'attribution, puis confirmation (`--yes` pour sauter).

Au confirm, le skill est écrit dans `~/.ghost/skills/<slug>/` et enregistré comme
un distillé : il apparaît dans `ghost skills`, se déploie via `ghost deploy` (global
par défaut) et se publie via `ghost publish` (retrievable sur sa signature générée).
Ré-importer le même lien crée une nouvelle version (l'ancienne est désactivée).

```
$ ghost create https://github.com/obra/superpowers/blob/main/skills/brainstorming/SKILL.md
```

**SKIP maintenu** : si le contenu est générique/du bruit (README marketing, doc
d'install, code source brut, pas un vrai skill), il est refusé avec la raison, rien
n'est écrit. **Échec propre** si le lien n'est pas résoluble en fichier de skill
(dépôt racine, page `/tree/…`, hôte non-GitHub, fichier vide) — message clair, aucune
écriture. Ce n'est PAS une publication (ça reste `ghost publish`, privé par défaut),
ni la distillation d'un repo de code sans skill.

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

## Compte & réseau

Ces commandes lisent le **vrai** état via l'API (jamais un chiffre inventé). Le
jeton Ghost vient du fichier local (`~/.ghost/ghost_token`, `chmod 600`), jamais
affiché. **Hors ligne** : elles montrent le dernier état connu avec un avertissement
« peut être périmé », jamais un plantage. Le réseau étant jeune, les gains sont
honnêtement à 0 tant qu'aucun lift n'est mesuré.

### `ghost login`
Connecte le CLI au réseau (**device flow**). Affiche un code court (ex.
`K7Q2-9FMX`) et ouvre `https://ghost-memory.com/device` : tu entres le code, la
page l'autorise, et le terminal récupère seul un **jeton Ghost** — jamais ta clé
Anthropic — stocké dans `~/.ghost/ghost_token` (`chmod 600`). Le code expire au
bout de 10 min ; s'il périme, relance `ghost login`. `--token <jeton>` adopte un
jeton déjà créé sur le web.

### `ghost logout`
Supprime le jeton Ghost local.

### `ghost retrieve [SIGNATURE]`
Cherche dans la **mémoire collective** les skills d'une classe de tâche :
slug, lift mesuré (ou *non mesuré*), statut, source/auteur. Métadonnées seules —
le corps se débloque séparément (Pro). Classé par lift mesuré ; les seeds non
mesurés suivent. Sans argument, la signature est déduite de ta **dernière
session locale** (`--session <id>` pour en choisir une). Vide → message honnête.

```
$ ghost retrieve "bash-alembic-upgrade+edit|py|duplicatecolumn|commit"
1 skill(s) pour cette classe de tâche
  alembic-idempotent-migrations · non mesuré · unverified ·seed · github.com/…
```

### `ghost usage`
Consommation du cycle : palier, déblocages utilisés / quota, reset, barre de
progression. Alerte + suggestion d'upgrade au-delà de 80 %. Un déblocage = 1ᵉʳ ajout
d'un skill communautaire **distinct** ; réutiliser un skill déjà débloqué ne compte pas.

```
Pro plan
  community unlocks: 47 / 200 this cycle   ██████░░░░░░░░░░░░░░░░░ 24%
  remaining: 153
  resets on: 2026-08-01
```
Free : `0 / 5 lifetime` (pas de reset — les 5 sont à vie, pour essayer).

### `ghost unlocked`
Skills communautaires débloqués ce cycle : slug, lift mesuré (ou *lift not yet
measured*), auteur. Triés par lift décroissant, non mesurés en bas. Vide →
message honnête.

### `ghost earnings`
Balance de rémunération (50 % du pool, payé au lift × adoption, seuil €50) :
balance, part d'impact mesuré, installs générés, lift moyen, distance au seuil,
statut des coordonnées de paiement. Pas encore de gains → message honnête, jamais
un faux montant.

```
Earnings  (50% of subscriptions, paid for lift x adoption)
  balance: €130.50
  measured impact share this cycle: 100.00%
  installs your skills generated: 68
  avg measured lift of your skills: -49%
  payout threshold: €130.50 / €50.00  — reached
  payout details: configured
```

### `ghost account`
Tableau de bord un écran : email, palier, cycle, résumé usage + earnings, lien
profil public. Le détail est dans les autres commandes. En Free, montre ce que Pro
débloquerait, sans être intrusif.

### `ghost history`
Historique des versements passés (date, montant, statut). Vide → *no payouts yet*.

### `ghost payout-setup`
Active les versements — **optionnel**, nécessaire seulement pour retirer (pas pour
contribuer ni gagner de la réputation). N'affiche/collecte **aucune** donnée bancaire
dans le terminal : ouvre une page sécurisée du backend (lien à usage unique).

### `ghost publish <skill>`
Publie un skill perso vers la mémoire collective. **Scan de secrets obligatoire**
(fail-closed) affichant ce qui est masqué, **diff** de ce qui part, confirmation
explicite. **Privé par défaut** ; `--public` pour entrer dans le registre classé au
lift. Le lift est mesuré après publication et apparaît dans `ghost earnings`.

Le skill est indexé sous la **signature de tâche** dominante de son candidat
(format `task_signature`, la même qu'interroge `ghost retrieve`) — pas la
signature de détecteur — pour qu'il soit trouvable quand la tâche correspond.

```
Publish demo-skill · visibility: public
secret scan — masked: {'api_key': 1, 'env_secret': 1, 'email': 1}
— exactly what will be sent (redacted) —
  ...export API_KEY=<redacted:env_secret> ... (email <redacted:email>)
✓ published demo-skill (public)
```

### `ghost whoami`
Palier + email/handle courant (debug rapide).

## Inspection

### `ghost stats`
Sessions, events, top 10 outils avec taux d'erreur, plage de dates.

## Affichage (couleur, animations, plain)

Ghost a une identité terminal sobre : un logo à l'accueil, des spinners
discrets qui décrivent l'étape en cours sur les commandes à traitement réel
(`scan`, `run`, `distill`, `validate`), des résumés encadrés, des verdicts
colorés (SKILL vert, SKIP gris). **Le style ne ralentit jamais** : les
animations reflètent le travail, elles ne le bloquent pas, et sont
interruptibles (Ctrl+C propre).

Tout le décoratif se coupe automatiquement quand il n'a pas lieu d'être :

- **Pas de TTY** (pipe, CI) : sortie plate, aucun code ANSI. Un
  `ghost stats | cat` ne sort que des données propres ; la progression, elle,
  part sur **stderr**.
- **`NO_COLOR`** (variable d'env) : couleur retirée (convention respectée).
- **`--plain`** (drapeau global) ou **`GHOST_PLAIN=1`** (variable d'env) :
  désactivent *tout* le décoratif — couleur ET animations — pour les scripts et
  les gens pressés.
- **Terminal étroit** (<80 colonnes) : le logo se réduit ou se masque, rien ne
  casse.

```
ghost --plain scan          # sortie plate, sans couleur ni spinner
GHOST_PLAIN=1 ghost run     # idem, via variable d'environnement
ghost stats | cat           # pipe : données propres, zéro ANSI
```
