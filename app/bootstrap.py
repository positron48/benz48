from __future__ import annotations

import fcntl
import gzip
import logging
import os
import shutil
import sqlite3
import tempfile
from contextlib import contextmanager
from pathlib import Path

import httpx

from app.storage import Storage

logger = logging.getLogger(__name__)

DEFAULT_BOOTSTRAP_PATH = Path(__file__).resolve().parent.parent / "bootstrap" / "history.db.gz"


def _min_snapshots_threshold() -> int:
    return int(os.getenv("BOOTSTRAP_MIN_SNAPSHOTS", "10"))


@contextmanager
def _bootstrap_lock(data_dir: Path):
    lock_path = data_dir / ".bootstrap.lock"
    data_dir.mkdir(parents=True, exist_ok=True)
    with lock_path.open("w") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        yield


def _validate_sqlite(path: Path) -> int:
    conn = sqlite3.connect(path)
    try:
        count = conn.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0]
        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            raise ValueError(f"integrity_check failed: {integrity}")
        return int(count)
    finally:
        conn.close()


def _install_db(source: Path, target: Path) -> int:
    target.parent.mkdir(parents=True, exist_ok=True)
    for suffix in ("-wal", "-shm"):
        extra = Path(str(target) + suffix)
        if extra.exists():
            extra.unlink()
    with tempfile.NamedTemporaryFile(dir=target.parent, delete=False, suffix=".db") as tmp:
        tmp_path = Path(tmp.name)
    try:
        shutil.copy2(source, tmp_path)
        snapshots = _validate_sqlite(tmp_path)
        tmp_path.replace(target)
        return snapshots
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise


def _install_gzip(source: Path, target: Path) -> int:
    with tempfile.NamedTemporaryFile(dir=target.parent, delete=False, suffix=".db") as tmp:
        tmp_path = Path(tmp.name)
    try:
        with gzip.open(source, "rb") as src, tmp_path.open("wb") as dst:
            shutil.copyfileobj(src, dst)
        snapshots = _validate_sqlite(tmp_path)
        for suffix in ("-wal", "-shm"):
            extra = Path(str(target) + suffix)
            if extra.exists():
                extra.unlink()
        tmp_path.replace(target)
        return snapshots
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise


def _download(url: str) -> Path:
    with httpx.Client(timeout=60, follow_redirects=True) as client:
        response = client.get(url)
        response.raise_for_status()
    tmp = Path(tempfile.mkstemp(suffix=".bootstrap")[1])
    tmp.write_bytes(response.content)
    return tmp


def bootstrap_database_if_needed(storage: Storage | None = None) -> bool:
    storage = storage or Storage()
    current = storage.get_meta().get("snapshot_count") or 0
    if current >= _min_snapshots_threshold():
        return False

    local_path = os.getenv("BOOTSTRAP_DB_PATH")
    url = os.getenv("BOOTSTRAP_DB_URL")
    candidates: list[Path] = []
    if local_path:
        candidates.append(Path(local_path))
    if DEFAULT_BOOTSTRAP_PATH.exists():
        candidates.append(DEFAULT_BOOTSTRAP_PATH)

    downloaded: Path | None = None
    try:
        with _bootstrap_lock(storage.data_dir):
            current = storage.get_meta().get("snapshot_count") or 0
            if current >= _min_snapshots_threshold():
                return False

            if url:
                downloaded = _download(url)
                candidates.insert(0, downloaded)

            for candidate in candidates:
                if not candidate.exists():
                    continue
                try:
                    if candidate.suffix == ".gz":
                        snapshots = _install_gzip(candidate, storage.db_path)
                    else:
                        snapshots = _install_db(candidate, storage.db_path)
                    logger.info(
                        "Bootstrapped database from %s (%s snapshots, was %s)",
                        candidate,
                        snapshots,
                        current,
                    )
                    return True
                except Exception:
                    logger.exception("Bootstrap failed for %s", candidate)
            return False
    finally:
        if downloaded and downloaded.exists():
            downloaded.unlink(missing_ok=True)


def bootstrap_on_startup() -> None:
    if os.getenv("BOOTSTRAP_ENABLED", "true").lower() in {"0", "false", "no"}:
        return
    bootstrap_database_if_needed(Storage())
