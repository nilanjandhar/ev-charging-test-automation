from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
import asyncio
import os

from .database import Base, engine
from .routers import reports, stations, metrics

Base.metadata.create_all(bind=engine)

app = FastAPI(
    title="NOC Station Health API",
    description="Ingests EV charging station health reports and computes network hygiene scores.",
    version="1.0.0",
)

# Configurable simulated processing latency — set SIMULATED_LATENCY_MS to mimic
# real-world I/O costs (DB round-trips, downstream calls) that SQLite hides.
_LATENCY_MS = int(os.getenv("SIMULATED_LATENCY_MS", "0"))


@app.middleware("http")
async def simulated_latency_middleware(request: Request, call_next):
    if _LATENCY_MS > 0:
        await asyncio.sleep(_LATENCY_MS / 1000)
    return await call_next(request)


app.include_router(reports.router)
app.include_router(stations.router)
app.include_router(metrics.router)

static_dir = os.path.join(os.path.dirname(__file__), "..", "static")
if os.path.isdir(static_dir):
    app.mount("/static", StaticFiles(directory=static_dir), name="static")

    @app.get("/", include_in_schema=False)
    def dashboard():
        return FileResponse(os.path.join(static_dir, "index.html"))


@app.get("/health", tags=["health"])
def health_check():
    return {"status": "ok"}
