<div align="center">

# 👻 Ghost Skills

**The collective memory for coding agents.**

Your agent forgets everything between sessions. Ghost Skills reads your
Claude Code history, distills what you already taught it into reusable
skills, feeds them back at the right moment — and connects you to the
collective memory of thousands of developers, ranked by what actually
works. *Measured, not upvoted.*

[Website](https://ghostskills.com) · [Docs](https://ghostskills.com/docs) · [Earn](https://ghostskills.com/earn)

</div>

---

## Table of contents

- [Why](#why)
- [Install](#install)
- [Quickstart](#quickstart)
- [How it works](#how-it-works)
- [Command reference](#command-reference)
- [Measuring lift](#measuring-lift)
- [The network — pricing & unlocks](#the-network--pricing--unlocks)
- [Contribute & earn](#contribute--earn)
- [Privacy & security](#privacy--security)
- [Where your data lives](#where-your-data-lives)
- [FAQ](#faq)
- [Development](#development)
- [License](#license)

---

## Why

What Claude gets right on the first try is generic and worthless. What
**cost you failures** is proprietary — and today it evaporates the moment a
session ends.

> **Monday.** You explain that migrations on your ledger table must go through
> `batch_alter_table`. It works.
> **Thursday.** New session. It doesn't know. Seven attempts, forty minutes.
> **Next month.** Your new hire hits the same wall.

You've paid for that lesson three times. Ghost Skills turns those scars into
skills your agent reuses — automatically, from the history you already have,
without you writing a single file.

And you're not alone: the wall you'll hit next Thursday, **someone already hit
it**. Their scar is distilled, measured, and waiting.

---

## Install

Not on PyPI yet — install from GitHub. It's a Python 3.12+ CLI named `ghost`.

```bash
# with uv (recommended)
uv tool install git+https://github.com/mathischarlesgauthier/ghost-memory

# or with pip
pip install git+https://github.com/mathischarlesgauthier/ghost-memory
```

Then:

```bash
ghost init      # guided setup: PATH, API key, Claude Code detection, first scan
```

`ghost init` never crashes on a fresh machine — every missing piece prints a fix,
not a stack trace. `ghost doctor` runs a full health check anytime.

---

## Quickstart

```bash
ghost run       # ingest history → scan for scars → distill new candidates
ghost skills    # triage what was distilled: cost, duplicates, status
ghost deploy    # push kept skills into ~/.claude/skills/  (Claude Code loads them)
```

**No hooks are installed.** `ghost deploy` writes plain `SKILL.md` files that
Claude Code discovers natively — nothing is silent. `ghost why` shows which
skills were injectable at your last prompt and why.

---

## How it works

Two behaviors that look like bugs — the **SKIP** and **"no measurable lift"** —
are exactly what keep the tool honest.

**1 · Scars.** `ghost scan` reads your ingested history and finds three shapes of
scar: `FAILURE_LOOP` (the agent hit the same error several times before
converging — the convergence is the knowledge), `HUMAN_OVERRIDE` (you corrected
it — expensive signal), `REPEATED_SEQUENCE` (a pattern to capture once). Every
candidate keeps a stable link to the raw events (`src_file:src_line`), so you can
always trace back to the proof.

**2 · Distillation.** `ghost distill` sends the *redacted* trace to an LLM that
condenses it into a `SKILL.md`: when to use it, the procedure, and a **Pitfalls**
section where every pitfall cites the failure that proves it. Nothing invented —
if the trace proves nothing non-obvious, nothing is written.

**3 · SKIP is a feature.** Many candidates return `SKIP`: what the agent already
did well is generic and worthless. An honest SKIP beats a hollow skill.

**4 · Lift, measured.** A skill only matters if it *changes* what your agent
produces. Ghost measures it instead of assuming it — see
[Measuring lift](#measuring-lift).

```
your sessions ──▶ ghost scan ──▶ candidates ──▶ ghost distill ──▶ SKILL.md
                                                      │
                                                 ghost deploy ──▶ ~/.claude/skills/
```

---

## Command reference

Every command accepts `--db` (default `~/.ghost/ghost.db`). Commands that take an
identifier accept a **skill id, a candidate id, or a slug** — Ghost resolves it
and tells you how (no more "not a valid int").

### The loop

| Command | What it does |
|---|---|
| `ghost init` | Guided onboarding: PATH, API key, Claude Code detection, first scan. |
| `ghost doctor` | Installation diagnostic — each ✗ says exactly what to do. |
| `ghost ingest` | Ingest `~/.claude/projects/**/*.jsonl` into SQLite (idempotent, streaming). |
| `ghost scan` | Detect scars (FAILURE_LOOP / HUMAN_OVERRIDE / REPEATED_SEQUENCE). |
| `ghost show <id>` | Dump a candidate's raw evidence, down to `src_file:src_line`. |
| `ghost distill <id>` | Candidate → `SKILL.md` (one LLM call, cited pitfalls, self-critique). `--force` regenerates. |
| `ghost keep / reject <id>` | Approve (deployable) / reject (survives re-scans). |
| `ghost skills` | List distilled skills: verdict, cost, status, deployment, duplicates. |
| `ghost deploy` | Push kept skills to `~/.claude/skills/`. `--dry-run` shows without writing. |
| `ghost run` | The full loop under a spend cap (`--budget`, default $2; `--top`, default 10). |

### Measurement

| Command | What it does |
|---|---|
| `ghost bench <skill>` | Measure lift on synthetic micro-benchmarks (deterministic grader, a baseline that actually works). |
| `ghost validate <skill>` | Replay real history tasks with/without the skill. Needs ≥3 eligible cases; `--allow-underpowered` runs the mechanics anyway (marked not statistically valid, nothing persisted). Budget/timeout cuts are a separate category, never failures. |
| `ghost watch` | Early exposure signal (sessions with vs without a skill). |

### Control & privacy

| Command | What it does |
|---|---|
| `ghost why` | Which skills were injectable at the last prompt, and why. |
| `ghost disable / enable <id>` | Remove a deployed skill / reactivate it. |
| `ghost uninstall` | Remove every `SKILL.md` Ghost deployed (no hooks were ever installed). |
| `ghost telemetry {status,on,off,preview,send}` | Off by default; `preview` prints the exact payload. Only aggregate counts. |

### Account & network

| Command | What it does |
|---|---|
| `ghost login` / `logout` | Connect to the network (device flow → **Ghost token**, never your Anthropic key). |
| `ghost upgrade <tier>` | Open Stripe Checkout for a paid tier. |
| `ghost usage` | This cycle: plan, unlocks used / quota, reset, progress bar. |
| `ghost unlocked` | Community skills unlocked this cycle, sorted by measured lift. |
| `ghost earnings` | Revenue-share balance, impact share, installs generated, distance to €50 threshold. |
| `ghost account` / `whoami` | One-screen dashboard / quick plan + email. |
| `ghost history` | Past payouts (date, amount, status). |
| `ghost publish <skill>` | Publish to the collective memory — **mandatory secret scan** + diff + confirm. Private by default; `--public` to enter the ranked registry. |
| `ghost payout-setup` | Enable payouts (optional, cash-out only) — secure browser page, no bank data in the CLI. |

> Account commands read the **real** state over the API — never an invented
> number. Offline, they show the last-known state flagged as possibly stale;
> they never crash.

---

## Measuring lift

The success criterion is a **resolved task** (a deterministic grader passes),
not "a commit was produced". Runs cut by budget or timeout are their own
category — never failures. If the with/without distributions overlap, the
verdict is **"no measurable lift"** — a result, not a bug. A skill that *always*
showed positive lift would be broken.

```
$ ghost bench edit-file-modified-since-read --yes
╭─ NO MEASURABLE LIFT  (synthetic)
│  success without 3/3 → with 3/3
│  n=1 bench · 6 runs · $0.72
   turns          without 8 → with 9   (+12%, overlapping)
```

Why synthetic benches and not only replay? A history may contain no short,
self-contained, replayable task (real missions mix network, external tools, prod
access). Without a baseline that works, a lift number means nothing. Synthetic
benches give an honest baseline — and are labeled as such.

---

## The network — pricing & unlocks

Your own skills are **free forever**, on your own key. The paid product is access
to the *collective* memory — ranked by measured lift, never by download count.
It's metered in **unlocks**: the first time a *distinct* community skill enters
your library in the billing period. Re-using one you already unlocked never
re-counts.

| Plan | Price | Included | |
|---|---|---|---|
| **Free** | $0 | 5 community unlocks (lifetime, to try) | your own memory, unlimited |
| **Pro** | $29/mo | 200 unlocks / month | sync across machines |
| **Team** | $95/mo | 1,000 unlocks / month | shared team registry |
| **Scale** | $195/mo | 4,000 unlocks / month | private registry |

Beyond your quota, pay as you go — like your API bill, but for skills that
actually work. Hitting the quota is a **clear message, never a crash**: your
local retrieve and already-unlocked skills keep working.

---

## Contribute & earn

On a paid plan you don't just use the collective memory — **you earn from it.**
Publish skills that work and a share of subscription revenue (**50%**, paid for
measured **lift × adoption**, €50 payout threshold) comes back to you.

```bash
ghost publish my-skill --public   # secret scan + diff + confirm → registry
ghost earnings                    # balance, impact share, distance to threshold
ghost payout-setup                # optional, only to cash out
```

We pay for **lift, not uploads** — the one thing you can't fake, because we
measure it. Publish your *best* work, not your *most* work. Even before the
money, your public profile (installs + measured lift) is a résumé no interview
can argue with. Full details: [ghostskills.com/earn](https://ghostskills.com/earn).

---

## Privacy & security

Everything lives in `~/.ghost/` (dir `0700`, db `0600`). `ingest`, `scan`,
`show`, `skills`, `deploy` touch **nothing** on the network. Only
`distill` / `run` / `create` / `validate` / `bench` call the Anthropic API, with
**your** key — read from `~/.ghost/api_key` (written by `ghost init`), falling
back to `ANTHROPIC_API_KEY`.

**Redaction before any send — fail closed.** Before a trace leaves for
distillation (or a skill is published) it passes a fail-closed redactor: when in
doubt, mask. Only counts are logged, never values.

```
# in your trace
export DATABASE_KEY=sk-live-abc123      # in /Users/you/app.py

# what actually leaves
export DATABASE_KEY=<redacted:env_secret>   # in ~/app.py

# what is logged (counts only)
redactions {env_secret: 1, home_path: 1}
```

**Telemetry is off by default.** Opt-in only, HTTPS required, strict allowlist.
Even enabled it sends only aggregate command names and error classes — never your
code, paths, prompts, or skill contents. `ghost telemetry preview` prints the
exact payload before anything is sent.

**Network security.** The Ghost token (network identity, *not* your Anthropic
key) lives in `~/.ghost/ghost_token` (`0600`) and is never printed. Account reads
are read-only; the only write flows — `publish` and `payout-setup` — go through
the mandatory secret scan and a secure browser page respectively. No bank or
identity data ever passes through the CLI or its logs.

---

## Where your data lives

```
~/.ghost/
├── ghost.db          # your ingested history + candidates + skills (SQLite, 0600)
├── api_key           # your Anthropic key (0600), never committed
├── ghost_token       # network identity token (0600), never printed
├── skills/<slug>/    # distilled SKILL.md files
└── deny.txt          # optional extra literals to redact
```

Deployed skills land in `~/.claude/skills/<slug>/SKILL.md` (or a project's
`.claude/skills/`). Remove them anytime with `ghost disable <id>` or
`ghost uninstall`.

---

## FAQ

**`ghost: command not found`** — installed but not on your PATH (often
`~/.local/bin`). Run `uv tool update-shell` and reopen your terminal.
`ghost init` and `ghost doctor` both detect and explain this.

**`ghost scan` finds no candidates** — almost always the history: empty base
(run `ghost ingest`), no Claude Code history yet, or a history too smooth to have
scars. That last one is normal, not a bug.

**`distill` says SKIP / `bench` says "no lift"** — both are honest results. SKIP
means what the agent already did well is generic. "No lift" means that, on a
baseline that actually works, the skill doesn't measurably change the outcome —
proof the measurement doesn't cheat.

**Does my code go over the network?** Not by default — ingest and scan are fully
local. Distillation sends *redacted* traces to the Anthropic API with your key.
Telemetry is off by default and never sends code.

**Why are my earnings €0?** The network is young and few community skills have
measured lift yet. `ghost earnings` reads real data — the number follows the
measurement as the network grows. It never shows a fake amount.

---

## Development

```bash
git clone https://github.com/mathischarlesgauthier/ghost-memory
cd ghost-memory
uv sync
uv run ruff check .
uv run mypy --strict ghost
uv run pytest -q
```

Python 3.12+, [uv](https://docs.astral.sh/uv/), `ruff`, `mypy --strict`. The CLI
is [Typer](https://typer.tiangolo.com/); storage is SQLite; there are no runtime
services to run for the local workflow.

---

## License

MIT.
