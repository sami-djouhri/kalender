"""Setzt die Wochenziele der Gewohnheiten auf ein Mass, das in die Woche passt.

Ausgangslage (gemessen am 2026-09-06): sieben Gewohnheiten mit zusammen **29,5 h
pro Woche**, alle Kopfarbeit, in einem Werktagsfenster von 17:00 bis 21:00. In den
60 Tagen davor hat der Planer daraus 355 Sitzungen vorgeschlagen und **keine
einzige** wurde abgehakt; die letzte erledigte Sitzung stammt vom 3. Juni.

Das ist kein Motivationsproblem, sondern ein Rechenfehler in der Vorgabe. Der
Planer nimmt `target_hours_per_week` als gegeben hin (`scheduler.py`, Kommentar
"Target = exactly what the user configured") und gleicht es nie mit dem ab, was
tatsaechlich passiert ist. Ein Ziel, das nie erreicht wird, erzeugt jede Woche
denselben unerfuellbaren Plan.

Neu gewichtet entlang zweier Vorgaben des Owners:

* **IHK Logistik, Abschlusspruefung Winter 26/27** ist das eine Ziel mit Stichtag.
  Es bekommt das groesste Kontingent, obwohl es bisher das kleinste hatte (2 h).
* **CompTIA/CCNA "nebenbei wenn Zeit"** faellt von 20 h auf 2 h. Die 20 h waren
  allein groesser als das gesamte realistische Wochenbudget und haben als groesster
  Posten alles andere verdraengt.
* **Sport neu**, kurz und oft (viermal 25 Minuten). Sport fehlte bisher vollstaendig.

Umkehrbar: `--zuruecksetzen` stellt die Werte von vor diesem Lauf wieder her, sie
stehen unten in ALT_STAND.

Aufruf im Container (Trockenlauf ist die Vorgabe):

    docker exec -i kalender python - < scripts/gewohnheiten-gewichten.py
    docker exec -i kalender python - --schreiben < scripts/gewohnheiten-gewichten.py
"""

import argparse
import sys
from datetime import datetime

sys.path.insert(0, "/app")

from backend.database import SessionLocal  # noqa: E402
from backend.models import Habit  # noqa: E402

# name -> (stunden_pro_woche, sitzungsdauer_min, begruendung)
NEUER_STAND = {
    "Lagerlogistik": (5.0, 45, "IHK-Abschlusspruefung Winter 26/27, das einzige Ziel mit Stichtag"),
    "Lernen fuer CompTIA": (2.0, 45, "laut Owner nebenbei, wenn Zeit bleibt"),
    "Mathematik": (1.0, 20, "stuetzt den Rechenteil der IHK-Pruefung"),
    "Sprache (EN/FR/DE)": (1.0, 20, "kleine taegliche Dosis"),
    "Allgemeinbildung": (0.5, 20, "zurueckgestellt bis nach der Pruefung"),
    "Schach": (0.5, 20, "Hobby, bleibt klein"),
    "Nietzsche lesen": (0.5, 15, "unveraendert"),
}

# Stand vor diesem Lauf, fuer --zuruecksetzen.
ALT_STAND = {
    "Lagerlogistik": (2.0, 30),
    "Lernen fuer CompTIA": (20.0, 60),
    "Mathematik": (1.75, 15),
    "Sprache (EN/FR/DE)": (1.75, 15),
    "Allgemeinbildung": (1.75, 15),
    "Schach": (1.75, 15),
    "Nietzsche lesen": (0.5, 15),
}

