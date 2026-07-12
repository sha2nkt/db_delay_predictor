"""Generate homepage scatter SVGs (DE + EN) from the June per-train delay averages.

Source data: monthly processed release from the HuggingFace dataset piebro/deutsche-bahn-data,
e.g. hf_hub_download('piebro/deutsche-bahn-data', 'monthly_processed_data/data-2026-06.parquet',
repo_type='dataset', local_dir='data'). Output is deterministic for a given seed.
"""

import argparse
from pathlib import Path

import duckdb
import numpy as np

PROJECT_ROOT = Path(__file__).resolve().parent.parent

parser = argparse.ArgumentParser()
parser.add_argument("--parquet", default=PROJECT_ROOT / "data" / "monthly_processed_data" / "data-2026-06.parquet")
parser.add_argument("--out-dir", default=PROJECT_ROOT / "static")
parser.add_argument("--n-dots", type=int, default=1600)
parser.add_argument("--seed", type=int, default=42)
args = parser.parse_args()

df = duckdb.sql(f"""
    WITH events AS (
        SELECT train_type || ' ' || ltrim(train_number, '0') AS train,
               CAST(time AS DATE) AS day, delay_in_min
        FROM read_parquet('{args.parquet}')
        WHERE NOT is_canceled AND delay_in_min IS NOT NULL
          AND train_number IS NOT NULL AND train_number != ''
          AND CAST(time AS DATE) BETWEEN DATE '2026-06-01' AND DATE '2026-06-30'
    )
    SELECT avg(delay_in_min) FILTER (WHERE day <  DATE '2026-06-16') AS h1,
           avg(delay_in_min) FILTER (WHERE day >= DATE '2026-06-16') AS h2,
           count(*) FILTER (WHERE day <  DATE '2026-06-16') AS n1,
           count(*) FILTER (WHERE day >= DATE '2026-06-16') AS n2
    FROM events GROUP BY train
    HAVING n1 >= 20 AND n2 >= 20
""").df()

AXIS_MAX = 12.0
vis = df[(df.h1 >= 0) & (df.h2 >= 0) & (df.h1 <= AXIS_MAX) & (df.h2 <= AXIS_MAX)]
print(f"{len(df)} trains, {len(vis)} inside 0..{AXIS_MAX} min ({len(vis)/len(df):.0%})")
print(f"pearson r (all): {df.h1.corr(df.h2):+.3f}")

slope, intercept = np.polyfit(vis.h1, vis.h2, 1)
print(f"trend on visible domain: h2 = {slope:.3f}*h1 + {intercept:.3f}")

rng = np.random.default_rng(args.seed)
sample = vis.sample(n=min(args.n_dots, len(vis)), random_state=args.seed)

# plot geometry
X0, X1 = 90, 700     # plot left/right
Y0, Y1 = 130, 480    # plot top/bottom
def sx(v): return X0 + (v / AXIS_MAX) * (X1 - X0)
def sy(v): return Y1 - (v / AXIS_MAX) * (Y1 - Y0)

dots = "\n".join(
    f'  <circle cx="{sx(r.h1):.1f}" cy="{sy(r.h2):.1f}" r="3" fill="#ec0016" fill-opacity="0.22"/>'
    for r in sample.itertuples()
)
grid = "\n".join(
    f'  <line x1="{X0}" y1="{sy(v):.1f}" x2="{X1}" y2="{sy(v):.1f}" stroke="#eceff2" stroke-width="1"/>\n'
    f'  <line x1="{sx(v):.1f}" y1="{Y0}" x2="{sx(v):.1f}" y2="{Y1}" stroke="#eceff2" stroke-width="1"/>'
    for v in (3, 6, 9, 12)
)
xticks = "\n".join(
    f'  <text class="tick" x="{sx(v):.1f}" y="{Y1 + 20}" text-anchor="middle">{v}</text>'
    for v in (0, 3, 6, 9, 12)
)
yticks = "\n".join(
    f'  <text class="tick" x="{X0 - 10}" y="{sy(v):.1f}" text-anchor="end" dominant-baseline="middle">{v}</text>'
    for v in (0, 3, 6, 9, 12)
)
trend = (f'  <line x1="{sx(0):.1f}" y1="{sy(intercept):.1f}" '
         f'x2="{sx(AXIS_MAX):.1f}" y2="{sy(slope * AXIS_MAX + intercept):.1f}" '
         f'stroke="#8f000e" stroke-width="2.5" stroke-linecap="round"/>')

