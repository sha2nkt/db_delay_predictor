"""Generate homepage violin SVGs (DE + EN): June delay distributions for trains
grouped by their May delay — the distributional version of the scatter's claim
that punctual trains stay punctual and late trains stay late.

Source data: monthly processed releases from the HuggingFace dataset
piebro/deutsche-bahn-data (see make_delay_scatter.py). Output is deterministic.
"""

import argparse
from pathlib import Path

import duckdb
import numpy as np

PROJECT_ROOT = Path(__file__).resolve().parent.parent

parser = argparse.ArgumentParser()
parser.add_argument("--parquet-x", default=PROJECT_ROOT / "data" / "monthly_processed_data" / "data-2026-05.parquet")
parser.add_argument("--parquet-y", default=PROJECT_ROOT / "data" / "monthly_processed_data" / "data-2026-06.parquet")
parser.add_argument("--out-dir", default=PROJECT_ROOT / "static")
parser.add_argument("--axis-max", type=float, default=12.0)
parser.add_argument("--bandwidth", type=float, default=0.4)
args = parser.parse_args()

df = duckdb.sql(f"""
    WITH events AS (
        SELECT train_type || ' ' || ltrim(train_number, '0') AS train,
               CAST(time AS DATE) AS day, delay_in_min
        FROM read_parquet(['{args.parquet_x}', '{args.parquet_y}'])
        WHERE NOT is_canceled AND delay_in_min IS NOT NULL
          AND train_number IS NOT NULL AND train_number != ''
          AND CAST(time AS DATE) BETWEEN DATE '2026-05-01' AND DATE '2026-06-30'
    )
    SELECT avg(delay_in_min) FILTER (WHERE day <  DATE '2026-06-01') AS h1,
           avg(delay_in_min) FILTER (WHERE day >= DATE '2026-06-01') AS h2,
           count(*) FILTER (WHERE day <  DATE '2026-06-01') AS n1,
           count(*) FILTER (WHERE day >= DATE '2026-06-01') AS n2
    FROM events GROUP BY train
    HAVING n1 >= 40 AND n2 >= 40
""").df()

BINS = [(-np.inf, 1), (1, 2), (2, 4), (4, 6), (6, np.inf)]

# plot geometry (matches make_delay_scatter.py, plot bottom raised for 2-line x labels)
X0, X1 = 90, 700
Y0, Y1 = 130, 470
HALF_W = 46
TOP_SHIFT = 34  # vertical space freed by dropping the in-chart headline
def sy(v): return Y1 - (v / args.axis_max) * (Y1 - Y0)

grid_y = np.arange(0, args.axis_max + 1e-9, 0.1)

violins, groups = [], []
for i, (lo, hi) in enumerate(BINS):
    vals = df.h2[(df.h1 >= lo) & (df.h1 < hi)].to_numpy()
    cx = X0 + (i + 0.5) * (X1 - X0) / len(BINS)
    z = (grid_y[:, None] - vals[None, :]) / args.bandwidth
    dens = (np.exp(-0.5 * z * z) / np.sqrt(2 * np.pi)).sum(axis=1) / (len(vals) * args.bandwidth)
    w = dens / dens.max() * HALF_W
    right = [f"{cx + wi:.1f},{sy(y):.1f}" for y, wi in zip(grid_y, w)]
    left = [f"{cx - wi:.1f},{sy(y):.1f}" for y, wi in zip(grid_y[::-1], w[::-1])]
    q1, med, q3 = np.percentile(vals, [25, 50, 75])
    violins.append(
        f'  <polygon points="{" ".join(right + left)}" fill="#ec0016" fill-opacity="0.14" '
        f'stroke="#ec0016" stroke-width="1.5" stroke-linejoin="round"/>\n'
        f'  <line x1="{cx:.1f}" y1="{sy(q1):.1f}" x2="{cx:.1f}" y2="{sy(q3):.1f}" '
        f'stroke="#8f000e" stroke-width="4" stroke-linecap="round"/>\n'
        f'  <circle cx="{cx:.1f}" cy="{sy(med):.1f}" r="4.5" fill="#8f000e" stroke="#ffffff" stroke-width="2"/>'
    )
    groups.append(dict(cx=cx, med=med, n=len(vals), pct_over=100 * (vals > args.axis_max).mean()))

grid = "\n".join(
    f'  <line x1="{X0}" y1="{sy(v):.1f}" x2="{X1}" y2="{sy(v):.1f}" stroke="#eceff2" stroke-width="1"/>'
    for v in (3, 6, 9, 12)
)
yticks = "\n".join(
    f'  <text class="tick" x="{X0 - 10}" y="{sy(v):.1f}" text-anchor="end" dominant-baseline="middle">{v}</text>'
    for v in (0, 3, 6, 9, 12)
)

n_total = f"{len(df):,}"
last = groups[-1]

