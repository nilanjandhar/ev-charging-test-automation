from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session
from ..database import get_db
from ..models import StationReport
from ..schemas import ReportIn, ReportOut
from ..scoring import compute_hygiene_score, is_flagged

router = APIRouter(prefix="/reports", tags=["reports"])


@router.post("", response_model=ReportOut, status_code=201)
def ingest_report(payload: ReportIn, db: Session = Depends(get_db)):
    score = compute_hygiene_score(
        connectivity_status=payload.connectivity_status,
        latency_ms=payload.latency_ms,
        error_count=payload.error_count,
    )
    flagged = is_flagged(score)

    report = StationReport(
        station_id=payload.station_id,
        timestamp=payload.timestamp,
        connectivity_status=payload.connectivity_status,
        latency_ms=payload.latency_ms,
        error_count=payload.error_count,
        firmware_version=payload.firmware_version,
        hygiene_score=score,
        flagged=flagged,
    )
    db.add(report)
    db.commit()

    return ReportOut(station_id=payload.station_id, hygiene_score=score, flagged=flagged)
