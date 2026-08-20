"""The stats window is anchored per country, not on the global newest day.

The per-country producers run independently, so one can fall behind: on
2026-08-20 the Dutch poller had 08-19 while the German build did not. With a
global anchor that silently cost every German leg a day of its window - every
card read "6/7 Tage" and the day chart drew a phantom empty slot for a day
Germany was never going to have.
"""

from datetime import date, datetime

import duckdb
import pytest

from app import delays

# DE stops start 080, NL stops 084 (merge_delays.SOURCES)
DE_EVA = "08011160"
NL_EVA = "08400058"


def _row(eva: str, day: date, train: str, delay_min: int):
    arr = datetime(day.year, day.month, day.day, 12, 0)
    return (
        "Teststation", "Teststation", eva, train, None, "Ziel", delay_min,
        arr, False, "ICE", "ride", 1,
        arr, arr.replace(minute=delay_min % 60), arr, arr, f"{eva}-{train}-{day}",
        None,
    )


@pytest.fixture
def table(tmp_path, monkeypatch):
    """A delays.duckdb where Germany ends a day before the Netherlands."""
    rows = []
    # DE: 08-13 .. 08-18 (the newest day never landed)
    for d in range(13, 19):
        rows.append(_row(DE_EVA, date(2026, 8, d), "1007", 5))
    # NL: 08-13 .. 08-19
    for d in range(13, 20):
        rows.append(_row(NL_EVA, date(2026, 8, d), "2731", 3))

    db = tmp_path / "delays.duckdb"
    con = duckdb.connect(str(db))
    con.execute(
        "CREATE TABLE delays (station_name VARCHAR, xml_station_name VARCHAR, eva VARCHAR,"
        " train_number VARCHAR, line_number VARCHAR, final_destination_station VARCHAR,"
        " delay_in_min INTEGER, time TIMESTAMP, is_canceled BOOLEAN, train_type VARCHAR,"
        " train_line_ride_id VARCHAR, train_line_station_num INTEGER,"
        " arrival_planned_time TIMESTAMP, arrival_change_time TIMESTAMP,"
        " departure_planned_time TIMESTAMP, departure_change_time TIMESTAMP, id VARCHAR,"
        " reason_code INTEGER)"
    )
    con.executemany("INSERT INTO delays VALUES (" + ",".join("?" * 18) + ")", rows)
    con.execute(f"ALTER TABLE delays ADD COLUMN train_no VARCHAR")
    con.execute(f"UPDATE delays SET train_no = {delays.TRAIN_NO_SQL}")
    con.close()

    monkeypatch.setattr(delays, "DELAYS_DB", db)
    delays._cache.clear()
    delays.init()
    yield
    delays._cache.clear()


def test_global_max_day_still_reports_the_newest_day_anywhere(table):
    assert delays.coverage()[1] == date(2026, 8, 19)


def test_window_anchor_is_per_country(table):
    assert delays.max_day_for(DE_EVA) == date(2026, 8, 18)
    assert delays.max_day_for(NL_EVA) == date(2026, 8, 19)
    # an unknown prefix falls back to the global anchor rather than returning None
    assert delays.max_day_for("09999999") == date(2026, 8, 19)


def test_lagging_country_still_gets_a_full_window(table):
    """The regression: with a global anchor this returned 6 of 7 days and a
    windowEnd Germany had no data for."""
    stats = delays.leg_delay_stats("1007", DE_EVA, datetime(2026, 8, 20, 12, 0), window=7)
    assert stats["daysMatched"] == 6
    assert stats["windowStart"] == "2026-08-12"
    assert stats["windowEnd"] == "2026-08-18"
    # every day in the window that Germany has is in the payload, and the chart
    # draws no trailing gap for a day the country was never going to report
    assert stats["days"][-1]["day"] == "2026-08-18"


def test_leading_country_is_unaffected(table):
    stats = delays.leg_delay_stats("2731", NL_EVA, datetime(2026, 8, 20, 12, 0), window=7)
    assert stats["daysMatched"] == 7
    assert stats["windowStart"] == "2026-08-13"
    assert stats["windowEnd"] == "2026-08-19"


def test_health_exposes_the_lag(table):
    assert delays.coverage_by_country() == {"080": "2026-08-18", "084": "2026-08-19"}
