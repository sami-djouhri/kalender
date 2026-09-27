"""Werte, die einem Mandanten gehoeren. Heute genau einer: sein Feed-Token.

**Das Loch, das dieses Modul schliesst.** Der Feed-Token lag ausschliesslich in der
bewusst nicht mandantengetrennten Tabelle ``settings``, und der Endpunkt
``/api/feed-token`` gab deshalb **jedem** denselben, naemlich den des Owners. Wer
ihn hat, oeffnet ueber ``token-login`` den ganzen Kalender des Owners und kann
seine iCal-Feeds lesen. Der ``kalender-bff`` hielt davor einen eigenen
Owner-Riegel (``KALENDER_OWNER_SUB``, fail-closed), der trug aber nur, solange die
Allowlist genau einen Eintrag hatte.

**Der Owner behaelt seinen Wert in ``settings``.** Das ist keine Bequemlichkeit,
sondern der eingefrorene Vertrag mit dem Homelab (Ebene 1,
``~/homelab-work/KALENDER_API_VERTRAG_2026-08-16.md``): Home Assistant fragt
``/api/day-type/today?token=…`` mit genau diesem Wert, und daran haengt das
05:35-Weckfenster. Ein Umzug haette den Wert neu vergeben oder die Leseroute
gescopt -- beides trifft den Wecker. Ein zweiter Mandant bekommt deshalb einen
**eigenen** Token daneben, statt dem Owner seinen wegzunehmen.

★ Daraus folgt die Rollenteilung, die man beim Nachbauen falsch treffen wuerde:
``settings`` ist der Ort des Owners, ``mandant_einstellungen`` der Ort aller
anderen. ``feed_token_fuer`` verdeckt das, ``mandant_fuer_feed_token`` loest es
in beide Richtungen auf.
"""

import secrets

from sqlalchemy.orm import Session

from backend.config import settings as app_settings
from backend.models import MandantEinstellung, Setting

FEED_TOKEN = "feed_token"


def _ist_owner(sub: str) -> bool:
    return sub == app_settings.DEFAULT_OWNER_SUB


def wert_lesen(db: Session, sub: str, key: str) -> str | None:
    """Ein Wert dieses Mandanten. Liest ausdruecklich gefiltert.

    ★ Der Filter steht hier, obwohl ``MandantEinstellung`` im ORM-Scoping haengt.
    Das ist Absicht und folgt ``backend/routers/account.py``: die Funktion wird
    auch mit System-Sessions gerufen (``owner_sub=None``, ungescopt), und dort
    waere ein Verlass auf das automatische Scoping ein Griff in fremde Zeilen.
    """
    zeile = (
        db.query(MandantEinstellung)
        .filter(MandantEinstellung.owner_sub == sub, MandantEinstellung.key == key)
        .first()
    )
    return zeile.value if zeile else None


def wert_setzen(db: Session, sub: str, key: str, wert: str) -> None:
    zeile = (
        db.query(MandantEinstellung)
        .filter(MandantEinstellung.owner_sub == sub, MandantEinstellung.key == key)
        .first()
    )
    if zeile:
        zeile.value = wert
    else:
        # owner_sub gehoert zum Primaerschluessel und wird deshalb ausdruecklich
        # gesetzt, nicht dem before_flush-Stempel ueberlassen.
        db.add(MandantEinstellung(owner_sub=sub, key=key, value=wert))
    db.commit()


def feed_token_fuer(db: Session, sub: str) -> str | None:
    """Der Feed-Token dieses Mandanten. Fuer den Owner der globale aus ``settings``.

    Erzeugt einen fehlenden Mandanten-Token beim ersten Abruf. Fuer den Owner wird
    **nichts** erzeugt: seinen legt der Start an (``main._ensure_feed_token``), und
    ein zweiter Wert daneben waere genau die Doppelung, die den Wecker treffen kann.
    """
    if _ist_owner(sub):
        zeile = db.query(Setting).filter(Setting.key == FEED_TOKEN).first()
        return zeile.value if zeile else None

    vorhanden = wert_lesen(db, sub, FEED_TOKEN)
    if vorhanden:
        return vorhanden
    neu = secrets.token_urlsafe(32)
    wert_setzen(db, sub, FEED_TOKEN, neu)
    return neu


def mandant_fuer_feed_token(db: Session, token: str) -> str | None:
    """Wem gehoert dieser Token? None, wenn niemandem.

    ⚠️ Diese Funktion muss **ungescopt** suchen: sie beantwortet die Frage, wer der
    Mandant ist, kann also nicht schon einen kennen. Sie wird deshalb mit einer
    System-Session gerufen (siehe Aufrufer) und vergleicht in konstanter Zeit.

    ★ Der Owner-Vergleich steht zuerst und bleibt Wort fuer Wort der alte: erst
    wenn der Token NICHT der globale ist, wird ueberhaupt in der Mandanten-Tabelle
    gesucht. Der CORE-Pfad laeuft damit durch genau eine zusaetzliche Verzweigung
    und keine zusaetzliche Abfrage.
    """
    import hmac

    if not token:
        return None
    global_zeile = db.query(Setting).filter(Setting.key == FEED_TOKEN).first()
    if global_zeile and hmac.compare_digest(token, global_zeile.value):
        return app_settings.DEFAULT_OWNER_SUB

    for zeile in db.query(MandantEinstellung).filter(MandantEinstellung.key == FEED_TOKEN).all():
        if hmac.compare_digest(token, zeile.value):
            return zeile.owner_sub
    return None
