"""Zeit von Hand: Timer starten und stoppen, nachtraeglich abhaken.

Entscheide 2026-11-01 (Kalender, Planung): Start per Knopf in App und Web,
nachtraeglich ein Haken mit vorbelegter, korrigierbarer Zeit, und beides **zaehlt
als manuelle Messung**. Gebaut auf dem Fundament vom 25.10. (`ZeitIst`,
`wochenbilanz`), ohne zweites Modell.

Wie ein laufender Timer aussieht
--------------------------------
Eine `ZeitIst`-Zeile mit `quelle="timer"` und dem Stand `laeuft`, in der `end`
gleich `start` ist. Damit ueberlebt er einen Neustart, weil er in der Datenbank
steht und nicht im Prozess.

Bewusst NICHT `end` nullable gemacht, wie im Plan vom 25.10. skizziert. Das
haette in SQLite einen Tabellenumbau verlangt, in `on_startup()`, an einem
CORE-Dienst, und an genau dieser Stelle kam der Dienst am 2026-09-06 nicht mehr
hoch. Eine Zeile der Laenge null ist fuer jeden Leser harmlos: die Bilanz zaehlt
nur `wochenbilanz.ZAEHLENDE_STAENDE`, und `laeuft` steht dort nicht; der Zuschnitt
verwirft alles unter der Mindestdauer.

Beim Stoppen wird die Laufzeile durch die echte Messung ersetzt, ueber
`zeit_ingest.eintrag_aufnehmen`. Damit gelten fuer Hand-Messungen dieselben
Regeln wie fuer Melder: Teilung an Mitternacht, hoechstens zwei Tage, Idempotenz.

Zuordnung zum Wochenziel
------------------------
Laeuft ein Timer fuer eine Habit-Sitzung, traegt die Messung `taetigkeit` = die
Habit-ID, und der Habit bekommt (falls noch nicht) die Quelle
`timer/<habit-id>`. Damit zaehlt sie ueber den normalen Weg der Bilanz, ohne
Sonderfall. Termine und Aufgaben haben kein Wochenziel; ihre Messung steht fuer
sich und traegt den Bezug in `verweis`.

⚠️ Die Autoerledigung in `routers/habits.py` bleibt vorerst (Entscheid
2026-11-01 „erst Ersatz, dann Wegfall"). Ein Haken auf einer Sitzung setzt
deshalb zusaetzlich `status = completed`, damit `completed_hours` ihn sieht.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from backend.models import Event, Habit, HabitQuelle, HabitSession, Todo, ZeitIst
from backend.scheduler import calculate_focus_minutes
from backend.wanduhr import als_wanduhr
from backend.zeit_ingest import IngestFehler, _als_zeitpunkt, eintrag_aufnehmen

QUELLE = "timer"
LAEUFT = "laeuft"

# Welche Art eine Messung bekommt, wenn der Aufrufer keine nennt.
ART_JE_BEZUG = {"session": "gewohnheit", "event": "fix", "todo": "aufgabe", None: "erholung"}

# Ohne Bezug zu einer Sitzung: so lange laeuft ein vergessener Timer hoechstens.
# Mit Sitzung gilt `Habit.max_session_minutes`.
KAPPUNG_OHNE_HABIT_MINUTEN = 8 * 60


def jetzt() -> datetime:
    return als_wanduhr(datetime.now(timezone.utc)).replace(second=0, microsecond=0)


def laufender(db: Session) -> ZeitIst | None:
    return (
        db.query(ZeitIst)
        .filter(ZeitIst.quelle == QUELLE, ZeitIst.bestaetigt == LAEUFT)
        .order_by(ZeitIst.start.desc())
        .first()
    )


def als_antwort(zeile: ZeitIst | None) -> dict | None:
    if zeile is None:
        return None
    bezug_typ, _, bezug_id = (zeile.verweis or "").partition(":")
    return {
        "id": zeile.id,
        "titel": zeile.titel,
        "art": zeile.art,
        "seit": zeile.start.replace(tzinfo=None).isoformat(timespec="minutes"),
        "bezug_typ": bezug_typ or None,
        "bezug_id": bezug_id or None,
    }


def _bezug_pruefen(db: Session, bezug_typ: str | None, bezug_id: str | None):
    """Findet das Gegenstueck oder sagt klar, warum nicht. Gibt (Sitzung, Habit) zurueck.

    Termine aus Serien haben virtuelle IDs (`<serie>::<datum>`, `recurrence.py`),
    die es in der Tabelle nicht gibt. Gesucht wird deshalb mit dem Teil davor,
    abgelegt wird die volle Instanz-ID: nur sie unterscheidet zwei Montage.
    """
    if not bezug_typ:
        return None, None
    if bezug_typ not in ("session", "event", "todo"):
        raise IngestFehler(f"Unbekannter Bezug '{bezug_typ}'")
    if not bezug_id:
        raise IngestFehler("'bezug_id' fehlt")
    if bezug_typ == "session":
        sitzung = db.query(HabitSession).filter(HabitSession.id == bezug_id).first()
        if sitzung is None:
            raise LookupError("Sitzung nicht gefunden")
        return sitzung, sitzung.habit
    if bezug_typ == "event":
        kern = bezug_id.split("::", 1)[0]
        if db.query(Event).filter(Event.id.in_({bezug_id, kern})).first() is None:
            raise LookupError("Termin nicht gefunden")
        return None, None
    if db.query(Todo).filter(Todo.id == bezug_id).first() is None:
        raise LookupError("Aufgabe nicht gefunden")
    return None, None


def _quelle_sicherstellen(db: Session, habit: Habit) -> None:
    """Legt `timer/<habit-id>` am Habit an, falls es fehlt.

    Ohne diese Quelle laege die Messung in der Tabelle und zaehlte auf kein Ziel,
    und zwar lautlos: die Bilanz loest die Zuordnung ueber `HabitQuelle` auf. Die
    Kennung ist die Habit-ID und nicht der Name, weil ein Name sich aendert.
    """
    vorhanden = (
        db.query(HabitQuelle)
        .filter(HabitQuelle.dienst == QUELLE, HabitQuelle.kennung == habit.id)
        .first()
    )
    if vorhanden is None:
        db.add(HabitQuelle(habit_id=habit.id, dienst=QUELLE, kennung=habit.id))
        db.flush()


def _sitzung_erledigt(db: Session, sitzung: HabitSession, minuten: int) -> None:
    """Haken auf der Sitzung, solange `completed_hours` noch am Status haengt."""
    from backend.routers.habits import _update_daily_load_on_completion

    sitzung.status = "completed"
    habit = sitzung.habit
    if habit is not None and habit.learning_mode:
        sitzung.focus_minutes_completed = calculate_focus_minutes(
            minuten, habit.focus_block_minutes, habit.break_minutes
        )
    _update_daily_load_on_completion(db, sitzung)


def _messung_schreiben(
    db: Session,
    *,
    titel: str,
    art: str,
    start: datetime,
    ende: datetime,
    bezug_typ: str | None,
    bezug_id: str | None,
    habit: Habit | None,
    extern_id: str,
) -> ZeitIst | None:
    if habit is not None:
        _quelle_sicherstellen(db, habit)
    zeile, _ = eintrag_aufnehmen(db, {
        "quelle": QUELLE,
        "extern_id": extern_id,
        "start": start,
        "ende": ende,
        "art": art,
        "titel": titel,
        "taetigkeit": habit.id if habit is not None else "",
        "verweis": f"{bezug_typ}:{bezug_id}" if bezug_typ else None,
        "bestaetigt": "manuell",
        # Von Hand gestoppt ist auf die Minute genau; ein Zehn-Minuten-Riegel
        # wuerde einen kurzen, bewusst gemessenen Anruf verwerfen.
        "genau": True,
    })
    return zeile


def _kappung(habit: Habit | None) -> timedelta:
    minuten = habit.max_session_minutes if habit is not None and habit.max_session_minutes else KAPPUNG_OHNE_HABIT_MINUTEN
    return timedelta(minutes=minuten)


def stoppen(db: Session, ende: datetime | None = None) -> dict | None:
    """Beendet den laufenden Timer und schreibt die Messung. `None`, wenn keiner laeuft.

    Gekappt bei `max_session_minutes` des Habits bzw. acht Stunden. Ein Timer,
    der ueber Nacht vergessen wurde, soll keine 14 Stunden Lesen belegen.
    """
    lauf = laufender(db)
    if lauf is None:
        return None
    beginn = lauf.start.replace(tzinfo=None)
    schluss = als_wanduhr(ende) if ende is not None else jetzt()
    bezug_typ, _, bezug_id = (lauf.verweis or "").partition(":")
    sitzung = habit = None
    if bezug_typ == "session":
        sitzung = db.query(HabitSession).filter(HabitSession.id == bezug_id).first()
        habit = sitzung.habit if sitzung is not None else None
    gekappt = False
    if schluss - beginn > _kappung(habit):
        schluss = beginn + _kappung(habit)
        gekappt = True
    titel, art, extern = lauf.titel or "Timer", lauf.art, lauf.extern_id
    db.delete(lauf)
    db.flush()
    minuten = int((schluss - beginn).total_seconds() // 60)
    zeile = None
    if minuten >= 1:
        zeile = _messung_schreiben(
            db, titel=titel, art=art, start=beginn, ende=schluss,
            bezug_typ=bezug_typ or None, bezug_id=bezug_id or None,
            habit=habit, extern_id=extern,
        )
        if sitzung is not None:
            _sitzung_erledigt(db, sitzung, minuten)
    db.commit()
    return {"minuten": max(minuten, 0), "gekappt": gekappt, "geschrieben": zeile is not None}


def starten(
    db: Session,
    *,
    titel: str,
    art: str | None = None,
    bezug_typ: str | None = None,
    bezug_id: str | None = None,
    seit: datetime | None = None,
) -> ZeitIst:
    """Startet einen Timer. Ein laufender wird dabei beendet, zum neuen Beginn.

    Die naechste Taetigkeit beendet die vorige (Owner-Idee vom 25.10.): es gibt
    keine ueberlappenden Hand-Messungen. Wer vergisst zu stoppen, verliert dadurch
    nichts, er beginnt einfach das Naechste.
    """
    titel = (titel or "").strip()[:300]
    if not titel:
        raise IngestFehler("'titel' fehlt")
    sitzung, habit = _bezug_pruefen(db, bezug_typ, bezug_id)
    art = art or ART_JE_BEZUG.get(bezug_typ, "erholung")
    from backend.tagesdecke import ARTEN

    if art not in ARTEN:
        raise IngestFehler(f"Unbekannte Art '{art}'")
    beginn = als_wanduhr(seit).replace(second=0, microsecond=0) if seit is not None else jetzt()
    if beginn > jetzt() + timedelta(minutes=1):
        raise IngestFehler("Ein Timer kann nicht in der Zukunft beginnen")
    if laufender(db) is not None:
        stoppen(db, ende=max(beginn, laufender(db).start.replace(tzinfo=None)))
    if habit is not None:
        # Schon beim Start, damit die Zuordnung sichtbar ist, solange er laeuft.
        _quelle_sicherstellen(db, habit)
    if sitzung is not None and sitzung.status == "pending":
        # Wer anfaengt, hat angenommen. Gleiche Regel wie die Auto-Annahme beim Abruf.
        sitzung.status = "accepted"
    zeile = ZeitIst(
        date=beginn.date(), start=beginn, end=beginn, art=art, titel=titel,
        quelle=QUELLE, extern_id=f"lauf-{uuid.uuid4()}", taetigkeit="",
        verweis=f"{bezug_typ}:{bezug_id}" if bezug_typ else None,
        bestaetigt=LAEUFT, genau=True,
    )
    db.add(zeile)
    db.commit()
    db.refresh(zeile)
    return zeile


def abhaken(
    db: Session,
    *,
    titel: str,
    start,
    ende,
    art: str | None = None,
    bezug_typ: str | None = None,
    bezug_id: str | None = None,
) -> dict:
    """Nachtraeglich: es hat stattgefunden, von bis. Zaehlt als manuelle Messung.

    Idempotent je Bezug: ein zweiter Haken auf denselben Termin korrigiert die
    Zeit, statt eine zweite Messung daneben zu legen. Ohne Bezug entsteht jedes
    Mal eine neue.
    """
    titel = (titel or "").strip()[:300]
    if not titel:
        raise IngestFehler("'titel' fehlt")
    beginn = als_wanduhr(_als_zeitpunkt(start, "start"))
    schluss = als_wanduhr(_als_zeitpunkt(ende, "ende"))
    if schluss <= beginn:
        raise IngestFehler("'ende' muss nach 'start' liegen")
    if schluss > jetzt() + timedelta(minutes=1):
        raise IngestFehler("Was noch nicht vorbei ist, laesst sich nicht abhaken")
    sitzung, habit = _bezug_pruefen(db, bezug_typ, bezug_id)
    art = art or ART_JE_BEZUG.get(bezug_typ, "erholung")
    extern = f"haken-{bezug_typ}-{bezug_id}" if bezug_typ else f"haken-{uuid.uuid4()}"
    _messung_schreiben(
        db, titel=titel, art=art, start=beginn, ende=schluss,
        bezug_typ=bezug_typ, bezug_id=bezug_id, habit=habit, extern_id=extern,
    )
    minuten = int((schluss - beginn).total_seconds() // 60)
    if sitzung is not None:
        _sitzung_erledigt(db, sitzung, minuten)
    db.commit()
    return {"minuten": minuten}


def belege(db: Session, von, bis) -> list[dict]:
    """Hand-Messungen mit Bezug im Zeitraum: woran die Oberflaeche „erledigt" erkennt."""
    zeilen = (
        db.query(ZeitIst)
        .filter(
            ZeitIst.quelle == QUELLE,
            ZeitIst.bestaetigt == "manuell",
            ZeitIst.date >= von,
            ZeitIst.date <= bis,
            ZeitIst.verweis.isnot(None),
        )
        .order_by(ZeitIst.start)
        .all()
    )
    return [
        {
            "verweis": z.verweis,
            "start": z.start.replace(tzinfo=None).isoformat(timespec="minutes"),
            "ende": z.end.replace(tzinfo=None).isoformat(timespec="minutes"),
            "titel": z.titel,
        }
        for z in zeilen
    ]
