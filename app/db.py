"""SQLite persistence layer.

Everything ResearchForge stores is auditable: raw normalized records, the
search ledger, per-claim evidence, verification results, dataset states,
downloads, compatibility reports, research-gap candidates, manuscripts and
job state. Nothing is derived from a language model at rest.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable, Iterator, Sequence

from .config import get_settings

SCHEMA_VERSION = 1

SCHEMA_SQL = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS projects (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    title       TEXT NOT NULL,
    question    TEXT NOT NULL,
    spec_json   TEXT NOT NULL DEFAULT '{}',
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS search_runs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id   INTEGER REFERENCES projects(id) ON DELETE CASCADE,
    question     TEXT NOT NULL,
    plan_json    TEXT NOT NULL DEFAULT '{}',
    status       TEXT NOT NULL DEFAULT 'pending',
    scope_json   TEXT NOT NULL DEFAULT '{}',
    failure_json TEXT NOT NULL DEFAULT '[]',
    started_at   TEXT NOT NULL,
    finished_at  TEXT
);

CREATE TABLE IF NOT EXISTS ledger_entries (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id         INTEGER NOT NULL REFERENCES search_runs(id) ON DELETE CASCADE,
    source         TEXT NOT NULL,
    family         TEXT NOT NULL,
    query          TEXT NOT NULL,
    page           INTEGER NOT NULL DEFAULT 1,
    result_count   INTEGER NOT NULL DEFAULT 0,
    new_records    INTEGER NOT NULL DEFAULT 0,
    duration_ms    INTEGER NOT NULL DEFAULT 0,
    status         TEXT NOT NULL,
    exclusion_json TEXT NOT NULL DEFAULT '[]',
    detail         TEXT NOT NULL DEFAULT '',
    created_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS records (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id         INTEGER REFERENCES search_runs(id) ON DELETE CASCADE,
    project_id     INTEGER REFERENCES projects(id) ON DELETE CASCADE,
    kind           TEXT NOT NULL,
    source         TEXT NOT NULL,
    source_id      TEXT NOT NULL,
    doi            TEXT,
    title          TEXT NOT NULL,
    abstract       TEXT,
    authors_json   TEXT NOT NULL DEFAULT '[]',
    year           INTEGER,
    published_date TEXT,
    venue          TEXT,
    publisher      TEXT,
    url            TEXT,
    pdf_url        TEXT,
    license        TEXT,
    access         TEXT NOT NULL DEFAULT 'unknown',
    citation_count INTEGER,
    reference_dois TEXT NOT NULL DEFAULT '[]',
    concepts_json  TEXT NOT NULL DEFAULT '[]',
    modality       TEXT,
    dataset_json   TEXT NOT NULL DEFAULT '{}',
    raw_json       TEXT NOT NULL DEFAULT '{}',
    relevance      REAL NOT NULL DEFAULT 0,
    verification_status TEXT NOT NULL DEFAULT 'unverified',
    retrieved_at   TEXT NOT NULL,
    dedupe_key     TEXT NOT NULL DEFAULT '',
    duplicate_of   INTEGER REFERENCES records(id) ON DELETE SET NULL,
    created_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_records_run ON records(run_id);
CREATE INDEX IF NOT EXISTS idx_records_doi ON records(doi);
CREATE INDEX IF NOT EXISTS idx_records_dedupe ON records(dedupe_key);

CREATE TABLE IF NOT EXISTS evidence (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    record_id    INTEGER NOT NULL REFERENCES records(id) ON DELETE CASCADE,
    project_id   INTEGER REFERENCES projects(id) ON DELETE CASCADE,
    claim        TEXT NOT NULL,
    passage      TEXT NOT NULL,
    locator      TEXT NOT NULL DEFAULT '',
    field        TEXT NOT NULL DEFAULT '',
    url          TEXT NOT NULL DEFAULT '',
    retrieved_at TEXT NOT NULL,
    origin       TEXT NOT NULL DEFAULT 'source_metadata',
    verified     INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_evidence_record ON evidence(record_id);

CREATE TABLE IF NOT EXISTS verifications (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    record_id     INTEGER NOT NULL REFERENCES records(id) ON DELETE CASCADE,
    check_name    TEXT NOT NULL,
    status        TEXT NOT NULL,
    method        TEXT NOT NULL,
    deterministic INTEGER NOT NULL DEFAULT 1,
    detail        TEXT NOT NULL DEFAULT '',
    evidence_url  TEXT NOT NULL DEFAULT '',
    checked_at    TEXT NOT NULL,
    UNIQUE(record_id, check_name, method)
);

CREATE TABLE IF NOT EXISTS dataset_states (
    record_id  INTEGER PRIMARY KEY REFERENCES records(id) ON DELETE CASCADE,
    state      TEXT NOT NULL DEFAULT 'discovered',
    reason     TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS downloads (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    record_id    INTEGER NOT NULL REFERENCES records(id) ON DELETE CASCADE,
    project_id   INTEGER REFERENCES projects(id) ON DELETE CASCADE,
    url          TEXT NOT NULL,
    target_path  TEXT NOT NULL DEFAULT '',
    status       TEXT NOT NULL DEFAULT 'queued',
    bytes_total  INTEGER,
    bytes_written INTEGER NOT NULL DEFAULT 0,
    sha256       TEXT,
    content_type TEXT NOT NULL DEFAULT '',
    members_json TEXT NOT NULL DEFAULT '[]',
    error        TEXT NOT NULL DEFAULT '',
    attempts     INTEGER NOT NULL DEFAULT 0,
    started_at   TEXT,
    finished_at  TEXT,
    created_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_downloads_record ON downloads(record_id);

CREATE TABLE IF NOT EXISTS compatibility_reports (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id   INTEGER REFERENCES projects(id) ON DELETE CASCADE,
    record_ids   TEXT NOT NULL,
    report_json  TEXT NOT NULL,
    created_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS gap_candidates (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id  INTEGER REFERENCES projects(id) ON DELETE CASCADE,
    run_id      INTEGER REFERENCES search_runs(id) ON DELETE SET NULL,
    gap_json    TEXT NOT NULL,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS manuscripts (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id    INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    title         TEXT NOT NULL DEFAULT '',
    sections_json TEXT NOT NULL DEFAULT '[]',
    checklist_json TEXT NOT NULL DEFAULT '[]',
    updated_at    TEXT NOT NULL,
    created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS jobs (
    id           TEXT PRIMARY KEY,
    project_id   INTEGER REFERENCES projects(id) ON DELETE CASCADE,
    kind         TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'queued',
    progress     REAL NOT NULL DEFAULT 0,
    message      TEXT NOT NULL DEFAULT '',
    request_json TEXT NOT NULL DEFAULT '{}',
    result_json  TEXT NOT NULL DEFAULT '{}',
    error        TEXT NOT NULL DEFAULT '',
    cancel_requested INTEGER NOT NULL DEFAULT 0,
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS http_cache (
    cache_key  TEXT PRIMARY KEY,
    url        TEXT NOT NULL,
    status     INTEGER NOT NULL,
    body       BLOB NOT NULL,
    created_at TEXT NOT NULL
);
"""


def utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


class Database:
    """Thin SQLite wrapper: one connection per thread, WAL, row dicts."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self._write_lock = threading.RLock()

    # -- connection handling ------------------------------------------------
    def _conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(str(self.path), timeout=30, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA busy_timeout=30000")
            self._local.conn = conn
        return conn

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        conn = self._conn()
        with self._write_lock:
            try:
                yield conn
                conn.commit()
            except Exception:
                conn.rollback()
                raise

    def init(self) -> None:
        conn = self._conn()
        with self._write_lock:
            conn.executescript(SCHEMA_SQL)
            conn.execute(
                "INSERT INTO meta(key, value) VALUES('schema_version', ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (str(SCHEMA_VERSION),),
            )
            conn.commit()

    # -- query helpers ------------------------------------------------------
    def query(self, sql: str, params: Sequence[Any] = ()) -> list[dict]:
        rows = self._conn().execute(sql, params).fetchall()
        return [dict(row) for row in rows]

    def query_one(self, sql: str, params: Sequence[Any] = ()) -> dict | None:
        row = self._conn().execute(sql, params).fetchone()
        return dict(row) if row else None

    def execute(self, sql: str, params: Sequence[Any] = ()) -> int:
        with self.transaction() as conn:
            cursor = conn.execute(sql, params)
            return cursor.lastrowid

    def executemany(self, sql: str, rows: Iterable[Sequence[Any]]) -> None:
        with self.transaction() as conn:
            conn.executemany(sql, list(rows))

    def scalar(self, sql: str, params: Sequence[Any] = ()) -> Any:
        row = self._conn().execute(sql, params).fetchone()
        return row[0] if row else None

    # -- cache --------------------------------------------------------------
    def cache_get(self, key: str, ttl_seconds: int) -> tuple[int, bytes] | None:
        row = self.query_one(
            "SELECT status, body, created_at FROM http_cache WHERE cache_key = ?", (key,)
        )
        if not row:
            return None
        created = _parse_ts(row["created_at"])
        if ttl_seconds >= 0 and (time.time() - created) > ttl_seconds:
            return None
        body = row["body"]
        return row["status"], bytes(body) if body is not None else b""

    def cache_put(self, key: str, url: str, status: int, body: bytes) -> None:
        with self.transaction() as conn:
            conn.execute(
                "INSERT INTO http_cache(cache_key, url, status, body, created_at) "
                "VALUES(?,?,?,?,?) ON CONFLICT(cache_key) DO UPDATE SET "
                "status=excluded.status, body=excluded.body, created_at=excluded.created_at",
                (key, url, status, sqlite3.Binary(body), utc_now()),
            )

    def cache_clear(self, older_than_seconds: int | None = None) -> int:
        if older_than_seconds is None:
            return self.execute("DELETE FROM http_cache")
        cutoff = time.strftime(
            "%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - older_than_seconds)
        )
        with self.transaction() as conn:
            cur = conn.execute("DELETE FROM http_cache WHERE created_at < ?", (cutoff,))
            return cur.rowcount


def _parse_ts(value: str) -> float:
    try:
        return time.mktime(time.strptime(value, "%Y-%m-%dT%H:%M:%SZ")) - time.timezone
    except Exception:
        return 0.0


_db: Database | None = None
_db_lock = threading.Lock()


def get_db() -> Database:
    global _db
    if _db is None:
        with _db_lock:
            if _db is None:
                db = Database(get_settings().db_path)
                db.init()
                _db = db
    return _db


def set_db(db: Database) -> None:
    """Used by tests."""
    global _db
    _db = db


def json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def json_loads(value: Any, default: Any = None) -> Any:
    if value is None or value == "":
        return default if default is not None else None
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default if default is not None else None
