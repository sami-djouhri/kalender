"""Erzeugt echte Decken als JSON, damit die Oberfläche mit ihnen gerendert werden kann.

Gegenstück zu `decke-zeigen.py`: das zeigt den Tag im Klartext, dies liefert
dieselben Tage in genau der Form, die der BFF an das Frontend gibt. Der Umweg
über dieses Skript statt über von Hand getippte Beispieldaten ist Absicht: eine
geratene Fixture zeigt die Blockverteilung, die man erwartet, und genau die ist
beim Prüfen einer Ansicht wertlos. Die Dauern, die Anzahl der Blöcke und die
Längenverhältnisse müssen aus `baue_decke` kommen, sonst prüft man sein eigenes
Wunschbild.

Vier Fälle, weil die Ansicht mehrere Zustände kennt:

* **offen**: kein Block hat eine Kennung, nichts ist korrigierbar, der Kasten
  „Tag festhalten" steht oben.
* **festgeschrieben**: jeder Block ist eine Zeile, die Ziehgriffe erscheinen.
* **festgeschrieben mit Verworfenem**: der durchgestrichene Block und die
  Entfernen-Zone.

Aufruf wie bei `decke-zeigen.py` (Produktionsdaten werden nie angefasst):

    docker compose run --rm -e DATABASE_URL=sqlite:////tmp/decke-fixtures.db \\
      -v "$(pwd)":/work -w /work --entrypoint python kalender \\
      scripts/decke-fixtures.py /work/fixtures.json
"""
import json
import os
import sys

if "/tmp/" not in os.environ.get("DATABASE_URL", ""):
    sys.exit("DATABASE_URL muss unter /tmp liegen. Abbruch, um Produktionsdaten zu schuetzen.")

os.environ.setdefault("KALENDER_PASSWORD", "schau")
os.environ.setdefault("SECRET_KEY", "schau")
os.environ.setdefault("DEFAULT_OWNER_SUB", "schau-owner")

from datetime import date, datetime  # noqa: E402

from backend.database import Base, engine, get_db  # noqa: E402
import backend.tenant  # noqa: F401,E402
from backend.models import Calendar, Event, Habit, HabitSession, TimeBlock, Todo  # noqa: E402
from backend.system_calendars import DAYTYPE_CALENDARS  # noqa: E402
from backend.tagesdecke import baue_decke, festschreiben  # noqa: E402

TERMINE_CAL_ID = "system-termine-0000-0000-000000000000"


def wanduhr(tag: date, stunde: int, minute: int = 0) -> datetime:
    return datetime(tag.year, tag.month, tag.day, stunde, minute)


def aufbauen(db, arbeitstag: date) -> None:
    """Derselbe Alltag wie in decke-zeigen.py, damit beide Wege dasselbe zeigen."""
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


def json_fest(wert):
    """datetime/date so ausgeben, wie FastAPI es tut (ISO mit T)."""
    if isinstance(wert, (datetime, date)):
        return wert.isoformat()
    raise TypeError(f"nicht serialisierbar: {type(wert)}")


def main() -> None:
    ziel = sys.argv[1] if len(sys.argv) > 1 else "/work/decke-fixtures.json"

    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    db = next(get_db())

    arbeitstag = date(2027, 3, 16)   # Dienstag
    freier_tag = date(2027, 3, 20)   # Samstag
    aufbauen(db, arbeitstag)

    faelle = {}

    # 1) Arbeitstag, noch offen: keine Kennungen, nichts korrigierbar.
    faelle["arbeitstag_offen"] = baue_decke(db, arbeitstag)

    # 2) Freier Tag, festgeschrieben: Ziehgriffe erscheinen.
    faelle["freier_tag_fest"] = festschreiben(db, freier_tag)

    # 3) Derselbe freie Tag mit einem verworfenen Block. Verworfen wird das
    #    Training, weil genau dieser Fall die Zusage traegt: geplant, nicht
    #    getan, und es bleibt trotzdem sichtbar stehen.
    training = (
        db.query(TimeBlock)
        .filter(TimeBlock.date == freier_tag, TimeBlock.art == "training")
        .first()
    )
    if training is not None:
        training.status = "verworfen"
        training.quelle = "manuell"
        db.commit()
    faelle["freier_tag_verworfen"] = baue_decke(db, freier_tag)

    # 4) Arbeitstag festgeschrieben: der laengste Fall, Arbeit 9 h am Stueck
    #    neben Bloecken von 20 min. Hier zeigt sich, ob die Hoehen tragen.
    faelle["arbeitstag_fest"] = festschreiben(db, arbeitstag)

    with open(ziel, "w", encoding="utf-8") as datei:
        json.dump(faelle, datei, default=json_fest, ensure_ascii=False, indent=1)

    for name, decke in faelle.items():
        bloecke = decke["bloecke"]
        kuerzester = min(b["minuten"] for b in bloecke)
        laengster = max(b["minuten"] for b in bloecke)
        print(f"{name:<24} {len(bloecke):>2} Bloecke, "
              f"{kuerzester} bis {laengster} min, "
              f"festgeschrieben={decke['festgeschrieben']}")
    print(f"\ngeschrieben: {ziel}")


if __name__ == "__main__":
    main()
