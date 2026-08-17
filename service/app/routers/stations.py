from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from sqlalchemy import func
from ..database import get_db
from ..models import StationReport
from ..schemas import StationStatus, StationSummary, PoorHygieneStation
from typing import List

router = APIRouter(prefix="/stations", tags=["stations"])


@router.get("", response_model=List[StationSummary])
def list_stations(db: Session = Depends(get_db)):
    """List all known stations with their latest hygiene status."""
    latest_per_station = (
        db.query(
            StationReport.station_id,
            func.max(StationReport.timestamp).label("max_ts"),
        )
        .group_by(StationReport.station_id)
        .subquery()
    )

    latest_reports = (
        db.query(StationReport)
        .join(
            latest_per_station,
            (StationReport.station_id == latest_per_station.c.station_id)
            & (StationReport.timestamp == latest_per_station.c.max_ts),
        )
        .order_by(StationReport.station_id)
        .all()
    )

    return [
        StationSummary(
            station_id=r.station_id,
            hygiene_score=r.hygiene_score,
            flagged=r.flagged,
            connectivity_status=r.connectivity_status,
            latest_timestamp=r.timestamp,
        )
        for r in latest_reports
    ]


@router.get("/{station_id}/status", response_model=StationStatus)
def get_station_status(station_id: str, db: Session = Depends(get_db)):
    report = (
        db.query(StationReport)
        .filter(StationReport.station_id == station_id)
        .order_by(StationReport.timestamp.desc())
        .first()
    )
    if not report:
        raise HTTPException(status_code=404, detail=f"Station '{station_id}' not found")

    return StationStatus(
        station_id=report.station_id,
        latest_timestamp=report.timestamp,
        connectivity_status=report.connectivity_status,
        latency_ms=report.latency_ms,
        error_count=report.error_count,
        firmware_version=report.firmware_version,
        hygiene_score=report.hygiene_score,
        flagged=report.flagged,
    )


@router.get("/poor-hygiene", response_model=List[PoorHygieneStation])
def get_poor_hygiene_stations(db: Session = Depends(get_db)):
    # Subquery: latest report per station
    latest_per_station = (
        db.query(
            StationReport.station_id,
            func.max(StationReport.timestamp).label("max_ts"),
        )
        .group_by(StationReport.station_id)
        .subquery()
    )

    flagged = (
        db.query(StationReport)
        .join(
            latest_per_station,
            (StationReport.station_id == latest_per_station.c.station_id)
            & (StationReport.timestamp == latest_per_station.c.max_ts),
        )
        .filter(StationReport.flagged == True)
        .all()
    )

    return [
        PoorHygieneStation(
            station_id=r.station_id,
            hygiene_score=r.hygiene_score,
            latest_timestamp=r.timestamp,
        )
        for r in flagged
    ]
