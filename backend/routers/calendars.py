from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from backend.database import get_db
from backend.models import Calendar
from backend.schemas import CalendarCreate, CalendarResponse, CalendarUpdate

router = APIRouter(prefix="/api/calendars", tags=["calendars"])


@router.get("", response_model=list[CalendarResponse])
def list_calendars(db: Session = Depends(get_db)):
    return db.query(Calendar).order_by(Calendar.name).all()


@router.post("", response_model=CalendarResponse, status_code=201)
def create_calendar(data: CalendarCreate, db: Session = Depends(get_db)):
    """Eigene Kalender anzulegen ist bewusst gesperrt.

    Der Dienst fuehrt EINEN globalen Satz System-Kalender (Arbeit/Schule/Urlaub/
    Krank/Feiertage/Geburtstage/Termine), den alle Mandanten teilen; getrennt sind
    die Termine darin, nicht die Huellen. Ein selbst angelegter Kalender waere ein
    Sonderfall ohne Gegenstueck in der Tagestyp-Logik, und bis 2026-08 hat ihn der
    naechste Neustart kommentarlos wieder eingesammelt. Lieber ehrlich 403 als ein
    Objekt anbieten, das wieder verschwindet.
    """
    raise HTTPException(
        status_code=403,
        detail="Eigene Kalender sind nicht vorgesehen: Termine gehören in die System-Kalender.",
    )


@router.get("/{calendar_id}", response_model=CalendarResponse)
def get_calendar(calendar_id: str, db: Session = Depends(get_db)):
    cal = db.query(Calendar).filter(Calendar.id == calendar_id).first()
    if not cal:
        raise HTTPException(status_code=404, detail="Calendar not found")
    return cal


@router.put("/{calendar_id}", response_model=CalendarResponse)
def update_calendar(calendar_id: str, data: CalendarUpdate, db: Session = Depends(get_db)):
    cal = db.query(Calendar).filter(Calendar.id == calendar_id).first()
    if not cal:
        raise HTTPException(status_code=404, detail="Calendar not found")
    if cal.is_system:
        raise HTTPException(status_code=403, detail="System-Kalender kann nicht bearbeitet werden")
    for key, value in data.model_dump(exclude_unset=True).items():
        setattr(cal, key, value)
    db.commit()
    db.refresh(cal)
    return cal


@router.delete("/{calendar_id}", status_code=204)
def delete_calendar(calendar_id: str, db: Session = Depends(get_db)):
    cal = db.query(Calendar).filter(Calendar.id == calendar_id).first()
    if not cal:
        raise HTTPException(status_code=404, detail="Calendar not found")
    if cal.is_system:
        raise HTTPException(status_code=403, detail="System-Kalender kann nicht geloescht werden")
    db.delete(cal)
    db.commit()
