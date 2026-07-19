# Comment ça marche

Ghost Memory transforme ton historique en mémoire réutilisable, en quatre temps.
Deux comportements qui ressemblent à des bugs — le **SKIP** et le **« pas de
lift »** — sont en fait ce qui rend l'outil honnête.

## 1. Cicatrices

`ghost scan` lit ton historique ingéré et repère trois formes de cicatrices :

- **FAILURE_LOOP** — l'agent bute plusieurs fois sur la même erreur avant de
  converger. La convergence est le savoir.
- **HUMAN_OVERRIDE** — tu as corrigé l'agent. Ta correction est du signal cher.
- **REPEATED_SEQUENCE** — la même séquence d'actions revient : un motif à
  capturer une fois pour toutes.

Chaque candidat garde ses occurrences et un lien stable vers les events bruts
(`src_file:src_line`) — tu peux toujours remonter à la preuve avec `ghost show`.

## 2. Distillation

`ghost distill` envoie la trace d'un candidat (redactée — voir
[Vie privée](privacy.md)) à un LLM qui la condense en `SKILL.md` : quand
l'utiliser, la procédure, et surtout une section **Pièges** où chaque piège
**cite l'échec qui le prouve**. Une auto-critique passe derrière. Rien
d'inventé : si la trace ne prouve rien de non-évident, on n'écrit rien.

## 3. Le SKIP est une feature

Beaucoup de candidats donnent un verdict **SKIP** : ce que l'agent faisait déjà
bien est générique et sans valeur. Un catalogue qui garderait tout te noierait
sous du bruit ; Ghost préfère jeter. Un SKIP honnête vaut mieux qu'un skill
creux — c'est le tri qui fait la valeur.

## 4. Le lift mesuré

Un skill n'a de valeur que s'il **change** ce que l'agent produit. Ghost le
mesure au lieu de le supposer :

- `ghost bench <skill>` rejoue des **micro-benchmarks synthétiques** —
  mini-tâches auto-contenues, sans réseau ni MCP, avec un grader déterministe
  (la tâche est résolue, ou non). La baseline *sans* skill réussit vraiment, puis
  on regarde si le skill change le nombre de tours, les erreurs, les tokens.
- `ghost validate <skill>` fait la même chose en rejouant ton historique réel,
  quand des cas courts et auto-contenus s'y prêtent.

### « Pas de lift mesurable » est aussi une feature

Le critère de succès est une **tâche résolue** (un grader qui passe), pas un
commit produit. Les runs coupés par le budget ou le timeout forment une
catégorie à part : ils ne comptent jamais comme des échecs. Et si les
distributions avec/sans se recouvrent, le verdict est **« pas de lift
mesurable »** — un résultat, pas un échec. Un skill volontairement nul doit
donner un lift ~0 ; un outil qui trouverait toujours un lift positif serait
cassé.

> Pourquoi des bancs synthétiques et pas seulement du replay ? Parce qu'un
> historique peut n'avoir aucune tâche courte et rejouable (les vraies missions
> mêlent réseau, outils externes, accès de prod). Sans baseline qui marche, un
> chiffre de lift ne veut rien dire. Les bancs synthétiques donnent une baseline
> honnête ; ils sont explicitement étiquetés comme tels.

## Déploiement & contrôle

`ghost deploy` place des `SKILL.md` dans `~/.claude/skills/` (ou le
`.claude/skills/` du projet). Claude Code les découvre nativement : **aucun hook
installé**. `ghost why` montre ce qui était injectable ; `ghost disable` retire
un skill ; `ghost uninstall` retire tout.
