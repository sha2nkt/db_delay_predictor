#!/bin/bash
# Cron watchdog for delaybahn.com: alerts via ntfy on state changes, and keeps
# re-paging while an upstream block stays unresolved (see check 2).
# Runs on ps083, outside the Hetzner box, so it catches server, tunnel and app
# failures alike. curl -4 because ps083 advertises broken IPv6.
#
# Two independent checks:
#   1. DOWN: /health unreachable / non-200 (as before).
#   2. UPSTREAM BLOCKED: bahn.de (Akamai) hard-blocking the server IP, detected
#      from /health's own counters: upstream_403 rising while upstream_200 stays
#      flat. During the 2026-08-19 OPS_BLOCKED incident the app stayed healthy
#      (circuit breaker + stale cache) and /health stayed 200, so neither the
#      systemd OnFailure hooks nor the old status-code check ever fired.
#      A block is not self-healing (it needs an IP move), so unlike check 1 it
#      keeps re-paging every BLOCK_REMIND_SECONDS until 200s return, carrying
#      the elapsed duration so the reminders read as one ongoing incident.
#
# Testing: DRY_RUN=1 prints alerts instead of sending; HEALTH_FILE=<path>
# feeds the detector a canned /health body instead of hitting the network;
# BLOCK_REMIND_SECONDS=0 makes every window re-page so the reminder path can
# be exercised without waiting.
#
# This repo copy is the canonical source; the installed copy is separate so
# branch switches in the dev checkout never change what cron runs. Install:
#   mkdir -p ~/.config/delaybahn
#   printf %s '<ntfy-topic>' > ~/.config/delaybahn/ntfy-topic
#   chmod 600 ~/.config/delaybahn/ntfy-topic
#   cp scripts/delaybahn-watchdog.sh ~/.local/bin/delaybahn-watchdog.sh
#   crontab: */5 * * * * $HOME/.local/bin/delaybahn-watchdog.sh
# An ntfy topic is a bearer secret: anyone who knows the name can read every
# alert and post convincing fake ones. This repo is public, so the topic is NOT
# stored here — put it in ~/.config/delaybahn/ntfy-topic (chmod 600) or pass
# NTFY_TOPIC in the environment.
TOPIC=${NTFY_TOPIC:-$(cat "${HOME}/.config/delaybahn/ntfy-topic" 2>/dev/null)}
STATE="${HOME}/.local/state/delaybahn-watchdog"
UPSTATE="${HOME}/.local/state/delaybahn-watchdog-upstream.json"
# how often an unresolved block re-pages; rounds up to the 5-min cron period
BLOCK_REMIND_SECONDS=${BLOCK_REMIND_SECONDS:-600}
# ssh alias, resolved in ~/.ssh/config — the origin IP is deliberately not
# recorded here: delaybahn.com is fronted by a Cloudflare tunnel, and naming
# the origin in a public repo would hand out a way around it
SERVER="delaybahn_hetzner2"
mkdir -p "$(dirname "$STATE")"

notify() {  # $1 title, $2 priority, $3 tags, $4 body
  if [ -n "$DRY_RUN" ]; then
    echo "NOTIFY [$2] $1 :: $4"
  elif [ -z "$TOPIC" ]; then
    # loud rather than silent: a watchdog that cannot page is worse than no
    # watchdog, because it still looks installed. cron mails stderr, and the
    # non-zero exit shows up in any job-level monitoring.
    echo "delaybahn-watchdog: no ntfy topic configured (~/.config/delaybahn/ntfy-topic or NTFY_TOPIC); DROPPED alert: $1 :: $4" >&2
    exit 78   # EX_CONFIG
  else
    curl -4 -s -m 10 -d "$4" -H "Title: $1" -H "Priority: $2" -H "Tags: $3" \
      "https://ntfy.sh/$TOPIC" >/dev/null
  fi
}

# ---------------------------------------------------------------- fetch
if [ -n "$HEALTH_FILE" ]; then
  body=$(cat "$HEALTH_FILE" 2>/dev/null); code=200
