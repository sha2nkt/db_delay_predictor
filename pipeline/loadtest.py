"""Load-test driver for the delaybahn web app.

Models real visitor sessions rather than hammering one URL: a session loads the page,
maybe types two station names (250ms debounce, as the frontend does), maybe searches,
maybe pages or switches to past mode. Arrivals are an open-loop Poisson process, so a
saturated server produces a growing queue instead of silently throttling the client the
way a fixed-concurrency loop would.

Point this at pipeline/loadtest_stub.py on a spare port, never at the real app: a run
against app.main sends thousands of requests to bahn.de and writes a row into Umami's
postgres per session.

Record fixtures once (makes ~25 real bahn.de calls):
    uv run python pipeline/loadtest.py --record

Then, with the stub running on :8001:
    uv run python pipeline/loadtest.py --rate 5 --duration 120
"""

import argparse
import asyncio
import json
import itertools
import multiprocessing as mp
import os
import re
import statistics
import subprocess
import sys
import threading
import time
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

import httpx
import numpy as np

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUT = PROJECT_ROOT / "data" / "loadtest"

# exactly what static/index.html pulls on first paint; the 123KB chart SVGs are not
# here because updateChartImg() only sets img.src after a toggle click
PAGE_ASSETS = [
    "/",
    "/style.css?v=27",
    "/app.js?v=48",
    "/logo.png?v=2",
    "/favicon.png",
    "/stats/script.js",
]
CHART_SVGS = ["/delay-correlation.svg", "/delay-violin.svg"]

# high-transfer routes, so recorded journeys have 5-7 legs and exercise the expensive shape
RECORD_ROUTES = [
    ("Berlin Hbf", "München Hbf"),
    ("Aachen Hbf", "Rostock Hbf"),
    ("Hamburg Hbf", "Stuttgart Hbf"),
    ("Köln Hbf", "Dresden Hbf"),
    ("Frankfurt(Main)Hbf", "Kiel Hbf"),
    ("Nürnberg Hbf", "Bremen Hbf"),
]

CLK_TCK = os.sysconf("SC_CLK_TCK")


# ---------------------------------------------------------------- record mode


def _parquet_coverage() -> tuple[str, str]:
    """Min/max covered day, straight from the parquet so recording needs no running server."""
    import duckdb

    parquet = PROJECT_ROOT / "data" / "delays.parquet"
    lo, hi = duckdb.connect().execute(
        "SELECT min(CAST(arrival_planned_time AS DATE)), max(CAST(arrival_planned_time AS DATE))"
        f" FROM read_parquet('{parquet}') WHERE arrival_planned_time IS NOT NULL"
    ).fetchone()
    return lo.isoformat(), hi.isoformat()


async def record(args) -> int:
    sys.path.insert(0, str(PROJECT_ROOT))
    from app import bahn_api

    fixtures = args.out / "fixtures"
    journeys_dir = fixtures / "journeys"
    journeys_dir.mkdir(parents=True, exist_ok=True)

    _, max_day = _parquet_coverage()
    future = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%dT08:00:00")
    past = f"{max_day}T08:00:00"
    print(f"recording: future={future} past={past}", flush=True)

    locations_path = fixtures / "locations.json"
    locations = json.loads(locations_path.read_text()) if locations_path.exists() else {}
    routes, failures = [], 0

    for origin, dest in RECORD_ROUTES:
        try:
            resolved = {}
            for name in (origin, dest):
                key = name.strip().lower()
                if key not in locations:
                    locations[key] = await bahn_api.locations(name)
                hits = locations[key]
                if not hits:
                    raise RuntimeError(f"no location match for {name!r}")
                resolved[name] = hits[0]

            src, dst = resolved[origin], resolved[dest]
            route = {
                "from_id": src["id"], "from_name": src["name"], "from_ext": src["extId"],
                "to_id": dst["id"], "to_name": dst["name"], "to_ext": dst["extId"],
            }

            for departure in (future, past):
                slug = re.sub(r"\W+", "-", f"{origin}-{dest}-{departure[:10]}").strip("-").lower()
                target = journeys_dir / f"{slug}.json"
                if target.exists() and not args.force:
                    continue
                data = await bahn_api.journeys(src["id"], dst["id"], departure)
                target.write_text(json.dumps({
                    "request": {"from_id": src["id"], "to_id": dst["id"],
                                "departure": departure, "paging_ref": None},
                    "response": data,
                }))
                legs = [len(v.get("verbindungsAbschnitte", [])) for v in data.get("verbindungen", [])]
                print(f"  {origin} -> {dest} {departure[:10]}: "
                      f"{len(data.get('verbindungen', []))} journeys, legs={legs}", flush=True)

            routes.append(route)
        except Exception as e:  # one dead route must not abort the capture
            failures += 1
            print(f"{origin} -> {dest}: FAILED ({e})", file=sys.stderr, flush=True)

    # autocomplete prefixes the driver will replay, recorded so misses don't hit bahn.de
    for origin, dest in RECORD_ROUTES:
        for name in (origin, dest):
            for n in (2, 3, 5):
                prefix = name[:n].strip()
                key = prefix.lower()
                if key in locations:
                    continue
                try:
                    locations[key] = await bahn_api.locations(prefix)
                except Exception as e:
                    failures += 1
                    print(f"prefix {prefix!r}: FAILED ({e})", file=sys.stderr, flush=True)

    locations_path.write_text(json.dumps(locations))
    (fixtures / "routes.json").write_text(json.dumps(routes, indent=2))
    await bahn_api.close()

    print(f"\nwrote {len(routes)} routes, {len(list(journeys_dir.glob('*.json')))} journey fixtures, "
          f"{len(locations)} location queries to {fixtures} ({failures} failures)")
    return 1 if not routes else 0


