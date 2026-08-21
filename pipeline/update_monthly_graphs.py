"""Refresh the homepage delay graphs to the two most recent complete months.

Downloads the needed monthly parquet releases from the HuggingFace dataset
piebro/deutsche-bahn-data (skipped when already on disk), regenerates the four
homepage SVGs, and rewrites the month-dependent strings and cache-busters in
static/app.js, static/index.html and static/sw.js. Exits quietly when the
homepage already shows the target months, so it is safe to run daily from cron
at the start of each month until the new release appears upstream.

With --push it additionally appends a log.md entry, commits the touched files,
pushes main, deploys to delaybahn.com over ssh and verifies /health. In --push
mode failures (and the one success per month) are reported via ntfy on the same
topic as the watchdog. Without --push nothing touches git — review the working
tree and ship manually.
"""

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path

from month_utils import MONTHS_DE, MONTHS_EN, default_months, name_de, name_en, pair_de, pair_en, range_de, range_en

PROJECT_ROOT = Path(__file__).resolve().parent.parent
STATIC = PROJECT_ROOT / "static"
SVG_NAMES = ["delay-correlation.svg", "delay-correlation-en.svg", "delay-violin.svg", "delay-violin-en.svg"]
DE = "|".join(MONTHS_DE)
EN = "|".join(MONTHS_EN)

DEFAULT_X, DEFAULT_Y = default_months()


def _default_ntfy_topic() -> str:
    """An ntfy topic is a bearer secret — knowing the name is enough to read the
    alerts and to post fake ones — and this repo is public, so it is never
    hardcoded here. Falls back to empty, which disables the notifications rather
    than failing a run that is otherwise fine."""
    env = os.environ.get("NTFY_TOPIC", "").strip()
    if env:
        return env
    try:
        return (Path.home() / ".config" / "delaybahn" / "ntfy-topic").read_text().strip()
    except OSError:
        return ""


parser = argparse.ArgumentParser()
parser.add_argument("--month-x", default=DEFAULT_X, help="earlier month, YYYY-MM")
parser.add_argument("--month-y", default=DEFAULT_Y, help="later month, YYYY-MM")
parser.add_argument("--push", action="store_true", help="commit, push main and deploy to delaybahn.com")
parser.add_argument("--grace-days", type=int, default=12,
                    help="through this day of the month, a release missing upstream exits 0 (cron retries next day)")
parser.add_argument("--ntfy-topic", default=_default_ntfy_topic(),
                    help="ntfy topic for --push failures/success; empty string disables. "
                         "Defaults to $NTFY_TOPIC, else ~/.config/delaybahn/ntfy-topic")
args = parser.parse_args()


def run(cmd, **kw):
    """Run a command, echo it, fail loudly with its output in the exception."""
    print("+", " ".join(str(c) for c in cmd))
    res = subprocess.run(cmd, cwd=PROJECT_ROOT, text=True, capture_output=True, **kw)
    if res.stdout:
        print(res.stdout.rstrip())
    if res.returncode != 0:
        raise RuntimeError(f"{cmd[0]} failed ({res.returncode}): {res.stderr.strip() or res.stdout.strip()}")
    return res.stdout


def ntfy(title, message, priority=None, tags=None):
    if not (args.push and args.ntfy_topic):
        return
    cmd = ["curl", "-4", "-s", "-m", "10", "-d", message, "-H", f"Title: {title}"]
    if priority:
        cmd += ["-H", f"Priority: {priority}"]
    if tags:
        cmd += ["-H", f"Tags: {tags}"]
    subprocess.run(cmd + [f"https://ntfy.sh/{args.ntfy_topic}"], capture_output=True)


def sub_once(text, pattern, repl, label):
    """Regex-replace exactly one occurrence; anything else means the prose or
    markup drifted and needs a human, so fail loudly rather than half-patch."""
    new, n = re.subn(pattern, repl, text)
    if n != 1:
        raise RuntimeError(f"expected exactly 1 match for {label}, found {n} — file layout changed?")
    return new


def bump(text, pattern, label):
    return sub_once(text, pattern, lambda m: f"{m.group(1)}{int(m.group(2)) + 1}", label)


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def fetch_data():
    """Download missing parquets; None (quiet retry-tomorrow exit) while the
    upstream release may legitimately not exist yet."""
    from huggingface_hub import hf_hub_download

    for month in (args.month_x, args.month_y):
        target = PROJECT_ROOT / "data" / "monthly_processed_data" / f"data-{month}.parquet"
        if target.exists():
            continue
        try:
            hf_hub_download("piebro/deutsche-bahn-data", f"monthly_processed_data/data-{month}.parquet",
                            repo_type="dataset", local_dir=PROJECT_ROOT / "data")
        except Exception as e:
            if dt.date.today().day <= args.grace_days:
                print(f"data-{month}.parquet not available yet ({type(e).__name__}); retrying on a later run")
                return None
            raise RuntimeError(f"data-{month}.parquet still unavailable after day {args.grace_days}: {e}") from e
    return True


