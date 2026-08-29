"""Render the post-journey report email: the historic median delays shown at
booking next to what actually happened.

Email-client HTML: tables, inline styles, ~600px, no images and no SVG - the
site's day charts cannot travel by mail, only the chips do. Every dynamic string
passes through html.escape(): the snapshot is user-supplied and must never
inject markup into mail sent from our domain. A plain-text part always
accompanies the HTML one.
"""

import json
from datetime import date
from html import escape

# palette mirrors static/style.css
BRAND = "#fd1c17"
BG = "#f0f3f5"
BORDER = "#d7dce1"
TEXT = "#282d37"
MUTED = "#646973"
GREEN = "#2a7230"
YELLOW = "#b8860b"
RED = "#c50014"
GRAY = "#9aa0a8"

# matches the badge thresholds in static/app.js (delayBadge/delayValueBadge)
def delay_color(minutes: float) -> str:
    return GREEN if minutes < 3 else YELLOW if minutes < 10 else RED


S = {
    "de": {
        "subjectReport": "Dein Verspätungs-Report: {frm} → {to} am {d}",
        "hello": "Hallo{name},",
        "reportIntro": "so lief deine Fahrt am {d} im Vergleich zur typischen Verspätung bei deiner Buchung:",
        "card1": "Typische Verspätung bei deiner Buchung",
        "card1Note": "Median der letzten {window} Tage vor deiner Buchung",
        "card2": "So lief es wirklich",
        "summary": "Am Ziel: bisher typisch {fc} · tatsächlich {act}",
        "transfers": "{n} Umstiege",
        "transfer1": "1 Umstieg",
        "direct": "direkt",
        "walk": "Fußweg",
        "noData": "keine Daten",
        "notTracked": "nicht erfasst",
        "canceled": "ausgefallen",
        "cancelNote": "⚠ Mindestens ein Zug dieser Fahrt ist (teilweise) ausgefallen.",
        "unresolvedNote": "Wir konnten deine Züge in unseren Ist-Daten leider nicht sicher"
        " wiederfinden – für diese Fahrt liegen keine tatsächlichen Zeiten vor.",
        "footerWhy": "Du erhältst diese E-Mail, weil du sie am {d} auf delaybahn.com für"
        " diese Verbindung angefordert hast.",
        "unsub": "Abmelden & Daten löschen",
        "legal": "Impressum & Datenschutz",
        "tagline": "Den Zug buchen, nicht die Verspätung",
    },
    "en": {
        "subjectReport": "Your delay report: {frm} → {to} on {d}",
        "hello": "Hi{name},",
        "reportIntro": "here is how your journey on {d} compared to the typical delays shown when you booked:",
        "card1": "Typical delays when you booked",
        "card1Note": "Median over the {window} days before your booking",
        "card2": "What actually happened",
        "summary": "At your destination: typical so far {fc} · actual {act}",
        "transfers": "{n} transfers",
        "transfer1": "1 transfer",
        "direct": "direct",
        "walk": "Walk",
        "noData": "no data",
        "notTracked": "not tracked",
        "canceled": "cancelled",
        "cancelNote": "⚠ At least one train on this journey was (partially) cancelled.",
        "unresolvedNote": "Unfortunately we could not reliably find your trains in our"
        " records – no actual times are available for this journey.",
        "footerWhy": "You are receiving this email because you requested it for this"
        " connection on delaybahn.com on {d}.",
        "unsub": "Unsubscribe & delete my data",
        "legal": "Legal notice & privacy",
        "tagline": "Book the train, not the delay",
    },
}

_WEEKDAYS = {
    "de": ["Mo.", "Di.", "Mi.", "Do.", "Fr.", "Sa.", "So."],
    "en": ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"],
}
_MONTHS = {
    "de": ["Jan.", "Feb.", "März", "Apr.", "Mai", "Juni",
           "Juli", "Aug.", "Sep.", "Okt.", "Nov.", "Dez."],
    "en": ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
           "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"],
}


