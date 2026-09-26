"""Die eine Stelle, die sagt, wie Zeit in diesem Dienst liegt.

DIE KONVENTION
--------------
``Event.start``/``Event.end``, ``HabitSession.start``/``.end`` und alle anderen
Termin-Zeitpunkte liegen **als Berliner Wanduhr ohne Zonenangabe** in der
Datenbank. Steht dort ``2026-05-13 23:30``, dann ist das halb zwoelf abends in
Berlin, im Sommer wie im Winter, ueber beide Zeitumstellungen hinweg.

Das ist fuer einen persoenlichen Kalender die richtige Wahl: ein Termin um 10:00
bleibt um 10:00, auch wenn die Uhr dazwischen umgestellt wird. Wer stattdessen
UTC speichert, verschiebt jede wiederkehrende Reihe zweimal im Jahr um eine
Stunde.

WARUM ES DIESE DATEI BRAUCHT
----------------------------
Die Konvention war bis 2026-08-24 nirgends festgehalten und wurde deshalb an
jeder Modulgrenze neu erfunden, mit drei verschiedenen Antworten:

* ``scheduler._ensure_aware`` liest eine zonenlose Zeit als **Berlin**,
* ``recurrence._aware`` liest dieselbe Zeit als **UTC**,
* ``models.utcnow`` schreibt ``created_at`` tatsaechlich in **UTC**.

Solange alle Werte durch dieselbe Funktion laufen, faellt das nicht auf. Es faellt
in dem Moment auf, wo von aussen eine *zonenbehaftete* Zeit hereinkommt, und
genau das tut das Frontend: ``frontend/js/app.js`` schickt die Fenstergrenzen der
Monats- und Wochenansicht als ``Date.toISOString()``, also ``…T22:00:00.000Z``
fuer Mitternacht Berliner Zeit.

SQLite kennt keine Zonen. SQLAlchemy schreibt eine zonenbehaftete Zeit, indem es
ihre Felder formatiert und ``tzinfo`` **stillschweigend wegwirft**. Aus dem
Fensterrand ``22:00Z`` wurde damit der Vergleichswert ``22:00``, verglichen
gegen Wanduhrzeiten. Das Fenster lag um den UTC-Versatz daneben: Termine nach
22:00 am letzten Tag der Ansicht fielen heraus und tauchten stattdessen am
Folgetag auf. Gemessen in ``scripts/simulieren.py``, Sonde „Fensterschnitt".

Deshalb: **alles, was von aussen hereinkommt, geht zuerst durch
``als_wanduhr()``.** Danach gilt genau eine Zeitrechnung.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

BERLIN = ZoneInfo("Europe/Berlin")


def als_wanduhr(dt: datetime) -> datetime:
    """Bringt einen Zeitpunkt auf die Hauskonvention: Berliner Wanduhr, zonenlos.

    * zonenlos herein  → unveraendert (gilt bereits als Wanduhr)
    * zonenbehaftet    → nach Berlin gerechnet, dann Zone abgestreift

    ``2026-05-13T22:00:00Z`` wird damit zu ``2026-05-14 00:00``, dem Wert, den
    das Frontend gemeint hat, als es Mitternacht in ISO-UTC verpackt hat.
    """
    if dt.tzinfo is None:
        return dt
    return dt.astimezone(BERLIN).replace(tzinfo=None)


def tagesgrenzen(tag: date) -> tuple[datetime, datetime]:
    """Halboffenes Tagesfenster ``[00:00, 00:00 des Folgetags)`` als Wanduhr.

    Halboffen ist Absicht: ein ganztaegiges Ereignis endet um Mitternacht des
    Folgetags und darf diesen Folgetag nicht mitfaerben. Die Ueberlappungsprobe
    lautet deshalb ueberall ``start < tagesende AND ende > tagesbeginn``.
    """
    beginn = datetime(tag.year, tag.month, tag.day)
    return beginn, beginn + timedelta(days=1)


def jetzt() -> datetime:
    """Jetzt, als Berliner Wanduhr ohne Zone: vergleichbar mit DB-Werten."""
    return datetime.now(BERLIN).replace(tzinfo=None)