# Der fehlende Sport. Bewusst kurz und oft statt lang und selten: eine Einheit von
# 25 Minuten zuhause braucht keinen Weg und keine Umkleide und passt deshalb auch
# an einen Arbeitstag, an dem 90 Minuten im Studio nicht passen wuerden.
#
# ★ `category` steht auf "sonstige", obwohl "sport" richtig waere. Das Schema laesst
#   nur `lernen|lesen|sonstige` zu (`schemas.py:192`), einen Sport-Wert gibt es
#   nicht. Erkannt wird die Einheit trotzdem als Sport, weil `activities.py` am
#   Namen erkennt ("sport", "training", "workout"). Sobald das Schema erweitert ist,
#   gehoert hier "sport" hin.
SPORT = {
    "name": "Sport (zuhause)",
    "color": "#e0894a",
    "target_hours_per_week": 1.75,      # viermal 25 Minuten plus etwas Luft
    "session_duration_minutes": 25,
    "weekday_start": "17:00",
    "weekday_end": "21:00",
    "weekend_start": "10:00",
    "weekend_end": "18:00",
    "learning_mode": False,             # kein Fokusblock/Pause-Schema, das ist kein Lernen
    "category": "sonstige",
    "weekday_target_ratio": 0.5,        # Obergrenze des Schemas, siehe Bericht
    "max_consecutive_days": 3,          # zwischendurch ein Tag Pause
    "max_session_minutes": 45,
    "can_be_split": False,              # eine Einheit ist eine Einheit
    "active": True,
}


def anwenden(db, schreiben: bool, zuruecksetzen: bool) -> list[str]:
    zeilen: list[str] = []
    stand = ALT_STAND if zuruecksetzen else NEUER_STAND

    for habit in db.query(Habit).order_by(Habit.name).all():
        ziel = stand.get(habit.name)
        if ziel is None:
            zeilen.append(f"  ?  {habit.name}: steht in keiner Liste, unveraendert")
            continue

        stunden, dauer = ziel[0], ziel[1]
        grund = ziel[2] if len(ziel) > 2 else "zurueckgesetzt"
        alt_h, alt_d = habit.target_hours_per_week, habit.session_duration_minutes
        if abs(alt_h - stunden) < 0.01 and alt_d == dauer:
            zeilen.append(f"  =  {habit.name}: schon {stunden} h/Woche")
            continue

        zeilen.append(
            f"  ->  {habit.name}: {alt_h} h -> {stunden} h/Woche, "
            f"Sitzung {alt_d} -> {dauer} min ({grund})"
        )
        if schreiben:
            habit.target_hours_per_week = stunden
            habit.session_duration_minutes = dauer
            habit.max_session_minutes = max(dauer, habit.max_session_minutes or dauer)
            habit.updated_at = datetime.utcnow()

    # Sport nur anlegen, nie doppelt.
    vorhanden = db.query(Habit).filter(Habit.name == SPORT["name"]).first()
    if zuruecksetzen:
        if vorhanden:
            zeilen.append(f"  x  {SPORT['name']}: wird geloescht")
            if schreiben:
                db.delete(vorhanden)
    elif vorhanden:
        zeilen.append(f"  =  {SPORT['name']}: existiert bereits")
    else:
        zeilen.append(
            f"  +  {SPORT['name']}: NEU, {SPORT['target_hours_per_week']} h/Woche "
            f"in Einheiten von {SPORT['session_duration_minutes']} min"
        )
        if schreiben:
            # owner_sub vom bestehenden Bestand uebernehmen, damit die Gewohnheit
            # demselben Mandanten gehoert. Ohne das waere sie fuer den Owner unsichtbar.
            eigner = db.query(Habit).filter(Habit.owner_sub.isnot(None)).first()
            db.add(Habit(owner_sub=eigner.owner_sub if eigner else None, **SPORT))

    if schreiben:
        db.commit()
    return zeilen


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--schreiben", action="store_true", help="ohne dies nur Trockenlauf")
    p.add_argument("--zuruecksetzen", action="store_true", help="Stand vor diesem Lauf herstellen")
    args = p.parse_args()

    db = SessionLocal()
    try:
        zeilen = anwenden(db, args.schreiben, args.zuruecksetzen)
        summe = sum(h.target_hours_per_week for h in db.query(Habit).filter(Habit.active).all())
    finally:
        db.close()

    modus = "GESCHRIEBEN" if args.schreiben else "Trockenlauf (nichts geaendert)"
    print(f"{modus}{' -- ZURUECKSETZEN' if args.zuruecksetzen else ''}\n")
    for z in zeilen:
        print(z)
    print(f"\n  Summe aktiver Wochenziele: {summe:.2f} h")
    if not args.schreiben:
        print("  (Summe zeigt den Stand VOR der Aenderung)")
        print("\n  --schreiben ergaenzen, um es wirklich zu setzen.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
