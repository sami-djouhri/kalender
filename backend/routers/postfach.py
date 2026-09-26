from datetime import date, datetime, time, timedelta, timezone

import jwt
from fastapi import APIRouter, Depends, Header, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from backend.config import settings
from backend.database import get_db
from backend.models import Calendar, Event, Setting
from backend.scheduler import check_conflicts_after_event_change

from backend.system_calendars import TERMINE_CAL_ID  # noqa: F401

router = APIRouter(prefix="/api/integrations/postfach", tags=["integrations"])


class PostfachDeadlineIn(BaseModel):
    letter_id: int = Field(ge=1)
    typ: str = Field(min_length=1, max_length=60)
    datum: date
    beschreibung: str = Field(min_length=1, max_length=300)


def _verify_feed_token(
    db: Session,
    feed_token: str | None,
    x_feed_token: str | None,
    authorization: str | None,
) -> None:
    token = x_feed_token or feed_token
    stored = db.query(Setting).filter(Setting.key == "feed_token").first()
    if stored and token and token == stored.value:
        return

    # K1: nur mit gesetztem SECRET_KEY dekodieren, sonst wuerde jwt.decode mit
    # leerem HMAC-Key einen vom Angreifer mit '' signierten Token akzeptieren.
    if authorization and authorization.startswith("Bearer ") and settings.SECRET_KEY:
        try:
            jwt.decode(
                authorization[7:],
                settings.SECRET_KEY,
                algorithms=[settings.JWT_ALGORITHM],
            )
            return
        except jwt.PyJWTError:
            pass

    raise HTTPException(status_code=401, detail="Invalid integration token")


def _ensure_termine_calendar(db: Session) -> None:
    existing = db.query(Calendar).filter(Calendar.id == TERMINE_CAL_ID).first()
    if existing:
        return
    db.add(
        Calendar(
            id=TERMINE_CAL_ID,
            name="Termine",
            color="#3788d8",
            is_system=True,
        )
    )
    db.flush()


@router.post("/deadlines", status_code=201)
def upsert_postfach_deadline(
    data: PostfachDeadlineIn,
    feed_token: str | None = Query(None),
    x_feed_token: str | None = Header(None),
    authorization: str | None = Header(None),
    db: Session = Depends(get_db),
):
    _verify_feed_token(db, feed_token, x_feed_token, authorization)
    _ensure_termine_calendar(db)

    start = datetime.combine(data.datum, time.min, tzinfo=timezone.utc)
    end = start + timedelta(days=1)
    marker = f"[Briefkasten #{data.letter_id}] Typ: {data.typ}"
    title = f"Frist: {data.beschreibung}"

    existing = (
        db.query(Event)
        .filter(Event.calendar_id == TERMINE_CAL_ID)
        .filter(Event.start == start)
        .filter(Event.description.like(f"%[Briefkasten #{data.letter_id}]%"))
        .filter(Event.description.like(f"%Typ: {data.typ}%"))
        .first()
    )

    description = f"{marker}\nQuelle: Postfach"
    if existing:
        existing.title = title
        existing.description = description
        existing.end = end
        existing.all_day = True
        db.commit()
        db.refresh(existing)
        check_conflicts_after_event_change(db, existing)
        return {"status": "existing", "event_id": existing.id}

    event = Event(
        calendar_id=TERMINE_CAL_ID,
        title=title,
        description=description,
        start=start,
        end=end,
        all_day=True,
    )
    db.add(event)
    db.commit()
    db.refresh(event)
    check_conflicts_after_event_change(db, event)
    return {"status": "created", "event_id": event.id}
