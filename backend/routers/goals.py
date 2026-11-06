"""Tagesziele (DailyGoal): Tagesfokus/Outcome, kein Todo.

Produktregel: max. ein A-Ziel pro Tag. Ein zweites A stuft das bestehende A auf B herab.
'Ziel aufgeben' (abandon) ist kein Löschen, es bleibt historisch sichtbar und gibt belegte
Zeit wieder frei (Habits/Pool-Todos können den Tag weiterplanen).
"""

from datetime import date as date_type

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from backend.database import get_db
from backend.models import DailyGoal, Todo
from backend.schemas import (
    GoalAbandonRequest,
    GoalCreate,
    GoalResponse,
    GoalUpdate,
)
from backend.scheduler import apply_goal_override, release_goal_block

router = APIRouter(prefix="/api/goals", tags=["goals"])

# Status, in denen ein A-Ziel den "einen Fokus pro Tag" beansprucht.
_LIVE_STATUSES = ("planned", "active")
# Sortierung der Prioritäten A < B < C.
_PRIORITY_ORDER = {"A": 0, "B": 1, "C": 2}


def _serialize(db: Session, goal: DailyGoal) -> GoalResponse:
    linked = [t.id for t in db.query(Todo.id).filter(Todo.goal_id == goal.id).all()]
    data = GoalResponse.model_validate(goal)
    data.linked_todo_ids = linked
    return data


def _demote_existing_a_goal(db: Session, day: date_type, exclude_id: str | None = None):
    """Stuft ein bestehendes aktives A-Ziel des Tages auf B herab (bestätigte UX-Regel)."""
    q = db.query(DailyGoal).filter(
        DailyGoal.date == day,
        DailyGoal.priority == "A",
        DailyGoal.status.in_(_LIVE_STATUSES),
    )
    if exclude_id:
        q = q.filter(DailyGoal.id != exclude_id)
    for existing in q.all():
        existing.priority = "B"


@router.get("", response_model=list[GoalResponse])
def list_goals(
    date: date_type | None = Query(None),
    db: Session = Depends(get_db),
):
    q = db.query(DailyGoal)
    if date:
        q = q.filter(DailyGoal.date == date)
    goals = q.all()
    goals.sort(key=lambda g: (_PRIORITY_ORDER.get(g.priority, 1), g.created_at))
    return [_serialize(db, g) for g in goals]


@router.post("", response_model=GoalResponse, status_code=201)
def create_goal(data: GoalCreate, db: Session = Depends(get_db)):
    if data.scheduled_start and data.scheduled_end and data.scheduled_start >= data.scheduled_end:
        raise HTTPException(status_code=422, detail="scheduled_start muss vor scheduled_end liegen")

    if data.priority == "A" and data.status in _LIVE_STATUSES:
        _demote_existing_a_goal(db, data.date)

    goal = DailyGoal(**data.model_dump())
    db.add(goal)
    db.flush()

    if goal.scheduled_start and goal.scheduled_end and goal.status in _LIVE_STATUSES:
        apply_goal_override(db, goal)

    db.commit()
    db.refresh(goal)
    return _serialize(db, goal)


@router.get("/{goal_id}", response_model=GoalResponse)
def get_goal(goal_id: str, db: Session = Depends(get_db)):
    goal = db.query(DailyGoal).filter(DailyGoal.id == goal_id).first()
    if not goal:
        raise HTTPException(status_code=404, detail="Ziel nicht gefunden")
    return _serialize(db, goal)


@router.put("/{goal_id}", response_model=GoalResponse)
def update_goal(goal_id: str, data: GoalUpdate, db: Session = Depends(get_db)):
    goal = db.query(DailyGoal).filter(DailyGoal.id == goal_id).first()
    if not goal:
        raise HTTPException(status_code=404, detail="Ziel nicht gefunden")

    prev_block = (goal.scheduled_start, goal.scheduled_end, goal.status)
    update_data = data.model_dump(exclude_unset=True)

    new_priority = update_data.get("priority", goal.priority)
    new_status = update_data.get("status", goal.status)
    new_date = update_data.get("date", goal.date)
    if new_priority == "A" and new_status in _LIVE_STATUSES:
        _demote_existing_a_goal(db, new_date, exclude_id=goal.id)

    for key, value in update_data.items():
        setattr(goal, key, value)

    if goal.scheduled_start and goal.scheduled_end and goal.scheduled_start >= goal.scheduled_end:
        raise HTTPException(status_code=422, detail="scheduled_start muss vor scheduled_end liegen")
    db.flush()

    # Zeitblock/Status geändert → Belegung neu auswerten.
    block_changed = prev_block != (goal.scheduled_start, goal.scheduled_end, goal.status)
    if block_changed:
        # Erst alte Verdrängung lösen, dann ggf. neu anwenden.
        release_goal_block(db, goal)
        if goal.scheduled_start and goal.scheduled_end and goal.status in _LIVE_STATUSES:
            apply_goal_override(db, goal)

    db.commit()
    db.refresh(goal)
    return _serialize(db, goal)


@router.post("/{goal_id}/abandon", response_model=GoalResponse)
def abandon_goal(goal_id: str, data: GoalAbandonRequest, db: Session = Depends(get_db)):
    """Ziel bewusst aufgeben, kein Löschen. Gibt belegte Zeit frei, Todos bleiben offen."""
    goal = db.query(DailyGoal).filter(DailyGoal.id == goal_id).first()
    if not goal:
        raise HTTPException(status_code=404, detail="Ziel nicht gefunden")

    goal.status = "abandoned"
    if data.abandoned_reason is not None:
        goal.abandoned_reason = data.abandoned_reason
    db.flush()

    release_goal_block(db, goal)

    db.commit()
    db.refresh(goal)
    return _serialize(db, goal)


@router.delete("/{goal_id}", status_code=204)
def delete_goal(goal_id: str, db: Session = Depends(get_db)):
    goal = db.query(DailyGoal).filter(DailyGoal.id == goal_id).first()
    if not goal:
        raise HTTPException(status_code=404, detail="Ziel nicht gefunden")
    # Verknüpfte Todos lösen (bleiben offen), belegte Zeit freigeben.
    db.query(Todo).filter(Todo.goal_id == goal_id).update({"goal_id": None})
    release_goal_block(db, goal)
    db.delete(goal)
    db.commit()
