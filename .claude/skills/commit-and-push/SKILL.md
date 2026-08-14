---
name: commit-and-push
description: Land finished work in this repo — commit, push the branch, merge into main, and remove the feature worktree. Use whenever the user gives the go-ahead to ship reviewed work ("commit and push", "push this", "ship it", "land it", "merge it"). Stops before deploying to delaybahn.com.
---

# Commit and push

The user has reviewed the work and wants it landed. Run the whole sequence without
asking for further confirmation — the go-ahead already covers every step below.

Stop before deploying. Deploying is a separate ask.

## 1. Locate the work

```bash
git worktree list
git status --short
```

Feature work lives in a worktree (`../db_delay_predictor-<slug>` on `feature/<slug>`);
small fixes are often straight on `main` in the primary checkout. Everything below
happens in whichever checkout holds the changes. If the work is already on `main`,
skip steps 5–7.

Commit only the files that belong to this change. Unrelated edits in the tree stay
uncommitted — never `git add -A` them along for the ride.

## 2. Bump the cache-busters (any change under `static/`)

Cloudflare edge-caches static assets and the service worker precaches the shell
cache-first, so a frontend change that skips this is invisible to returning users.
Three places, kept in step:

- `static/index.html` — `app.js?v=N` and, if the stylesheet changed, `style.css?v=N`
- `static/sw.js` — `SHELL_VERSION` (`v11` → `v12`), which drops the stale shell cache
- `static/sw.js` — the matching `PRECACHE` URLs, so they point at the new `?v=`

All three, or the bump does not take effect. Skip entirely for backend-only changes.

## 3. Update the docs (required — a hook enforces it)

A `PreToolUse` hook denies `git commit` unless `log.md` has uncommitted changes.
That is a policy, not an obstacle to route around:

- `log.md` — append a dated entry (append-only; never edit past entries). Match the
  existing style: what shipped and why, the busters used, what was verified and how,
  deliberate non-goals, and anything found but not fixed.
- `progress.md` — refresh the snapshot in place: current state, verified items,
  decisions, next candidates.
- `feature_list.md` — update statuses and known limitations if features changed.
- `README.md` — only if this change makes it factually wrong.

Stage the docs so they land in the same commit.

## 4. Commit

```bash
git add <the changed files> log.md progress.md
git commit -m "<imperative one-line summary>"
```

No `Co-Authored-By: Claude`, no "Generated with Claude Code" — never, in any form.

## 5. Push the feature branch

```bash
git push -u origin feature/<slug>
```

## 6. Merge into main from the primary checkout

Run this in `/home/stripathi/Documents/code_local/db_delay_predictor`. Stash any
unrelated local changes around the merge and restore them after:

```bash
git stash push -u -m premerge && STASHED=1
git merge --no-edit feature/<slug>
[ -n "$STASHED" ] && git stash pop
git push origin main
```

`git stash push` exits non-zero when there is nothing to stash, so `STASHED` stays
empty and the pop is skipped — it can never pop an unrelated stash.

## 7. Remove the worktree

Prefer the `ExitWorktree` tool. Otherwise:

```bash
git worktree remove ../db_delay_predictor-<slug>
```

The branch survives on origin; only the working directory goes away.

## 8. Report, do not deploy

State the merge commit on `main`, the buster versions that shipped, and that
delaybahn.com is still serving the old build. Then hand over the deploy commands
for the user to run themselves:

```bash
ssh root@delaybahn_hetzner
```

```bash
sudo -u stripathi -i bash -c '
cd /home/stripathi/Documents/code_local/db_delay_predictor
git stash push -u -m deploy-$(date +%s) && STASHED=1
git pull --no-rebase --no-edit https://github.com/sha2nkt/delay_bahn.git main
[ -n "$STASHED" ] && git stash pop
git log --oneline -1
'
systemctl restart delaybahn
systemctl is-active delaybahn
curl -sS https://delaybahn.com/health
```

Only run the deploy yourself if the user asks for it separately.
