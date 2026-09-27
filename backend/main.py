import hmac
import logging
import secrets
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import jwt
from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy.orm import Session

from sqlalchemy import text

from backend.activities import infer_activity_type
from backend.birthday_utils import GEBURTSTAGE_CALENDAR_ID, regenerate_birthday_events
from backend.config import settings
from backend.dashboard import build_dashboard_data
from backend.database import engine, get_db, init_db, system_db
from backend.holidays_nrw import get_nrw_holidays
from backend.mandant_ableiten import einzigen_mandanten_ableiten
from backend.models import ActivityFeedback, Calendar, Contact, DailyCheckIn, DailyGoal, DailyReview, Event, EventReminderLog, HabitSession, SecretaryRun, SessionNotification, Setting, TimeBlock, Todo, TodoCompletion, ZeitIst
from backend.parameter_wache import parameter_wache
from backend import mandant_einstellungen, sitzung
from backend.tenant_auth import SIG_HEADER, SUB_HEADER, expected_signature
from backend.zugriffsprotokoll import filter_installieren

# Frueh und ohne Bedingung: haelt den feed_token aus uvicorns Zugriffsprotokoll
# (und damit aus Loki und den Off-Site-Sicherungen). Begruendung im Modul.
filter_installieren()
from backend.routers import account, assistant, calendars, capture, contacts, events, feedback, goals, habits, ical, internal, mobile, places, postfach, projects, reviews, schedule, search, secretary, tagesdecke, todos
from backend.routers.daytype import DAYTYPE_CALENDARS, DAYTYPE_IDS, FEIERTAG_CALENDAR_ID, _make_daytype_event, protected_router as daytype_protected, public_router as daytype_public
import backend.tenant  # noqa: F401  (registriert Multi-Tenant-Scoping fuer Session-Events)
from backend.schemas import AuthStatus, ChangePasswordRequest, LoginRequest, TokenResponse
from backend.security_utils import RateLimiter, hash_password, is_hashed, verify_password
from backend.session_notifications import start_session_notification_runner, stop_session_notification_runner

# Rate-Limiter für Login-Fehlversuche (in-memory, pro Client-IP).
_login_limiter = RateLimiter(max_attempts=8, window_seconds=300)

# Fixed system calendar ID for user events ("Termine")
from backend.system_calendars import TERMINE_CAL_ID, WEGE_CALENDAR_ID  # noqa: E402

app = FastAPI(title="Kalender", docs_url="/api/docs", redoc_url=None)

FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"


# --- Startup ---

@app.on_event("startup")
def on_startup():
    init_db()
    _ensure_secret_key()
    _migrate_db()
    # ★★ MUSS vor jeder ORM-Query auf Event/Contact stehen, nicht erst bei den
    #    thematisch verwandten Migrationen weiter unten. Sobald das ORM-Modell
    #    eine Spalte kennt, selektiert JEDE Query sie mit, auch die in den
    #    Migrationsschritten selbst. Auf einer gewachsenen Datenbank gibt es sie
    #    dann noch nicht, und der Start bricht mit „no such column" ab, bevor die
    #    anlegende Migration ueberhaupt an die Reihe kommt.
    #    Gemessen am 2026-09-06 gegen eine Kopie der Produktionsdatenbank: mit
    #    dem Aufruf weiter unten starb der Dienst in _migrate_activity_feedback().
    #    Die Testsuite sah das NICHT, weil create_all auf einer frischen Datenbank
    #    alle Spalten sofort anlegt. Regressionstest: tests/test_migration_alt.py.
    _migrate_orte()
    _mandant_sicherstellen()
    _migrate_multitenant()
    _migrate_learning_mode()
    _migrate_goals_scheduling()
    _migrate_event_recurrence()
    # Früh (vor allen ORM-Event-Queries): sobald das Event-Modell activity_type kennt,
    # selektieren auch die folgenden Migrationsschritte diese Spalte → muss vorher existieren.
    _migrate_activity_feedback()
    _ensure_goal_tables()
    _migrate_category_from_learning_mode()
    _ensure_feed_token()
    _ensure_daytype_calendars()
    _migrate_daytype_times()
    _migrate_timezone()
    _ensure_feiertag_calendar()
    _seed_holidays()
    _ensure_geburtstage_calendar()
    _ensure_wege_calendar()
    _seed_birthdays()
    _ensure_default_calendar()
    _ensure_password()
    _ensure_todo_tables()
    _ensure_notification_tables()
    _ensure_secretary_tables()
    _ensure_checkin_table()
    _ensure_tagesdecke_tables()
    _ensure_indexes()
    start_session_notification_runner()


@app.on_event("shutdown")
def on_shutdown():
    stop_session_notification_runner()


def _migrate_db():
    """Add new columns to existing tables (idempotent)."""
    with engine.connect() as conn:
        try:
            conn.execute(
                text("ALTER TABLE calendars ADD COLUMN is_system BOOLEAN DEFAULT 0")
            )
            conn.commit()
        except Exception:
            conn.rollback()


def _mandant_sicherstellen():
    """Fehlt der konfigurierte Mandant, aus den Daten ableiten und warnen.

    ⚠️ Steht bewusst VOR _migrate_multitenant(). Jene Migration verteilt die
    Bestandsdaten an settings.DEFAULT_OWNER_SUB; laeuft die Ableitung danach,
    ist der Bestand bereits einem leeren sub zugeschrieben und die Ableitung
    findet nur noch das Ergebnis ihres eigenen zu spaeten Starts.

    Auf einer noch nicht migrierten Datenbank gibt es die Spalte owner_sub noch
    nicht. Die Ableitung faengt das ab und liefert None: dort gibt es auch
    nichts abzuleiten, weil ein Bestand ohne Mandantenspalte per Definition
    keinem gehoert. Fuer genau diesen Fall warnt der else-Zweig.
    """
    if settings.DEFAULT_OWNER_SUB:
        return
    abgeleitet = einzigen_mandanten_ableiten()
    if abgeleitet:
        settings.DEFAULT_OWNER_SUB = abgeleitet
        logging.getLogger(__name__).warning(
            "DEFAULT_OWNER_SUB war leer und wurde aus den Daten abgeleitet "
            "(genau ein Mandant vorhanden). Dauerhaft eintragen mit "
            "saganta/scripts/owner-kennung-eintragen.sh"
        )
    else:
        logging.getLogger(__name__).warning(
            "DEFAULT_OWNER_SUB ist leer und nicht ableitbar. Headerlose Aufrufe "
            "sehen keine Daten, und daran haengt der Tagestyp fuer Home "
            "Assistant. Eintragen mit "
            "saganta/scripts/owner-kennung-eintragen.sh"
        )


