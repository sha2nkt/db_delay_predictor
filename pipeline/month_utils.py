"""Month arithmetic and labels shared by the homepage graph scripts.

Months are "YYYY-MM" strings; the homepage always compares an earlier month x
with a later month y (normally consecutive)."""

import datetime as dt

MONTHS_DE = ["Januar", "Februar", "März", "April", "Mai", "Juni", "Juli",
             "August", "September", "Oktober", "November", "Dezember"]
MONTHS_EN = ["January", "February", "March", "April", "May", "June", "July",
             "August", "September", "October", "November", "December"]


def default_months(today=None):
    """The two most recent complete calendar months: in August, June and July."""
    today = today or dt.date.today()
    last_prev = today.replace(day=1) - dt.timedelta(days=1)
    last_prevprev = last_prev.replace(day=1) - dt.timedelta(days=1)
    return last_prevprev.strftime("%Y-%m"), last_prev.strftime("%Y-%m")


def month_start(month):
    return dt.date(int(month[:4]), int(month[5:7]), 1)


def month_end(month):
    return (month_start(month) + dt.timedelta(days=32)).replace(day=1) - dt.timedelta(days=1)


def name_de(month):
    return MONTHS_DE[int(month[5:7]) - 1]


def name_en(month):
    return MONTHS_EN[int(month[5:7]) - 1]


def _joined(month_x, month_y, names, joiner):
    """Year appears once within a year ("Mai<j>Juni 2026"), twice across a
    boundary ("Dezember 2026<j>Januar 2027")."""
    nx, ny = names[int(month_x[5:7]) - 1], names[int(month_y[5:7]) - 1]
    if month_x[:4] == month_y[:4]:
        return f"{nx}{joiner}{ny} {month_y[:4]}"
    spaced = joiner if joiner.startswith(" ") else f" {joiner.strip()} "
    return f"{nx} {month_x[:4]}{spaced}{ny} {month_y[:4]}"


def range_de(month_x, month_y):
    return _joined(month_x, month_y, MONTHS_DE, "–")


def range_en(month_x, month_y):
    return _joined(month_x, month_y, MONTHS_EN, "–")


def pair_de(month_x, month_y):
    return _joined(month_x, month_y, MONTHS_DE, " und ")


def pair_en(month_x, month_y):
    return _joined(month_x, month_y, MONTHS_EN, " and ")
