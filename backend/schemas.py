from datetime import date, datetime
from typing import Literal

# Alias, damit Felder, die selbst "date" heißen, ihren Typ nicht verschatten.
date_type = date

from pydantic import BaseModel, Field, model_validator


# --- Auth ---

class LoginRequest(BaseModel):
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"


class AuthStatus(BaseModel):
    authenticated: bool


class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str = Field(..., min_length=4, max_length=200)


# --- Calendar ---

class CalendarCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)
    color: str = Field(default="#3788d8", pattern=r"^#[0-9a-fA-F]{6}$")
    description: str | None = None


class CalendarUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    color: str | None = Field(default=None, pattern=r"^#[0-9a-fA-F]{6}$")
    description: str | None = None


class CalendarResponse(BaseModel):
    id: str
    name: str
    color: str
    description: str | None
    is_system: bool
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


# --- Contact ---

class ContactCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)
    birthday: date | None = None
    email: str | None = Field(default=None, max_length=300)
    phone: str | None = Field(default=None, max_length=50)
    notes: str | None = None
    address: str | None = Field(default=None, max_length=300)
    lat: float | None = Field(default=None, ge=-90, le=90)
    lon: float | None = Field(default=None, ge=-180, le=180)


class ContactUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    birthday: date | None = None
    email: str | None = Field(default=None, max_length=300)
    phone: str | None = Field(default=None, max_length=50)
    notes: str | None = None
    address: str | None = Field(default=None, max_length=300)
    lat: float | None = Field(default=None, ge=-90, le=90)
    lon: float | None = Field(default=None, ge=-180, le=180)


class ContactResponse(BaseModel):
    id: str
    name: str
    birthday: date | None
    email: str | None
    phone: str | None
    notes: str | None
    address: str | None = None
    lat: float | None = None
    lon: float | None = None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


# --- Place (Ort) ---

class PlaceCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)
    kind: str = Field(default="sonstiges", pattern=r"^(zuhause|arbeit|sonstiges)$")
    address: str | None = Field(default=None, max_length=300)
    lat: float | None = Field(default=None, ge=-90, le=90)
    lon: float | None = Field(default=None, ge=-180, le=180)
    notes: str | None = None


class PlaceUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    kind: str | None = Field(default=None, pattern=r"^(zuhause|arbeit|sonstiges)$")
    address: str | None = Field(default=None, max_length=300)
    lat: float | None = Field(default=None, ge=-90, le=90)
    lon: float | None = Field(default=None, ge=-180, le=180)
    notes: str | None = None


class PlaceResponse(BaseModel):
    id: str
    name: str
    kind: str
    address: str | None
    lat: float | None
    lon: float | None
    notes: str | None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


# --- Event ---

class EventCreate(BaseModel):
    calendar_id: str
    title: str = Field(..., min_length=1, max_length=500)
    description: str | None = None
    location: str | None = None
    start: datetime
    end: datetime
    all_day: bool = False
    recurrence_rule: str | None = None
    reminder_minutes: int | None = Field(default=None, ge=0, le=10080)
    activity_type: str | None = Field(default=None, pattern=r"^(lernen|sport|lesen|hobby|sonstige)$")
    place_id: str | None = None
    lat: float | None = Field(default=None, ge=-90, le=90)
    lon: float | None = Field(default=None, ge=-180, le=180)
    travel_mode: str | None = Field(default=None, pattern=r"^(pedestrian|bicycle|auto|transit)$")

    @model_validator(mode="after")
    def validate_start_before_end(self):
        if self.start and self.end and self.start >= self.end:
            raise ValueError("Event start must be before end")
        return self


