---
name: git-detached-head-recovery
description: Use when git reports a detached HEAD after a checkout of a commit, tag, or remote branch, and commits made there risk being lost when you switch branches.
tags: [git, version-control, recovery]
stack: git
signature: bash-git-checkout+bash-git-branch|sans-fichier|head-detached|commit
source: https://github.com/git/git
license: GPL-2.0
---

# Recovering from a detached HEAD

## When to use
`git status` says *HEAD detached at <sha>*. You checked out a commit, tag, or a
remote branch directly, made commits, and now those commits aren't on any branch —
switching away would strand them.

## Procedure
Give the work a branch before you move.

- Still on the detached commit: `git switch -c my-fix` (or `git branch my-fix`)
  turns the current HEAD into a real branch, keeping every commit you made.
- Already switched away and lost the commits: `git reflog` lists where HEAD has
  been; find the sha and `git branch rescued <sha>`.
- Only wanted to *look* at an old state (no commits): `git switch -` returns to
  your previous branch, nothing to save.

## Pitfalls
- **Switching branches from a detached HEAD with un-branched commits** leaves them
  reachable only via the reflog, which expires (default 90 days) — branch first.
- **`git checkout <sha>` is the classic trap**: it silently detaches. Prefer
  `git switch --detach <sha>` when you mean it, so the intent is explicit.
- **A rebase or hard reset while detached** can drop commits with no branch to fall
  back on; `git reflog` is your safety net, not a guarantee.
