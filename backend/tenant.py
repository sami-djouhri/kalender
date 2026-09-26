"""Multi-Tenant-Scoping auf ORM-Ebene (fail-closed): Muster wie lager/fitness/mealprep.

Statt jede einzelne Query zu editieren (eine vergessene = Cross-Tenant-Leck),
haengt sich das Scoping global in die Session:

* do_orm_execute: jede ORM-SELECT/UPDATE/DELETE gegen ein Tenant-Modell bekommt
  automatisch ``owner_sub == <session-sub>`` angehaengt (with_loader_criteria,
  inkl. Joins, Relationship-Loads und Bulk-Query.delete()/update()).
* before_flush: neue Tenant-Objekte werden mit dem sub der Session gestempelt.

Der sub steht in ``session.info["owner_sub"]`` (gesetzt in get_db aus dem
X-Saganta-Sub-Header, Fallback DEFAULT_OWNER_SUB fuer headerlose Aufrufe).

Kontexte:
* Key fehlt (direkte SessionLocal(), z.B. Notification-Runner) → Fallback
  DEFAULT_OWNER_SUB = bisheriges Single-User-Verhalten.
* Key explizit ``None`` (system_db(), Startup-Migrationen/Seeds) → ungescoped,
  kein Stempeln; neue Zeilen bleiben NULL = global/shared.

Drei Modell-Klassen:
* STRICT (Contact, Project, Habit, HabitSession, Todo, TodoCompletion,
  DailyGoal, DailyLoad, DailyReview, DailyCheckIn, ActivityFeedback,
  SecretaryRun): owner_sub NOT NULL, hart pro sub gescoped.
* SHARED (Calendar): owner_sub nullable, NULL = systemweit sichtbar. Der Satz
  System-Kalender ist bewusst global: jeder Mandant braucht die Huellen
  (Arbeit/Schule/Urlaub/Krank/Feiertage/Geburtstage/Termine), um seinen eigenen
  Tagestyp zu setzen. Geteilt sind die Container, nicht die Inhalte.
* Event: STRICT mit genau EINER Ausnahme, Feiertags-Events. Die sind fuer alle
  gleich und tragen deshalb owner_sub NULL.

  ⚠️ Die Ausnahme ist bewusst ein UND (Feiertagskalender UND owner_sub IS NULL),
  nicht nur die Kalender-ID: sonst waere jeder Termin, den irgendein Mandant in den
  Feiertagskalender legt, fuer alle sichtbar. Vorher galt fuer Event die
  SHARED-Regel (eigener sub ODER NULL), damit sah ein fremder Nutzer die
  Arbeitszeiten des Owners im Kalender ``daytype-arbeit``. Genau dieses Leck war
  die Begruendung fuer den (verworfenen) Kalender-Fork.

Nicht tenant-gescoped: Setting (App-global: feed_token/passwort, tenantisiert
man es, brechen _ensure_feed_token/_ensure_password und damit die CORE-Auth),
SessionNotification/EventReminderLog (interne Dedup-Logs ohne Lese-API).
Bitte so lassen.
"""

from sqlalchemy import and_, event, or_
from sqlalchemy.orm import with_loader_criteria

from backend.config import settings
from backend.database import SessionLocal
from backend.models import (
    ActivityFeedback,
    Calendar,
    Contact,
    DailyCheckIn,
    DailyGoal,
    DailyLoad,
    DailyReview,
    Event,
    Habit,
    HabitSession,
    Place,
    Project,
    SecretaryRun,
    TimeBlock,
    Todo,
    TodoCompletion,
    ZeitIst,
)
from backend.system_calendars import FEIERTAG_CALENDAR_ID

STRICT_TENANT_MODELS = (
    Contact,
    Place,
    Project,
    Habit,
    HabitSession,
    Todo,
    TodoCompletion,
    DailyGoal,
    DailyLoad,
    DailyReview,
    DailyCheckIn,
    ActivityFeedback,
    SecretaryRun,
    TimeBlock,
    ZeitIst,
    # Neue STRICT-Modelle hier eintragen (z.B. kuenftig ExternalCalendar/ExternalEvent
    # fuer abonnierte iCal-Feeds), mehr ist fuer die Isolation nicht noetig.
)