class EventUpdate(BaseModel):
    calendar_id: str | None = None
    title: str | None = Field(default=None, min_length=1, max_length=500)
    description: str | None = None
    location: str | None = None
    start: datetime | None = None
    end: datetime | None = None
    all_day: bool | None = None
    recurrence_rule: str | None = None
    recurrence_exdates: str | None = None
    reminder_minutes: int | None = Field(default=None, ge=0, le=10080)
    activity_type: str | None = Field(default=None, pattern=r"^(lernen|sport|lesen|hobby|sonstige)$")
    place_id: str | None = None
    lat: float | None = Field(default=None, ge=-90, le=90)
    lon: float | None = Field(default=None, ge=-180, le=180)
    travel_mode: str | None = Field(default=None, pattern=r"^(pedestrian|bicycle|auto|transit)$")

    @model_validator(mode="after")
    def validate_start_before_end(self):
        if self.start and self.end and self.start >= self.end:
            raise ValueError("Event start must be before end")
        return self


class EventResponse(BaseModel):
    id: str
    calendar_id: str
    title: str
    description: str | None
    location: str | None
    start: datetime
    end: datetime
    all_day: bool
    recurrence_rule: str | None
    recurrence_exdates: str | None = None
    reminder_minutes: int | None = None
    activity_type: str | None = None
    place_id: str | None = None
    lat: float | None = None
    lon: float | None = None
    travel_for_event_id: str | None = None
    travel_mode: str | None = None
    is_recurring_instance: bool = False
    series_id: str | None = None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


# --- Project ---

class ProjectCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)
    color: str = Field(default="#6c5ce7", pattern=r"^#[0-9a-fA-F]{6}$")
    icon: str | None = Field(default=None, max_length=10)
    status: str = Field(default="active", pattern=r"^(active|paused|completed)$")


class ProjectUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    color: str | None = Field(default=None, pattern=r"^#[0-9a-fA-F]{6}$")
    icon: str | None = None
    status: str | None = Field(default=None, pattern=r"^(active|paused|completed)$")


class ProjectResponse(BaseModel):
    id: str
    name: str
    color: str
    icon: str | None
    status: str
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


# --- Habit ---

class HabitCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)
    color: str = Field(default="#4a9eff", pattern=r"^#[0-9a-fA-F]{6}$")
    project_id: str | None = None
    target_hours_per_week: float = Field(default=6.0, gt=0, le=80)
    session_duration_minutes: int = Field(default=90, ge=15, le=480)
    weekday_start: str = Field(default="17:00", pattern=r"^\d{2}:\d{2}$")
    weekday_end: str = Field(default="20:00", pattern=r"^\d{2}:\d{2}$")
    weekend_start: str = Field(default="10:00", pattern=r"^\d{2}:\d{2}$")
    weekend_end: str = Field(default="18:00", pattern=r"^\d{2}:\d{2}$")
    learning_mode: bool = False
    focus_block_minutes: int = Field(default=25, ge=5, le=120)
    break_minutes: int = Field(default=5, ge=1, le=30)
    category: str = Field(default="sonstige", pattern=r"^(lernen|lesen|sonstige)$")
    weekday_target_ratio: float = Field(default=0.3, ge=0.1, le=0.5)
    max_consecutive_days: int = Field(default=3, ge=1, le=7)
    max_session_minutes: int = Field(default=120, ge=15, le=480)


class HabitUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    color: str | None = Field(default=None, pattern=r"^#[0-9a-fA-F]{6}$")
    project_id: str | None = None
    target_hours_per_week: float | None = Field(default=None, gt=0, le=80)
    session_duration_minutes: int | None = Field(default=None, ge=15, le=480)
    weekday_start: str | None = Field(default=None, pattern=r"^\d{2}:\d{2}$")
    weekday_end: str | None = Field(default=None, pattern=r"^\d{2}:\d{2}$")
    weekend_start: str | None = Field(default=None, pattern=r"^\d{2}:\d{2}$")
    weekend_end: str | None = Field(default=None, pattern=r"^\d{2}:\d{2}$")
    active: bool | None = None
    learning_mode: bool | None = None
    focus_block_minutes: int | None = Field(default=None, ge=5, le=120)
    break_minutes: int | None = Field(default=None, ge=1, le=30)
    category: str | None = Field(default=None, pattern=r"^(lernen|lesen|sonstige)$")
    weekday_target_ratio: float | None = Field(default=None, ge=0.1, le=0.5)
    max_consecutive_days: int | None = Field(default=None, ge=1, le=7)
    max_session_minutes: int | None = Field(default=None, ge=15, le=480)


