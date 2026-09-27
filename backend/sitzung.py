"""Das Sitzungs-Token des Kalenders: es sagt jetzt WER angemeldet ist, nicht nur DASS.

**Was hier bis 2026-09-27 fehlte.** ``create_jwt_token`` schrieb fest ``sub: "admin"``
in jedes Token, und ``verify_jwt_token`` beantwortete genau eine Frage: ist die
Unterschrift gueltig. Das war fuer einen Kalender mit einem Benutzer richtig und
vollstaendig. Sobald aber ein zweiter Mandant existiert, ist es die Luecke, an der
die ganze Mandantentrennung vorbeilaeuft: das ORM-Scoping in ``tenant.py`` haengt am
``owner_sub`` der Session, und den kannte der Anmeldeweg nicht. Wer ueber das native
Frontend hereinkam, landete deshalb immer auf ``DEFAULT_OWNER_SUB``, also beim Owner
und seinen Daten, unabhaengig davon, wer sich angemeldet hatte.

**Warum ein eigenes Modul und nicht einfach ein Feld mehr in main.py.**
``backend/tenant_auth.py`` muss den Mandanten eines Requests bestimmen und wird von
``database.get_db`` gerufen, also von fast jeder Route. Es kann ``main`` nicht
importieren (``main`` importiert ``database``, das waere ein Zyklus). Beide brauchen
denselben Schluessel und dieselbe Leseregel. Genau dafuer ist dies der gemeinsame
Ort: es importiert nur ``config``, haengt an nichts und kann von beiden Seiten
benutzt werden.

**Der Altbestand bleibt gueltig** (Owner-Entscheid 2026-09-27). Ein Token mit
``sub: "admin"`` wird als Owner gelesen, statt abgelehnt zu werden. Sonst waere mit
dem Aufspielen jede offene Sitzung ungueltig geworden, inklusive der Handy-App und
des Auto-Logins am Vhost, und das sieht fuer den Benutzer nach einem kaputten Dienst
aus. Neu ausgestellt wird nur noch mit echtem ``sub``; der Altbestand laeuft mit
seiner eigenen Laufzeit aus.
"""

from datetime import datetime, timedelta, timezone

import jwt

from backend.config import settings

# Der Wert, den jedes vor dem 2026-09-27 ausgestellte Token traegt. Er ist kein
# Mandant, sondern die Abwesenheit eines Mandanten.
ALT_SUB = "admin"

# Der Schluessel darf genau eine Quelle haben, sonst signieren zwei Stellen mit
# verschiedenen Werten und jede haelt die Token der anderen fuer gefaelscht.
#
# Beim Import steht hier der Wert aus der Umgebung, falls es einen gibt -- genau
# wie in ``main``, das sein ``_SECRET_KEY`` ebenfalls beim Import belegt. Das ist
# noetig, weil Routen schon vor dem Startup-Ereignis erreichbar sind (mehrere
# Testmodule benutzen den TestClient ohne Kontextmanager, und auch im Betrieb ist
# die Reihenfolge nichts, worauf man sich verlassen will).
#
# ⚠️ Ohne Umgebungsvariable bleibt er hier LEER und wird erst im Startup aus der
# Datenbank belegt (``main._ensure_secret_key``). Ein eigener Zufallswert an
# dieser Stelle waere der schlimmste Fall: zwei Module mit je einem gueltigen,
# aber verschiedenen Geheimnis.
_schluessel: str = settings.SECRET_KEY or ""


def schluessel_setzen(wert: str) -> None:
    global _schluessel
    _schluessel = wert


def schluessel() -> str:
    return _schluessel


def token_bauen(sub: str, stunden: int | None = None) -> str:
    """Sitzungs-Token fuer genau diesen Mandanten.

    ★★ Ohne gesetzten Schluessel wird **nichts** ausgestellt. Das ist kein
    Formalismus: ``jwt.encode(nutzlast, "")`` liefert bereitwillig ein Token, das
    mit dem leeren Geheimnis signiert ist, und ein solches Token kann jeder
    nachbauen. Der Schluessel kommt aus ``main._ensure_secret_key`` beim Start;
    wer hier vorher landet, hat ein Reihenfolgeproblem, und das soll lautstark
    auffallen statt eine faelschbare Sitzung zu erzeugen. Gefunden am
    2026-09-27, weil genau dieser Fall in den Tests eintrat.
    """
    if not _schluessel:
        raise RuntimeError(
            "Sitzungs-Schluessel nicht gesetzt (main._ensure_secret_key). "
            "Ein Token mit leerem Geheimnis waere faelschbar."
        )
    laufzeit = settings.JWT_EXPIRATION_HOURS if stunden is None else stunden
    nutzlast = {
        "sub": sub,
        "exp": datetime.now(timezone.utc) + timedelta(hours=laufzeit),
        "iat": datetime.now(timezone.utc),
    }
    return jwt.encode(nutzlast, _schluessel, algorithm=settings.JWT_ALGORITHM)


def sub_aus_token(token: str, *, ablauf_pruefen: bool = True) -> str | None:
    """Mandant aus einem Token, oder None wenn es nicht gueltig ist.

    ``ablauf_pruefen=False`` braucht nur ``/api/auth/refresh``: der darf ein
    abgelaufenes Token noch einmal ansehen, um zu entscheiden, ob es die
    Nachfrist trifft. Jeder andere Aufrufer prueft den Ablauf.
    """
    if not token or not _schluessel:
        return None
    try:
        nutzlast = jwt.decode(
            token,
            _schluessel,
            algorithms=[settings.JWT_ALGORITHM],
            options={"verify_exp": ablauf_pruefen},
        )
    except jwt.PyJWTError:
        return None
    sub = nutzlast.get("sub")
    if not sub:
        return None
    # Der Altbestand gilt als Owner, siehe Modulkopf.
    return settings.DEFAULT_OWNER_SUB if sub == ALT_SUB else str(sub)


def token_aus_anfrage(request) -> str | None:
    """Das Token dieser Anfrage: Authorization-Kopf zuerst, dann Cookie.

    Dieselbe Reihenfolge wie in ``main.get_current_user``. Sie steht an zwei
    Stellen, weil ``get_current_user`` zusaetzlich 401 wirft und diese Funktion
    bewusst nicht: ``resolve_owner_sub`` laeuft auch auf Routen ohne Anmeldung
    (day-type mit Feed-Token, iCal, Health) und darf sie nicht abweisen.
    """
    if request is None:
        return None
    kopf = request.headers.get("Authorization", "")
    if kopf.startswith("Bearer "):
        return kopf[7:]
    try:
        return request.cookies.get("session_token")
    except AttributeError:
        # Nicht jedes Request-artige Objekt fuehrt Cookies (Testdoubles).
        return None


def sub_aus_anfrage(request) -> str | None:
    """Mandant aus dem Sitzungs-Token dieser Anfrage, oder None."""
    token = token_aus_anfrage(request)
    return sub_aus_token(token) if token else None
