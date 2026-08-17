from pydantic import BaseModel, Field
from datetime import datetime
from typing import Literal, List, Optional


class ReportIn(BaseModel):
    station_id: str = Field(..., min_length=1)
    timestamp: datetime
    connectivity_status: Literal["online", "offline"]
    latency_ms: float = Field(..., ge=0)
    error_count: int = Field(..., ge=0)
    firmware_version: str = Field(..., min_length=1)


class ReportOut(BaseModel):
    station_id: str
    hygiene_score: float
    flagged: bool


class StationStatus(BaseModel):
    station_id: str
    latest_timestamp: datetime
    connectivity_status: str
    latency_ms: float
    error_count: int
    firmware_version: str
    hygiene_score: float
    flagged: bool


class StationSummary(BaseModel):
    station_id: str
    hygiene_score: float
    flagged: bool
    connectivity_status: str
    latest_timestamp: datetime


class PoorHygieneStation(BaseModel):
    station_id: str
    hygiene_score: float
    latest_timestamp: datetime


class MetricsSummary(BaseModel):
    total_stations: int
    online_count: int
    offline_count: int
    flagged_count: int
    average_latency_ms: Optional[float]
    total_error_count: int