TEXTS = {
    "de": dict(
        aria="Violinplot: Züge, die im Mai pünktlich waren, sind es auch im Juni — Züge, die im Mai verspätet waren, bleiben verspätet.",
        sub1="Züge sind nach ihrer Ø-Verspätung im Mai gruppiert: links die pünktlichen, rechts die verspäteten.",
        sub2="Jede Form zeigt, wie sich ihre Ø-Verspätung im Juni verteilt: je breiter, desto häufiger. Punkt = Median.",
        ylab="Ø Verspätung Juni (Minuten)",
        names=["unter 1 min", "1–2 min", "2–4 min", "4–6 min", "über 6 min"],
        med=lambda m: f"Juni: {m:.1f} min".replace(".", ","),
        note=(f"{last['pct_over']:.0f} % dieser Züge:", "im Juni über 12 min"),
        xcaption="Ø Verspätung im Mai",
        footer=f"{n_total.replace(',', '.')} Züge mit mind. 40 Halten pro Monat · Datenquelle: Deutsche-Bahn-Fahrplandaten (IRIS) · Mai–Juni 2026",
        fname="delay-violin.svg",
    ),
    "en": dict(
        aria="Violin plot: trains that were punctual in May stay punctual in June — trains that were late in May stay late.",
        sub1="Trains are grouped by their average May delay: punctual ones on the left, late ones on the right.",
        sub2="Each shape shows how their average June delay is distributed: the wider, the more frequent. Dot = median.",
        ylab="avg delay June (minutes)",
        names=["under 1 min", "1–2 min", "2–4 min", "4–6 min", "over 6 min"],
        med=lambda m: f"June: {m:.1f} min",
        note=(f"{last['pct_over']:.0f}% of these trains:", "over 12 min in June"),
        xcaption="avg delay in May",
        footer=f"{n_total} trains with at least 40 stops per month · Data: Deutsche Bahn timetable data (IRIS) · May–June 2026",
        fname="delay-violin-en.svg",
    ),
}

for t in TEXTS.values():
    xlabels = "\n".join(
        f'  <text class="xname" x="{g["cx"]:.1f}" y="{Y1 + 22}" text-anchor="middle">{name}</text>\n'
        f'  <text class="xmed" x="{g["cx"]:.1f}" y="{Y1 + 39}" text-anchor="middle">{t["med"](g["med"])}</text>'
        for g, name in zip(groups, t["names"])
    )
    # the headline lives in the page above the chart; everything below the subtitle
    # is shifted up by TOP_SHIFT to close the gap the dropped title left behind
    svg = f'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 760 {560 - TOP_SHIFT}" role="img"
     aria-label="{t['aria']}">
  <style>
    text {{ font-family: -apple-system, "Segoe UI", Helvetica, Arial, sans-serif; }}
    .subtitle {{ font-size: 13.5px; fill: #646973; }}
    .tick     {{ font-size: 12px; fill: #8a8f98; }}
    .axis     {{ font-size: 12.5px; fill: #646973; }}
    .xname    {{ font-size: 13px; font-weight: 600; fill: #282d37; }}
    .xmed     {{ font-size: 11.5px; fill: #8a8f98; }}
    .corner   {{ font-size: 13px; font-weight: 600; fill: #282d37;
                 paint-order: stroke; stroke: #ffffff; stroke-width: 4px; stroke-linejoin: round; }}
    .footer   {{ font-size: 11px; fill: #8a8f98; }}
  </style>

  <rect width="760" height="{560 - TOP_SHIFT}" fill="#ffffff"/>

  <text class="subtitle" x="40" y="28">{t['sub1']}</text>
  <text class="subtitle" x="40" y="46">{t['sub2']}</text>

  <g transform="translate(0,-{TOP_SHIFT})">
{grid}
  <line x1="{X0}" y1="{Y1}.5" x2="{X1}" y2="{Y1}.5" stroke="#c9cfd6" stroke-width="1"/>

{chr(10).join(violins)}

  <text class="corner" x="{last['cx']:.0f}" y="{Y0 - 10:.0f}" text-anchor="middle">{t['note'][0]}</text>
  <text class="corner" x="{last['cx']:.0f}" y="{Y0 + 6:.0f}" text-anchor="middle">{t['note'][1]}</text>

{yticks}
{xlabels}
  <text class="axis" x="{(X0 + X1) / 2:.0f}" y="{Y1 + 60}" text-anchor="middle">{t['xcaption']}</text>
  <text class="axis" transform="translate({X0 - 48},{(Y0 + Y1) / 2:.0f}) rotate(-90)" text-anchor="middle">{t['ylab']}</text>
  </g>

  <text class="footer" x="40" y="{546 - TOP_SHIFT}">{t['footer']}</text>
</svg>
'''
    out = Path(args.out_dir) / t["fname"]
    out.write_text(svg)
    print(f"wrote {out}")

for g, name in zip(groups, TEXTS["en"]["names"]):
    print(f"{name}: n={g['n']}, june median={g['med']:.2f}, >12min={g['pct_over']:.1f}%")