def _migrate_multitenant():
    """Multi-Tenant: owner_sub auf allen Tenant-Tabellen (idempotent, additiv).

    Bestandsdaten gehen an DEFAULT_OWNER_SUB (Owner behaelt alles). Geteilte
    System-Inhalte bleiben NULL = fuer alle Tenants sichtbar:
    * calendars mit is_system=1 (Termine/Daytype/Feiertage/Geburtstage)
    * events im Feiertags-Kalender (NRW-Feiertage)
    Alle anderen events (Termine, Daytypes, Geburtstage) sind persoenlich -> Owner.

    daily_load/daily_reviews haben date als PK -> Composite-PK (owner_sub, date)
    via Tabellen-Rebuild (SQLite kann PKs nicht in-place aendern). Kopie zuerst,
    Rename zuletzt - laeuft in EINER Transaktion, kein Datenverlust moeglich.
    """
    owner = settings.DEFAULT_OWNER_SUB

    def _cols(conn, table):
        return {row[1] for row in conn.execute(text(f"PRAGMA table_info({table})"))}

    with engine.begin() as conn:
        # 1) Strikte Tenant-Tabellen: NOT NULL + Backfill auf Owner via DEFAULT
        for tbl in (
            "contacts",
            "projects",
            "habits",
            "habit_sessions",
            "todos",
            "todo_completions",
            "daily_goals",
        ):
            if "owner_sub" not in _cols(conn, tbl):
                conn.execute(text(
                    f"ALTER TABLE {tbl} ADD COLUMN owner_sub VARCHAR(128) "
                    f"NOT NULL DEFAULT '{owner}'"
                ))
            conn.execute(text(
                f"CREATE INDEX IF NOT EXISTS ix_{tbl}_owner_sub ON {tbl} (owner_sub)"
            ))

        # 2) Geteilte Tabellen: nullable, System-Inhalte bleiben NULL
        if "owner_sub" not in _cols(conn, "calendars"):
            conn.execute(text("ALTER TABLE calendars ADD COLUMN owner_sub VARCHAR(128)"))
            conn.execute(
                text("UPDATE calendars SET owner_sub = :o "
                     "WHERE is_system IS NULL OR is_system = 0"),
                {"o": owner},
            )
        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_calendars_owner_sub ON calendars (owner_sub)"
        ))
        if "owner_sub" not in _cols(conn, "events"):
            conn.execute(text("ALTER TABLE events ADD COLUMN owner_sub VARCHAR(128)"))
            conn.execute(
                text("UPDATE events SET owner_sub = :o WHERE calendar_id != :feiertag"),
                {"o": owner, "feiertag": FEIERTAG_CALENDAR_ID},
            )
        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_events_owner_sub ON events (owner_sub)"
        ))

        # 3) date-PK-Tabellen: Rebuild auf Composite-PK (owner_sub, date)
        if "owner_sub" not in _cols(conn, "daily_load"):
            conn.execute(text(f"""
                CREATE TABLE daily_load_mt (
                    owner_sub VARCHAR(128) NOT NULL DEFAULT '{owner}',
                    date DATE NOT NULL,
                    learning_minutes INTEGER NOT NULL,
                    strain FLOAT NOT NULL,
                    is_off_day BOOLEAN NOT NULL,
                    off_day_source VARCHAR(20),
                    deferred_minutes INTEGER NOT NULL,
                    PRIMARY KEY (owner_sub, date)
                )
            """))
            conn.execute(text(
                "INSERT INTO daily_load_mt (owner_sub, date, learning_minutes, "
                "strain, is_off_day, off_day_source, deferred_minutes) "
                f"SELECT '{owner}', date, learning_minutes, strain, is_off_day, "
                "off_day_source, deferred_minutes FROM daily_load"
            ))
            conn.execute(text("DROP TABLE daily_load"))
            conn.execute(text("ALTER TABLE daily_load_mt RENAME TO daily_load"))

        if "owner_sub" not in _cols(conn, "daily_reviews"):
            conn.execute(text(f"""
                CREATE TABLE daily_reviews_mt (
                    owner_sub VARCHAR(128) NOT NULL DEFAULT '{owner}',
                    date DATE NOT NULL,
                    what_went_well TEXT,
                    blockers TEXT,
                    abandoned_goals_reason TEXT,
                    carry_over_to_tomorrow TEXT,
                    energy_level VARCHAR(10),
                    created_at DATETIME,
                    updated_at DATETIME,
                    PRIMARY KEY (owner_sub, date)
                )
            """))
            conn.execute(text(
                "INSERT INTO daily_reviews_mt (owner_sub, date, what_went_well, "
                "blockers, abandoned_goals_reason, carry_over_to_tomorrow, "
                "energy_level, created_at, updated_at) "
                f"SELECT '{owner}', date, what_went_well, blockers, "
                "abandoned_goals_reason, carry_over_to_tomorrow, energy_level, "
                "created_at, updated_at FROM daily_reviews"
            ))
            conn.execute(text("DROP TABLE daily_reviews"))
            conn.execute(text("ALTER TABLE daily_reviews_mt RENAME TO daily_reviews"))