def fmt_date(iso_day: str, lang: str) -> str:
    d = date.fromisoformat(iso_day[:10])
    wd, mon = _WEEKDAYS[lang][d.weekday()], _MONTHS[lang][d.month - 1]
    return f"{wd}, {d.day}. {mon} {d.year}" if lang == "de" else f"{wd}, {d.day} {mon} {d.year}"


def fmt_time(iso_dt: str) -> str:
    return iso_dt[11:16] if len(iso_dt) >= 16 else ""


def _chip(text: str, color: str) -> str:
    return (
        f'<span style="display:inline-block;padding:3px 8px;border-radius:12px;'
        f'font-size:12px;font-weight:700;color:#ffffff;background:{color};'
        f'white-space:nowrap;">{escape(text)}</span>'
    )


def _delay_chip(minutes: float) -> str:
    v = round(minutes, 1)
    v = int(v) if float(v).is_integer() else v
    return _chip(f"{'+' if v >= 0 else ''}{v} min", delay_color(minutes))


def _forecast_chip(leg: dict, lang: str) -> str:
    if "delayStats" not in leg:
        return _chip(S[lang]["notTracked"], GRAY)
    stats = leg.get("delayStats")
    if not stats or stats.get("medianDelay") is None:
        return _chip(S[lang]["noData"], GRAY)
    return _delay_chip(stats["medianDelay"])


def _actual_chip(leg: dict, actual: dict | None, lang: str) -> str:
    if "delayStats" not in leg:
        return _chip(S[lang]["notTracked"], GRAY)
    if actual is None:
        return _chip(S[lang]["noData"], GRAY)
    if actual.get("canceled"):
        return _chip(S[lang]["canceled"], RED)
    return _delay_chip(actual.get("delayMin") or 0)


def _leg_rows(legs: list[dict], lang: str, chip_for) -> str:
    rows = []
    for i, leg in enumerate(legs):
        if leg.get("walking"):
            origin = escape(str((leg.get("origin") or {}).get("name") or ""))
            dest = escape(str((leg.get("destination") or {}).get("name") or ""))
            rows.append(
                f'<tr><td colspan="2" style="padding:5px 0;font-size:12.5px;'
                f'color:{MUTED};font-style:italic;">{S[lang]["walk"]} · {origin} → {dest}</td></tr>'
            )
            continue
        line_name = escape(str((leg.get("line") or {}).get("name") or ""))
        origin = escape(str((leg.get("origin") or {}).get("name") or ""))
        dest = escape(str((leg.get("destination") or {}).get("name") or ""))
        dep = fmt_time(str(leg.get("plannedDeparture") or ""))
        arr = fmt_time(str(leg.get("plannedArrival") or ""))
        rows.append(
            '<tr>'
            f'<td style="padding:6px 10px 6px 0;vertical-align:top;">'
            f'<span style="display:inline-block;background:#ffffff;border:1px solid {BORDER};'
            f'border-radius:4px;padding:2px 8px;font-weight:700;font-size:12.5px;'
            f'white-space:nowrap;">{line_name}</span><br>'
            f'<span style="font-size:13px;color:{TEXT};">{origin} {dep} → {dest} {arr}</span></td>'
            f'<td align="right" style="padding:6px 0;vertical-align:top;">{chip_for(i, leg)}</td>'
            '</tr>'
        )
    return "".join(rows)


def _card(title: str, note: str, rows_html: str) -> str:
    note_html = (
        f'<div style="font-size:11.5px;color:{MUTED};padding-top:6px;">{escape(note)}</div>'
        if note
        else ""
    )
    return (
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0"'
        ' style="margin-top:14px;"><tr>'
        f'<td style="background:{BG};border-radius:8px;padding:14px 16px;">'
        f'<div style="font-size:13px;font-weight:700;color:{TEXT};'
        f'padding-bottom:4px;">{escape(title)}</div>'
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0">'
        f'{rows_html}</table>{note_html}</td></tr></table>'
    )


