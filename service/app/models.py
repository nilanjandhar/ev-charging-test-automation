from sqlalchemy import Column, String, Float, Integer, Boolean, DateTime
from .database import Base
import datetime


class StationReport(Base):
    __tablename__ = "station_reports"

    id = Column(Integer, primary_key=True, index=True)
    station_id = Column(String, index=True, nullable=False)
    timestamp = Column(DateTime, nullable=False)
    connectivity_status = Column(String, nullable=False)
    latency_ms = Column(Float, nullable=False)
    error_count = Column(Integer, nullable=False)
    firmware_version = Column(String, nullable=False)
    hygiene_score = Column(Float, nullable=False)
    flagged = Column(Boolean, nullable=False)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