# ---------------------------------------------------------------- host sampler


def _pid_for_port(port: int) -> int | None:
    try:
        out = subprocess.run(["ss", "-tlnp"], capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    for line in out.splitlines():
        if f":{port} " in line:
            m = re.search(r"pid=(\d+)", line)
            if m:
                return int(m.group(1))
    return None


class HostSampler(threading.Thread):
    """1Hz /proc sampling of the server under test. CPU is reported as a percentage of a
    single core, because uvicorn runs one process -- 100% means saturated, and it cannot
    go higher no matter how many cores the box has."""

    def __init__(self, pid: int):
        super().__init__(daemon=True)
        self.pid = pid
        self.samples: list[dict] = []
        self._done = threading.Event()

    def _read(self) -> dict | None:
        try:
            stat = Path(f"/proc/{self.pid}/stat").read_text().rsplit(") ", 1)[1].split()
            status = Path(f"/proc/{self.pid}/status").read_text()
        except (OSError, IndexError):
            return None
        rss = re.search(r"VmRSS:\s+(\d+) kB", status)
        threads = re.search(r"Threads:\s+(\d+)", status)
        return {
            "t": time.monotonic(),
            "cpu_s": (int(stat[11]) + int(stat[12])) / CLK_TCK,  # utime + stime
            "rss_mb": int(rss.group(1)) / 1024 if rss else None,
            "threads": int(threads.group(1)) if threads else None,
        }

    def run(self):
        while not self._done.is_set():
            s = self._read()
            if s:
                self.samples.append(s)
            self._done.wait(1.0)

    def stop(self) -> dict:
        self._done.set()
        self.join(timeout=3)
        if len(self.samples) < 2:
            return {}
        first, last = self.samples[0], self.samples[-1]
        span = last["t"] - first["t"]
        rss = [s["rss_mb"] for s in self.samples if s["rss_mb"] is not None]
        ts = [s["t"] - first["t"] for s in self.samples if s["rss_mb"] is not None]
        slope = float(np.polyfit(ts, rss, 1)[0]) * 60 if len(rss) > 2 else 0.0
        return {
            "cpuPctOfOneCore": round(100 * (last["cpu_s"] - first["cpu_s"]) / span, 1) if span else None,
            "rssStartMb": round(rss[0], 1) if rss else None,
            "rssEndMb": round(rss[-1], 1) if rss else None,
            "rssSlopeMbPerMin": round(slope, 2),
            "threadsMax": max((s["threads"] or 0) for s in self.samples),
            "samples": len(self.samples),
        }


# ---------------------------------------------------------------- load driver


class Recorder:
    def __init__(self):
        self.samples: dict[str, list[float]] = defaultdict(list)
        self.errors: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))

    def ok(self, category: str, ms: float):
        self.samples[category].append(ms)

    def fail(self, category: str, label: str):
        self.errors[category][label] += 1

    def merge(self, other: dict):
        for cat, vals in other["samples"].items():
            self.samples[cat].extend(vals)
        for cat, errs in other["errors"].items():
            for label, n in errs.items():
                self.errors[cat][label] += n

    def as_dict(self) -> dict:
        return {
            "samples": {k: v for k, v in self.samples.items()},
            "errors": {k: dict(v) for k, v in self.errors.items()},
        }