else
  body=$(curl -4 -s --max-time 20 https://delaybahn.com/health)
  code=$(curl -4 -s -o /dev/null -w '%{http_code}' --max-time 20 https://delaybahn.com/health)
  # one retry after 30s to ride out transient network/DNS flakes on this box
  if [ "$code" != "200" ]; then
    sleep 30
    body=$(curl -4 -s --max-time 20 https://delaybahn.com/health)
    code=$(curl -4 -s -o /dev/null -w '%{http_code}' --max-time 20 https://delaybahn.com/health)
  fi
fi

# ---------------------------------------------------------------- check 1: down
prev=$(cat "$STATE" 2>/dev/null || echo up)
if [ "$code" = "200" ]; then
  if [ "$prev" = "down" ]; then
    notify "delaybahn recovered" default white_check_mark \
      "delaybahn.com is back up (HTTP $code)."
  fi
  echo up > "$STATE"
else
  if [ "$prev" != "down" ]; then
    notify "delaybahn.com DOWN" urgent rotating_light \
      "delaybahn.com health check failed twice (HTTP $code). Check: ssh $SERVER 'systemctl status delaybahn cloudflared-delaybahn'"
  fi
  echo down > "$STATE"
  exit 0   # counters unavailable; nothing for check 2 to do
fi

# ------------------------------------------------- check 2: upstream hard block
# the body is handed over via the environment: python3 - reads its *program*
# from stdin, so piping the JSON in as well would silently starve json.load
verdict=$(HEALTH_BODY="$body" REMIND="$BLOCK_REMIND_SECONDS" python3 - "$UPSTATE" <<'PY'
import json, os, sys, time

state_path = sys.argv[1]
remind = int(os.environ.get("REMIND", "600"))
try:
    c = json.loads(os.environ["HEALTH_BODY"]).get("upstream", {}).get("counters", {})
except Exception:
    sys.exit(0)                     # odd body; availability is check 1's job
cur403, cur200 = c.get("upstream_403", 0), c.get("upstream_200", 0)

st = {}
if os.path.exists(state_path):
    try: st = json.load(open(state_path))
    except Exception: st = {}
prev403, prev200 = st.get("c403"), st.get("c200")
streak, blocked = st.get("streak", 0), st.get("blocked", False)
now = time.time()
# a state file written before reminders existed has neither key; anchoring both
# to now costs at most one reminder interval of silence, where defaulting to 0
# would fire a "blocked for 20323 min" reminder on the very first run
since = st.get("since") or now
last = st.get("last_alert") or now

if prev403 is not None:
    # counters reset when the app restarts (daily pipeline restart): a decrease
    # means they started over, so the current value IS the delta since restart
    d403 = cur403 if cur403 < prev403 else cur403 - prev403
    d200 = cur200 if cur200 < prev200 else cur200 - prev200
    bad  = d403 >= 3  and d200 == 0     # 403s and not a single success
    hard = d403 >= 20 and d200 == 0     # unambiguous, alert without waiting
    streak = streak + 1 if bad else 0
    if (streak >= 2 or hard) and not blocked:
        blocked, since, last = True, now, now
        print(f"BLOCKED {d403}")
    elif blocked and d200 > 0:
        blocked = False
        print(f"RECOVERED {d200}")
    elif blocked and now - last >= remind - 60:
        # Still blocked, still zero successes. The 60s of slack absorbs cron
        # jitter: without it a 600s reminder measured against a 300s cron that
        # fired a second late would slip to the third window, i.e. 15 min.
        # Deliberately also fires when d403 is 0 (no traffic, so no evidence
        # either way) — the block is unresolved until 200s actually return.
        last = now
        print(f"STILL {d403} {int((now - since) // 60)}")

json.dump({"c403": cur403, "c200": cur200, "streak": streak, "blocked": blocked,
           "since": since, "last_alert": last}, open(state_path, "w"))
PY
)

case "$verdict" in
  BLOCKED*)
    d403=${verdict#BLOCKED }
    notify "bahn.de is HARD-BLOCKING delaybahn" urgent "no_entry,rotating_light" \
      "Akamai is returning 403 to every bahn.de API call from the server IP ($d403 blocked, 0 succeeded in the last watchdog window). The site stays up on cached data but live journey search is dead. This is IP-scoped (same as 2026-08-19): verify with the curl-cffi probe from another IP, then plan an IP move. Check: curl -s https://delaybahn.com/health | python3 -m json.tool" ;;
  STILL*)
    read -r d403 mins <<<"${verdict#STILL }"
    notify "bahn.de STILL blocking delaybahn (${mins}m)" urgent "no_entry,rotating_light" \
      "Akamai has been 403ing every bahn.de API call from the server IP for ${mins} min ($d403 blocked, 0 succeeded in the last window). Live journey search is still dead; the site is serving cached data only. This does not clear on its own — it needs an IP move. Check: curl -s https://delaybahn.com/health | python3 -m json.tool" ;;
  RECOVERED*)
    notify "bahn.de unblocked delaybahn" default white_check_mark \
      "Upstream 200s are flowing again (${verdict#RECOVERED } in the last window). The Akamai block has lifted." ;;
esac
