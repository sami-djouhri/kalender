from datetime import date, datetime, timedelta, timezone

from dateutil.relativedelta import relativedelta
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from backend.database import get_db
from backend.models import DailyGoal, Project, Todo, TodoCompletion
from backend.schemas import (
    TodoCompletionResponse,
    TodoCreate,
    TodoDeferRequest,
    TodoResponse,
    TodoScheduleRequest,
    TodoUpdate,
)

router = APIRouter(prefix="/api/todos", tags=["todos"])


def _validate_goal(db: Session, goal_id: str | None):
    if goal_id:
        if not db.query(DailyGoal).filter(DailyGoal.id == goal_id).first():
            raise HTTPException(status_code=404, detail="Ziel nicht gefunden")


def _advance_due_date(current: date, recurrence: str) -> date:
    """Calculate the next due date based on recurrence pattern."""
    match recurrence:
        case "daily":
            return current + timedelta(days=1)
        case "weekly":
            return current + timedelta(weeks=1)
        case "biweekly":
            return current + timedelta(weeks=2)
        case "monthly":
            return current + relativedelta(months=1)
        case "quarterly":
            return current + relativedelta(months=3)
        case "yearly":
            return current + relativedelta(years=1)
        case _:
            return current + timedelta(days=1)


@router.get("", response_model=list[TodoResponse])
def list_todos(
    week_start: date | None = Query(None),
    due_from: date | None = Query(None),
    due_to: date | None = Query(None),
    include_completed: bool = Query(False),
    include_without_due_date: bool = Query(True),
    project_id: str | None = Query(None),
    db: Session = Depends(get_db),
):
    q = db.query(Todo)
    if project_id:
        q = q.filter(Todo.project_id == project_id)
    if week_start:
        week_end = week_start + timedelta(days=6)
        q = q.filter(
            (Todo.due_date >= week_start) & (Todo.due_date <= week_end)
            | (Todo.due_date == None)  # noqa: E711
        )
    elif due_from or due_to:
        if include_without_due_date:
            due_filters = []
            if due_from:
                due_filters.append(Todo.due_date >= due_from)
            if due_to:
                due_filters.append(Todo.due_date <= due_to)
            if due_filters:
                combined = due_filters[0]
                for clause in due_filters[1:]:
                    combined = combined & clause
                q = q.filter(combined | (Todo.due_date == None))  # noqa: E711
        else:
            if due_from:
                q = q.filter(Todo.due_date >= due_from)
            if due_to:
                q = q.filter(Todo.due_date <= due_to)
    if not include_completed:
        q = q.filter(Todo.completed == False)
    return q.order_by(Todo.due_date.asc().nullslast(), Todo.created_at.asc()).all()


@router.post("", response_model=TodoResponse, status_code=201)
def create_todo(data: TodoCreate, db: Session = Depends(get_db)):
    if data.project_id:
        project = db.query(Project).filter(Project.id == data.project_id).first()
        if not project:
            raise HTTPException(status_code=404, detail="Projekt nicht gefunden")
    _validate_goal(db, data.goal_id)
    todo = Todo(**data.model_dump())
    # Status aus scheduling_mode ableiten, wenn ein Zeitblock gesetzt ist.
    if todo.scheduled_start and todo.scheduled_end:
        todo.status = "scheduled"
    db.add(todo)
    db.commit()
    db.refresh(todo)
    return todo


@router.get("/{todo_id}", response_model=TodoResponse)
def get_todo(todo_id: str, db: Session = Depends(get_db)):
    todo = db.query(Todo).filter(Todo.id == todo_id).first()
    if not todo:
        raise HTTPException(status_code=404, detail="Todo nicht gefunden")
    return todo


@router.put("/{todo_id}", response_model=TodoResponse)
def update_todo(todo_id: str, data: TodoUpdate, db: Session = Depends(get_db)):
    todo = db.query(Todo).filter(Todo.id == todo_id).first()
    if not todo:
        raise HTTPException(status_code=404, detail="Todo nicht gefunden")

    update_data = data.model_dump(exclude_unset=True)
    if "project_id" in update_data and update_data["project_id"]:
        project = db.query(Project).filter(Project.id == update_data["project_id"]).first()
        if not project:
            raise HTTPException(status_code=404, detail="Projekt nicht gefunden")
    if "goal_id" in update_data:
        _validate_goal(db, update_data["goal_id"])

    for key, value in update_data.items():
        setattr(todo, key, value)

    # completed <-> status synchron halten.
    if "completed" in update_data:
        todo.status = "done" if todo.completed else ("scheduled" if todo.scheduled_start else "open")
    elif update_data.get("status") == "done":
        todo.completed = True
    elif "status" in update_data and todo.status != "done":
        todo.completed = False

    db.commit()
    db.refresh(todo)
    return todo