def _shell(lang: str, content: str, footer: str) -> str:
    return (
        '<!DOCTYPE html><html lang="' + lang + '"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1"></head>'
        f'<body style="margin:0;padding:0;background:{BG};">'
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0"'
        f' style="background:{BG};"><tr><td align="center" style="padding:24px 12px;">'
        '<table role="presentation" width="600" cellpadding="0" cellspacing="0"'
        ' style="max-width:600px;width:100%;background:#ffffff;border-radius:8px;'
        "font-family:-apple-system,'Segoe UI',Helvetica,Arial,sans-serif;"
        f'color:{TEXT};">'
        f'<tr><td style="background:{BRAND};border-radius:8px 8px 0 0;padding:16px 24px;">'
        '<span style="color:#ffffff;font-size:20px;font-weight:700;">DelayBahn</span>'
        '<span style="color:#ffffff;opacity:.85;font-size:12px;font-style:italic;">'
        f' &nbsp;{escape(S[lang]["tagline"])}</span></td></tr>'
        f'<tr><td style="padding:22px 24px;">{content}</td></tr>'
        f'<tr><td style="padding:16px 24px 20px;border-top:1px solid {BORDER};'
        f'font-size:11.5px;color:{MUTED};line-height:1.55;">{footer}</td></tr>'
        '</table></td></tr></table></body></html>'
    )


def _footer(lang: str, base_url: str, unsub_token: str, created_ts: str) -> str:
    s = S[lang]
    why = escape(s["footerWhy"].format(d=fmt_date(created_ts[:10], lang)))
    unsub_url = f"{base_url}/r/unsubscribe?token={unsub_token}"
    return (
        f"{why}<br>"
        f'<a href="{escape(unsub_url)}" style="color:{MUTED};">{escape(s["unsub"])}</a> · '
        f'<a href="{base_url}/impressum.html" style="color:{MUTED};">{escape(s["legal"])}</a>'
    )


def _hello(lang: str, name: str) -> str:
    return S[lang]["hello"].format(name=f" {name}" if name else "")


def _transfers_text(lang: str, journey: dict) -> str:
    n = journey.get("transfers")
    if n is None:
        n = max(0, sum(1 for leg in journey.get("legs") or [] if not leg.get("walking")) - 1)
    return S[lang]["direct"] if n == 0 else (
        S[lang]["transfer1"] if n == 1 else S[lang]["transfers"].format(n=n)
    )


def _final_tracked_index(legs: list[dict]) -> int | None:
    for i in range(len(legs) - 1, -1, -1):
        if "delayStats" in legs[i]:
            return i
    return None


