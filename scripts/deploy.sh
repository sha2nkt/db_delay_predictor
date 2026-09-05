#!/usr/bin/env bash
# Deploy delaybahn.com: pull main on the Hetzner box, restart the service, verify.
# This is the only command Claude Code is pre-approved to run against the server
# (settings.local.json allows exactly "bash scripts/deploy.sh"), so every remote
# action must live in this reviewed, version-controlled file.
set -euo pipefail

ssh root@delaybahn_hetzner3 'sudo -u stripathi -i bash -s' <<'EOF'
set -euo pipefail
cd /home/stripathi/Documents/code_local/db_delay_predictor
STASHED=
git diff --quiet && git diff --cached --quiet || { git stash push -m deploy-$(date +%s) && STASHED=1; }
git pull --no-rebase --no-edit https://github.com/sha2nkt/delay_bahn.git main
# The DE pipeline step reaches into the deutsche-bahn-data submodule for
# scripts.create_monthly_data_release (pipeline/fchg_parse.py puts it on sys.path).
# A plain `git pull` moves the gitlink but never checks the submodule out, and a
# clone made without --recurse-submodules leaves the directory empty altogether --
# which is how the German build died at import on every nightly run from
# 2026-08-19 to 08-25 while the other five countries kept /health and the row
# count green. No-op once the submodule is already at the recorded commit.
git submodule update --init --recursive
# Fail the deploy rather than hand the 05:30 timer a checkout whose DE step will
# crash: the whole point of this incident was that the breakage was silent.
test -f deutsche-bahn-data/scripts/create_monthly_data_release.py || {
  echo "deploy aborted: deutsche-bahn-data submodule is empty after update" >&2
  exit 1
}
if [ -n "$STASHED" ]; then git stash pop; fi
# Dependencies before the restart, never after: a commit that adds one (and
# `set -e` here) must abort the deploy rather than restart into an app whose
# imports fail. A no-op when the lockfile hasn't moved.
uv sync --frozen
echo "deployed: $(git log --oneline -1)"
EOF

ssh root@delaybahn_hetzner3 'systemctl restart delaybahn && systemctl is-active delaybahn'
# -4: this dev box (ps083) has broken IPv6; the check must go through Cloudflare.
# --retry rides out the restart race (502 until uvicorn binds); -f turns a health
# check that still fails after that into a nonzero exit instead of a silent pass.
curl -4 -sS -f --retry 6 --retry-delay 3 --max-time 20 https://delaybahn.com/health
echo
