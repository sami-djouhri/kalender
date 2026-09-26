"""Zeigt eine Tagesdecke im Klartext, damit man sie liest statt sie sich vorzustellen.

Gegenstück zu den Tests: die prüfen Zusagen, dies zeigt das Ergebnis. Eine Decke
kann jede Invariante erfüllen und trotzdem Unsinn sein (drei Stunden Erholung am
Stück vormittags, Abendessen um 17:35, Training zwischen zwei Terminen mit fünf
Minuten Weg). Das sieht man nur, wenn man den Tag einmal untereinander liest.

Aufruf (Produktionsdaten werden nie angefasst, die Datenbank liegt unter /tmp):

    docker compose run --rm -e DATABASE_URL=sqlite:////tmp/decke-schau.db \\
      -v "$(pwd)":/work -w /work --entrypoint python kalender scripts/decke-zeigen.py
"""
import os
import sys

if "/tmp/" not in os.environ.get("DATABASE_URL", ""):
    # Dieselbe Schutzschiene wie in simulieren.sh: ein Anschauungsskript, das
    # Tabellen leert, darf nie gegen die echte Datenbank laufen.
    sys.exit("DATABASE_URL muss unter /tmp liegen. Abbruch, um Produktionsdaten zu schuetzen.")

os.environ.setdefault("KALENDER_PASSWORD", "schau")
os.environ.setdefault("SECRET_KEY", "schau")
os.environ.setdefault("DEFAULT_OWNER_SUB", "schau-owner")

from datetime import date, datetime  # noqa: E402

from backend.database import Base, engine, get_db  # noqa: E402
import backend.tenant  # noqa: F401,E402  (registriert das Mandanten-Scoping)
from backend.models import Calendar, Event, Habit, HabitSession, Todo  # noqa: E402
from backend.system_calendars import DAYTYPE_CALENDARS  # noqa: E402
from backend.tagesdecke import ARTEN, baue_decke  # noqa: E402

TERMINE_CAL_ID = "system-termine-0000-0000-000000000000"


def wanduhr(tag: date, stunde: int, minute: int = 0) -> datetime:
    return datetime(tag.year, tag.month, tag.day, stunde, minute)


def aufbauen(db, arbeitstag: date) -> None:
    """Ein Alltag, wie er wirklich vorkommt: Arbeit, Abendtermin, Gewohnheit, Aufgabe."""
    db.add(Calendar(id=TERMINE_CAL_ID, name="Termine", is_system=True))
    for name, kennung in DAYTYPE_CALENDARS.items():
        db.add(Calendar(id=kennung, name=name, is_system=True))
    db.commit()

    db.add(Event(calendar_id=DAYTYPE_CALENDARS["arbeit"], title="Arbeit",
                 start=wanduhr(arbeitstag, 7), end=wanduhr(arbeitstag, 16)))
    db.add(Event(calendar_id=TERMINE_CAL_ID, title="Elternabend",
                 start=wanduhr(arbeitstag, 19, 30), end=wanduhr(arbeitstag, 21)))
    gewohnheit = Habit(name="Gitarre", weekly_minimum_hours=2)
    db.add(gewohnheit)
    db.commit()
    db.refresh(gewohnheit)
    db.add(HabitSession(habit_id=gewohnheit.id, week_iso="2027-W11", status="accepted",
                        start=wanduhr(arbeitstag, 17, 30), end=wanduhr(arbeitstag, 18, 15)))
    db.add(Todo(title="Steuerunterlagen sortieren", scheduling_mode="planned_day",
                planned_date=arbeitstag, status="scheduled",
                scheduled_start=wanduhr(arbeitstag, 6, 30),
                scheduled_end=wanduhr(arbeitstag, 7)))
    db.commit()


def zeigen(db, ueberschrift: str, tag: date) -> None:
    decke = baue_decke(db, tag)
    strich = "=" * 68
    print(f"\n{strich}\n{ueberschrift}   {decke['datum']}")
    wach = decke["wach_minuten"]
    print(f"wach {decke['wach_von'].strftime('%H:%M')} bis "
          f"{decke['wach_bis'].strftime('%H:%M')}   ({wach // 60} h {wach % 60:02d} min)"
          f"   Kapazitaet: {decke['capacity']['level']}")
    print(f"verplant {decke['verplant_minuten']} min, offen {decke['offen_minuten']} min")
    print("-" * 68)
    for block in decke["bloecke"]:
        marke = ARTEN.get(block["art"], block["art"])
        grund = f"   ({block['begruendung']})" if block["begruendung"] else ""
        print(f"  {block['start'].strftime('%H:%M')}-{block['ende'].strftime('%H:%M')}"
              f" {block['minuten']:>4} min  {marke:<11} {block['titel']}{grund}")
    print("-" * 68)
    for art, minuten in sorted(decke["summe_je_art"].items(), key=lambda paar: -paar[1]):
        print(f"  {art:<12} {minuten // 60:>2} h {minuten % 60:02d} min")
    for hinweis in decke.get("hinweise", []):
        print(f"  Hinweis: {hinweis}")


def main() -> None:
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    db = next(get_db())
    arbeitstag = date(2027, 3, 16)   # Dienstag
    aufbauen(db, arbeitstag)
    zeigen(db, "ARBEITSTAG (Di)", arbeitstag)
    zeigen(db, "FREIER TAG (Sa)", date(2027, 3, 20))


if __name__ == "__main__":
    main()
