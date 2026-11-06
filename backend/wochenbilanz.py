"""Was von einem Wochenziel wirklich getan wurde, gemessen statt behauptet.

Der Kalender konnte bisher nur das Soll: ein Habit hat ein Wochenziel, der Planer
legt Sitzungen, und `routers/habits.py` macht aus einer Sitzung, deren Uhrzeit
vorbei ist, eine erledigte. Gemessen ueber den Bestand am 2026-10-25: **36**
`completed` seit Februar gegen **260** vergangene `pending` zurueck bis zum 17.07.
Die Zahl, die den Erfolg belegen sollte, wurde also erfunden.

Dieses Modul rechnet die Gegenzahl: Minuten, die ein Melder gemessen hat, einem
Ziel zugeordnet ueber `HabitQuelle`.

Rueckwirkend, und zwar ohne Zutun. Die Zuordnung wird bei der Abfrage aufgeloest
und nicht beim Schreiben, also repariert eine heute angelegte Quelle die
Vergangenheit mit. Wer sie beim Einliefern festschreiben wuerde, muesste fuer
dasselbe Ergebnis eine Nachlaufrechnung bauen.

⚠️ Was hier NOCH NICHT passiert: `completed_hours` in `routers/habits.py` kommt
weiter aus `HabitSession.status`, die Autoerledigung bleibt bestehen. Das ist
Absicht (Entscheid): sie faellt zusammen mit ihrem Ersatz, also mit Timer
und Haken in der naechsten Auslieferung. Dazwischen haetten die sieben Habits ohne
Melder eine Bilanz von dauerhaft null gezeigt, und das ist schlechter als die
heutige zu hohe Zahl. Bis dahin stehen beide Zahlen nebeneinander, und das ist der
Zweck: so laesst sich vergleichen, bevor umgeschaltet wird.
"""
from __future__ import annotations

from datetime import date, timedelta

from sqlalchemy.orm import Session

from backend.models import Habit, HabitQuelle, ZeitIst
from backend.zeitschnitt import zuschneiden

# ⚠️⚠️ Die eine Stelle, an der steht, welche Staende auf das Ziel zaehlen.
#
# `bestaetigt` traegt `automatisch`, `offen` und `manuell`. Die Namen sagen, woher
# eine Messung kommt, und NICHT, ob sie gilt - der Owner hat die Herkunft der
# Geltung vorgezogen (2026-10-25), weil man die Herkunft beim Eintragen weiss und
# die Geltung erst beim Auswerten. Die Folge ist diese Konstante: dass `offen`
# nicht zaehlt, steht in keinem Feldnamen, also darf es auch nur an einem Ort im
# Code stehen. Gegenprobe:
# tests/test_zeitmessung.py::BilanzTest::test_offene_messung_zaehlt_nicht_aufs_ziel
ZAEHLENDE_STAENDE = ("automatisch", "manuell")
WARTENDE_STAENDE = ("offen",)


def quellen_text(quelle: HabitQuelle) -> str:
    """Eine Quelle als ein Wort fuer die Anzeige: `dienst` oder `dienst/kennung`."""
    return f"{quelle.dienst}/{quelle.kennung}" if quelle.kennung else quelle.dienst


def _wochenfenster(week_start: date) -> tuple[date, date]:
    return week_start, week_start + timedelta(days=6)


def _messungen(db: Session, quellen: list[HabitQuelle], von: date, bis: date, staende) -> list[ZeitIst]:
    """Die Zeilen dieser Woche, die zu einer der Quellen gehoeren.

    Gefiltert wird auf Paare, nicht auf zwei Listen. `dienst IN (...) AND kennung
    IN (...)` waere falsch: bei den Quellen `lernen/mathe` und `schach/` zoege das
    auch `lernen/` und `schach/mathe` herein, also Messungen, die zu keinem Ziel
    gehoeren.
    """
    if not quellen:
        return []
    paare = {(q.dienst, q.kennung) for q in quellen}
    kandidaten = (
        db.query(ZeitIst)
        .filter(
            ZeitIst.date >= von,
            ZeitIst.date <= bis,
            ZeitIst.bestaetigt.in_(staende),
            ZeitIst.quelle.in_({q.dienst for q in quellen}),
        )
        .all()
    )
    return [z for z in kandidaten if (z.quelle, z.taetigkeit or "") in paare]


def gemessene_minuten(db: Session, habit: Habit, week_start: date) -> dict:
    """Gemessene Minuten dieses Ziels in dieser Woche, zugeschnitten.

    Gibt `minuten` (zaehlt aufs Ziel), `offene_minuten` (wartet auf Bestaetigung)
    und `quellen` (Klartext, leer heisst „nur Handarbeit") zurueck.
    """
    von, bis = _wochenfenster(week_start)
    quellen = db.query(HabitQuelle).filter(HabitQuelle.habit_id == habit.id).all()

    gezaehlt = zuschneiden(_messungen(db, quellen, von, bis, ZAEHLENDE_STAENDE))
    wartend = zuschneiden(_messungen(db, quellen, von, bis, WARTENDE_STAENDE))

    return {
        "minuten": sum(s.minuten for s in gezaehlt),
        "offene_minuten": sum(s.minuten for s in wartend),
        "quellen": sorted(quellen_text(q) for q in quellen),
    }