async def _hit(client: httpx.AsyncClient, rec: Recorder, category: str, path: str, method: str = "GET", **kw):
    start = time.perf_counter()
    try:
        resp = await client.request(method, path, **kw)
        ms = (time.perf_counter() - start) * 1000
        if resp.status_code >= 400:
            rec.fail(category, f"http_{resp.status_code}")
        else:
            rec.ok(category, ms)
        return resp
    except Exception as e:
        rec.fail(category, type(e).__name__)
        return None


def _departure(rng, args, route_idx: int, past: bool) -> str:
    """Search departure time. --unique-routes gives every session a distinct minute so
    nothing hits the bahn.de LRU or the delays caches -- the honest cold number."""
    if past:
        base = datetime.fromisoformat(args.past_day + "T08:00:00")
    else:
        base = datetime.now().replace(second=0, microsecond=0) + timedelta(days=1)
    if args.unique_routes:
        base += timedelta(minutes=int(rng.integers(0, 100000)))
    else:
        base += timedelta(minutes=int(rng.integers(0, 12)) * 10)
    return base.strftime("%Y-%m-%dT%H:%M:00")


_variant_seq = itertools.count()


async def _session(client: httpx.AsyncClient, rec: Recorder, rng, args, routes):
    """One simulated visitor, following static/app.js's actual request pattern."""
    # Tagging the session makes the stub return an itinerary built from different real
    # trains, so delays._cache misses. Without it a run measures a warm cache: only 12
    # fixtures exist and their trains are all resident within seconds.
    headers = {"X-Loadtest-Variant": str(next(_variant_seq))} if args.unique_routes else {}
    if args.mix in ("full", "static"):
        await asyncio.gather(*(_hit(client, rec, "static", p) for p in PAGE_ASSETS))
        if rng.random() < 0.95:
            await _hit(client, rec, "stats", "/stats/api/send", method="POST",
                       json={"type": "event", "payload": {"website": "loadtest", "url": "/"}})
    if args.mix == "static":
        return

    route = routes[int(rng.integers(0, len(routes)))]

    if args.mix == "full":
        await asyncio.sleep(1.5)  # reading the page before typing
        if rng.random() < 0.45:
            for name in (route["from_name"], route["to_name"]):
                for n in (2, 3, 5):
                    await _hit(client, rec, "locations", "/api/locations", params={"query": name[:n].strip()}, headers=headers)
                    await asyncio.sleep(0.25)  # frontend debounce
        else:
            return

    past = rng.random() < 0.08
    if past:
        await _hit(client, rec, "coverage", "/api/coverage")

    params = {
        "from": route["from_id"],
        "to": route["to_id"],
        "departure": _departure(rng, args, 0, past),
        "window": 7,
        "mode": "past" if past else "future",
    }
    category = "journeys_past" if past else "journeys_future"
    await _hit(client, rec, category, "/api/journeys", params=params, headers=headers)

    if args.mix != "full":
        return

    if rng.random() < 0.12:  # window switch: different cache key, full recompute
        await _hit(client, rec, "journeys_future", "/api/journeys", params={**params, "window": 30}, headers=headers)
    if rng.random() < 0.10:
        await asyncio.sleep(2.0)
        await _hit(client, rec, "journeys_future", "/api/journeys", params=params, headers=headers)
    if rng.random() < 0.15:
        await _hit(client, rec, "static", CHART_SVGS[int(rng.integers(0, len(CHART_SVGS)))])


async def _canary(client: httpx.AsyncClient, rec: Recorder, stop: asyncio.Event):
    """/api/coverage is ~0.3ms of pure Python with no I/O, so it can only be slow when
    something else is holding the event loop. Its p99 is the event-loop lag measurement."""
    while not stop.is_set():
        await _hit(client, rec, "canary", "/api/coverage")
        try:
            await asyncio.wait_for(stop.wait(), timeout=0.5)
        except asyncio.TimeoutError:
            pass


