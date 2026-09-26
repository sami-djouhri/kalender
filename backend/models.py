import uuid
from datetime import date, datetime, timezone

from sqlalchemy import Boolean, Date, DateTime, Float, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from backend.config import settings
from backend.database import Base

# Multi-Tenant: gehoert eine Zeile zu keinem sub (Alt-Daten/Backfill), dann Owner.
_OWNER = settings.DEFAULT_OWNER_SUB


def generate_uuid() -> str:
    return str(uuid.uuid4())


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Setting(Base):
    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    value: Mapped[str] = mapped_column(Text, nullable=False)


class Calendar(Base):
    __tablename__ = "calendars"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=generate_uuid)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    color: Mapped[str] = mapped_column(String(7), nullable=False, default="#3788d8")
    # NULL = systemweit sichtbar (System-Kalender/Feiertage); sonst Tenant.
    owner_sub: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_system: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    events: Mapped[list["Event"]] = relationship("Event", back_populates="calendar", cascade="all, delete-orphan")


class Event(Base):
    __tablename__ = "events"
    __table_args__ = (
        Index("ix_events_calendar_id", "calendar_id"),
        Index("ix_events_start", "start"),
        Index("ix_events_end", "end"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=generate_uuid)
    calendar_id: Mapped[str] = mapped_column(String(36), ForeignKey("calendars.id"), nullable=False)
    # NULL = systemweit sichtbar (System-Kalender/Feiertage); sonst Tenant.
    owner_sub: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    location: Mapped[str | None] = mapped_column(String(500), nullable=True)
    start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    end: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    all_day: Mapped[bool] = mapped_column(Boolean, default=False)
    recurrence_rule: Mapped[str | None] = mapped_column(String(500), nullable=True)
    # CSV von ISO-Dates (YYYY-MM-DD), die aus der Wiederholungsreihe ausgenommen sind.
    recurrence_exdates: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Optionale Erinnerung: Minuten vor Beginn → ntfy-Push (siehe session_notifications-Runner).
    reminder_minutes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Markiert ein Event als flexible, bewertbare Aktivität UND liefert die Kategorie
    # fürs Präferenz-Lernen (lernen|sport|lesen|hobby|sonstige). NULL = normaler Termin
    # (nicht bewertbar, nicht dynamisch geplant). Opt-in, überlebt die Termine-Konsolidierung.
    activity_type: Mapped[str | None] = mapped_column(String(20), nullable=True)
    # --- Ort und Weg -------------------------------------------------------
    # `location` oben ist und bleibt der Freitext, den ein Mensch tippt. Die drei
    # Felder hier tragen den AUFGELOESTEN Ort: entweder ein Verweis auf einen
    # gespeicherten Ort, oder Koordinaten aus der Adresssuche. Getrennt, weil ein
    # Freitext ohne Koordinaten weiterhin erlaubt sein muss (nicht jeder Termin
    # hat eine Adresse, und ein halb erkannter Ort darf den Termin nicht blockieren).
    place_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    lat: Mapped[float | None] = mapped_column(Float, nullable=True)
    lon: Mapped[float | None] = mapped_column(Float, nullable=True)
    # Ist dieses Feld gesetzt, ist der Termin selbst ein WEG zu einem anderen
    # Termin. Ein eigenes Feld statt eines Titel-Praefix: ein Titel ist Text, den
    # jemand aendert, und ein Weg-Termin muss maschinell wiederfindbar bleiben,
    # sonst sammeln sich Leichen an, sobald der Zieltermin verschoben wird.
    travel_for_event_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    travel_mode: Mapped[str | None] = mapped_column(String(20), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    calendar: Mapped["Calendar"] = relationship("Calendar", back_populates="events")


class Place(Base):
    """Ein benannter Ort: Zuhause, Arbeit, Sportverein.

    Bewusst getrennt von `Contact`: ein Kontakt ist ein Mensch, der zufaellig
    irgendwo wohnt, ein Ort ist ein Ort. „Arbeit" ist kein Kontakt, und ein
    Kontakt kann umziehen, ohne dass der Termin von letztem Jahr nachtraeglich
    woanders stattgefunden haben soll. Die Ortsauswahl im Frontend fuehrt beide
    Quellen zusammen, das Datenmodell haelt sie auseinander.
    """

    __tablename__ = "places"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=generate_uuid)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    # zuhause | arbeit | sonstiges. `zuhause` ist der Startort fuer die Wegzeit,
    # wenn an dem Tag noch kein Termin vorher liegt, und darf es nur einmal geben.
    kind: Mapped[str] = mapped_column(String(20), nullable=False, default="sonstiges")
    address: Mapped[str | None] = mapped_column(String(300), nullable=True)
    lat: Mapped[float | None] = mapped_column(Float, nullable=True)
    lon: Mapped[float | None] = mapped_column(Float, nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    owner_sub: Mapped[str] = mapped_column(
        String(128), nullable=False, index=True, server_default=_OWNER
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class Contact(Base):
    __tablename__ = "contacts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=generate_uuid)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    birthday: Mapped[date | None] = mapped_column(Date, nullable=True)
    owner_sub: Mapped[str] = mapped_column(
        String(128), nullable=False, index=True, server_default=_OWNER
    )
    email: Mapped[str | None] = mapped_column(String(300), nullable=True)
    phone: Mapped[str | None] = mapped_column(String(50), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Adresse als Freitext plus die aufgeloesten Koordinaten. Beide duerfen
    # einzeln fehlen: eine Adresse ohne Treffer in der Adresssuche bleibt als
    # Text stehen (nuetzlich fuer Post), und ein Kontakt ohne Adresse ist der
    # Normalfall. Die kleine Karte im Kontaktbuch erscheint nur mit Koordinaten.
    address: Mapped[str | None] = mapped_column(String(300), nullable=True)
    lat: Mapped[float | None] = mapped_column(Float, nullable=True)
    lon: Mapped[float | None] = mapped_column(Float, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class Project(Base):
    __tablename__ = "projects"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=generate_uuid)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    color: Mapped[str] = mapped_column(String(7), nullable=False, default="#6c5ce7")
    owner_sub: Mapped[str] = mapped_column(
        String(128), nullable=False, index=True, server_default=_OWNER
    )
    icon: Mapped[str | None] = mapped_column(String(10), nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="active")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    habits: Mapped[list["Habit"]] = relationship("Habit", back_populates="project")


class Habit(Base):
    __tablename__ = "habits"
    __table_args__ = (
        Index("ix_habits_active", "active"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=generate_uuid)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    color: Mapped[str] = mapped_column(String(7), nullable=False, default="#4a9eff")
    owner_sub: Mapped[str] = mapped_column(
        String(128), nullable=False, index=True, server_default=_OWNER
    )
    project_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("projects.id", ondelete="SET NULL"), nullable=True)
    target_hours_per_week: Mapped[float] = mapped_column(Float, nullable=False, default=6.0)
    session_duration_minutes: Mapped[int] = mapped_column(Integer, nullable=False, default=90)
    weekday_start: Mapped[str] = mapped_column(String(5), nullable=False, default="17:00")
    weekday_end: Mapped[str] = mapped_column(String(5), nullable=False, default="20:00")
    weekend_start: Mapped[str] = mapped_column(String(5), nullable=False, default="10:00")
    weekend_end: Mapped[str] = mapped_column(String(5), nullable=False, default="18:00")
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    learning_mode: Mapped[bool] = mapped_column(Boolean, default=False)
    focus_block_minutes: Mapped[int] = mapped_column(Integer, nullable=False, default=25)
    break_minutes: Mapped[int] = mapped_column(Integer, nullable=False, default=5)
    category: Mapped[str] = mapped_column(String(20), nullable=False, default="sonstige")
    weekday_target_ratio: Mapped[float] = mapped_column(Float, nullable=False, default=0.3)
    max_consecutive_days: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    max_session_minutes: Mapped[int] = mapped_column(Integer, nullable=False, default=120)
    # --- Ziel-/Wochenminimum-Erweiterung ---
    can_be_overridden_by_goals: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    can_be_split: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    weekly_minimum_hours: Mapped[float | None] = mapped_column(Float, nullable=True)
    preferred_days: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    project: Mapped["Project | None"] = relationship("Project", back_populates="habits")
    sessions: Mapped[list["HabitSession"]] = relationship("HabitSession", back_populates="habit", cascade="all, delete-orphan")


class HabitSession(Base):
    __tablename__ = "habit_sessions"
    __table_args__ = (
        Index("ix_habit_sessions_habit_id", "habit_id"),
        Index("ix_habit_sessions_week_iso", "week_iso"),
        Index("ix_habit_sessions_status", "status"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=generate_uuid)
    habit_id: Mapped[str] = mapped_column(String(36), ForeignKey("habits.id", ondelete="CASCADE"), nullable=False)
    owner_sub: Mapped[str] = mapped_column(
        String(128), nullable=False, index=True, server_default=_OWNER
    )
    start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    end: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    week_iso: Mapped[str] = mapped_column(String(8), nullable=False)
    focus_minutes_completed: Mapped[int | None] = mapped_column(Integer, nullable=True)
    session_type: Mapped[str] = mapped_column(String(20), nullable=False, default="standard")
    pinned: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    overridden_by_goal_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    habit: Mapped["Habit"] = relationship("Habit", back_populates="sessions")


class SessionNotification(Base):
    __tablename__ = "session_notifications"
    __table_args__ = (
        UniqueConstraint("session_id", "kind", name="uq_session_notifications_session_kind"),
        Index("ix_session_notifications_session_id", "session_id"),
        Index("ix_session_notifications_sent_at", "sent_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=generate_uuid)
    session_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("habit_sessions.id", ondelete="CASCADE"),
        nullable=False,
    )
    kind: Mapped[str] = mapped_column(String(20), nullable=False)
    scheduled_for: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    sent_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class EventReminderLog(Base):
    """Dedup-Marker für versendete Event-Erinnerungen (ntfy). Pro Event genau einmal."""
    __tablename__ = "event_reminder_log"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=generate_uuid)
    event_id: Mapped[str] = mapped_column(String(36), nullable=False, unique=True)
    fire_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    sent_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class DailyLoad(Base):
    __tablename__ = "daily_load"

    owner_sub: Mapped[str] = mapped_column(
        String(128), primary_key=True, server_default=_OWNER
    )
    date: Mapped[date] = mapped_column(Date, primary_key=True)
    learning_minutes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    strain: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    is_off_day: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    off_day_source: Mapped[str | None] = mapped_column(String(20), nullable=True)
    deferred_minutes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class DailyGoal(Base):
    """Tagesziel: Fokus/gewünschtes Ergebnis eines Tages (kein Todo).

    Priorität A/B/C (max. ein A pro Tag). Status-Lebenszyklus:
    planned → active → achieved | partial | missed; abandoned = bewusst aufgegeben
    (bleibt historisch sichtbar, gibt belegte Zeit frei).
    """

    __tablename__ = "daily_goals"
    __table_args__ = (
        Index("ix_daily_goals_date", "date"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=generate_uuid)
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    owner_sub: Mapped[str] = mapped_column(
        String(128), nullable=False, index=True, server_default=_OWNER
    )
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    date: Mapped[date] = mapped_column(Date, nullable=False)
    priority: Mapped[str] = mapped_column(String(1), nullable=False, default="B")
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="planned")
    estimated_minutes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    scheduled_start: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    scheduled_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    category: Mapped[str | None] = mapped_column(String(50), nullable=True)
    review_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    abandoned_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    todos: Mapped[list["Todo"]] = relationship("Todo", back_populates="goal")


class DailyReview(Base):
    """Optionales Tagesreview (nachrangig)."""

    __tablename__ = "daily_reviews"

    owner_sub: Mapped[str] = mapped_column(
        String(128), primary_key=True, server_default=_OWNER
    )
    date: Mapped[date] = mapped_column(Date, primary_key=True)
    what_went_well: Mapped[str | None] = mapped_column(Text, nullable=True)
    blockers: Mapped[str | None] = mapped_column(Text, nullable=True)
    abandoned_goals_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    carry_over_to_tomorrow: Mapped[str | None] = mapped_column(Text, nullable=True)
    energy_level: Mapped[str | None] = mapped_column(String(10), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class DailyCheckIn(Base):
    """Morgen-Check-in, das Tagessignal, das die adaptive Planung steuert.

    „Wie fit fühlst du dich heute?" Der Owner gibt morgens (oder wann er mag)
    an, wie er geschlafen hat, wie viel Energie er hat, seine Stimmung und ob er
    körperlich belastbar ist. Daraus leitet `daily_energy.compute_day_capacity`
    ein Kapazitäts-Level ab, das Tagesplanung + Vorschläge dämpft oder anhebt.

    Composite-PK (owner_sub, date): genau ein Check-in pro Tenant und Tag
    (upsert). Reine Metadaten-Ablage; ohne Check-in verhält sich alles wie bisher.
    """

    __tablename__ = "daily_checkins"

    owner_sub: Mapped[str] = mapped_column(
        String(128), primary_key=True, server_default=_OWNER
    )
    date: Mapped[date] = mapped_column(Date, primary_key=True)
    # Selbstauskunft (deutsche Werte, wie im UI):
    sleep_quality: Mapped[str | None] = mapped_column(String(10), nullable=True)   # gut | mittel | schlecht
    energy: Mapped[str | None] = mapped_column(String(10), nullable=True)          # hoch | mittel | niedrig
    mood: Mapped[str | None] = mapped_column(String(10), nullable=True)            # gut | neutral | mies
    # Körperlich belastbar für Anstrengung (Sport/schwere Aufgaben)? None = keine Angabe.
    physical_ready: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    # manual = vom User; inferred = aus Kontext geschätzt (später).
    source: Mapped[str] = mapped_column(String(10), nullable=False, default="manual")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class SecretaryRun(Base):
    """Idempotenz-Marker für den automatischen Sekretär-Lauf.

    Pro Tenant, Tag und Art (planning/briefing/evening) genau einmal: verhindert,
    dass der 60s-Runner-Tick mehrfach plant oder brieft. Wird beim Tages-Rollover
    von selbst wieder frei (neuer date-Key).
    """

    __tablename__ = "secretary_runs"
    __table_args__ = (
        UniqueConstraint("owner_sub", "run_date", "kind", name="uq_secretary_runs_owner_date_kind"),
        Index("ix_secretary_runs_run_date", "run_date"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=generate_uuid)
    owner_sub: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    run_date: Mapped[date] = mapped_column(Date, nullable=False)
    kind: Mapped[str] = mapped_column(String(20), nullable=False)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    ran_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Todo(Base):
    __tablename__ = "todos"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=generate_uuid)
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    priority: Mapped[str] = mapped_column(String(20), nullable=False, default="mittel")
    owner_sub: Mapped[str] = mapped_column(
        String(128), nullable=False, index=True, server_default=_OWNER
    )
    due_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    due_time: Mapped[str | None] = mapped_column(String(5), nullable=True)
    completed: Mapped[bool] = mapped_column(Boolean, default=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    recurrence: Mapped[str | None] = mapped_column(String(20), nullable=True)
    project_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("projects.id", ondelete="SET NULL"), nullable=True)
    # --- Scheduling-/Pool-Modell (Tagesziele-Erweiterung) ---
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="open")
    estimated_minutes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    scheduling_mode: Mapped[str] = mapped_column(String(20), nullable=False, default="pool")
    planned_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    scheduled_start: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    scheduled_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    earliest_start_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    energy_required: Mapped[str | None] = mapped_column(String(10), nullable=True)
    goal_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("daily_goals.id", ondelete="SET NULL"), nullable=True)
    defer_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_deferred_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    defer_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    project: Mapped["Project | None"] = relationship("Project")
    goal: Mapped["DailyGoal | None"] = relationship("DailyGoal", back_populates="todos")
    completions: Mapped[list["TodoCompletion"]] = relationship("TodoCompletion", back_populates="todo", cascade="all, delete-orphan")


class TodoCompletion(Base):
    __tablename__ = "todo_completions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=generate_uuid)
    todo_id: Mapped[str] = mapped_column(String(36), ForeignKey("todos.id", ondelete="CASCADE"), nullable=False)
    owner_sub: Mapped[str] = mapped_column(
        String(128), nullable=False, index=True, server_default=_OWNER
    )
    completed_date: Mapped[date] = mapped_column(Date, nullable=False)
    completed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    todo: Mapped["Todo"] = relationship("Todo", back_populates="completions")


class ActivityFeedback(Base):
    """Retrospektives Feedback pro Aktivitäts-Instanz, die „Randnotiz nach der Aktivität".

    Nach einer flexiblen Aktivität (Event mit `activity_type`) gibt der Owner ein kurzes
    Rating („nach Sport: stark/aufgepumpt vs. total zerstört") + optionale Randnotiz. Die
    strukturierten Ratings treiben **deterministisch** die Vorschläge/Planung
    (`energy_after` je Kategorie) und sind die Datenbasis fürs spätere Präferenz-Lernen.
    Der Freitext wird gespeichert und kann später **opt-in** per lokalem LLM ausgewertet
    werden (`llm_*`-Spalten, Phase 3).

    Gebunden an eine konkrete Event-Instanz über (`event_id` = Basis-Reihen-ID,
    `occurrence_date`); Instanz-Schema `{base}::{YYYY-MM-DD}`. Bewusst **kein FK** auf
    events: Instanzen sind virtuell und Events/Reihen können gelöscht werden, das
    Feedback (denormalisierter Kontext) bleibt für die Auswertung erhalten.
    Upsert pro (`owner_sub`, `event_id`, `occurrence_date`). STRICT-Tenant (owner_sub NOT NULL).
    """

    __tablename__ = "activity_feedback"
    __table_args__ = (
        UniqueConstraint(
            "owner_sub", "event_id", "occurrence_date",
            name="uq_activity_feedback_instance",
        ),
        Index("ix_activity_feedback_occurrence_date", "occurrence_date"),
        Index("ix_activity_feedback_activity_type", "activity_type"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=generate_uuid)
    owner_sub: Mapped[str] = mapped_column(
        String(128), nullable=False, index=True, server_default=_OWNER
    )
    activity_kind: Mapped[str] = mapped_column(String(20), nullable=False, default="event")
    event_id: Mapped[str] = mapped_column(String(36), nullable=False)  # Basis-Reihen-ID
    occurrence_date: Mapped[date] = mapped_column(Date, nullable=False)
    # Denormalisierter Kontext (überlebt Event-Löschung, Basis fürs Lernen):
    activity_type: Mapped[str | None] = mapped_column(String(20), nullable=True)
    title: Mapped[str | None] = mapped_column(String(500), nullable=True)
    scheduled_start: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    scheduled_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    weekday: Mapped[int | None] = mapped_column(Integer, nullable=True)  # 0=Mo .. 6=So
    # Strukturierte Ratings (das deterministische Planungssignal):
    energy_after: Mapped[str | None] = mapped_column(String(15), nullable=True)  # energetisiert|ok|erschöpft
    satisfaction: Mapped[str | None] = mapped_column(String(10), nullable=True)  # gut|mittel|schlecht
    took_place: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    # LLM-abgeleitet (Phase 3, opt-in, jetzt nur reservierte Spalten):
    llm_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    llm_followups: Mapped[str | None] = mapped_column(Text, nullable=True)
    llm_sentiment: Mapped[str | None] = mapped_column(String(15), nullable=True)
    llm_processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class TimeBlock(Base):
    """Ein Stueck Tagesdecke: die lueckenlose Belegung des wachen Tages.

    Die Decke hat zwei Lebensphasen, und das ist der Kern dieses Modells.

    **Solange ein Tag offen ist**, wird die Decke bei jedem Aufruf frisch gerechnet
    (`backend/tagesdecke.py`). Das meiste daran steht schon woanders: ein
    Arbeitsblock ist ein Event, ein Uebungsblock eine HabitSession, ein
    Aufgabenblock ein Todo mit Zeitfenster. Diese Tabelle haelt dann nur, was sonst
    nirgends steht: Erholung, Grundlast (Essen, Haushalt) und bewusst offen
    gelassene Puffer. Wer die Fixpunkte schon hier mitspeichert, hat denselben
    Termin zweimal. Verschiebt er sich, zeigt die Decke die alte Lage an, und zwar
    plausibel genug, dass es niemand merkt.

    **Sobald der Tag festgeschrieben wird** (`festschreiben()`, beim ersten
    Korrigieren oder abends automatisch), kippt das Verhaeltnis: jeder Block des
    Tages wird zur Zeile, auch die gespiegelten. Ab da ist die Decke ein Protokoll
    und kein Spiegel mehr. Das ist Absicht. Wer abends sagt "das habe ich nicht
    gemacht", beschreibt den gelebten Tag, und der darf sich nicht mehr aendern,
    weil jemand naechste Woche einen Termin verschiebt.

    `status` traegt genau diese Korrektur: `geplant` (so war es vorgesehen),
    `bestaetigt` (so war es), `verworfen` (war geplant, fand nicht statt).
    ★ Verworfenes wird nicht geloescht. Die Luecke zwischen dem, was regelmaessig
    geplant und nie getan wird, ist das interessanteste Signal, das dieser Kalender
    ueberhaupt erzeugen kann. Ein DELETE wuerde es jeden Abend wegwerfen.

    ★★ Bewusst NICHT in `events`. An `events` haengen die vier Tagestyp-Kalender
    und damit `/api/day-type/today`, das Home Assistant fuer das 05:35-Weckfenster
    abfragt. Eine Schicht, die den Tag von morgens bis abends vollschreibt (gut ein
    Dutzend Zeilen taeglich), darf diese Tabelle nicht anfassen. Sichtbar wird die
    Decke trotzdem: im Kalender-Raster und ueber einen eigenen iCal-Feed
    (`/api/tagesdecke/ical`), der sie als echte Termine ausliefert. STRICT-Tenant.
    """

    __tablename__ = "time_blocks"
    __table_args__ = (
        Index("ix_time_blocks_date", "owner_sub", "date"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=generate_uuid)
    owner_sub: Mapped[str] = mapped_column(
        String(128), nullable=False, index=True, server_default=_OWNER
    )
    date: Mapped[date] = mapped_column(Date, nullable=False)
    # Berliner Wanduhr ohne Zone, wie Event.start/.end (siehe backend/wanduhr.py).
    start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    end: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    # Siehe tagesdecke.ARTEN: fix, gewohnheit, ziel, aufgabe, erholung, grundlast, puffer.
    art: Mapped[str] = mapped_column(String(20), nullable=False)
    titel: Mapped[str] = mapped_column(String(300), nullable=False)
    # Warum liegt der Block hier? Bleibt in der Oberflaeche sichtbar: eine Planung,
    # die sich nicht begruenden kann, wird nicht befolgt, sondern weggeklickt.
    begruendung: Mapped[str | None] = mapped_column(String(300), nullable=True)
    status: Mapped[str] = mapped_column(String(12), nullable=False, default="geplant")
    # plan = automatisch erzeugt (Neuplanung ersetzt ihn), manuell = angefasst und fest.
    quelle: Mapped[str] = mapped_column(String(10), nullable=False, default="plan")
    # Woher der Block stammt, wenn er gespiegelt ist: event | habit_session | todo | goal.
    # Kein FK. Die Quelle darf geloescht werden, ohne das Protokoll zu zerreissen,
    # und Event-Instanzen einer Reihe existieren ohnehin nur virtuell.
    herkunft_typ: Mapped[str | None] = mapped_column(String(20), nullable=True)
    herkunft_id: Mapped[str | None] = mapped_column(String(80), nullable=True)
    # Wann dieser Tag vom Spiegel zum Protokoll wurde. Gesetzt für alle Zeilen
    # eines Tages beim Festschreiben.
    #
    # ★ Ein ausdrücklicher Marker, weil jede Ableitung daneben lag: "es gibt
    # gespiegelte Blöcke" erkennt einen Tag ohne einen einzigen Termin nicht als
    # festgeschrieben, und ein leerer Tag ist gerade der Normalfall beim
    # Nachtragen. Der Tag galt dann weiter als offen, die Decke wurde neu
    # gerechnet, die Blöcke bekamen neue Kennungen, und eine Messung fand nichts
    # mehr zum Zuordnen. Zwei Symptome, eine Ursache.
    festgeschrieben_am: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class ZeitIst(Base):
    """Gemessene Zeitverwendung: was ein Geraet mitgeschrieben hat.

    Gefuellt ueber `POST /api/zeit/ingest`, vorgesehen fuer eine Smartwatch ueber
    einen Adapter. Ergaenzt die abendliche Korrektur von Hand, ersetzt sie nicht:
    eine Uhr weiss, dass 40 Minuten Bewegung waren, aber nicht, ob das der Einkauf
    oder der Spaziergang war.

    ★ Eigene Tabelle statt `ist_start`/`ist_ende` an `TimeBlock`. Eine Uhr kennt die
    Decke nicht: sie liefert Intervalle, die quer zu den geplanten Bloecken liegen,
    sich ueberlappen und mehrere ueberspannen koennen. Wer sie beim Einliefern in
    Soll-Zeilen presst, muss sofort entscheiden, wohin sie gehoeren, und verliert
    dabei die Rohlage. Zugeordnet wird deshalb erst beim Auswerten
    (`tagesdecke.abgleich`), wo die Entscheidung revidierbar ist.

    `extern_id` traegt die Kennung des liefernden Systems und ist zusammen mit
    `quelle` eindeutig: dieselbe Aktivitaet zweimal geschickt aktualisiert die
    vorhandene Zeile. Ohne diese Idempotenz erzeugt jeder Wiederholungsversuch nach
    einem Verbindungsabbruch ein Doppel, und ein Tag haette mehr als 24 Stunden Ist.
    STRICT-Tenant.
    """

    __tablename__ = "zeit_ist"
    __table_args__ = (
        UniqueConstraint("owner_sub", "quelle", "extern_id", name="uq_zeit_ist_extern"),
        Index("ix_zeit_ist_date", "owner_sub", "date"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=generate_uuid)
    owner_sub: Mapped[str] = mapped_column(
        String(128), nullable=False, index=True, server_default=_OWNER
    )
    date: Mapped[date] = mapped_column(Date, nullable=False)
    # Berliner Wanduhr ohne Zone. ★ Ein Geraet liefert ISO-8601 mit Zone; der Eingang
    # rechnet ueber wanduhr.als_wanduhr um. Wer das ueberspringt, legt im Sommer jeden
    # Eintrag zwei Stunden daneben ab, und das faellt an einem vollen Tag nicht auf.
    start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    end: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    # Vereinheitlichte Art (tagesdecke.ARTEN). Der Adapter uebersetzt, was das Geraet
    # sagt ("workout", "deep_sleep"), in dieses Vokabular.
    art: Mapped[str] = mapped_column(String(20), nullable=False)
    titel: Mapped[str | None] = mapped_column(String(300), nullable=True)
    quelle: Mapped[str] = mapped_column(String(30), nullable=False)
    extern_id: Mapped[str] = mapped_column(String(200), nullable=False)
    # Was das Geraet selbst gesagt hat, unveraendert aufbewahrt. Stellt sich eine
    # Uebersetzung spaeter als falsch heraus, laesst sie sich daraus neu ableiten.
    roh_typ: Mapped[str | None] = mapped_column(String(100), nullable=True)
    puls_schnitt: Mapped[int | None] = mapped_column(Integer, nullable=True)
    schritte: Mapped[int | None] = mapped_column(Integer, nullable=True)
    kalorien: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