TEXTS = {
    "de": dict(
        aria="Punktwolke: Züge, die vom 1. bis 15. Juni verspätet waren, waren auch vom 16. bis 30. Juni verspätet.",
        title="Verspätete Züge bleiben verspätet.",
        sub1="Jeder Punkt ist ein Zug. Rechts: je mehr Verspätung er vom 1.–15. Juni hatte.",
        sub2="Oben: je mehr Verspätung derselbe Zug vom 16.–30. Juni hatte.",
        xlab="Ø Verspätung 1.–15. Juni (Minuten)",
        ylab="Ø Verspätung 16.–30. Juni (Minuten)",
        corner_lo=("Pünktlich –", "bleibt pünktlich"),
        corner_hi=("Verspätet –", "bleibt verspätet"),
        footer="Jeder Punkt = ein Zug (zufällige Auswahl von 1.600 aus 40.736) · Datenquelle: Deutsche-Bahn-Fahrplandaten (IRIS) · Juni 2026",
        fname="delay-correlation.svg",
    ),
    "en": dict(
        aria="Scatter plot: trains that were delayed from June 1 to 15 were also delayed from June 16 to 30.",
        title="Late trains stay late.",
        sub1="Each dot is one train. Further right: the more delay it had June 1–15.",
        sub2="Further up: the more delay the same train had June 16–30.",
        xlab="avg delay June 1–15 (minutes)",
        ylab="avg delay June 16–30 (minutes)",
        corner_lo=("Punctual —", "stays punctual"),
        corner_hi=("Late —", "stays late"),
        footer="Each dot = one train (random sample of 1,600 out of 40,736) · Data: Deutsche Bahn timetable data (IRIS) · June 2026",
        fname="delay-correlation-en.svg",
    ),
}

for t in TEXTS.values():
    svg = f'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 760 560" role="img"
     aria-label="{t['aria']}">
  <style>
    text {{ font-family: -apple-system, "Segoe UI", Helvetica, Arial, sans-serif; }}
    .title    {{ font-size: 23px; font-weight: 700; fill: #282d37; }}
    .subtitle {{ font-size: 13.5px; fill: #646973; }}
    .tick     {{ font-size: 12px; fill: #8a8f98; }}
    .axis     {{ font-size: 12.5px; fill: #646973; }}
    .corner   {{ font-size: 14px; font-weight: 600; fill: #282d37;
                 paint-order: stroke; stroke: #ffffff; stroke-width: 4px; stroke-linejoin: round; }}
    .footer   {{ font-size: 11px; fill: #8a8f98; }}
  </style>

  <rect width="760" height="560" fill="#ffffff"/>

  <text class="title" x="40" y="38">{t['title']}</text>
  <text class="subtitle" x="40" y="63">{t['sub1']}</text>
  <text class="subtitle" x="40" y="81">{t['sub2']}</text>

{grid}
  <line x1="{X0}" y1="{Y1}.5" x2="{X1}" y2="{Y1}.5" stroke="#c9cfd6" stroke-width="1"/>
  <line x1="{X0}.5" y1="{Y0}" x2="{X0}.5" y2="{Y1}" stroke="#c9cfd6" stroke-width="1"/>

{dots}
{trend}

  <text class="corner" x="{sx(2.1):.0f}" y="{sy(0.75):.0f}">{t['corner_lo'][0]}</text>
  <text class="corner" x="{sx(2.1):.0f}" y="{sy(0.75) + 18:.0f}">{t['corner_lo'][1]}</text>
  <text class="corner" x="{sx(7.4):.0f}" y="{sy(10.9):.0f}">{t['corner_hi'][0]}</text>
  <text class="corner" x="{sx(7.4):.0f}" y="{sy(10.9) + 18:.0f}">{t['corner_hi'][1]}</text>

{xticks}
{yticks}
  <text class="axis" x="{(X0 + X1) / 2:.0f}" y="{Y1 + 44}" text-anchor="middle">{t['xlab']}</text>
  <text class="axis" transform="translate({X0 - 48},{(Y0 + Y1) / 2:.0f}) rotate(-90)" text-anchor="middle">{t['ylab']}</text>

  <text class="footer" x="40" y="546">{t['footer']}</text>
</svg>
'''
    out = Path(args.out_dir) / t["fname"]
    out.write_text(svg)
    print(f"wrote {out}")