def _migrate_learning_mode():
    """Add learning mode columns to habits and habit_sessions (idempotent)."""
    migrations = [
        "ALTER TABLE habits ADD COLUMN learning_mode BOOLEAN DEFAULT 0",
        "ALTER TABLE habits ADD COLUMN focus_block_minutes INTEGER DEFAULT 25",
        "ALTER TABLE habits ADD COLUMN break_minutes INTEGER DEFAULT 5",
        "ALTER TABLE habit_sessions ADD COLUMN focus_minutes_completed INTEGER",
        # Load management columns on Habit
        "ALTER TABLE habits ADD COLUMN category VARCHAR(20) DEFAULT 'sonstige'",
        "ALTER TABLE habits ADD COLUMN weekday_target_ratio REAL DEFAULT 0.3",
        "ALTER TABLE habits ADD COLUMN max_consecutive_days INTEGER DEFAULT 3",
        "ALTER TABLE habits ADD COLUMN max_session_minutes INTEGER DEFAULT 120",
        # Session type and pinning on HabitSession
        "ALTER TABLE habit_sessions ADD COLUMN session_type VARCHAR(20) DEFAULT 'standard'",
        "ALTER TABLE habit_sessions ADD COLUMN pinned BOOLEAN DEFAULT 0",
    ]
    with engine.connect() as conn:
        for sql in migrations:
            try:
                conn.execute(text(sql))
                conn.commit()
            except Exception:
                conn.rollback()


def _migrate_goals_scheduling():
    """Add Tagesziele + Todo-Scheduling + Habit-Override columns (idempotent)."""
    migrations = [
        # Todo: Pool-/Scheduling-/Defer-Modell
        "ALTER TABLE todos ADD COLUMN status VARCHAR(20) DEFAULT 'open'",
        "ALTER TABLE todos ADD COLUMN estimated_minutes INTEGER",
        "ALTER TABLE todos ADD COLUMN scheduling_mode VARCHAR(20) DEFAULT 'pool'",
        "ALTER TABLE todos ADD COLUMN planned_date DATE",
        "ALTER TABLE todos ADD COLUMN scheduled_start DATETIME",
        "ALTER TABLE todos ADD COLUMN scheduled_end DATETIME",
        "ALTER TABLE todos ADD COLUMN earliest_start_date DATE",
        "ALTER TABLE todos ADD COLUMN energy_required VARCHAR(10)",
        "ALTER TABLE todos ADD COLUMN goal_id VARCHAR(36)",
        "ALTER TABLE todos ADD COLUMN defer_count INTEGER DEFAULT 0",
        "ALTER TABLE todos ADD COLUMN last_deferred_at DATETIME",
        "ALTER TABLE todos ADD COLUMN defer_reason TEXT",
        # Habit: Ziel-Override + Wochenminimum
        "ALTER TABLE habits ADD COLUMN can_be_overridden_by_goals BOOLEAN DEFAULT 1",
        "ALTER TABLE habits ADD COLUMN can_be_split BOOLEAN DEFAULT 1",
        "ALTER TABLE habits ADD COLUMN weekly_minimum_hours REAL",
        "ALTER TABLE habits ADD COLUMN preferred_days TEXT",
        # HabitSession: durch Ziel verdrängt
        "ALTER TABLE habit_sessions ADD COLUMN overridden_by_goal_id VARCHAR(36)",
    ]
    with engine.connect() as conn:
        for sql in migrations:
            try:
                conn.execute(text(sql))
                conn.commit()
            except Exception:
                conn.rollback()
    # Backfill: bestehende erledigte Todos auf status='done' setzen
    with engine.connect() as conn:
        try:
            conn.execute(text("UPDATE todos SET status='done' WHERE completed=1 AND (status IS NULL OR status='open')"))
            conn.commit()
        except Exception:
            conn.rollback()


def _migrate_event_recurrence():
    """Add EXDATE + reminder columns to events (idempotent)."""
    migrations = [
        "ALTER TABLE events ADD COLUMN recurrence_exdates TEXT",
        "ALTER TABLE events ADD COLUMN reminder_minutes INTEGER",
    ]
    with engine.connect() as conn:
        for sql in migrations:
            try:
                conn.execute(text(sql))
                conn.commit()
            except Exception:
                conn.rollback()


def _ensure_goal_tables():
    """Create Tagesziele/Review tables if they don't exist."""
    DailyGoal.__table__.create(engine, checkfirst=True)
    DailyReview.__table__.create(engine, checkfirst=True)


def _migrate_category_from_learning_mode():
    """Migrate existing learning_mode=True habits to category='lernen' (one-time)."""
    db = next(system_db())
    try:
        from backend.models import Habit
        habits_to_migrate = (
            db.query(Habit)
            .filter(Habit.learning_mode == True, Habit.category == "sonstige")
            .all()
        )
        for h in habits_to_migrate:
            h.category = "lernen"
        if habits_to_migrate:
            db.commit()
    finally:
        db.close()


def _ensure_daytype_calendars():
    """Create the system calendars for day types if they don't exist."""
    colors = {
        "arbeit": "#4CAF50",
        "schule": "#FF9800",
        "urlaub": "#2196F3",
        "krank": "#9E9E9E",
    }
    db = next(system_db())
    try:
        for dtype, cal_id in DAYTYPE_CALENDARS.items():
            existing = db.query(Calendar).filter(Calendar.id == cal_id).first()
            if not existing:
                cal = Calendar(
                    id=cal_id,
                    name=dtype.capitalize(),
                    color=colors[dtype],
                    is_system=True,
                )
                db.add(cal)
        db.commit()
    finally:
        db.close()


