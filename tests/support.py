"""Shared fixtures for the offline test-suite.

Every test case runs against a throwaway SQLite database and download directory
inside a temporary folder, so the suite never touches real user data and can run
in parallel with a live server.
"""

from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

from app import http_client
from app.config import Settings, override_settings
from app.db import Database, set_db


class DatabaseTestCase(unittest.TestCase):
    """Base class that points the whole app at a temporary database."""

    def setUp(self) -> None:  # noqa: D102 - unittest hook
        self._tmp = tempfile.TemporaryDirectory(prefix="rf_test_", ignore_cleanup_errors=True)
        root = Path(self._tmp.name)
        data = root / "data"
        data.mkdir(parents=True, exist_ok=True)
        settings = Settings(
            project_root=root,
            host="127.0.0.1",
            port=0,
            open_browser=False,
            data_dir=data,
            db_path=data / "test.db",
            cache_dir=data / "cache",
            download_dir=data / "downloads",
            http_timeout=5.0,
            http_max_retries=2,
            http_backoff_base=0.0,
            cache_ttl_seconds=3600,
            default_search_depth=1,
            default_page_size=10,
            max_results_per_source=20,
        )
        settings.ensure_dirs()
        override_settings(settings)
        db = Database(settings.db_path)
        db.init()
        set_db(db)
        http_client.reset_http_clients()
        self.settings = settings
        self.db = db
        self.root = root

    def tearDown(self) -> None:  # noqa: D102 - unittest hook
        http_client.reset_http_clients()
        # Windows refuses to delete a file that still has an open handle, and
        # sqlite keeps the connection alive per thread, so close it explicitly.
        conn = getattr(self.db._local, "conn", None)
        if conn is not None:
            try:
                conn.close()
            finally:
                self.db._local.conn = None
        self._tmp.cleanup()

    # -- convenience -------------------------------------------------------
    def write_file(self, name: str, content: bytes | str) -> Path:
        path = self.settings.download_dir / name
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, str):
            path.write_text(content, encoding="utf-8")
        else:
            path.write_bytes(content)
        return path

    def project_with_run(self, question: str = "test question", spec: dict | None = None) -> tuple[int, int]:
        from app.services import store

        project_id = store.create_project("test project", question, spec or {})
        run_id = store.create_run(project_id, question, {"plan": "test"})
        return project_id, run_id

    def add_record(self, run_id: int, project_id: int, **fields: Any) -> int:
        from app.services import store

        relevance = float(fields.pop("relevance", 1.0))
        fields.pop("record_key", None)
        record = {
            "source": "openalex",
            "source_id": "W1",
            "kind": "paper",
            "title": "A test record",
            "url": "https://example.org/record",
        }
        record.update(fields)
        return store.save_record(
            run_id=run_id,
            project_id=project_id,
            record=record,
            relevance=relevance,
            dedupe_key=f"{record['source']}:{record['source_id']}",
        )


# ---------------------------------------------------------------------------
# minimal ASGI client (FastAPI's TestClient needs httpx, which is not a
# dependency of this project; this harness only uses the standard library)
# ---------------------------------------------------------------------------


class AsgiResponse:
    def __init__(self, status: int, headers: dict[str, str], body: bytes) -> None:
        self.status_code = status
        self.headers = headers
        self.content = body

    @property
    def text(self) -> str:
        return self.content.decode("utf-8", errors="replace")

    def json(self) -> Any:
        return json.loads(self.content.decode("utf-8"))


def _run_asgi(app, scope: dict, body: bytes) -> AsgiResponse:
    messages: list[dict] = []
    pending = [{"type": "http.request", "body": body, "more_body": False}]

    async def receive() -> dict:
        if pending:
            return pending.pop(0)
        # Starlette streams FileResponse bodies while concurrently awaiting a
        # disconnect on ``receive``. Returning ``http.disconnect`` immediately
        # cancels that stream, so block instead: the response task cancels this
        # waiter once the body has been fully sent.
        await asyncio.Event().wait()
        return {"type": "http.disconnect"}  # pragma: no cover - unreachable

    async def send(message: dict) -> None:
        messages.append(message)

    async def main() -> None:
        await app(scope, receive, send)

    asyncio.run(main())

    status = 500
    headers: dict[str, str] = {}
    chunks: list[bytes] = []
    for message in messages:
        if message["type"] == "http.response.start":
            status = int(message["status"])
            headers = {
                key.decode("latin-1").lower(): value.decode("latin-1")
                for key, value in message.get("headers", [])
            }
        elif message["type"] == "http.response.body":
            chunks.append(message.get("body", b""))
    return AsgiResponse(status, headers, b"".join(chunks))


def asgi_request(
    app,
    method: str,
    path: str,
    *,
    query: str = "",
    payload: Any = None,
    headers: dict[str, str] | None = None,
) -> AsgiResponse:
    body = b""
    request_headers = {"host": "testserver"}
    if payload is not None:
        body = json.dumps(payload).encode("utf-8")
        request_headers["content-type"] = "application/json"
        request_headers["content-length"] = str(len(body))
    if headers:
        request_headers.update({key.lower(): value for key, value in headers.items()})
    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": method.upper(),
        "scheme": "http",
        "path": path,
        "raw_path": path.encode("latin-1"),
        "query_string": query.encode("latin-1"),
        "root_path": "",
        "headers": [(key.encode("latin-1"), value.encode("latin-1")) for key, value in request_headers.items()],
        "client": ("127.0.0.1", 50000),
        "server": ("testserver", 80),
    }
    return _run_asgi(app, scope, body)
