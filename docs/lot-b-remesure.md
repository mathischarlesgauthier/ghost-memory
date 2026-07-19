# Lot B — Re-mesure sur harnais corrigé

## Avant / après

| | baseline SANS skill | instrument |
|---|---|---|
| **Avant** (replay réel, skill 16, Lot A) | **1/22** (~5 %) | méga-missions ghost-world → on mesurait du bruit |
| **Après** (micro-bench synthétique, skill 16) | **3/3** (100 %) | tâche auto-contenue, grader déterministe → on mesure du signal |

La cible du Lot B — *baseline SANS skill > 50 %* — est atteinte : une baseline
qui réussit vraiment. À partir de là, un verdict de lift veut dire quelque chose.

## Le run (2026-07-19)

`ghost bench 16 --yes --runs 3 --run-budget 0.40 --max-cost 3.0`

```
╭─ PAS DE LIFT MESURABLE  (synthétique)
│  succès sans 3/3 → avec 3/3
│  n=1 bancs · 6 runs · coût 0.72$
   turns         sans 8 → avec 9   (+12%, cohérent)
   output_tokens sans 1162 → avec 1139  (-2%, cohérent)
   tool_errors   sans 0 → avec 0   (—)
   duration_ms   sans 16299 → avec 16905  (+4%, cohérent)
```

**Verdict : pas de lift mesurable, sur une baseline qui MARCHE.** C'est l'un des
deux résultats acceptables du Lot B — le skill `edit-file-modified-since-read`
est neutre sur ce banc précis (tâche simple read→edit où un agent compétent ne
tombe pas dans le piège du stale-read). Ce n'est plus « on ne peut rien
conclure » : c'est un vrai négatif, mesuré sur du signal.

## Honnêteté sur la portée

- **Synthétique, pas du vrai replay** (cf. Lot A : le corpus n'a aucun cas court
  rejouable). Le banc est un proxy de la scar, pas une reproduction de
  l'historique.
- Le banc est assez simple pour que les DEUX conditions atteignent 100 % de
  succès : l'axe « taux de succès » est saturé, seul l'axe efficacité
  (tours/tokens) pourrait montrer un lift, et il n'en montre pas ici.
- Coût : 0,72 $ / 6 runs (~0,12 $/run), ~2 min. La détection de coupure budget
  (`error_max_budget_usd`) est confirmée en vrai.

## Garde-fous prouvés (tests, sans API)

- baseline SANS skill > 50 % exprimable par le harnais ;
- runs coupés (budget/timeout) = catégorie distincte, jamais des échecs ;
- **anti-triche** : un skill nul donne un lift ~0 (jamais positif) ;
- un skill réellement utile (moins de tours partout) produit un lift ;
- les graders réels rejettent le stub et acceptent une solution correcte.
