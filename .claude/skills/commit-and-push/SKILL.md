---
name: commit-and-push
description: Land finished work in this repo — commit, push the branch, merge into main, remove the feature worktree, and deploy to delaybahn.com. Use whenever the user gives the go-ahead to ship reviewed work ("commit and push", "push this", "ship it", "land it", "merge it"). Ends with the change live and /health verified.
---

# Commit and push

The user has reviewed the work and wants it landed. Run the whole sequence without
asking for further confirmation — the go-ahead already covers every step below.

The sequence ends with the change live on delaybahn.com — the go-ahead covers the
deploy too (user decision, 2026-08-15).

If a step is denied by the permission classifier, stop and hand the remaining
commands to the user. Do not reach for a different command that reaches the same
end — a denial is the user's call, not an obstacle to route around.

Use `git -C <path>` rather than `cd <path> && git …`; a `cd` inside a compound
command trips a permission prompt of its own.

## 1. Locate the work

```bash
git worktree list
git status --short
```

Feature work lives in a worktree — `.claude/worktrees/<slug>` on `feature/<slug>`
when it was made with `EnterWorktree`, which is the preferred route, or
`../db_delay_predictor-<slug>` when it came from the fallback `git worktree add`.
Take the path from `git worktree list` rather than assuming either. Small fixes are
often straight on `main` in the primary checkout. Everything below happens in
whichever checkout holds the changes. If the work is already on `main`, skip
steps 5–7.

Commit only the files that belong to this change. Unrelated edits in the tree stay
uncommitted — never `git add -A` them along for the ride.

## 2. Bump the cache-busters (any change under `static/`)

Cloudflare edge-caches static assets and the service worker precaches the shell
cache-first, so a frontend change that skips this is invisible to returning users.
Three places, kept in step:

- `static/index.html` — `app.js?v=N` and, if the stylesheet changed, `style.css?v=N`
- `static/sw.js` — `SHELL_VERSION` (`v11` → `v12`), which drops the stale shell cache
- `static/sw.js` — the matching `PRECACHE` URLs, so they point at the new `?v=`

All of them, or the bump does not take effect — and note that a stylesheet change
makes it four sites, not three, since `style.css?v=N` appears in both files. Other
pages that pin `style.css?v=N` (stories, login, verify, impressum) move with it.
Skip entirely for backend-only changes.

Buster numbers are a global namespace across every session and branch, not a
per-branch counter: before picking the next number, check what production is
already serving (`curl -sI "https://delaybahn.com/style.css?v=N"` — a
`cf-cache-status: HIT` means N is taken) and go one past the highest number
anyone has used, not one past what your branch sees.

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

The gate matches on the raw command string, so *any* command whose text contains the
phrase it looks for is treated as a commit attempt — including a heredoc appending a
log entry that quotes it. Write the entry to a scratch file with the Write tool and
append that file instead.

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

Everything here targets `/home/stripathi/Documents/code_local/db_delay_predictor`.
Look at that tree before touching it:

```bash
git -C /home/stripathi/Documents/code_local/db_delay_predictor status --short
```

**Never `git stash push -u` here.** The `??` line you will almost always see is
`.claude/worktrees/` — the feature worktree itself, untracked from main's point of
view. `-u` sweeps it into the stash and the working directory vanishes out from
under the branch being merged. Untracked entries in this repo are not "unrelated
local changes"; they are usually the worktree.

If that status shows nothing but `??` lines, there is nothing to preserve. Skip the
stash and merge:

```bash
git -C <primary> merge --no-edit feature/<slug>
git -C <primary> push origin main
```

Only when it shows *modified tracked* files (`M`/`A`/`D` in either column) is a
stash needed. Run it as **one command** — shell variables do not survive between
tool calls, so a `STASHED` flag set in one call is empty in the next:

```bash
cd /home/stripathi/Documents/code_local/db_delay_predictor && \
git stash push -m premerge && \
git merge --no-edit feature/<slug> && \
git stash pop && \
git push origin main
```

Do not reintroduce an exit-code guard. `git stash push` exits **0** when there is
nothing to stash (it just prints "No local changes to save"), so `… && STASHED=1`
sets the flag either way and the later pop lands somebody else's stash on the tree.
An earlier version of this file asserted the opposite, and that is what silently
dropped staged files on 2026-08-14. The decision to stash comes from reading
`git status`, never from a return code.

### After the merge: burned busters

If the merge touched `?v=` pins or `SHELL_VERSION` and BOTH sides had bumped
them, identical values on the two sides are the *dangerous* case, not the safe
one — the merge auto-resolves them without a conflict, but the other branch may
already have deployed that number with different file content, and Cloudflare
then serves the old cached copy under the new build (2026-08-24: the Delay
Stories card shipped as unstyled blue links this way; hotfix 6ca652e). A number
is burned the moment any deploy has served it.

So after any such merge, before deploying: bump every colliding pin one further
(style + its six referencing pages, `SHELL_VERSION`, PRECACHE) in the merge
resolution or a follow-up commit. A skipped number costs nothing; a burned one
ships invisible CSS. After deploying, confirm the new URL actually carries the
change: `curl -s "https://delaybahn.com/style.css?v=<new>" | grep <new-selector>`
and check the deploy output's `Updating <old>..<new>` line — if the server's
`<old>` already contained the other branch's bump, that number was burned.

## 7. Remove the worktree

Kill anything still running out of it first — a dev server started there holds its
working directory, and the removal refuses while the checkout is busy. Kill the
process you started (by its port), not every `uvicorn` on the box; other sessions
run their own.

Prefer the `ExitWorktree` tool. Otherwise, with the path from step 1:

```bash
git worktree remove .claude/worktrees/<slug>
```

The branch survives on origin; only the working directory goes away. If the removal
objects to untracked files, that is the deliberate scaffolding — the `data` symlink,
`.env`, a `.venv` — and `--force` is the escape hatch, but read what it is about to
discard before reaching for it.

## 8. Deploy and verify

Run this only after `main` is pushed. If anything earlier was aborted or denied,
nothing new is on origin — stop and report instead of deploying the old build.

The whole deploy is one reviewed script — the only command pre-approved to reach
the server (user decision, 2026-08-15: a narrow rule on this exact invocation
instead of blanket ssh). Run it exactly like this, no arguments:

```bash
bash scripts/deploy.sh
```

It pulls main on the box as user stripathi (stashing and restoring any
server-side edits around the pull), restarts `delaybahn`, prints `is-active`,
and curls https://delaybahn.com/health through Cloudflare (IPv4-forced — this
dev box's IPv6 is broken). Do not inline these steps in ssh commands instead of
running the script, and do not edit the script to smuggle other remote actions
through its permission; any change to what "deploy" means must be a reviewed
diff of `scripts/deploy.sh` that the user asked for.

Check its output: the `deployed:` line must show the merge commit just pushed —
a pull that stopped on a conflict with server-side edits leaves the old build
running (report it, do not force anything on the box); `is-active` must print
`active`; `/health` must answer `"ok":true`. If any of the three is off, say so
immediately with the output — the ntfy watchdog will also fire, but the user
hears it from this session first.

## 9. Report

State the merge commit on `main`, the buster versions that shipped, and the deploy
verification: the commit `git log` showed on the box, `is-active`, and the `/health`
body. For frontend changes, note that returning visitors pick up the new shell on
their next load via the bumped `SHELL_VERSION`.
