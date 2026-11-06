"""Internal API für system-internen Zugriff (knowledge-gateway).

Auth: Bearer-Token aus env (KG_INTERNAL_TOKEN). Sucht über Events (Titel,
Beschreibung) und Todos (Titel), die beiden Inhaltsquellen, die KG-Cross-Source
sinnvoll machen.
"""
import os
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from backend.database import get_db
from backend.models import Event, Todo

router = APIRouter(prefix="/api/internal", tags=["internal"])


def _expected_token() -> str:
    return os.environ.get("KG_INTERNAL_TOKEN", "")


def _check_token(authorization: str | None = Header(None)) -> None:
    expected = _expected_token()
    if not expected:
        raise HTTPException(status_code=403, detail="internal API disabled")
    if authorization != f"Bearer {expected}":
        raise HTTPException(status_code=401, detail="invalid token")


@router.get("/search")
def search(
    q: str = Query(min_length=1),
    limit: int = Query(20, ge=1, le=100),
    _: None = Depends(_check_token),
    db: Session = Depends(get_db),
):
    pattern = f"%{q}%"
    per_kind = max(1, limit // 2)

    event_stmt = (
        select(Event)
        .where(or_(Event.title.ilike(pattern), Event.description.ilike(pattern)))
        .order_by(Event.start.desc())
        .limit(per_kind)
    )
    events = db.execute(event_stmt).scalars().all()

    todo_stmt = (
        select(Todo)
        .where(or_(Todo.title.ilike(pattern), Todo.description.ilike(pattern)))
        .order_by(Todo.id.desc())
        .limit(per_kind)
    )
    todos = db.execute(todo_stmt).scalars().all()

    results = []
    for e in events:
        results.append({
            "id": e.id,
            "kind": "event",
            "title": e.title,
            "snippet": (e.description or "")[:200],
            "start": e.start.isoformat() if e.start else None,
            "end": e.end.isoformat() if e.end else None,
            "url": f"/events/{e.id}",
        })
    for t in todos:
        results.append({
            "id": t.id,
            "kind": "todo",
            "title": t.title,
            "snippet": (t.description or "")[:200],
            "due_date": t.due_date.isoformat() if t.due_date else None,
            "url": f"/todos/{t.id}",
        })
    return results[:limit]