def patch_texts(n_trains):
    """Rewrite every month-dependent string; returns the new app.js version."""
    x, y = args.month_x, args.month_y
    n_de, n_en = f"{n_trains:,}".replace(",", "."), f"{n_trains:,}"
    pd, pe = pair_de(x, y), pair_en(x, y)
    im_de = rf"im (?:{DE}) (?:\d{{4}} )?und (?:{DE}) \d{{4}}"
    across_en = rf"(?:{EN}) (?:\d{{4}} )?and (?:{EN}) \d{{4}}"

    app = (STATIC / "app.js").read_text()
    app = sub_once(app, rf'(heroSubScope: ")[\d.]+ Züge {im_de} verglichen:',
                   rf'\g<1>{n_de} Züge im {pd} verglichen:', "app.js heroSubScope (de)")
    app = sub_once(app, rf"Wer im (?:{DE}) zu spät kam, kam auch im (?:{DE}) zu spät\.",
                   f"Wer im {name_de(x)} zu spät kam, kam auch im {name_de(y)} zu spät.", "app.js heroSubFinding (de)")
    app = sub_once(app, rf"Züge, die im (?:{DE}) verspätet waren, waren es auch im (?:{DE})\.",
                   f"Züge, die im {name_de(x)} verspätet waren, waren es auch im {name_de(y)}.", "app.js chartAlt (de)")
    app = sub_once(app, rf"gruppiert nach ihrer (?:{DE})-Verspätung, zeigen im (?:{DE}) dieselbe Rangfolge\.",
                   f"gruppiert nach ihrer {name_de(x)}-Verspätung, zeigen im {name_de(y)} dieselbe Rangfolge.",
                   "app.js violinAlt (de)")
    app = sub_once(app, rf'(heroSubScope: ")[\d,]+ trains compared across {across_en}',
                   rf'\g<1>{n_en} trains compared across {pe}', "app.js heroSubScope (en)")
    app = sub_once(app, rf"the ones that ran late in (?:{EN}) ran late again in (?:{EN})\.",
                   f"the ones that ran late in {name_en(x)} ran late again in {name_en(y)}.", "app.js heroSubFinding (en)")
    app = sub_once(app, rf"trains that ran late in (?:{EN}) also ran late in (?:{EN})\.",
                   f"trains that ran late in {name_en(x)} also ran late in {name_en(y)}.", "app.js chartAlt (en)")
    app = sub_once(app, rf"trains grouped by their (?:{EN}) delay show the same ranking in (?:{EN})\.",
                   f"trains grouped by their {name_en(x)} delay show the same ranking in {name_en(y)}.",
                   "app.js violinAlt (en)")
    for svg in SVG_NAMES:
        app = bump(app, rf'("{re.escape(svg)}\?v=)(\d+)', f"app.js buster {svg}")
    (STATIC / "app.js").write_text(app)

    html = (STATIC / "index.html").read_text()
    html = sub_once(html, rf'(data-i18n="heroSubScope">)[\d.]+ Züge {im_de} verglichen<',
                    rf'\g<1>{n_de} Züge im {pd} verglichen<', "index.html heroSubScope fallback")
    html = sub_once(html, rf"Wer im (?:{DE}) zu spät kam, kam auch im (?:{DE}) zu spät\.",
                    f"Wer im {name_de(x)} zu spät kam, kam auch im {name_de(y)} zu spät.",
                    "index.html heroSubFinding fallback")
    html = bump(html, r"(app\.js\?v=)(\d+)", "index.html app.js buster")
    new_v = re.search(r"app\.js\?v=(\d+)", html).group(1)
    (STATIC / "index.html").write_text(html)

    sw = (STATIC / "sw.js").read_text()
    sw = bump(sw, r"(/app\.js\?v=)(\d+)", "sw.js PRECACHE app.js buster")
    sw = bump(sw, r'(SHELL_VERSION = "v)(\d+)', "sw.js SHELL_VERSION")
    (STATIC / "sw.js").write_text(sw)
    return new_v