async def _drive(args, routes, seed: int, rate: float) -> dict:
    rec = Recorder()
    rng = np.random.default_rng(seed)
    limits = httpx.Limits(max_connections=args.max_connections, max_keepalive_connections=args.max_connections)
    async with httpx.AsyncClient(base_url=args.base_url, timeout=args.timeout, limits=limits) as client:
        stop = asyncio.Event()
        canary = asyncio.create_task(_canary(client, rec, stop)) if args.canary else None

        end = time.monotonic() + args.duration
        live: set[asyncio.Task] = set()

        if args.model == "open":
            while time.monotonic() < end:
                await asyncio.sleep(float(rng.exponential(1.0 / rate)))
                t = asyncio.create_task(_session(client, rec, rng, args, routes))
                live.add(t)
                t.add_done_callback(live.discard)
        else:
            async def worker(wid: int):
                wrng = np.random.default_rng(seed + 1000 + wid)
                while time.monotonic() < end:
                    await _session(client, rec, wrng, args, routes)
            live = {asyncio.create_task(worker(i)) for i in range(int(args.concurrency))}

        if live:
            await asyncio.gather(*live, return_exceptions=True)
        if canary:
            stop.set()
            await asyncio.gather(canary, return_exceptions=True)

    return rec.as_dict()


def _worker_entry(args, routes, seed, rate, queue):
    queue.put(asyncio.run(_drive(args, routes, seed, rate)))


# ---------------------------------------------------------------- reporting


def _percentiles(vals: list[float]) -> dict:
    s = sorted(vals)
    def q(p):
        return round(s[min(len(s) - 1, int(len(s) * p))], 1)
    return {"n": len(s), "p50": q(0.50), "p90": q(0.90), "p95": q(0.95),
            "p99": q(0.99), "max": round(s[-1], 1)}


PASS_CRITERIA = {
    "canary": ("p99", 250),
    "journeys_future": ("p95", 800),
    "journeys_past": ("p95", 800),
    "static": ("p99", 300),
}


def _report(rec: Recorder, host: dict, duration: float, args) -> dict:
    per_endpoint, breaches = {}, []
    for cat in sorted(set(rec.samples) | set(rec.errors)):
        vals = rec.samples.get(cat, [])
        errs = dict(rec.errors.get(cat, {}))
        entry = {"errors": errs, "throughputPerSec": round((len(vals) + sum(errs.values())) / duration, 2)}
        if vals:
            entry.update(_percentiles(vals))
        per_endpoint[cat] = entry
        if cat in PASS_CRITERIA and vals:
            metric, limit = PASS_CRITERIA[cat]
            if entry[metric] > limit:
                breaches.append(f"{cat} {metric}={entry[metric]}ms > {limit}ms")

    total_ok = sum(len(v) for v in rec.samples.values())
    total_err = sum(sum(e.values()) for e in rec.errors.values())
    error_rate = total_err / max(1, total_ok + total_err)
    if error_rate > 0.005:
        breaches.append(f"error rate {error_rate:.2%} > 0.50%")
    if host.get("cpuPctOfOneCore") and host["cpuPctOfOneCore"] > 80:
        breaches.append(f"cpu {host['cpuPctOfOneCore']}% of one core > 80%")

    print(f"\n{'endpoint':18} {'n':>7} {'rps':>7} {'p50':>8} {'p95':>8} {'p99':>8} {'max':>9}  errors")
    print("-" * 88)
    for cat, e in per_endpoint.items():
        if "p50" in e:
            print(f"{cat:18} {e['n']:>7} {e['throughputPerSec']:>7} {e['p50']:>8} {e['p95']:>8} "
                  f"{e['p99']:>8} {e['max']:>9}  {e['errors'] or ''}")
        else:
            print(f"{cat:18} {'-':>7} {e['throughputPerSec']:>7} {'-':>8} {'-':>8} {'-':>8} {'-':>9}  {e['errors']}")

    if host:
        print(f"\nserver  cpu={host.get('cpuPctOfOneCore')}% of one core   "
              f"rss {host.get('rssStartMb')} -> {host.get('rssEndMb')} MB "
              f"(slope {host.get('rssSlopeMbPerMin')} MB/min)   threads_max={host.get('threadsMax')}")
    print(f"total   {total_ok} ok, {total_err} failed ({error_rate:.2%})")
    print("\nVERDICT: " + ("PASS" if not breaches else "FAIL\n  - " + "\n  - ".join(breaches)))

    return {
        "config": {k: (str(v) if isinstance(v, Path) else v) for k, v in vars(args).items()},
        "durationSec": round(duration, 1),
        "perEndpoint": per_endpoint,
        "host": host,
        "totals": {"ok": total_ok, "failed": total_err, "errorRate": round(error_rate, 5)},
        "breaches": breaches,
        "verdict": "PASS" if not breaches else "FAIL",
    }


