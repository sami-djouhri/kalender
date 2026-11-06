"""NRW public holidays calculation using Gauss/Lichtenberg Easter algorithm.

No external dependencies required.
"""

from datetime import date, timedelta


def easter_date(year: int) -> date:
    """Calculate Easter Sunday for a given year (Gauss/Lichtenberg algorithm)."""
    a = year % 19
    b = year // 100
    c = year % 100
    d = b // 4
    e = b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i = c // 4
    k = c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    day = ((h + l - 7 * m + 114) % 31) + 1
    return date(year, month, day)


def get_nrw_holidays(year: int) -> list[tuple[date, str]]:
    """Return all NRW public holidays for the given year as (date, name) tuples."""
    easter = easter_date(year)

    holidays = [
        # Fixed holidays
        (date(year, 1, 1), "Neujahr"),
        (date(year, 5, 1), "Tag der Arbeit"),
        (date(year, 10, 3), "Tag der Deutschen Einheit"),
        (date(year, 11, 1), "Allerheiligen"),
        (date(year, 12, 25), "1. Weihnachtstag"),
        (date(year, 12, 26), "2. Weihnachtstag"),
        # Easter-dependent holidays
        (easter + timedelta(days=-2), "Karfreitag"),
        (easter + timedelta(days=1), "Ostermontag"),
        (easter + timedelta(days=39), "Christi Himmelfahrt"),
        (easter + timedelta(days=50), "Pfingstmontag"),
        (easter + timedelta(days=60), "Fronleichnam"),
    ]

    holidays.sort(key=lambda h: h[0])
    return holidays
