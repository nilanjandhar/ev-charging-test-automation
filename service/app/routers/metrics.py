from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session
from sqlalchemy import func
from ..database import get_db
from ..models import StationReport
from ..schemas import MetricsSummary

router = APIRouter(prefix="/metrics", tags=["metrics"])


@router.get("/summary", response_model=MetricsSummary)
def get_metrics_summary(db: Session = Depends(get_db)):
    # Latest report per station via subquery
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
        .all()
    )

    total = len(latest_reports)
    online = sum(1 for r in latest_reports if r.connectivity_status == "online")
    offline = total - online
    flagged = sum(1 for r in latest_reports if r.flagged)
    total_errors = sum(r.error_count for r in latest_reports)
    avg_latency = (
        round(sum(r.latency_ms for r in latest_reports) / total, 2) if total > 0 else None
    )

    return MetricsSummary(
        total_stations=total,
        online_count=online,
        offline_count=offline,
        flagged_count=flagged,
        average_latency_ms=avg_latency,
        total_error_count=total_errors,
    )
