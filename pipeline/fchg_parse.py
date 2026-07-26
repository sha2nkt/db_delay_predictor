"""fchg parsing with delay-cause extraction.

Local replacement for the submodule's fchg path in process_files_to_temp: keeps
the same change-time/cancellation columns and additionally extracts the IRIS
delay-cause code (<m t="d" c="43" ts="..."/>) that the submodule drops. Plan
parsing is reused from the submodule unchanged.
"""

import sys
from pathlib import Path

import pandas as pd
from lxml import etree

SUBMODULE_ROOT = Path(__file__).resolve().parent.parent / "deutsche-bahn-data"
if str(SUBMODULE_ROOT) not in sys.path:
    sys.path.insert(0, str(SUBMODULE_ROOT))

from scripts.create_monthly_data_release import get_plan_db, to_datetime  # noqa: E402


def get_fchg_xml_rows(xml_string: str, xml_timestamp) -> list[dict]:
    root = etree.fromstring(xml_string.encode())

    rows = []
    for s in root.findall("s"):
        s_id = s.get("id")
        ar = s.find("ar")
        dp = s.find("dp")
        ar_ct = ar.get("ct") if ar is not None else None  # arrival change
        dp_ct = dp.get("ct") if dp is not None else None  # departure change
        ar_clt = ar.get("clt") if ar is not None else None  # arrival cancellation time
        dp_clt = dp.get("clt") if dp is not None else None  # departure cancellation time
        is_canceled = not (ar_clt is None and dp_clt is None)

        if ar_ct is None and dp_ct is None and not is_canceled:
            continue

        # latest delay-cause message anywhere on the stop; ts is yymmddhhmm so
        # string comparison orders chronologically
        reason_code, reason_ts = None, None
        for m in s.iter("m"):
            code = m.get("c")
            if m.get("t") != "d" or not code or not code.isdigit():
                continue
            ts = m.get("ts") or ""
            if reason_ts is None or ts > reason_ts:
                reason_code, reason_ts = int(code), ts

        rows.append(
            {
                "id": s_id,
                "arrival_change_time": to_datetime(ar_ct),
                "departure_change_time": to_datetime(dp_ct),
                "is_canceled": is_canceled,
                "reason_code": reason_code,
                "reason_ts": reason_ts,
                "xml_timestamp": xml_timestamp,
            }
        )
    return rows


def get_fchg_db(xml_df) -> pd.DataFrame:
    raw_fchg_df = xml_df[xml_df["api_name"] == "timetables/v1/fchg"]
    rows = []
    for row in raw_fchg_df.itertuples():
        if row.response_data:
            rows.extend(get_fchg_xml_rows(row.response_data, row.timestamp))
    fchg_df = pd.DataFrame(rows)
    if len(fchg_df) > 0:
        # keep parquet schemas identical across batches even when a batch has
        # no reasons at all (all-None object columns would type as NULL)
        fchg_df["reason_code"] = fchg_df["reason_code"].astype("Int64")
        fchg_df["reason_ts"] = fchg_df["reason_ts"].astype("string")
    return fchg_df


def process_files_to_temp(parquet_files: list[Path], eva_to_station: dict[str, str], temp_dir: Path):
    """Mirror of the submodule's process_files_to_temp using the reason-aware fchg parser."""
    plan_dir = temp_dir / "plan"
    fchg_dir = temp_dir / "fchg"
    plan_dir.mkdir(parents=True, exist_ok=True)
    fchg_dir.mkdir(parents=True, exist_ok=True)

    total_xml_count = 0
    total_plan_count = 0
    total_fchg_count = 0

    for i, parquet_file in enumerate(parquet_files):
        xml_df = pd.read_parquet(parquet_file)
        xml_df = xml_df[xml_df["status_code"] == "200"]
        total_xml_count += len(xml_df)

        plan_df = get_plan_db(xml_df, eva_to_station)
        if len(plan_df) > 0:
            plan_df.to_parquet(plan_dir / f"batch_{i:05d}.parquet", index=False)
            total_plan_count += len(plan_df)

        fchg_df = get_fchg_db(xml_df)
        if len(fchg_df) > 0:
            fchg_df.to_parquet(fchg_dir / f"batch_{i:05d}.parquet", index=False)
            total_fchg_count += len(fchg_df)

        del xml_df, plan_df, fchg_df

    return total_xml_count, total_plan_count, total_fchg_count
