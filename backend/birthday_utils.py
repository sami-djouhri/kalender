"""Birthday event generation utilities."""

import calendar
from datetime import date, datetime, timedelta, timezone

from sqlalchemy.orm import Session

from backend.models import Contact, Event
from backend.system_calendars import GEBURTSTAGE_CALENDAR_ID  # noqa: F401


def _birthday_date_for_year(birthday: date, year: int) -> date:
    """Return the birthday date in a given year, handling Feb 29."""
    if birthday.month == 2 and birthday.day == 29:
        if not calendar.isleap(year):
            return date(year, 2, 28)
    return date(year, birthday.month, birthday.day)


def _age_at(birthday: date, on_date: date) -> int:
    """Calculate age on a given date."""
    age = on_date.year - birthday.year
    if (on_date.month, on_date.day) < (birthday.month, birthday.day):
        age -= 1
    return age


def regenerate_birthday_events(db: Session) -> None:
    """Delete all birthday events and recreate from current contacts."""
    # Delete all existing birthday events
    db.query(Event).filter(Event.calendar_id == GEBURTSTAGE_CALENDAR_ID).delete()

    # Get all contacts with a birthday set
    contacts = db.query(Contact).filter(Contact.birthday.isnot(None)).all()

    today = date.today()
    years = [today.year, today.year + 1]

    for contact in contacts:
        for year in years:
            bday = _birthday_date_for_year(contact.birthday, year)
            age = _age_at(contact.birthday, bday)
            if age < 0:
                continue
            title = f"{contact.name} ({age})"
            day_start = datetime(bday.year, bday.month, bday.day, tzinfo=timezone.utc)
            day_end = day_start + timedelta(days=1)
            db.add(Event(
                calendar_id=GEBURTSTAGE_CALENDAR_ID,
                title=title,
                start=day_start,
                end=day_end,
                all_day=True,
                # Geburtstags-Event gehoert dem Tenant des Kontakts - wichtig im
                # System-Kontext (Startup-Seed regeneriert ALLE Tenants; ohne
                # explizites owner_sub bliebe die Zeile NULL = global sichtbar).
                owner_sub=contact.owner_sub,
            ))

    db.commit()
