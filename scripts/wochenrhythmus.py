"""Traegt den festen Wochenrhythmus als Tagestypen ein.

Mo/Di/Mi = Arbeit (07:00-15:45), Do/Fr = Berufsschule (ab 08:00).
Daran haengt der Wecker: `arbeit` weckt 05:30, `schule` 06:00, `frei` erst 08:00
(`backend/sleep_schedule.py`). Ein Kalender ohne Arbeitstage weckt zu spaet.

Bewusst behutsam:

* **Nur leere Tage werden belegt.** Feiertag, Urlaub, Krankenschein und jeder schon
  gesetzte Typ bleiben unangetastet. Damit ist ein zweiter Lauf folgenlos, und ein
  von Hand eingetragener Urlaub ueberlebt.
* **`reschedule_week` laeuft einmal je Woche**, nicht einmal je Tag. Ueber
  `_tagestyp_setzen` waere es ein Reschedule pro Tag gewesen, also rund hundert
  fuer ein halbes Jahr.
* **Geplant werden nur die naechsten `--planen-wochen` Wochen** (Vorgabe 3), obwohl
  die Tagestypen fuer den ganzen Zeitraum gesetzt werden. Der Tagestyp traegt den
  Wecker und soll weit vorausstehen; ein Sitzungsvorschlag fuer die Woche in vier
  Monaten ist dagegen wertlos, und die Datenbank haelt bereits 355 unbeantwortete
  Vorschlaege. Der Scheduler plant ohnehin laufend nach.
* **Schulferien kennt dieses Skript nicht.** Der Kalender rechnet Feiertage selbst
  aus (`holidays_nrw.py`), Ferien der Berufsschule stehen nirgends. Die Do/Fr in
  den Ferien muessen von Hand auf `frei` gestellt werden. `--bericht` zeigt, welche
  Tage gesetzt wurden.

Aufruf im Container (Trockenlauf ist die Vorgabe):

    docker exec kalender python /app/scripts/wochenrhythmus.py --bis 2027-01-31
    docker exec kalender python /app/scripts/wochenrhythmus.py --bis 2027-01-31 --schreiben
"""

import argparse
import sys
from datetime import date, timedelta

sys.path.insert(0, "/app")

from datetime import datetime  # noqa: E402

from backend.database import SessionLocal  # noqa: E402
from backend.models import Event  # noqa: E402
from backend.routers.daytype import _get_daytype  # noqa: E402
from backend.system_calendars import DAYTYPE_CALENDARS  # noqa: E402

# Wochentag (Montag=0) -> Tagestyp. Sa/So fehlen bewusst: "wochenende" leitet der
# Kalender aus dem Datum ab, es gibt dafuer keinen Kalender zum Eintragen.
RHYTHMUS = {0: "arbeit", 1: "arbeit", 2: "arbeit", 3: "schule", 4: "schule"}

TITEL = {"arbeit": "Arbeit", "schule": "Schule"}

# ★ Die echten Zeiten stehen hier, nicht in `_make_daytype_event`. Dessen Konstanten
# stammen aus dem alten Rhythmus und stimmen nicht mehr: `ARBEIT_TIMES["friday"]`
# sagt Arbeit 07:00-13:00, obwohl Freitag inzwischen Berufsschule ist, und
# `SCHULE_TIMES` endet 14:40 statt nach der sechsten Stunde. Beides sind
# Code-Konstanten und brauchen einen Deploy; die hier angelegten Events sind Daten
# und tragen deshalb sofort die Wahrheit. Wird `_make_daytype_event` nachgezogen,
# aendert das nichts an bereits eingetragenen Tagen.
#
# Schule endet nach 6 Stunden a 45 min ab 08:00 zuzueglich Pausen, also gegen 13:15.
# Das ist eine Annahme aus der Stundenzahl, kein abgelesener Stundenplan.
ZEITEN = {
    "arbeit": (7, 0, 15, 45),
    "schule": (8, 0, 13, 15),
}