class HabitResponse(BaseModel):
    id: str
    name: str
    color: str
    project_id: str | None
    target_hours_per_week: float
    session_duration_minutes: int
    weekday_start: str
    weekday_end: str
    weekend_start: str
    weekend_end: str
    active: bool
    learning_mode: bool = False
    focus_block_minutes: int = 25
    break_minutes: int = 5
    category: str = "sonstige"
    weekday_target_ratio: float = 0.3
    max_consecutive_days: int = 3
    max_session_minutes: int = 120
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


# --- HabitSession ---

class HabitSessionResponse(BaseModel):
    id: str
    habit_id: str
    start: datetime
    end: datetime
    status: str
    week_iso: str
    focus_minutes_completed: int | None = None
    session_type: str = "standard"
    pinned: bool = False
    created_at: datetime
    updated_at: datetime
    habit_name: str | None = None
    habit_color: str | None = None

    model_config = {"from_attributes": True}


class HabitSessionAction(BaseModel):
    action: Literal["accepted", "dismissed", "cancelled", "start_early"]


class HabitWeeklyProgress(BaseModel):
    habit_id: str
    name: str
    color: str
    project_name: str | None
    target_hours: float
    completed_hours: float
    accepted_hours: float
    pending_count: int
    next_session: datetime | None
    learning_mode: bool = False
    focus_minutes_target: float | None = None
    focus_minutes_done: float | None = None
    pomodoros_target: float | None = None
    pomodoros_done: float | None = None
    category: str = "sonstige"
    strain: float | None = None
    off_days_this_week: int | None = None
    deferred_minutes: int | None = None


class DailyLoadResponse(BaseModel):
    date: date_type
    weekday: str
    day_type: str
    learning_minutes: int
    strain: float
    is_off_day: bool
    off_day_source: str | None
    deferred_minutes: int


class LoadOverviewResponse(BaseModel):
    week_iso: str
    current_strain: float
    strain_level: str
    days: list[DailyLoadResponse]


# --- Todo ---

_ENERGY = r"^(low|medium|high)$"
_SCHED_MODE = r"^(pool|planned_day|fixed_slot)$"
_TODO_STATUS = r"^(open|scheduled|in_progress|done|deferred|cancelled)$"


class TodoCreate(BaseModel):
    title: str = Field(..., min_length=1, max_length=500)
    description: str | None = None
    priority: str = Field(default="mittel", pattern=r"^(niedrig|mittel|hoch|dringend)$")
    due_date: date | None = None
    due_time: str | None = Field(default=None, pattern=r"^\d{2}:\d{2}$")
    recurrence: str | None = Field(default=None, pattern=r"^(daily|weekly|biweekly|monthly|quarterly|yearly)$")
    project_id: str | None = None
    goal_id: str | None = None
    estimated_minutes: int | None = Field(default=None, ge=1, le=1440)
    scheduling_mode: str = Field(default="pool", pattern=_SCHED_MODE)
    planned_date: date | None = None
    scheduled_start: datetime | None = None
    scheduled_end: datetime | None = None
    earliest_start_date: date | None = None
    energy_required: str | None = Field(default=None, pattern=_ENERGY)


class TodoUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=500)
    description: str | None = None
    priority: str | None = Field(default=None, pattern=r"^(niedrig|mittel|hoch|dringend)$")
    due_date: date | None = None
    due_time: str | None = Field(default=None, pattern=r"^\d{2}:\d{2}$")
    recurrence: str | None = Field(default=None, pattern=r"^(daily|weekly|biweekly|monthly|quarterly|yearly)$")
    project_id: str | None = None
    completed: bool | None = None
    goal_id: str | None = None
    status: str | None = Field(default=None, pattern=_TODO_STATUS)
    estimated_minutes: int | None = Field(default=None, ge=1, le=1440)
    scheduling_mode: str | None = Field(default=None, pattern=_SCHED_MODE)
    planned_date: date | None = None
    scheduled_start: datetime | None = None
    scheduled_end: datetime | None = None
    earliest_start_date: date | None = None
    energy_required: str | None = Field(default=None, pattern=_ENERGY)


