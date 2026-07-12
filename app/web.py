from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Query, Response
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.cache import TTLCache, build_cache_key
from app.config import settings
from app.storage import Storage

storage = Storage()
api_cache = TTLCache(settings.api_cache_seconds)
static_dir = Path(__file__).parent / "static"

app = FastAPI(title="Lipetsk Gas Monitor")


def _set_cache_headers(response: Response, *, hit: bool) -> None:
    if not api_cache.enabled:
        return
    response.headers["Cache-Control"] = f"public, max-age={api_cache.ttl_seconds}"
    response.headers["X-Cache"] = "HIT" if hit else "MISS"


def _cached(response: Response, key: str, loader: Callable[[], Any]) -> Any:
    cached = api_cache.get(key)
    if cached is not None:
        _set_cache_headers(response, hit=True)
        return cached
    data = loader()
    api_cache.set(key, data)
    _set_cache_headers(response, hit=False)
    return data


@app.on_event("startup")
def _bootstrap_on_startup() -> None:
    from app.bootstrap import bootstrap_on_startup

    bootstrap_on_startup()
    global storage, api_cache
    storage = Storage()
    api_cache = TTLCache(settings.api_cache_seconds)


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/meta")
def meta(response: Response) -> dict[str, Any]:
    return _cached(response, "meta", storage.get_meta)


@app.get("/api/latest")
def latest(response: Response) -> dict[str, Any] | None:
    return _cached(response, "latest", storage.get_latest)


@app.get("/api/timeseries")
def timeseries(
    response: Response,
    date_from: str | None = Query(default=None, alias="from"),
    date_to: str | None = Query(default=None, alias="to"),
    station_ids: list[str] | None = Query(default=None),
    brands: list[str] | None = Query(default=None),
) -> list[dict[str, Any]]:
    key = build_cache_key(
        "timeseries",
        date_from=date_from,
        date_to=date_to,
        station_ids=station_ids,
        brands=brands,
    )
    return _cached(
        response,
        key,
        lambda: storage.query_timeseries(date_from, date_to, station_ids, brands),
    )


@app.get("/api/summary")
def summary(
    response: Response,
    date_from: str | None = Query(default=None, alias="from"),
    date_to: str | None = Query(default=None, alias="to"),
    station_ids: list[str] | None = Query(default=None),
    brands: list[str] | None = Query(default=None),
) -> list[dict[str, Any]]:
    key = build_cache_key(
        "summary",
        date_from=date_from,
        date_to=date_to,
        station_ids=station_ids,
        brands=brands,
    )
    return _cached(
        response,
        key,
        lambda: storage.query_summary(date_from, date_to, station_ids, brands),
    )


@app.get("/api/fuel-since")
def fuel_since(
    response: Response,
    date_from: str | None = Query(default=None, alias="from"),
    date_to: str | None = Query(default=None, alias="to"),
    station_ids: list[str] | None = Query(default=None),
    brands: list[str] | None = Query(default=None),
) -> list[dict[str, Any]]:
    key = build_cache_key(
        "fuel-since",
        date_from=date_from,
        date_to=date_to,
        station_ids=station_ids,
        brands=brands,
    )
    return _cached(
        response,
        key,
        lambda: storage.query_fuel_since(date_from, date_to, station_ids, brands),
    )


@app.get("/favicon.ico")
def favicon() -> FileResponse:
    return FileResponse(static_dir / "favicon.svg", media_type="image/svg+xml")


@app.get("/")
def index() -> FileResponse:
    return FileResponse(
        static_dir / "index.html",
        headers={"Cache-Control": "no-cache, must-revalidate"},
    )


app.mount("/static", StaticFiles(directory=static_dir), name="static")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app.web:app", host="0.0.0.0", port=settings.listen_port, reload=False)
