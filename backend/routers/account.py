"""Konto-/DSGVO-API: Datenexport (Art. 20), Löschung (Art. 17), Transparenz.

Tenant-gescopt über get_db. Die Löschung verlangt eine explizite Bestätigung, damit
sie nicht versehentlich (Owner-Fallback!) das ganze Konto leert.
"""

from datetime import date

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from backend.account import account_summary, delete_account_data, export_account_data
from backend.database import get_db

router = APIRouter(prefix="/api/account", tags=["account"])

CONFIRM_PHRASE = "KONTO LOESCHEN"


@router.get("/summary")
def summary(db: Session = Depends(get_db)):
    """Übersicht der gespeicherten Datensätze pro Kategorie (Transparenz)."""
    return account_summary(db)


@router.get("/export")
def export(db: Session = Depends(get_db)):
    """Vollständiger Datenexport als JSON-Download (Recht auf Datenübertragbarkeit)."""
    data = export_account_data(db)
    filename = f"saganta-kalender-export-{date.today().isoformat()}.json"
    return JSONResponse(
        content=data,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


class DeleteRequest(BaseModel):
    confirm: str


@router.post("/delete")
def delete_account(data: DeleteRequest, db: Session = Depends(get_db)):
    """Löscht das gesamte Konto (alle nutzerbezogenen Daten). Erfordert Bestätigung."""
    if (data.confirm or "").strip().upper() != CONFIRM_PHRASE:
        raise HTTPException(
            status_code=400,
            detail=f'Bestätigung erforderlich: sende confirm="{CONFIRM_PHRASE}".',
        )
    counts = delete_account_data(db)
    return {"deleted": counts, "total": sum(counts.values())}