def _migrate_daytype_times():
    """Convert existing all-day Arbeit/Schule events to timed events."""
    db = next(system_db())
    try:
        for dtype in ("arbeit", "schule"):
            cal_id = DAYTYPE_CALENDARS[dtype]
            allday_events = (
                db.query(Event)
                .filter(Event.calendar_id == cal_id, Event.all_day == True)
                .all()
            )
            for evt in allday_events:
                day = evt.start.date() if hasattr(evt.start, 'date') else date.fromisoformat(str(evt.start)[:10])
                new_times = _make_daytype_event(dtype, day)
                evt.start = new_times["start"]
                evt.end = new_times["end"]
                evt.all_day = new_times["all_day"]
                evt.title = "Arbeit" if dtype == "arbeit" else "Schule"
        db.commit()
    finally:
        db.close()


def _migrate_timezone():
    """Migrate existing data from UTC to Berlin timezone.

    - Deletes pending habit sessions ONCE (schedule-ahead in initApp recreates them
      with Berlin TZ). Gated by a `tz_migration_done` flag in settings so subsequent
      container restarts do NOT wipe freshly scheduled sessions (war fragil: lief bei
      JEDEM Boot und verließ sich auf Frontend-Re-Scheduling).
    - Recreates timed Arbeit/Schule daytype events with Berlin timezone (idempotent).
    """
    db = next(system_db())
    try:
        flag = db.query(Setting).filter(Setting.key == "tz_migration_done").first()
        if flag is None:
            # Einmalig: bestehende pending Sessions löschen, dann Flag setzen.
            db.query(HabitSession).filter(HabitSession.status == "pending").delete()
            db.add(Setting(key="tz_migration_done", value="1"))

        # Recreate timed Arbeit/Schule events with Berlin TZ (idempotent, jeder Boot)
        for dtype in ("arbeit", "schule"):
            cal_id = DAYTYPE_CALENDARS[dtype]
            timed_events = (
                db.query(Event)
                .filter(Event.calendar_id == cal_id, Event.all_day == False)
                .all()
            )
            for evt in timed_events:
                day = evt.start.date() if hasattr(evt.start, 'date') else date.fromisoformat(str(evt.start)[:10])
                new_times = _make_daytype_event(dtype, day)
                evt.start = new_times["start"]
                evt.end = new_times["end"]

        db.commit()
    finally:
        db.close()


def _ensure_feiertag_calendar():
    """Create the system calendar for Feiertage if it doesn't exist."""
    db = next(system_db())
    try:
        existing = db.query(Calendar).filter(Calendar.id == FEIERTAG_CALENDAR_ID).first()
        if not existing:
            cal = Calendar(
                id=FEIERTAG_CALENDAR_ID,
                name="Feiertage",
                color="#E91E63",
                is_system=True,
            )
            db.add(cal)
            db.commit()
    finally:
        db.close()


def _migrate_orte():
    """Adressen an Kontakten, Orte-Tabelle, Ort und Weg am Termin (idempotent).

    Die Tabelle `places` legt `Base.metadata.create_all` in init_db() selbst an;
    hier stehen nur die Spalten, die zu BESTEHENDEN Tabellen kommen.
    """
    migrations = [
        # Kontakt: Adresse als Text plus aufgeloeste Koordinaten fuer die Karte.
        "ALTER TABLE contacts ADD COLUMN address VARCHAR(300)",
        "ALTER TABLE contacts ADD COLUMN lat FLOAT",
        "ALTER TABLE contacts ADD COLUMN lon FLOAT",
        # Termin: aufgeloester Ort neben dem bestehenden Freitext `location`.
        "ALTER TABLE events ADD COLUMN place_id VARCHAR(36)",
        "ALTER TABLE events ADD COLUMN lat FLOAT",
        "ALTER TABLE events ADD COLUMN lon FLOAT",
        # Weg-Termine: Rueckverweis auf den Termin, zu dem der Weg fuehrt.
        "ALTER TABLE events ADD COLUMN travel_for_event_id VARCHAR(36)",
        "ALTER TABLE events ADD COLUMN travel_mode VARCHAR(20)",
    ]
    with engine.connect() as conn:
        for sql in migrations:
            try:
                conn.execute(text(sql))
                conn.commit()
            except Exception:
                conn.rollback()


def _ensure_wege_calendar():
    """System-Kalender fuer berechnete Wege anlegen, falls er fehlt."""
    db = next(system_db())
    try:
        vorhanden = db.query(Calendar).filter(Calendar.id == WEGE_CALENDAR_ID).first()
        if not vorhanden:
            db.add(Calendar(
                id=WEGE_CALENDAR_ID,
                name="Wege",
                color="#00897B",
                description="Berechnete Wegzeiten zu Terminen mit Ort. Wird automatisch "
                            "gepflegt: von Hand geaenderte Eintraege werden ueberschrieben.",
                is_system=True,
            ))
            db.commit()
    finally:
        db.close()


def _ensure_geburtstage_calendar():
    """Create the system calendar for Geburtstage if it doesn't exist."""
    db = next(system_db())
    try:
        existing = db.query(Calendar).filter(Calendar.id == GEBURTSTAGE_CALENDAR_ID).first()
        if not existing:
            cal = Calendar(
                id=GEBURTSTAGE_CALENDAR_ID,
                name="Geburtstage",
                color="#9C27B0",
                is_system=True,
            )
            db.add(cal)
            db.commit()
    finally:
        db.close()


def _seed_birthdays():
    """Regenerate birthday events from contacts on startup."""
    db = next(system_db())
    try:
        regenerate_birthday_events(db)
    finally:
        db.close()


