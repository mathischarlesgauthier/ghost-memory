# FAQ

## `ghost : command not found`

Le binaire est installé mais pas sur ton PATH (souvent `~/.local/bin`). Deux
options :

```bash
uv tool update-shell     # ajoute le dossier des outils uv au PATH
```

puis **rouvre ton terminal**. Sinon, ajoute `~/.local/bin` au PATH dans ton
`.zshrc`/`.bashrc`. `ghost init` détecte le problème et te dit quoi faire ;
`ghost doctor` aussi.

## `ghost scan` ne trouve aucun candidat

C'est presque toujours l'historique :

- **Base vide** — lance `ghost ingest` d'abord (`ghost init` le fait pour toi).
- **Pas d'historique Claude Code** — `~/.claude/projects` n'existe pas ou est
  vide. Utilise Claude Code un peu ; Ghost apprend de ce que tu as réellement
  vécu. `ghost doctor` confirme la présence de l'historique.
- **Historique trop lisse** — si tu n'as ni boucles d'échec, ni corrections, ni
  répétitions, il n'y a pas de cicatrice à extraire. C'est normal, pas un bug.

## `ghost distill` répond SKIP

Ce que l'agent faisait déjà bien est générique et sans valeur. Un SKIP est un
tri honnête : Ghost préfère ne rien écrire plutôt qu'un skill creux. Voir
[Comment ça marche](how-it-works.md#3-le-skip-est-une-feature).

## `ghost bench`/`validate` dit « pas de lift mesurable »

C'est un **résultat**, pas une panne. Ça veut dire que, sur une baseline qui
réussit vraiment, le skill ne change pas mesurablement le résultat (les
distributions avec/sans se recouvrent). Un outil qui trouverait toujours un lift
positif serait cassé : « pas de lift » prouve que la mesure ne triche pas. Le
skill peut rester utile ailleurs, ou être neutre ici — les deux sont honnêtes.

## `ghost validate` ne trouve pas assez de cas

`validate` rejoue de **vraies** tâches de ton historique, et exige des cas
courts et auto-contenus (sans réseau, ni outils externes, ni accès de prod). Si
tes sessions sont de grosses missions, il n'y en a pas — utilise alors
`ghost bench <skill>`, qui mesure sur des micro-benchmarks synthétiques avec une
baseline garantie. Voir [Comment ça marche](how-it-works.md).

## `291 est un candidat avec plusieurs skills…`

Tu as passé un id de candidat à une commande qui attend un skill, et ce candidat
a plusieurs skills (un doublon). Passe l'id du skill voulu, ou nettoie le doublon
(`ghost skills` le signale ; `ghost distill <candidat> --force` régénère et
désactive l'ancien).

## Est-ce que mon code part sur le réseau ?

Non par défaut pour l'ingestion et le scan (tout est local). La distillation
envoie des traces **redactées** (secrets, chemins home, emails masqués — fail
closed) à l'API Anthropic avec ta clé. La télémétrie est off par défaut et
n'envoie jamais de code. Détails : [Vie privée](privacy.md).
