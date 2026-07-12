"""Do past delays predict future delays?

Per train (type + number), average the delay over all stop events in the
first half of a month (days 1-15) and in the second half (day 16 - end),
then correlate the two averages across trains.
"""

import argparse
import calendar
from datetime import date
from pathlib import Path

import duckdb

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def main():
    parser = argparse.ArgumentParser(description="Correlate per-train avg delay: first half of a month vs second half")
    parser.add_argument("--parquet", type=Path, default=PROJECT_ROOT / "data" / "delays.parquet")
    parser.add_argument("--month", default="2026-06", help="month to analyze, YYYY-MM (default: 2026-06)")
    parser.add_argument("--min-stops", type=int, default=20, help="min stop events per train in EACH half (default: 20)")
    args = parser.parse_args()

    year, month = map(int, args.month.split("-"))
    h1_start = date(year, month, 1)
    h2_start = date(year, month, 16)
    month_end = date(year, month, calendar.monthrange(year, month)[1])

    conn = duckdb.connect()
    df = conn.execute(
        f"""
        WITH events AS (
            SELECT train_type || ' ' || ltrim(train_number, '0') AS train,
                   train_type,
                   CAST(time AS DATE) AS day,
                   delay_in_min
            FROM read_parquet('{args.parquet}')
            WHERE NOT is_canceled
              AND delay_in_min IS NOT NULL
              AND train_number IS NOT NULL AND train_number != ''
              AND CAST(time AS DATE) BETWEEN DATE '{h1_start}' AND DATE '{month_end}'
        )
        SELECT train,
               any_value(train_type) AS train_type,
               avg(delay_in_min) FILTER (WHERE day <  DATE '{h2_start}') AS h1_avg,
               avg(delay_in_min) FILTER (WHERE day >= DATE '{h2_start}') AS h2_avg,
               count(*) FILTER (WHERE day <  DATE '{h2_start}') AS h1_n,
               count(*) FILTER (WHERE day >= DATE '{h2_start}') AS h2_n
        FROM events
        GROUP BY train
        HAVING h1_n >= {args.min_stops} AND h2_n >= {args.min_stops}
        """
    ).df()

    print(f"Window: {h1_start}..{h2_start - date.resolution} vs {h2_start}..{month_end}")
    print(f"Trains with >= {args.min_stops} stop events in each half: {len(df)}\n")

    def report(sub, label):
        if len(sub) < 10:
            return
        pearson_r = sub.h1_avg.corr(sub.h2_avg)
        spearman_r = sub.h1_avg.rank().corr(sub.h2_avg.rank())
        print(f"{label:<12} n={len(sub):>5}  pearson={pearson_r:+.3f}  spearman={spearman_r:+.3f}")

    report(df, "ALL")
    for tt, sub in sorted(df.groupby("train_type"), key=lambda x: -len(x[1])):
        report(sub, tt)

    # persistence view: bucket trains by first-half delay, show second-half outcome
    df["quintile"] = df.h1_avg.rank(pct=True).mul(5).clip(upper=4.999).astype(int) + 1
    print("\nFirst-half delay quintile -> second-half avg delay (min):")
    q = df.groupby("quintile").agg(
        trains=("train", "count"),
        h1_avg=("h1_avg", "mean"),
        h2_avg=("h2_avg", "mean"),
        h2_median=("h2_avg", "median"),
    )
    print(q.round(2).to_string())


if __name__ == "__main__":
    main()