def _ensure_default_calendar():
    """Legt den System-Kalender 'Termine' mit fester ID an (idempotent).

    ⚠️ Frueher raeumte diese Funktion zusaetzlich auf: sie schob die Termine ALLER
    nicht-System-Kalender nach 'Termine' und loeschte diese Kalender, bei JEDEM
    Start, ungescoped ueber alle Mandanten hinweg. Solange es nur den Owner gab,
    traf das nichts. Mit mehreren Nutzern waere es stiller Datenverlust ueber
    Mandantengrenzen: der Kalender eines fremden Nutzers verschwindet beim naechsten
    Neustart, und seine Termine landen im GETEILTEN 'Termine'-Kalender, also
    sichtbar fuer andere. Deshalb entfernt: Aufraeumen gehoert nicht in den Boot.
    """
    db = next(system_db())
    try:
        existing = db.query(Calendar).filter(Calendar.id == TERMINE_CAL_ID).first()
        if not existing:
            db.add(Calendar(
                id=TERMINE_CAL_ID,
                name="Termine",
                color="#3788d8",
                is_system=True,
            ))
        db.commit()
    finally:
        db.close()


def _seed_holidays():
    """Seed holiday events for the current and next year (idempotent)."""
    db = next(system_db())
    try:
        today = date.today()
        for year in [today.year, today.year + 1]:
            for holiday_date, holiday_name in get_nrw_holidays(year):
                day_start = datetime(
                    holiday_date.year, holiday_date.month, holiday_date.day,
                    tzinfo=timezone.utc,
                )
                day_end = day_start + timedelta(days=1)
                existing = (
                    db.query(Event)
                    .filter(
                        Event.calendar_id == FEIERTAG_CALENDAR_ID,
                        Event.start == day_start,
                    )
                    .first()
                )
                if not existing:
                    db.add(Event(
                        calendar_id=FEIERTAG_CALENDAR_ID,
                        title=holiday_name,
                        start=day_start,
                        end=day_end,
                        all_day=True,
                    ))
        db.commit()
    finally:
        db.close()


def _ensure_feed_token():
    db = next(system_db())
    try:
        existing = db.query(Setting).filter(Setting.key == "feed_token").first()
        if not existing:
            token = settings.FEED_TOKEN or secrets.token_urlsafe(32)
            db.add(Setting(key="feed_token", value=token))
            db.commit()
    finally:
        db.close()


def _ensure_password():
    """Seed the password from the env var into the DB if not already stored."""
    db = next(system_db())
    try:
        existing = db.query(Setting).filter(Setting.key == "kalender_password").first()
        if not existing and settings.KALENDER_PASSWORD:
            db.add(Setting(key="kalender_password", value=hash_password(settings.KALENDER_PASSWORD)))
            db.commit()
    finally:
        db.close()


def _ensure_todo_tables():
    """Create Todo and TodoCompletion tables if they don't exist."""
    Todo.__table__.create(engine, checkfirst=True)
    TodoCompletion.__table__.create(engine, checkfirst=True)


def _ensure_notification_tables():
    """Create notification tracking tables if they do not exist."""
    SessionNotification.__table__.create(engine, checkfirst=True)
    EventReminderLog.__table__.create(engine, checkfirst=True)


def _ensure_secretary_tables():
    """Create secretary-run tracking table if it does not exist (idempotent)."""
    SecretaryRun.__table__.create(engine, checkfirst=True)


def _ensure_checkin_table():
    """Create the DailyCheckIn table if it does not exist (idempotent, adaptive Sekretär)."""
    DailyCheckIn.__table__.create(engine, checkfirst=True)


def _ensure_tagesdecke_tables():
    """Tabellen der Tagesdecke anlegen, falls sie fehlen (idempotent).

    `time_blocks` hält die lückenlose Tagesplanung, `zeit_ist` die gemessene
    Zeitverwendung. Beide sind additiv: ohne sie verhält sich der Dienst wie
    vorher, es gibt nur keine Decke.
    """
    TimeBlock.__table__.create(engine, checkfirst=True)
    ZeitIst.__table__.create(engine, checkfirst=True)


def _migrate_activity_feedback():
    """Aktivitäts-Feedback: `events.activity_type`-Spalte + `activity_feedback`-Tabelle (idempotent).

    Backfill: bestehende **wiederkehrende** Termine-Events (die realen flexiblen Aktivitäten
    des Owners) bekommen per Titel-Heuristik eine Kategorie, nur wo NULL, konservativ
    (unklare Titel bleiben NULL = normaler Termin). Der Owner kann jederzeit im UI ändern.
    """
    with engine.connect() as conn:
        try:
            conn.execute(text("ALTER TABLE events ADD COLUMN activity_type VARCHAR(20)"))
            conn.commit()
        except Exception:
            conn.rollback()
    ActivityFeedback.__table__.create(engine, checkfirst=True)

    db = next(system_db())
    try:
        candidates = (
            db.query(Event)
            .filter(
                Event.recurrence_rule.isnot(None),
                Event.calendar_id.notin_(DAYTYPE_IDS),
                Event.calendar_id != FEIERTAG_CALENDAR_ID,
                Event.activity_type.is_(None),
            )
            .all()
        )
        changed = False
        for evt in candidates:
            inferred = infer_activity_type(evt.title)
            if inferred:
                evt.activity_type = inferred
                changed = True
        if changed:
            db.commit()
    finally:
        db.close()


def _ensure_indexes():
    """Create indexes on existing tables (idempotent)."""
    index_statements = [
        "CREATE INDEX IF NOT EXISTS ix_events_calendar_id ON events (calendar_id)",
        "CREATE INDEX IF NOT EXISTS ix_events_start ON events (start)",
        "CREATE INDEX IF NOT EXISTS ix_events_end ON events (end)",
        "CREATE INDEX IF NOT EXISTS ix_habits_active ON habits (active)",
        "CREATE INDEX IF NOT EXISTS ix_habit_sessions_habit_id ON habit_sessions (habit_id)",
        "CREATE INDEX IF NOT EXISTS ix_habit_sessions_week_iso ON habit_sessions (week_iso)",
        "CREATE INDEX IF NOT EXISTS ix_habit_sessions_status ON habit_sessions (status)",
        "CREATE UNIQUE INDEX IF NOT EXISTS ux_session_notifications_session_kind ON session_notifications (session_id, kind)",
        "CREATE INDEX IF NOT EXISTS ix_session_notifications_session_id ON session_notifications (session_id)",
        "CREATE INDEX IF NOT EXISTS ix_session_notifications_sent_at ON session_notifications (sent_at)",
    ]
    with engine.connect() as conn:
        for sql in index_statements:
            conn.execute(text(sql))
        conn.commit()