def _zeitfenster(typ: str, tag: date) -> tuple[datetime, datetime]:
    h1, m1, h2, m2 = ZEITEN[typ]
    return (
        datetime(tag.year, tag.month, tag.day, h1, m1),
        datetime(tag.year, tag.month, tag.day, h2, m2),
    )


def eintragen(db, von: date, bis: date, schreiben: bool, planen_wochen: int = 3) -> dict:
    gesetzt: list[tuple[date, str]] = []
    belegt: list[tuple[date, str]] = []

    tag = von
    while tag <= bis:
        typ = RHYTHMUS.get(tag.weekday())
        if typ is None:
            tag += timedelta(days=1)
            continue

        # Nur wirklich leere Tage. `_get_daytype` liefert die geltende Prioritaet
        # (feiertag > krank > urlaub > schule > arbeit > frei). Alles ausser "frei"
        # ist eine bestehende Aussage, die dieses Skript nicht ueberschreibt.
        vorhanden = _get_daytype(db, tag)
        if vorhanden != "frei":
            belegt.append((tag, vorhanden))
            tag += timedelta(days=1)
            continue

        gesetzt.append((tag, typ))
        if schreiben:
            beginn, ende = _zeitfenster(typ, tag)
            db.add(Event(
                calendar_id=DAYTYPE_CALENDARS[typ],
                title=TITEL[typ],
                start=beginn,
                end=ende,
                all_day=False,
            ))
        tag += timedelta(days=1)

    # Nur die vorderen Wochen neu planen (siehe Modulkopf).
    alle_wochen = sorted({t - timedelta(days=t.weekday()) for t, _ in gesetzt})
    zu_planen = alle_wochen[:max(0, planen_wochen)]

    if schreiben:
        db.commit()
        # Erst nach dem commit neu planen, und nur einmal je Kalenderwoche.
        from backend.scheduler import reschedule_week
        for wochenstart in zu_planen:
            reschedule_week(db, wochenstart)

    return {
        "gesetzt": gesetzt,
        "belegt": belegt,
        "wochen": len(alle_wochen),
        "geplant": zu_planen,
    }


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--von", default=date.today().isoformat(), help="erster Tag (Vorgabe: heute)")
    p.add_argument("--bis", required=True, help="letzter Tag, YYYY-MM-DD")
    p.add_argument("--schreiben", action="store_true", help="ohne dies nur Trockenlauf")
    p.add_argument("--bericht", action="store_true", help="jeden gesetzten Tag einzeln zeigen")
    p.add_argument("--planen-wochen", type=int, default=3,
                   help="wie viele Wochen neu geplant werden (Vorgabe 3, 0 = gar nicht)")
    args = p.parse_args()

    von, bis = date.fromisoformat(args.von), date.fromisoformat(args.bis)
    if bis < von:
        print("FEHLER: --bis liegt vor --von", file=sys.stderr)
        return 2

    db = SessionLocal()
    try:
        e = eintragen(db, von, bis, args.schreiben, args.planen_wochen)
    finally:
        db.close()

    modus = "GESCHRIEBEN" if args.schreiben else "Trockenlauf (nichts geaendert)"
    print(f"{modus}: {von} bis {bis}")
    print(f"  neu gesetzt  : {len(e['gesetzt'])} Tage in {e['wochen']} Wochen")
    print(f"  uebersprungen: {len(e['belegt'])} Tage (Feiertag/Urlaub/schon gesetzt)")
    print(f"  neu geplant  : {len(e['geplant'])} Wochen"
          + (f" (ab {e['geplant'][0]})" if e["geplant"] else ""))

    if e["belegt"]:
        print("\n  uebersprungen im Einzelnen:")
        for tag, typ in e["belegt"]:
            print(f"    {tag} {tag.strftime('%a')}  bleibt {typ}")

    if args.bericht and e["gesetzt"]:
        print("\n  gesetzt im Einzelnen:")
        for tag, typ in e["gesetzt"]:
            print(f"    {tag} {tag.strftime('%a')}  -> {typ}")

    if not args.schreiben:
        print("\n  --schreiben ergaenzen, um es wirklich einzutragen.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
