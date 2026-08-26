"""Report email rendering: chips, thresholds, escaping, subjects, text part."""

import json

from app import report_email


def test_fixture_report_renders():
    subject, html, text = report_email.render_report(
        report_email.fixture_row(), "https://delaybahn.com")
    assert "Tübingen Hbf" in subject and "Hamburg Hbf" in subject
    assert "IC 2167" in html and "+25 min" in html and "+10.5 min" in html
    assert "nicht erfasst" in html  # the untracked bus leg, in both cards
    assert "Fußweg" in html
    assert "/r/unsubscribe?token=SAMPLE-TOKEN" in html
    assert "IC 2167" in text and "+25 min" in text  # plain-text part mirrors it


def test_accepts_json_strings_from_sqlite():
    row = report_email.fixture_row()
    row["snapshot"] = json.dumps(row["snapshot"])
    row["actuals"] = json.dumps(row["actuals"])
    _, html, _ = report_email.render_report(row, "https://delaybahn.com")
    assert "ICE 1080" in html


def test_delay_chip_thresholds_match_the_site():
    assert report_email.delay_color(2) == report_email.GREEN
    assert report_email.delay_color(5) == report_email.YELLOW
    assert report_email.delay_color(12) == report_email.RED


def test_station_names_are_escaped():
    evil = "<script>alert(1)</script>"
    row = report_email.fixture_row()
    snap = json.loads(json.dumps(row["snapshot"]))  # deep copy
    snap["search"]["fromName"] = evil
    snap["journey"]["legs"][2]["origin"]["name"] = evil
    row["snapshot"] = snap
    _, html, _ = report_email.render_report(row, "https://delaybahn.com")
    assert "<script>" not in html and "&lt;script&gt;" in html


def test_cancelled_leg_gets_red_chip_and_note():
    row = report_email.fixture_row()
    actuals = json.loads(json.dumps(row["actuals"]))
    actuals["legs"]["2"] = {"delayMin": None, "canceled": True, "reason": 37}
    row["actuals"] = actuals
    _, html, _ = report_email.render_report(row, "https://delaybahn.com")
    assert "ausgefallen" in html


def test_all_unresolved_note():
    row = report_email.fixture_row()
    row["actuals"] = {"legs": {}}
    _, html, text = report_email.render_report(row, "https://delaybahn.com")
    assert "wiederfinden" in html and "wiederfinden" in text