@router.post("/{todo_id}/defer", response_model=TodoResponse)
def defer_todo(todo_id: str, data: TodoDeferRequest, db: Session = Depends(get_db)):
    """Todo aufschieben: geht nie verloren (bleibt Pool/geplant/Carry-over)."""
    todo = db.query(Todo).filter(Todo.id == todo_id).first()
    if not todo:
        raise HTTPException(status_code=404, detail="Todo nicht gefunden")

    todo.defer_count = (todo.defer_count or 0) + 1
    todo.last_deferred_at = datetime.now(timezone.utc)
    if data.reason is not None:
        todo.defer_reason = data.reason

    if data.scheduled_start and data.scheduled_end:
        todo.scheduled_start = data.scheduled_start
        todo.scheduled_end = data.scheduled_end
        todo.planned_date = data.scheduled_start.date()
        todo.scheduling_mode = "fixed_slot"
        todo.status = "scheduled"
    elif data.planned_date:
        todo.planned_date = data.planned_date
        todo.scheduled_start = None
        todo.scheduled_end = None
        todo.scheduling_mode = "planned_day"
        todo.status = "scheduled"
    else:
        # Zurück in den Pool (z.B. "später entscheiden"): bleibt erhalten.
        todo.planned_date = None
        todo.scheduled_start = None
        todo.scheduled_end = None
        todo.scheduling_mode = "pool"
        todo.status = "deferred"

    db.commit()
    db.refresh(todo)
    return todo


@router.post("/{todo_id}/schedule", response_model=TodoResponse)
def schedule_todo(todo_id: str, data: TodoScheduleRequest, db: Session = Depends(get_db)):
    """Todo planen: Pool / Tag / fester Slot."""
    todo = db.query(Todo).filter(Todo.id == todo_id).first()
    if not todo:
        raise HTTPException(status_code=404, detail="Todo nicht gefunden")

    todo.scheduling_mode = data.scheduling_mode
    todo.planned_date = data.planned_date
    todo.scheduled_start = data.scheduled_start
    todo.scheduled_end = data.scheduled_end
    if data.scheduling_mode == "pool":
        todo.status = "open" if not todo.completed else "done"
    elif not todo.completed:
        todo.status = "scheduled"
    db.commit()
    db.refresh(todo)
    return todo


@router.post("/{todo_id}/cancel", response_model=TodoResponse)
def cancel_todo(todo_id: str, db: Session = Depends(get_db)):
    """Todo bewusst abbrechen (≠ erledigt, ≠ Löschen)."""
    todo = db.query(Todo).filter(Todo.id == todo_id).first()
    if not todo:
        raise HTTPException(status_code=404, detail="Todo nicht gefunden")
    todo.status = "cancelled"
    db.commit()
    db.refresh(todo)
    return todo


@router.delete("/{todo_id}", status_code=204)
def delete_todo(todo_id: str, db: Session = Depends(get_db)):
    todo = db.query(Todo).filter(Todo.id == todo_id).first()
    if not todo:
        raise HTTPException(status_code=404, detail="Todo nicht gefunden")
    db.delete(todo)
    db.commit()


@router.post("/{todo_id}/complete", response_model=TodoResponse)
def complete_todo(todo_id: str, db: Session = Depends(get_db)):
    todo = db.query(Todo).filter(Todo.id == todo_id).first()
    if not todo:
        raise HTTPException(status_code=404, detail="Todo nicht gefunden")

    now = datetime.now(timezone.utc)
    today = date.today()

    if todo.recurrence:
        # Recurring: log completion and advance due_date
        completion = TodoCompletion(
            todo_id=todo.id,
            completed_date=today,
            completed_at=now,
        )
        db.add(completion)
        if todo.due_date:
            todo.due_date = _advance_due_date(todo.due_date, todo.recurrence)
        else:
            todo.due_date = _advance_due_date(today, todo.recurrence)
        todo.completed_at = now
    else:
        # One-time: mark completed
        todo.completed = True
        todo.completed_at = now
        todo.status = "done"

    db.commit()
    db.refresh(todo)
    return todo


@router.post("/{todo_id}/uncomplete", response_model=TodoResponse)
def uncomplete_todo(todo_id: str, db: Session = Depends(get_db)):
    todo = db.query(Todo).filter(Todo.id == todo_id).first()
    if not todo:
        raise HTTPException(status_code=404, detail="Todo nicht gefunden")

    if todo.recurrence:
        raise HTTPException(status_code=400, detail="Wiederkehrende Todos koennen nicht rueckgaengig gemacht werden")

    todo.completed = False
    todo.completed_at = None
    todo.status = "scheduled" if todo.scheduled_start else "open"
    db.commit()
    db.refresh(todo)
    return todo


@router.get("/{todo_id}/completions", response_model=list[TodoCompletionResponse])
def list_completions(todo_id: str, db: Session = Depends(get_db)):
    todo = db.query(Todo).filter(Todo.id == todo_id).first()
    if not todo:
        raise HTTPException(status_code=404, detail="Todo nicht gefunden")
    return (
        db.query(TodoCompletion)
        .filter(TodoCompletion.todo_id == todo_id)
        .order_by(TodoCompletion.completed_at.desc())
        .all()
    )