def commit_and_deploy(n_trains, app_v):
    files = [f"static/{n}" for n in SVG_NAMES] + ["static/app.js", "static/index.html", "static/sw.js", "log.md"]
    entry = (
        f"\n## {dt.date.today().isoformat()} — Automated monthly graph refresh ({range_en(args.month_x, args.month_y)})\n\n"
        f"- pipeline/update_monthly_graphs.py (cron): homepage scatter + violin SVGs regenerated from "
        f"data-{args.month_x} and data-{args.month_y} parquets; {n_trains:,} qualifying trains.\n"
        f"- Month strings and train count patched in app.js, index.html fallbacks; SVG busters, app.js?v={app_v} "
        f"and sw.js SHELL_VERSION bumped.\n"
        f"- Deployed to delaybahn.com and /health verified by the same run.\n"
    )
    with (PROJECT_ROOT / "log.md").open("a") as f:
        f.write(entry)

    run(["git", "add"] + files)
    run(["git", "commit", "-m", f"Update homepage graphs to {range_en(args.month_x, args.month_y)}"])
    run(["git", "push", "origin", "main"])
    local_head = run(["git", "rev-parse", "--short", "HEAD"]).strip()

    # mirror the manual deploy flow: stash-guarded pull as stripathi, then restart
    pull_script = (
        "cd /home/stripathi/Documents/code_local/db_delay_predictor\n"
        "STASHED=\n"
        "git diff --quiet && git diff --cached --quiet || { git stash push -m deploy-$(date +%s) && STASHED=1; }\n"
        "git pull --no-rebase --no-edit https://github.com/sha2nkt/delay_bahn.git main\n"
        '[ -n "$STASHED" ] && git stash pop\n'
        "git log --oneline -1\n"
    )
    out = run(["ssh", "-o", "BatchMode=yes", "root@delaybahn_hetzner3", "sudo -u stripathi -i bash -s"],
              input=pull_script)
    if local_head not in out:
        raise RuntimeError(f"server HEAD after pull does not match pushed commit {local_head}: {out.strip()}")
    out = run(["ssh", "-o", "BatchMode=yes", "root@delaybahn_hetzner3",
               "systemctl restart delaybahn && systemctl is-active delaybahn"])
    if "active" not in out:
        raise RuntimeError(f"delaybahn service not active after restart: {out.strip()}")
    health = run(["curl", "-4", "-sS", "--max-time", "20", "https://delaybahn.com/health"])
    if '"ok":true' not in health.replace(" ", ""):
        raise RuntimeError(f"health check failed: {health.strip()}")


def main():
    if args.push:
        branch = run(["git", "rev-parse", "--abbrev-ref", "HEAD"]).strip()
        if branch != "main":
            raise RuntimeError(f"refusing to auto-push from branch {branch!r}")
        dirty = run(["git", "status", "--porcelain", "--", "static", "log.md"]).strip()
        if dirty:
            raise RuntimeError(f"uncommitted local changes under static/ or log.md; refusing to touch them:\n{dirty}")
        run(["git", "pull", "--ff-only", "origin", "main"])

    if pair_de(args.month_x, args.month_y) in (STATIC / "app.js").read_text():
        print(f"homepage already shows {range_en(args.month_x, args.month_y)}; nothing to do")
        return

    if fetch_data() is None:
        return

    before = {n: sha(STATIC / n) for n in SVG_NAMES}
    meta_path = PROJECT_ROOT / "data" / "graph_meta.json"
    common = ["--month-x", args.month_x, "--month-y", args.month_y]
    run([sys.executable, str(PROJECT_ROOT / "pipeline" / "make_delay_scatter.py"), *common,
         "--meta-out", str(meta_path)])
    run([sys.executable, str(PROJECT_ROOT / "pipeline" / "make_delay_violin.py"), *common])
    unchanged = [n for n in SVG_NAMES if sha(STATIC / n) == before[n]]
    if unchanged:
        raise RuntimeError(f"SVGs did not change for {unchanged} — generator no longer writes them?")

    n_trains = json.loads(meta_path.read_text())["n_trains"]
    app_v = patch_texts(n_trains)
    print(f"graphs and texts now show {range_en(args.month_x, args.month_y)} ({n_trains:,} trains), app.js v{app_v}")

    if args.push:
        commit_and_deploy(n_trains, app_v)
        ntfy("delaybahn graphs updated",
             f"Homepage now compares {range_en(args.month_x, args.month_y)} ({n_trains:,} trains). "
             "Deployed, /health ok.", tags="bar_chart")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        ntfy("delaybahn graph update FAILED", f"{e}\n"
             "Fix and re-run: uv run pipeline/update_monthly_graphs.py --push",
             priority="urgent", tags="rotating_light")
        raise