SHARED_TENANT_MODELS = (Calendar,)

TENANT_MODELS = STRICT_TENANT_MODELS + SHARED_TENANT_MODELS + (Event,)

# ★ Kein Mandant, sondern ein Platzhalter. Der taegliche Retention-Lauf ist
# systemweit und gehoert keinem Menschen, braucht aber denselben
# Einmal-pro-Tag-Marker wie die mandantengebundenen Laeufe. NULL ginge dafuer
# nicht: SecretaryRun.owner_sub ist NOT NULL. Also traegt genau diese eine
# Zeilensorte einen erfundenen sub.
#
# Er steht hier und nicht in backend/secretary.py, weil "was ist ein Mandant und
# was nur eine Marke" eine Frage der Mandanten-Schicht ist. Wer echte Mandanten
# zaehlt (backend/mandant_ableiten.py), muss ihn ausnehmen. Sonst sieht eine
# Ein-Personen-Installation zwei Mandanten, die Ableitung verweigert sich, und
# das ist hier teuer: am headerlosen Pfad haengt die HA-Weckkette.
#
# ⚠️ secretary.py wiederholt die Zeichenkette derzeit als Literal (zweimal).
# tests/test_mandant_ableiten.py haelt beide Seiten aneinander fest, damit ein
# Umbenennen dort nicht still die Ableitung hier verdirbt.
SYSTEM_RUN_SUB = "__system__"

_MISSING = object()


def _session_sub(session):
    """sub der Session; None = System-Kontext (ungescoped)."""
    sub = session.info.get("owner_sub", _MISSING)
    if sub is _MISSING:
        return settings.DEFAULT_OWNER_SUB
    return sub  # kann None sein → System-Kontext


@event.listens_for(SessionLocal, "do_orm_execute")
def _apply_tenant_scope(execute_state) -> None:
    if execute_state.is_column_load or execute_state.is_relationship_load:
        # Lazy-Loads einer bereits gescopten Instanz nicht doppelt filtern.
        return
    if not (
        execute_state.is_select
        or execute_state.is_update
        or execute_state.is_delete
    ):
        return
    if execute_state.execution_options.get("skip_tenant"):
        return
    sub = _session_sub(execute_state.session)
    if sub is None:  # System-Kontext (Startup-Migrationen/Seeds)
        return
    for model in STRICT_TENANT_MODELS:
        execute_state.statement = execute_state.statement.options(
            with_loader_criteria(
                model, lambda cls: cls.owner_sub == sub, include_aliases=True
            )
        )
    for model in SHARED_TENANT_MODELS:
        execute_state.statement = execute_state.statement.options(
            with_loader_criteria(
                model,
                lambda cls: or_(cls.owner_sub == sub, cls.owner_sub.is_(None)),
                include_aliases=True,
            )
        )
    # Event: eigener Kram ODER ein echtes (owner-loses) Feiertags-Event.
    # FEIERTAG_CALENDAR_ID ist bewusst eine Modulkonstante und keine lokale
    # Variable: SQLAlchemys Lambda-Analyse behandelt Globals als konstant,
    # lokale Closure-Variablen wuerden als Bindparam getrackt.
    execute_state.statement = execute_state.statement.options(
        with_loader_criteria(
            Event,
            lambda cls: or_(
                cls.owner_sub == sub,
                and_(
                    cls.calendar_id == FEIERTAG_CALENDAR_ID,
                    cls.owner_sub.is_(None),
                ),
            ),
            include_aliases=True,
        )
    )


@event.listens_for(SessionLocal, "before_flush")
def _stamp_tenant(session, _flush_context, _instances) -> None:
    sub = _session_sub(session)
    if sub is None:  # System-Kontext: NULL belassen (= global/shared)
        return
    for obj in session.new:
        if isinstance(obj, TENANT_MODELS) and not getattr(obj, "owner_sub", None):
            obj.owner_sub = sub