# ---------------------------------------------------------------- entry point


def main():
    p = argparse.ArgumentParser(description="Load-test the delaybahn app against recorded upstream fixtures.")
    p.add_argument("--record", action="store_true", help="capture bahn.de fixtures instead of running a load test (makes real upstream calls)")
    p.add_argument("--force", action="store_true", help="re-record fixtures that already exist")
    p.add_argument("--base-url", default="http://127.0.0.1:8001", help="server under test (default: http://127.0.0.1:8001)")
    p.add_argument("--rate", type=float, default=5.0, help="sessions per second, open model (default: 5.0)")
    p.add_argument("--concurrency", type=int, default=10, help="parallel sessions, closed model only (default: 10)")
    p.add_argument("--duration", type=float, default=120, help="seconds to run (default: 120)")
    p.add_argument("--model", choices=["open", "closed"], default="open", help="arrival process; open = Poisson, queues up when saturated (default: open)")
    p.add_argument("--mix", choices=["full", "static", "journeys"], default="full", help="traffic profile (default: full)")
    p.add_argument("--unique-routes", action="store_true", help="give every session a distinct departure minute so no cache is hit")
    p.add_argument("--workers", type=int, default=1, help="client processes to fan out across (default: 1)")
    p.add_argument("--max-connections", type=int, default=200, help="client connection pool size (default: 200)")
    p.add_argument("--timeout", type=float, default=30, help="per-request timeout in seconds (default: 30)")
    p.add_argument("--no-canary", dest="canary", action="store_false", help="skip the /api/coverage event-loop canary")
    p.add_argument("--target-pid", type=int, default=None, help="server pid for /proc sampling (default: resolved from the base-url port)")
    p.add_argument("--past-day", default=None, help="past-mode date, must be inside parquet coverage (default: the parquet max day)")
    p.add_argument("--seed", type=int, default=42, help="random seed (default: 42)")
    p.add_argument("--out", type=Path, default=DEFAULT_OUT, help=f"artifact directory (default: {DEFAULT_OUT})")
    p.add_argument("--label", default=None, help="label embedded in the artifact filename (default: none)")
    args = p.parse_args()

    args.out.mkdir(parents=True, exist_ok=True)

    if args.record:
        sys.exit(asyncio.run(record(args)))

    routes_path = args.out / "fixtures" / "routes.json"
    if not routes_path.exists():
        sys.exit(f"{routes_path} not found - run: uv run python pipeline/loadtest.py --record")
    routes = json.loads(routes_path.read_text())

    if args.past_day is None:
        args.past_day = _parquet_coverage()[1]

    port = int(args.base_url.rsplit(":", 1)[-1].split("/")[0])
    pid = args.target_pid or _pid_for_port(port)
    if pid is None:
        print(f"warning: no pid found on port {port}; skipping host sampling", file=sys.stderr)
    sampler = HostSampler(pid) if pid else None

    print(f"{args.model} model, {args.rate} sessions/s x {args.duration}s, mix={args.mix}, "
          f"unique_routes={args.unique_routes}, workers={args.workers}, target pid={pid}")

    if sampler:
        sampler.start()
    start = time.monotonic()

    rec = Recorder()
    if args.workers <= 1:
        rec.merge(asyncio.run(_drive(args, routes, args.seed, args.rate)))
    else:
        ctx = mp.get_context("spawn")
        queue = ctx.Queue()
        procs = [
            ctx.Process(target=_worker_entry, args=(args, routes, args.seed + i, args.rate / args.workers, queue))
            for i in range(args.workers)
        ]
        for proc in procs:
            proc.start()
        for _ in procs:
            rec.merge(queue.get())
        for proc in procs:
            proc.join()

    duration = time.monotonic() - start
    host = sampler.stop() if sampler else {}

    try:
        host["fixtureStats"] = httpx.get(f"{args.base_url}/_loadtest/stats", timeout=5).json()
    except Exception:
        pass

    result = _report(rec, host, duration, args)

    stamp = datetime.now().strftime("%Y%m%dT%H%M%S")
    name = f"run-{stamp}{'-' + args.label if args.label else ''}.json"
    (args.out / name).write_text(json.dumps(result, indent=2))
    print(f"\nwrote {args.out / name}")
    sys.exit(0 if result["verdict"] == "PASS" else 2)


if __name__ == "__main__":
    main()