def render_report(sub: dict, base_url: str) -> tuple[str, str, str]:
    """(subject, html, text) for one resolved subscription row.

    sub is a subscriptions row as a dict; snapshot/actuals may be JSON strings
    (straight from SQLite) or already-parsed dicts (tests, fixtures)."""
    lang = sub.get("lang") or "de"
    s = S[lang]
    snapshot = sub["snapshot"]
    if isinstance(snapshot, str):
        snapshot = json.loads(snapshot)
    actuals = sub.get("actuals") or {}
    if isinstance(actuals, str):
        actuals = json.loads(actuals)
    actual_legs: dict = actuals.get("legs") or {}
    journey = snapshot.get("journey") or {}
    search = snapshot.get("search") or {}
    legs = journey.get("legs") or []
    first, last = legs[0], legs[-1]
    frm = search.get("fromName") or (first.get("origin") or {}).get("name") or ""
    to = search.get("toName") or (last.get("destination") or {}).get("name") or ""
    day = fmt_date(sub["travel_date"], lang)
    window = search.get("window") or 7

    dep = fmt_time(str(first.get("plannedDeparture") or ""))
    arr = fmt_time(str(last.get("plannedArrival") or ""))
    header = (
        f'<div style="font-size:16px;font-weight:700;">{escape(frm)} → {escape(to)}</div>'
        f'<div style="font-size:13px;color:{MUTED};padding-top:2px;">{escape(day)} · '
        f'{dep} → {arr} · {escape(_transfers_text(lang, journey))}</div>'
    )

    card1 = _card(
        s["card1"],
        s["card1Note"].format(window=window),
        _leg_rows(legs, lang, lambda i, leg: _forecast_chip(leg, lang)),
    )
    card2_rows = _leg_rows(
        legs, lang, lambda i, leg: _actual_chip(leg, actual_legs.get(str(i)), lang)
    )
    resolved = [a for a in actual_legs.values() if a is not None]
    unresolved_note = (
        f'<div style="font-size:12.5px;color:{MUTED};padding-top:12px;">'
        f'{escape(s["unresolvedNote"])}</div>'
        if not resolved
        else ""
    )
    card2 = _card(s["card2"], "", card2_rows)
    cancel_note = (
        f'<div style="font-size:12px;color:{RED};padding-top:8px;">{escape(s["cancelNote"])}</div>'
        if any(a.get("canceled") for a in resolved)
        else ""
    )

    # headline comparison at the passenger's destination (final tracked leg)
    summary = ""
    fi = _final_tracked_index(legs)
    if fi is not None and resolved:
        stats = legs[fi].get("delayStats") or {}
        fc = (
            f"+{stats['medianDelay']} min"
            if stats.get("medianDelay") is not None
            else s["noData"]
        )
        final_actual = actual_legs.get(str(fi))
        if final_actual is None:
            act = s["noData"]
        elif final_actual.get("canceled"):
            act = s["canceled"]
        else:
            act = f"+{final_actual.get('delayMin') or 0} min"
        summary = (
            f'<div style="font-size:14px;font-weight:700;padding-top:14px;">'
            f'{escape(s["summary"].format(fc=fc, act=act))}</div>'
        )

    content = (
        f'<div style="font-size:14px;padding-bottom:12px;">{escape(_hello(lang, sub.get("name") or ""))}<br>'
        f'{escape(s["reportIntro"].format(d=day))}</div>'
        + header + card1 + unresolved_note + card2 + cancel_note + summary
    )
    html = _shell(lang, content, _footer(lang, base_url, sub["unsub_token"], sub["created_ts"]))

    subject = s["subjectReport"].format(frm=frm, to=to, d=f"{int(sub['travel_date'][8:10])}.{int(sub['travel_date'][5:7])}." if lang == "de" else fmt_date(sub["travel_date"], lang))

    text_lines = [
        _hello(lang, sub.get("name") or ""),
        s["reportIntro"].format(d=day),
        "",
        f"{frm} -> {to} · {day} · {dep} -> {arr}",
        "",
        s["card1"] + ":",
    ]
    for leg in legs:
        text_lines.append("  " + _leg_text(leg, lang, _forecast_text(leg, lang)))
    text_lines += ["", s["card2"] + ":"]
    for i, leg in enumerate(legs):
        text_lines.append(
            "  " + _leg_text(leg, lang, _actual_text(leg, actual_legs.get(str(i)), lang))
        )
    if not resolved:
        text_lines += ["", s["unresolvedNote"]]
    text_lines += [
        "",
        s["footerWhy"].format(d=fmt_date(sub["created_ts"][:10], lang)),
        f"{s['unsub']}: {base_url}/r/unsubscribe?token={sub['unsub_token']}",
    ]
    return subject, html, "\n".join(text_lines)


def _leg_text(leg: dict, lang: str, chip_text: str) -> str:
    if leg.get("walking"):
        return (
            f"{S[lang]['walk']} · {(leg.get('origin') or {}).get('name') or ''}"
            f" -> {(leg.get('destination') or {}).get('name') or ''}"
        )
    line_name = (leg.get("line") or {}).get("name") or ""
    return (
        f"{line_name}  {(leg.get('origin') or {}).get('name') or ''}"
        f" {fmt_time(str(leg.get('plannedDeparture') or ''))}"
        f" -> {(leg.get('destination') or {}).get('name') or ''}"
        f" {fmt_time(str(leg.get('plannedArrival') or ''))}  [{chip_text}]"
    )


