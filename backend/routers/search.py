"""Volltext-Schnellsuche über Events, Todos, Tagesziele und Kontakte.

Liefert ein vereinheitlichtes Trefferformat für die Command-Palette (⌘K) im Frontend.
SQLite-LIKE, case-insensitiv; bewusst schlank (kein FTS) für den Single-User-Maßstab.
"""
from datetime import date

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from backend.database import get_db
from backend.models import Contact, DailyGoal, Event, Todo

router = APIRouter(prefix="/api/search", tags=["search"])


def _like(column, term: str):
    return func.lower(column).like(f"%{term.lower()}%")


@router.get("")
def search(q: str = Query(..., min_length=1, max_length=100), limit: int = Query(8, ge=1, le=25), db: Session = Depends(get_db)):
    term = q.strip()
    results: list[dict] = []

    events = (
        db.query(Event)
        .filter(or_(_like(Event.title, term), _like(Event.description, term), _like(Event.location, term)))
        .order_by(Event.start.desc())
        .limit(limit)
        .all()
    )
    for e in events:
        results.append({
            "kind": "event", "kind_label": "Termin", "icon": "📅",
            "id": e.id, "title": e.title,
            "date": e.start.date().isoformat() if e.start else None,
        })

    todos = (
        db.query(Todo)
        .filter(or_(_like(Todo.title, term), _like(Todo.description, term)))
        .order_by(Todo.created_at.desc())
        .limit(limit)
        .all()
    )
    for t in todos:
        results.append({
            "kind": "todo", "kind_label": "Aufgabe", "icon": "✅",
            "id": t.id, "title": t.title,
            "date": t.due_date.isoformat() if t.due_date else None,
        })

    goals = (
        db.query(DailyGoal)
        .filter(_like(DailyGoal.title, term))
        .order_by(DailyGoal.date.desc())
        .limit(limit)
        .all()
    )
    for g in goals:
        results.append({
            "kind": "goal", "kind_label": "Tagesziel", "icon": "🎯",
            "id": g.id, "title": g.title,
            "date": g.date.isoformat() if g.date else None,
        })

    contacts = (
        db.query(Contact)
        .filter(or_(_like(Contact.name, term), _like(Contact.email, term)))
        .order_by(Contact.name)
        .limit(limit)
        .all()
    )
    for c in contacts:
        results.append({
            "kind": "contact", "kind_label": "Kontakt", "icon": "👤",
            "id": c.id, "title": c.name, "date": None,
        })

    return {"query": term, "results": results}