def _get_password() -> str:
    """Read the current password from DB, falling back to env var."""
    db = next(system_db())
    try:
        row = db.query(Setting).filter(Setting.key == "kalender_password").first()
        return row.value if row else settings.KALENDER_PASSWORD
    finally:
        db.close()


# --- Auth helpers ---

# Effektiver JWT-Schlüssel: env-Key falls gesetzt, sonst ein zufälliger Sofort-Wert,
# der beim Startup durch den persistenten DB-Schlüssel ersetzt wird
# (_ensure_secret_key), damit ausgestellte Tokens einen Neustart überleben.
#
# ★ Die Weitergabe an `sitzung` passiert direkt hier und nicht erst im Startup.
# `sitzung` belegt sich beim Import nur aus der Umgebung und bleibt sonst leer;
# der Zufallswert entsteht ausschliesslich an dieser Stelle. Damit tragen beide
# garantiert denselben Wert, auch wenn eine Route vor dem Startup-Ereignis
# erreicht wird. Zwei eigene Zufallswerte waeren der schlimmste Fall: jede Seite
# haette ein gueltiges Geheimnis und hielte die Token der anderen fuer gefaelscht.
_SECRET_KEY: str = settings.SECRET_KEY or secrets.token_hex(32)
sitzung.schluessel_setzen(_SECRET_KEY)


def _ensure_secret_key():
    """Persistenter JWT-Schlüssel: env gewinnt, sonst DB-Setting laden/erzeugen.

    ★★ Die Weitergabe an ``backend/sitzung`` steht am ENDE und wird in **jedem**
    Zweig erreicht. Der erste Entwurf hatte sie nach dem ``finally`` stehen, und
    der Env-Zweig oben hat ein eigenes ``return``: im Betrieb ist ``SECRET_KEY``
    per Umgebung gesetzt, also wäre genau der genommen worden, ``sitzung`` hätte
    keinen Schlüssel bekommen und **jede Anmeldung** hätte nach dem Aufspielen
    401 geliefert. Aufgefallen ist das nur, weil ``token_bauen`` ohne Schlüssel
    wirft statt mit dem leeren zu signieren.
    """
    global _SECRET_KEY
    if settings.SECRET_KEY:
        _SECRET_KEY = settings.SECRET_KEY
    else:
        db = next(system_db())
        try:
            row = db.query(Setting).filter(Setting.key == "secret_key").first()
            if row and row.value:
                _SECRET_KEY = row.value
            else:
                _SECRET_KEY = secrets.token_hex(32)
                db.add(Setting(key="secret_key", value=_SECRET_KEY))
                db.commit()
                logging.getLogger(__name__).warning(
                    "SECRET_KEY nicht via Env gesetzt: persistenter Schlüssel in DB erzeugt. "
                    "Für Multi-Instance/Rotation SECRET_KEY als Umgebungsvariable setzen."
                )
        finally:
            db.close()

    # Der Schluessel hat genau eine Quelle, und ab hier kennt ihn auch
    # backend/sitzung.py -- das Modul, das tenant_auth fuer den Mandanten aus dem
    # Token braucht und das main nicht importieren darf (Zyklus).
    sitzung.schluessel_setzen(_SECRET_KEY)


def create_jwt_token(sub: str | None = None) -> str:
    """Sitzungs-Token. Ohne Angabe fuer den Owner (nativer Passwort-Login).

    Das Token trug bis 2026-09-27 fest ``sub: "admin"``, also keinen Mandanten.
    Wer sich anmeldete, bekam damit immer die Sicht des Owners. Begruendung und
    Umgang mit dem Altbestand: backend/sitzung.py.
    """
    return sitzung.token_bauen(sub or settings.DEFAULT_OWNER_SUB)


def verify_jwt_token(token: str) -> bool:
    """Nur die Frage, ob das Token gueltig ist. Fuer den Mandanten: sitzung.sub_aus_token."""
    return sitzung.sub_aus_token(token) is not None


def _set_session_cookie(response, token: str):
    """Setzt das Session-Cookie einheitlich (httponly, samesite=lax, optional secure)."""
    response.set_cookie(
        key="session_token",
        value=token,
        httponly=True,
        samesite="lax",
        secure=settings.COOKIE_SECURE,
        max_age=settings.JWT_EXPIRATION_HOURS * 3600,
    )
    return response


def get_current_user(request: Request) -> str:
    """Der angemeldete Mandant, oder 401.

    Gibt seit 2026-09-27 den ``sub`` zurueck statt ``True``. Beides ist wahr, die
    bestehenden ``dependencies=[Depends(get_current_user)]`` bleiben also
    unveraendert gueltig; wer den Mandanten braucht, kann ihn jetzt bekommen.
    """
    sub = sitzung.sub_aus_anfrage(request)
    if sub:
        return sub
    raise HTTPException(status_code=401, detail="Not authenticated")


# --- Auth endpoints ---

def _client_ip(request: Request) -> str:
    xff = request.headers.get("x-forwarded-for", "")
    if xff:
        return xff.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


