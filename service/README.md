# NOC Station Health API — Service

A FastAPI microservice that ingests health reports from EV charging stations, computes a **network hygiene score**, and exposes REST APIs to query station health.

## Quick Start

### Option 1 — Docker (recommended)

```bash
cd service && docker compose up
```

This starts a **PostgreSQL** database and the API together. Service will be available at `http://localhost:8000`.

### Option 2 — Local Python (SQLite)

```bash
cd service
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload
```

> **Note:** Local mode uses SQLite. The Docker environment uses PostgreSQL — behaviour under concurrent load will differ significantly.

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `DATABASE_URL` | `sqlite:///./noc.db` | SQLAlchemy database URL |
| `SIMULATED_LATENCY_MS` | `40` (Docker) / `0` (local) | Artificial per-request delay in ms |

## Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/reports` | Ingest a station health report |
| `GET` | `/stations` | List all known stations with latest status |
| `GET` | `/stations/{station_id}/status` | Latest status for a specific station |
| `GET` | `/stations/poor-hygiene` | All currently flagged stations |
| `GET` | `/metrics/summary` | Aggregated network metrics |
| `GET` | `/` | Live dashboard UI |
| `GET` | `/health` | Health check |
| `GET` | `/docs` | Interactive OpenAPI docs (Swagger UI) |

## Example Request

```bash
curl -X POST http://localhost:8000/reports \
  -H "Content-Type: application/json" \
  -d '{
    "station_id": "STATION-001",
    "timestamp": "2024-06-01T10:00:00Z",
    "connectivity_status": "online",
    "latency_ms": 120,
    "error_count": 2,
    "firmware_version": "v2.3.1"
  }'
```

## Hygiene Score Formula

The score starts at **100** and is reduced by:

- Offline connectivity: **−40 points**
- High error count: **−5 per error**, capped at **−30**
- High latency: **−(latency_ms ÷ 20)**, capped at **−20**

A station is **flagged** when its score falls below **60**.
