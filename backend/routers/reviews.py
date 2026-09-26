"""Tagesreview (DailyReview): optionale Abend-Retrospektive pro Tag (nachrangig).

Upsert pro Datum: was lief gut, Blocker, Grund aufgegebener Ziele, Carry-over, Energie.
"""

from datetime import date as date_type

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from backend.database import get_db
from backend.models import DailyReview
from backend.schemas import DailyReviewResponse, DailyReviewUpsert

router = APIRouter(prefix="/api/reviews", tags=["reviews"])


@router.get("/{review_date}", response_model=DailyReviewResponse | None)
def get_review(review_date: date_type, db: Session = Depends(get_db)):
    return db.query(DailyReview).filter(DailyReview.date == review_date).first()


@router.put("/{review_date}", response_model=DailyReviewResponse)
def upsert_review(review_date: date_type, data: DailyReviewUpsert, db: Session = Depends(get_db)):
    review = db.query(DailyReview).filter(DailyReview.date == review_date).first()
    if not review:
        review = DailyReview(date=review_date)
        db.add(review)
    for key, value in data.model_dump(exclude_unset=True).items():
        setattr(review, key, value)
    db.commit()
    db.refresh(review)
    return review


@router.delete("/{review_date}", status_code=204)
def delete_review(review_date: date_type, db: Session = Depends(get_db)):
    review = db.query(DailyReview).filter(DailyReview.date == review_date).first()
    if not review:
        raise HTTPException(status_code=404, detail="Kein Review fuer dieses Datum")
    db.delete(review)
    db.commit()