@app.post("/api/auth/login", response_model=TokenResponse)
def login(data: LoginRequest, request: Request, db: Session = Depends(get_db)):
    ip = _client_ip(request)
    allowed, retry_after = _login_limiter.check(ip)
    if not allowed:
        raise HTTPException(
            status_code=429,
            detail="Zu viele Fehlversuche. Bitte später erneut versuchen.",
            headers={"Retry-After": str(retry_after)},
        )
    current_pw = _get_password()
    if not current_pw:
        raise HTTPException(status_code=500, detail="No password configured")
    if not verify_password(data.password, current_pw):
        raise HTTPException(status_code=401, detail="Invalid password")
    # Erfolgreicher Login: Rate-Limit zurücksetzen; Legacy-Klartext auf Hash migrieren.
    _login_limiter.reset(ip)
    if not is_hashed(current_pw):
        row = db.query(Setting).filter(Setting.key == "kalender_password").first()
        if row:
            row.value = hash_password(data.password)
            db.commit()
    token = create_jwt_token()
    response = JSONResponse(content={"access_token": token, "token_type": "bearer"})
    return _set_session_cookie(response, token)


@app.post("/api/auth/logout")
def logout():
    response = JSONResponse(content={"detail": "Logged out"})
    response.delete_cookie("session_token")
    return response


@app.post("/api/auth/refresh", response_model=TokenResponse)
def refresh_token(request: Request):
    token = sitzung.token_aus_anfrage(request)
    if not token:
        raise HTTPException(status_code=401, detail="Missing token")

    try:
        payload = jwt.decode(
            token,
            sitzung.schluessel(),
            algorithms=[settings.JWT_ALGORITHM],
            options={"verify_exp": False},
        )
    except jwt.PyJWTError:
        raise HTTPException(status_code=401, detail="Invalid token")

    iat = payload.get("iat")
    if iat is None:
        raise HTTPException(status_code=401, detail="Token missing iat")
    issued_at = datetime.fromtimestamp(iat, tz=timezone.utc)
    if datetime.now(timezone.utc) - issued_at > timedelta(days=settings.JWT_REFRESH_GRACE_DAYS):
        raise HTTPException(status_code=401, detail="Token too old to refresh")

    # ★ Der Mandant muss mit. Ein Refresh, der `create_jwt_token()` ohne Argument
    # ruft, macht aus der Sitzung eines zweiten Nutzers stillschweigend eine
    # Owner-Sitzung, und zwar erst nach Stunden, wenn das Frontend verlaengert.
    # Der Ablauf wird hier absichtlich nicht geprueft (Nachfrist oben), deshalb
    # `ablauf_pruefen=False`.
    alter_sub = sitzung.sub_aus_token(token, ablauf_pruefen=False)
    new_token = create_jwt_token(alter_sub)
    response = JSONResponse(
        content={"access_token": new_token, "token_type": "bearer"}
    )
    return _set_session_cookie(response, new_token)


@app.post("/api/auth/change-password", dependencies=[Depends(get_current_user)])
def change_password(data: ChangePasswordRequest, db: Session = Depends(get_db)):
    current_pw = _get_password()
    if not verify_password(data.current_password, current_pw):
        raise HTTPException(status_code=400, detail="Aktuelles Passwort ist falsch")
    hashed = hash_password(data.new_password)
    row = db.query(Setting).filter(Setting.key == "kalender_password").first()
    if row:
        row.value = hashed
    else:
        db.add(Setting(key="kalender_password", value=hashed))
    db.commit()
    return {"detail": "Passwort geaendert"}


@app.get("/api/auth/status", response_model=AuthStatus)
def auth_status(request: Request):
    try:
        get_current_user(request)
        return {"authenticated": True}
    except HTTPException:
        return {"authenticated": False}


def _sicheres_ziel(redirect: str) -> str:
    """Nur Ziele auf diesem Dienst. Alles andere wird zur Wurzel.

    ★ Beide Anmeldewege hier nehmen ein Ziel aus der Adresse und leiten dorthin
    weiter, nachdem sie ein Sitzungs-Cookie gesetzt haben. Ohne diese Pruefung ist
    das ein offener Weiterleiter an einem Anmeldepunkt: ein Link auf
    ``…/api/auth/token-login?token=…&redirect=https://fremde.seite`` sieht aus wie
    eine Adresse des eigenen Kalenders, meldet an und schickt den Benutzer dann
    woandershin. Ein Schema-relativer Wert (``//fremd.example``) traegt dasselbe
    Risiko, deshalb faellt der Doppel-Slash mit heraus.
    """
    if not redirect.startswith("/") or redirect.startswith("//"):
        return "/"
    return redirect


@app.get("/api/auth/token-login")
def token_login(token: str = Query(...), redirect: str = Query("/")):
    """Auto-login via feed token – sets session cookie and redirects.

    Seit 2026-09-27 wird der Token **aufgeloest**, statt nur gegen den einen
    globalen verglichen zu werden: der Owner-Token ergibt eine Owner-Sitzung
    (unveraendert, daran haengt der Vhost-Auto-Login), ein Mandanten-Token die
    Sitzung dieses Mandanten.

    ★ Bewusst **ohne** ``Depends(get_db)``: die Aufloesung muss ungescopt suchen,
    weil sie erst herausfindet, wer der Mandant ist. Eine gescopte Session sieht
    nur die Zeilen des schon bestimmten Mandanten -- und der waere hier per
    Rueckfall der Owner, also wuerde ein fremder Token nie gefunden und die
    Anmeldung landete beim Owner. Genau die Sorte Fehler, die aussieht wie ein
    funktionierender Login.
    """
    db = next(system_db())
    try:
        sub = mandant_einstellungen.mandant_fuer_feed_token(db, token)
    finally:
        db.close()
    if not sub:
        raise HTTPException(status_code=401, detail="Invalid token")
    response = RedirectResponse(url=_sicheres_ziel(redirect), status_code=302)
    return _set_session_cookie(response, create_jwt_token(sub))


