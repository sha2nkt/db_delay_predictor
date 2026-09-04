"""The leaderboard's ranking rules on fixture rows, without the delay table:
punctuality decides, average delay breaks ties, thin countries are listed but
unranked, and each period only sees its own days."""

from datetime import date, timedelta

from app import leaderboard

AS_OF = date(2026, 8, 5)


def row(cc, day=AS_OF, total=10000, cancelled=100, observed=9000, on_time=8000, delay_sum=27000):
    return (cc, day, total, cancelled, observed, on_time, delay_sum)


def codes(period):
    return [(c["code"], c["rank"]) for c in period["countries"]]


def test_rank_by_punctuality_then_avg_delay():
    daily = [
        row("80", observed=10000, on_time=8400, delay_sum=32000),  # DE 84.0 %, 3.2 min
        row("85", observed=10000, on_time=9800, delay_sum=6000),   # CH 98.0 %, 0.6 min
        row("87", observed=10000, on_time=9800, delay_sum=9000),   # FR 98.0 %, 0.9 min
    ]
    day = leaderboard.build(daily, AS_OF)["periods"]["day"]
    assert codes(day) == [("CH", 1), ("FR", 2), ("DE", 3)]
    ch = day["countries"][0]
    assert ch["punctuality"] == 98.0 and ch["avgDelay"] == 0.6
    assert ch["cancelled"] == 1.0 and ch["stops"] == 10000 and ch["observed"] == 10000


def test_thin_country_listed_last_but_unranked():
    daily = [
        row("80"),
        row("81", total=120, cancelled=0, observed=120, on_time=120, delay_sum=0),  # AT: perfect, but 120 stops
    ]
    day = leaderboard.build(daily, AS_OF)["periods"]["day"]
    assert codes(day) == [("DE", 1), ("AT", None)]
    at = day["countries"][1]
    assert at["punctuality"] == 100.0 and at["observed"] == 120


def test_periods_see_only_their_days_and_series_keeps_all():
    daily = [
        row("80", AS_OF - timedelta(days=40), observed=1000, on_time=0),        # outside every period
        row("80", AS_OF - timedelta(days=20), observed=1000, on_time=1000),
        row("80", AS_OF - timedelta(days=3), observed=1000, on_time=1000),
        row("80", AS_OF, observed=1000, on_time=500),
    ]
    doc = leaderboard.build(daily, AS_OF)
    p = doc["periods"]
    assert (p["day"]["from"], p["day"]["to"]) == (AS_OF.isoformat(), AS_OF.isoformat())
    assert p["week"]["from"] == (AS_OF - timedelta(days=6)).isoformat()
    assert p["month"]["from"] == (AS_OF - timedelta(days=29)).isoformat()
    de = {name: p[name]["countries"][0] for name in ("day", "week", "month")}
    assert (de["day"]["punctuality"], de["day"]["days"]) == (50.0, 1)
    assert (de["week"]["punctuality"], de["week"]["days"]) == (75.0, 2)
    assert (de["month"]["punctuality"], de["month"]["days"]) == (83.3, 3)
    assert [d["day"] for d in doc["series"]["DE"]] == [
        (AS_OF - timedelta(days=n)).isoformat() for n in (40, 20, 3, 0)
    ]
    assert doc["asOf"] == AS_OF.isoformat()


def test_country_without_rows_in_a_period_stays_listed_unranked():
    daily = [
        row("85"),                                   # CH: today
        row("80", AS_OF - timedelta(days=5)),        # DE: last row five days ago
    ]
    p = leaderboard.build(daily, AS_OF)["periods"]
    assert codes(p["day"]) == [("CH", 1), ("DE", None)]
    de_day = p["day"]["countries"][1]
    assert (de_day["observed"], de_day["punctuality"], de_day["avgDelay"], de_day["days"]) == (0, None, None, 0)
    assert codes(p["week"]) == [("CH", 1), ("DE", 2)]  # same numbers, so ties go to the code
    assert p["week"]["countries"][1]["days"] == 1 and p["week"]["days"] == 7


def test_unknown_prefix_ignored_and_empty_periods_still_present():
    doc = leaderboard.build([row("99")], AS_OF)
    assert all(period["countries"] == [] for period in doc["periods"].values())
    assert set(doc["periods"]) == {"day", "week", "month"}
    assert doc["series"] == {}