def _forecast_text(leg: dict, lang: str) -> str:
    if "delayStats" not in leg:
        return S[lang]["notTracked"]
    stats = leg.get("delayStats")
    if not stats or stats.get("medianDelay") is None:
        return S[lang]["noData"]
    return f"+{stats['medianDelay']} min"


def _actual_text(leg: dict, actual: dict | None, lang: str) -> str:
    if "delayStats" not in leg:
        return S[lang]["notTracked"]
    if actual is None:
        return S[lang]["noData"]
    if actual.get("canceled"):
        return S[lang]["canceled"]
    return f"+{actual.get('delayMin') or 0} min"


# --- fixture for the design mockup (pipeline/send_reports.py --sample) ---

FIXTURE_SNAPSHOT = {
    "journey": {
        "transfers": 2,
        "legs": [
            {
                "walking": False,
                "line": {"name": "Bus RE6", "fahrtNr": "6", "product": "BUS"},
                "origin": {"id": "8000141", "name": "Hauptbahnhof, Tübingen"},
                "destination": {"id": "683006", "name": "Hauptbf (Pariser Platz), Stuttgart"},
                "plannedDeparture": "2026-08-14T19:05:00",
                "plannedArrival": "2026-08-14T20:00:00",
            },
            {
                "walking": True,
                "origin": {"id": "683006", "name": "Hauptbf (Pariser Platz), Stuttgart"},
                "destination": {"id": "8000096", "name": "Stuttgart Hbf"},
                "plannedDeparture": "2026-08-14T20:00:00",
                "plannedArrival": "2026-08-14T20:07:00",
            },
            {
                "walking": False,
                "line": {"name": "IC 2167", "fahrtNr": "2167", "product": "IC"},
                "origin": {"id": "8000096", "name": "Stuttgart Hbf"},
                "destination": {"id": "8000284", "name": "Nürnberg Hbf"},
                "plannedDeparture": "2026-08-14T20:09:00",
                "plannedArrival": "2026-08-14T22:18:00",
                "delayStats": {"medianDelay": 10.5, "maxDelay": 32, "daysMatched": 7, "canceledDays": 1},
            },
            {
                "walking": False,
                "line": {"name": "ICE 1080", "fahrtNr": "1080", "product": "ICE"},
                "origin": {"id": "8000284", "name": "Nürnberg Hbf"},
                "destination": {"id": "8002549", "name": "Hamburg Hbf"},
                "plannedDeparture": "2026-08-15T00:17:00",
                "plannedArrival": "2026-08-15T06:57:00",
                "delayStats": {"medianDelay": 4, "maxDelay": 18, "daysMatched": 7, "canceledDays": 0},
            },
        ],
    },
    "search": {"fromName": "Tübingen Hbf", "toName": "Hamburg Hbf", "window": 7},
    "shownAt": "2026-08-11T09:30:00+00:00",
}

FIXTURE_ACTUALS = {
    "resolvedAt": "2026-08-17T05:45:00+00:00",
    "parquetMaxDay": "2026-08-16",
    "legs": {
        "2": {"delayMin": 25, "canceled": False, "reason": 36},
        "3": {"delayMin": 15, "canceled": False, "reason": None},
    },
}


def fixture_row() -> dict:
    """A subscriptions row for --sample / --test-to renders."""
    return {
        "id": 0,
        "lang": "en",
        "name": "Shashank",
        "snapshot": FIXTURE_SNAPSHOT,
        "actuals": FIXTURE_ACTUALS,
        "travel_date": "2026-08-15",
        "created_ts": "2026-08-11T09:30:00+00:00",
        "unsub_token": "SAMPLE-TOKEN",
    }