@app.get("/api/auth/gate-login")
def gate_login(request: Request, redirect: str = Query("/")):
    """Anmeldung fuer den Weg ueber einen vorgeschalteten Torwaechter.

    **Das Loch, das dieser Endpunkt schliesst.** Die Vhosts ``calendar.home.arpa``
    und ``mail.saganta.*`` pruefen im dev-portal per ``auth_request`` gegen
    ``shell-api:/auth/check``, und der beantwortet nur, **ob** jemand angemeldet ist.
    Dahinter schrieb nginx den ersten Aufruf auf ``token-login?token=<feed_token>``
    um, und der feed_token ist der des Owners: jeder angemeldete Saganta-Nutzer
    landete damit in dessen Kalender. Nicht durch einen Fehler in der
    Mandantentrennung, sondern daran vorbei.

    Hier kommt stattdessen der **signierte** Mandant an (``X-Saganta-Sub`` plus
    ``X-Saganta-Sub-Sig``, gesetzt vom Torwaechter, der die Sitzung geprueft hat)
    und wird gegen ein Sitzungs-Token fuer genau diesen Mandanten getauscht.

    ★★ **Fail-closed, und zwar strenger als ``resolve_owner_sub``.** Dort darf ein
    unsignierter Header in der Beobachtungsphase durchgehen, weil er nur die Sicht
    auf Daten steuert und der Zugang vorher schon geprueft wurde. Hier ist der
    Header der Zugang selbst. Ohne Geheimnis und ohne gueltige Signatur wird nichts
    ausgestellt, unabhaengig von ``TENANT_HEADER_ENFORCE`` -- sonst waere das der
    bequemste Anmelde-Bypass des ganzen Hauses.
    """
    secret = settings.KALENDER_TENANT_SECRET
    if not secret:
        raise HTTPException(
            status_code=503,
            detail="KALENDER_TENANT_SECRET nicht konfiguriert, Gate-Anmeldung deaktiviert",
        )
    sub = (request.headers.get(SUB_HEADER) or "").strip()
    signatur = request.headers.get(SIG_HEADER, "")
    if not sub or not signatur:
        raise HTTPException(status_code=401, detail="Mandant oder Signatur fehlt")
    if not hmac.compare_digest(signatur, expected_signature(sub, secret)):
        logging.getLogger(__name__).warning(
            "Gate-Anmeldung mit falscher Signatur abgelehnt (sub=%s…)", sub[:8]
        )
        raise HTTPException(status_code=401, detail="Signatur ungueltig")

    response = RedirectResponse(url=_sicheres_ziel(redirect), status_code=302)
    return _set_session_cookie(response, create_jwt_token(sub))


# --- Feed token endpoint ---

@app.get("/api/feed-token")
def get_feed_token(sub: str = Depends(get_current_user), db: Session = Depends(get_db)):
    """Der Feed-Token DES ANGEMELDETEN Mandanten.

    ★ Bis 2026-09-27 stand hier ``db.query(Setting).filter(key == "feed_token")``,
    also der eine globale Wert: der Endpunkt gab **jedem** den Token des Owners,
    und wer ihn hat, oeffnet ueber ``token-login`` dessen ganzen Kalender. Das war
    harmlos, solange es nur einen Mandanten gab, und wird mit dem zweiten zur
    stillen Uebernahme. Der ``kalender-bff`` hielt deshalb einen eigenen
    Owner-Riegel davor; der ist jetzt nicht mehr die einzige Sperre.
    """
    return {"feed_token": mandant_einstellungen.feed_token_fuer(db, sub)}


# --- Dashboard widget ---

@app.get("/api/dashboard", dependencies=[Depends(get_current_user)])
def get_dashboard(db: Session = Depends(get_db)):
    return build_dashboard_data(db)


# --- Health ---

@app.get("/health")
def health():
    return {"status": "ok"}


# --- Protected routers ---

# Nur Ebene 3 des API-Vertrags. Die parameter_wache haengt bewusst NICHT an den
# eingefrorenen Ebene-1-Routen (Home Assistant, iCal, dev-portal), dort wird nichts
# angefasst, was das Weckverhalten beruehren koennte.
protected_dependencies = [Depends(get_current_user), Depends(parameter_wache)]

app.include_router(calendars.router, dependencies=protected_dependencies)
app.include_router(events.router, dependencies=protected_dependencies)
app.include_router(contacts.router, dependencies=protected_dependencies)
app.include_router(places.router, dependencies=protected_dependencies)
app.include_router(projects.router, dependencies=protected_dependencies)
app.include_router(habits.router, dependencies=protected_dependencies)
app.include_router(todos.router, dependencies=protected_dependencies)
app.include_router(goals.router, dependencies=protected_dependencies)
app.include_router(schedule.router, dependencies=protected_dependencies)
app.include_router(secretary.router, dependencies=protected_dependencies)
app.include_router(assistant.router, dependencies=protected_dependencies)
app.include_router(capture.router, dependencies=protected_dependencies)
app.include_router(account.router, dependencies=protected_dependencies)
app.include_router(reviews.router, dependencies=protected_dependencies)
app.include_router(feedback.router, dependencies=protected_dependencies)
app.include_router(search.router, dependencies=protected_dependencies)
app.include_router(mobile.router, dependencies=protected_dependencies)
app.include_router(tagesdecke.router, dependencies=protected_dependencies)
# Eigener Token statt JWT: ein Messgeraet kann kein JWT halten. Ohne gesetzten
# ZEIT_INGEST_TOKEN lehnt der Pfad jeden Aufruf ab (backend/zeit_ingest.py).
app.include_router(tagesdecke.ingest_router)
app.include_router(ical.router)
app.include_router(postfach.router)
app.include_router(internal.router)
app.include_router(daytype_public)
app.include_router(daytype_protected, dependencies=protected_dependencies)


# --- Static files / SPA ---

app.mount("/css", StaticFiles(directory=str(FRONTEND_DIR / "css")), name="css")
app.mount("/js", StaticFiles(directory=str(FRONTEND_DIR / "js")), name="js")
app.mount("/fonts", StaticFiles(directory=str(FRONTEND_DIR / "fonts")), name="fonts")


@app.get("/")
def serve_index():
    return FileResponse(
        str(FRONTEND_DIR / "index.html"),
        headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
    )
