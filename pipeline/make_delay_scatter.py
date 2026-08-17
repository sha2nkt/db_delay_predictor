"""Generate homepage scatter SVGs (DE + EN) from per-train delay averages of two
consecutive months (month x vs month y, default: the two most recent complete months).

Source data: monthly processed releases from the HuggingFace dataset piebro/deutsche-bahn-data,
e.g. hf_hub_download('piebro/deutsche-bahn-data', 'monthly_processed_data/data-2026-06.parquet',
repo_type='dataset', local_dir='data'). Output is deterministic for a given seed.
"""

import argparse
import json
from pathlib import Path

import duckdb
import numpy as np

from month_utils import default_months, month_end, month_start, name_de, name_en, range_de, range_en

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_X, DEFAULT_Y = default_months()

parser = argparse.ArgumentParser()
parser.add_argument("--month-x", default=DEFAULT_X, help="earlier month, YYYY-MM")
parser.add_argument("--month-y", default=DEFAULT_Y, help="later month, YYYY-MM")
parser.add_argument("--out-dir", default=PROJECT_ROOT / "static")
parser.add_argument("--n-dots", type=int, default=1600)
parser.add_argument("--seed", type=int, default=42)
parser.add_argument("--meta-out", default=None, help="optional path for a JSON summary (train counts)")
args = parser.parse_args()

parquet_x = PROJECT_ROOT / "data" / "monthly_processed_data" / f"data-{args.month_x}.parquet"
parquet_y = PROJECT_ROOT / "data" / "monthly_processed_data" / f"data-{args.month_y}.parquet"

df = duckdb.sql(f"""
    WITH events AS (
        SELECT train_type || ' ' || ltrim(train_number, '0') AS train,
               CAST(time AS DATE) AS day, delay_in_min
        FROM read_parquet(['{parquet_x}', '{parquet_y}'])
        WHERE NOT (arrival_is_canceled OR departure_is_canceled) AND delay_in_min IS NOT NULL
          AND train_number IS NOT NULL AND train_number != ''
          AND CAST(time AS DATE) BETWEEN DATE '{month_start(args.month_x)}' AND DATE '{month_end(args.month_y)}'
    )
    SELECT avg(delay_in_min) FILTER (WHERE day <  DATE '{month_start(args.month_y)}') AS h1,
           avg(delay_in_min) FILTER (WHERE day >= DATE '{month_start(args.month_y)}') AS h2,
           count(*) FILTER (WHERE day <  DATE '{month_start(args.month_y)}') AS n1,
           count(*) FILTER (WHERE day >= DATE '{month_start(args.month_y)}') AS n2
    FROM events GROUP BY train
    HAVING n1 >= 40 AND n2 >= 40
""").df()

AXIS_MAX = 12.0
TOP_SHIFT = 34  # vertical space freed by dropping the in-chart headline
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
    f'  <circle cx="{sx(r.h1):.1f}" cy="{sy(r.h2):.1f}" r="3" fill="#fd1c17" fill-opacity="0.22"/>'
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

n_sample_de = f"{len(sample):,}".replace(",", ".")
n_total_de = f"{len(df):,}".replace(",", ".")
de_x, de_y = name_de(args.month_x), name_de(args.month_y)
en_x, en_y = name_en(args.month_x), name_en(args.month_y)
TEXTS = {
    "de": dict(
        aria=f"Punktwolke: Züge, die im {de_x} verspätet waren, waren auch im {de_y} verspätet.",
        sub1=f"Jeder Punkt ist ein Zug. Rechts: je mehr Verspätung er im {de_x} hatte.",
        sub2=f"Oben: je mehr Verspätung derselbe Zug im {de_y} hatte.",
        xlab=f"Ø Verspätung {de_x} (Minuten)",
        ylab=f"Ø Verspätung {de_y} (Minuten)",
        corner_lo=("Pünktlich –", "bleibt pünktlich"),
        corner_hi=("Verspätet –", "bleibt verspätet"),
        footer=f"Jeder Punkt = ein Zug (zufällige Auswahl von {n_sample_de} aus {n_total_de}) · Datenquelle: Deutsche-Bahn-Fahrplandaten (IRIS) · {range_de(args.month_x, args.month_y)}",
        fname="delay-correlation.svg",
    ),
    "en": dict(
        aria=f"Scatter plot: trains that were delayed in {en_x} were also delayed in {en_y}.",
        sub1=f"Each dot is one train. Further right: the more delay it had in {en_x}.",
        sub2=f"Further up: the more delay the same train had in {en_y}.",
        xlab=f"avg delay {en_x} (minutes)",
        ylab=f"avg delay {en_y} (minutes)",
        corner_lo=("Punctual —", "stays punctual"),
        corner_hi=("Late —", "stays late"),
        footer=f"Each dot = one train (random sample of {len(sample):,} out of {len(df):,}) · Data: Deutsche Bahn timetable data (IRIS) · {range_en(args.month_x, args.month_y)}",
        fname="delay-correlation-en.svg",
    ),
}

for t in TEXTS.values():
    # the headline lives in the page above the chart; everything below the subtitle
    # is shifted up by TOP_SHIFT to close the gap the dropped title left behind
    svg = f'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 760 {560 - TOP_SHIFT}" role="img"
     aria-label="{t['aria']}">
  <style>
    text {{ font-family: -apple-system, "Segoe UI", Helvetica, Arial, sans-serif; }}
    .subtitle {{ font-size: 13.5px; fill: #646973; }}
    .tick     {{ font-size: 12px; fill: #8a8f98; }}
    .axis     {{ font-size: 12.5px; fill: #646973; }}
    .corner   {{ font-size: 14px; font-weight: 600; fill: #282d37;
                 paint-order: stroke; stroke: #ffffff; stroke-width: 4px; stroke-linejoin: round; }}
    .footer   {{ font-size: 11px; fill: #8a8f98; }}
  </style>

  <rect width="760" height="{560 - TOP_SHIFT}" fill="#ffffff"/>

  <text class="subtitle" x="40" y="28">{t['sub1']}</text>
  <text class="subtitle" x="40" y="46">{t['sub2']}</text>

  <g transform="translate(0,-{TOP_SHIFT})">
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
  </g>

  <text class="footer" x="40" y="{546 - TOP_SHIFT}">{t['footer']}</text>
</svg>
'''
    out = Path(args.out_dir) / t["fname"]
    out.write_text(svg)
    print(f"wrote {out}")

if args.meta_out:
    Path(args.meta_out).write_text(json.dumps({
        "month_x": args.month_x, "month_y": args.month_y,
        "n_trains": int(len(df)), "n_sample": int(len(sample)),
    }))