class TodoDeferRequest(BaseModel):
    """Todo aufschieben: geht nie verloren."""
    planned_date: date | None = None
    scheduled_start: datetime | None = None
    scheduled_end: datetime | None = None
    reason: str | None = Field(default=None, max_length=500)


class TodoScheduleRequest(BaseModel):
    scheduling_mode: str = Field(..., pattern=_SCHED_MODE)
    planned_date: date | None = None
    scheduled_start: datetime | None = None
    scheduled_end: datetime | None = None


class TodoResponse(BaseModel):
    id: str
    title: str
    description: str | None
    priority: str
    due_date: date | None
    due_time: str | None
    completed: bool
    completed_at: datetime | None
    recurrence: str | None
    project_id: str | None
    goal_id: str | None = None
    status: str = "open"
    estimated_minutes: int | None = None
    scheduling_mode: str = "pool"
    planned_date: date | None = None
    scheduled_start: datetime | None = None
    scheduled_end: datetime | None = None
    earliest_start_date: date | None = None
    energy_required: str | None = None
    defer_count: int = 0
    last_deferred_at: datetime | None = None
    defer_reason: str | None = None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


# --- Tagesziele (DailyGoal) ---

_GOAL_PRIORITY = r"^[ABC]$"
_GOAL_STATUS = r"^(planned|active|achieved|partial|abandoned|missed)$"


class GoalCreate(BaseModel):
    title: str = Field(..., min_length=1, max_length=300)
    description: str | None = None
    date: date_type
    priority: str = Field(default="B", pattern=_GOAL_PRIORITY)
    status: str = Field(default="planned", pattern=_GOAL_STATUS)
    estimated_minutes: int | None = Field(default=None, ge=0, le=1440)
    scheduled_start: datetime | None = None
    scheduled_end: datetime | None = None
    category: str | None = Field(default=None, max_length=50)


class GoalUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=300)
    description: str | None = None
    date: date_type | None = None
    priority: str | None = Field(default=None, pattern=_GOAL_PRIORITY)
    status: str | None = Field(default=None, pattern=_GOAL_STATUS)
    estimated_minutes: int | None = Field(default=None, ge=0, le=1440)
    scheduled_start: datetime | None = None
    scheduled_end: datetime | None = None
    category: str | None = Field(default=None, max_length=50)
    review_note: str | None = None


class GoalAbandonRequest(BaseModel):
    abandoned_reason: str | None = Field(default=None, max_length=500)


class GoalResponse(BaseModel):
    id: str
    title: str
    description: str | None
    date: date_type
    priority: str
    status: str
    estimated_minutes: int | None
    scheduled_start: datetime | None
    scheduled_end: datetime | None
    category: str | None
    review_note: str | None
    abandoned_reason: str | None
    created_at: datetime
    updated_at: datetime
    linked_todo_ids: list[str] = []

    model_config = {"from_attributes": True}


# --- Tagesreview ---

class DailyReviewUpsert(BaseModel):
    what_went_well: str | None = None
    blockers: str | None = None
    abandoned_goals_reason: str | None = None
    carry_over_to_tomorrow: str | None = None
    energy_level: str | None = Field(default=None, pattern=r"^(low|medium|high)$")


class DailyReviewResponse(BaseModel):
    date: date_type
    what_went_well: str | None
    blockers: str | None
    abandoned_goals_reason: str | None
    carry_over_to_tomorrow: str | None
    energy_level: str | None

    model_config = {"from_attributes": True}


# --- Scheduling / freie Slots ---

class FreeSlot(BaseModel):
    start: datetime
    end: datetime
    minutes: int


class TodoSuggestion(BaseModel):
    todo_id: str
    title: str
    start: datetime
    end: datetime
    minutes: int


class ScheduleDayResponse(BaseModel):
    date: date_type
    free_slots: list[FreeSlot]
    suggestions: list[TodoSuggestion]
    committed: bool = False


class TodoCompletionResponse(BaseModel):
    id: str
    todo_id: str
    completed_date: date
    completed_at: datetime

    model_config = {"from_attributes": True}
