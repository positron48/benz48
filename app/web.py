from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import FastAPI, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.storage import Storage

storage = Storage()
static_dir = Path(__file__).parent / "static"

app = FastAPI(title="Lipetsk Gas Monitor")


@app.on_event("startup")
def _bootstrap_on_startup() -> None:
    from app.bootstrap import bootstrap_on_startup

    bootstrap_on_startup()
    global storage
    storage = Storage()


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/meta")
def meta() -> dict[str, Any]:
    return storage.get_meta()


@app.get("/api/latest")
def latest() -> dict[str, Any] | None:
    return storage.get_latest()


@app.get("/api/timeseries")
def timeseries(
    date_from: str | None = Query(default=None, alias="from"),
    date_to: str | None = Query(default=None, alias="to"),
    station_ids: list[str] | None = Query(default=None),
    brands: list[str] | None = Query(default=None),
) -> list[dict[str, Any]]:
    return storage.query_timeseries(date_from, date_to, station_ids, brands)


@app.get("/api/summary")
def summary(
    date_from: str | None = Query(default=None, alias="from"),
    date_to: str | None = Query(default=None, alias="to"),
    station_ids: list[str] | None = Query(default=None),
    brands: list[str] | None = Query(default=None),
) -> list[dict[str, Any]]:
    return storage.query_summary(date_from, date_to, station_ids, brands)


@app.get("/api/fuel-since")
def fuel_since(
    date_from: str | None = Query(default=None, alias="from"),
    date_to: str | None = Query(default=None, alias="to"),
    station_ids: list[str] | None = Query(default=None),
    brands: list[str] | None = Query(default=None),
) -> list[dict[str, Any]]:
    return storage.query_fuel_since(date_from, date_to, station_ids, brands)


@app.get("/")
def index() -> FileResponse:
    return FileResponse(
        static_dir / "index.html",
        headers={"Cache-Control": "no-cache, must-revalidate"},
    )


app.mount("/static", StaticFiles(directory=static_dir), name="static")


if __name__ == "__main__":
    import uvicorn

    from app.config import settings

    uvicorn.run("app.web:app", host="0.0.0.0", port=settings.listen_port, reload=False)
