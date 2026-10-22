"""Persistence helpers shared by the orchestrator and the API layer.

Every read returns plain dicts with JSON columns already decoded, so routers can
serialize them directly and the frontend always sees the same shape as the
orchestrator wrote.
"""

from __future__ import annotations

import uuid
from typing import Any, Iterable, Sequence

from ..db import get_db, json_dumps, json_loads, utc_now
from ..logging_utils import get_logger

log = get_logger("services.store")


# ---------------------------------------------------------------------------
# projects and runs
# ---------------------------------------------------------------------------


def create_project(title: str, question: str, spec: dict) -> int:
    now = utc_now()
    return get_db().execute(
        "INSERT INTO projects(title, question, spec_json, created_at, updated_at) VALUES(?,?,?,?,?)",
        (title or question[:120], question, json_dumps(spec), now, now),
    )


def update_project_spec(project_id: int, spec: dict) -> None:
    get_db().execute(
        "UPDATE projects SET spec_json = ?, updated_at = ? WHERE id = ?",
        (json_dumps(spec), utc_now(), project_id),
    )


def get_project(project_id: int) -> dict | None:
    row = get_db().query_one("SELECT * FROM projects WHERE id = ?", (project_id,))
    return _decode_project(row) if row else None


def list_projects(limit: int = 50) -> list[dict]:
    rows = get_db().query(
        "SELECT * FROM projects ORDER BY updated_at DESC LIMIT ?", (max(1, limit),)
    )
    return [_decode_project(row) for row in rows]


def delete_project(project_id: int) -> None:
    get_db().execute("DELETE FROM projects WHERE id = ?", (project_id,))


def _decode_project(row: dict) -> dict:
    row["spec"] = json_loads(row.pop("spec_json", "{}"), {})
    return row


def create_run(project_id: int, question: str, plan: dict) -> int:
    return get_db().execute(
        "INSERT INTO search_runs(project_id, question, plan_json, status, started_at) "
        "VALUES(?,?,?,?,?)",
        (project_id, question, json_dumps(plan), "running", utc_now()),
    )


def finish_run(run_id: int, status: str, scope: dict, failures: list[dict]) -> None:
    get_db().execute(
        "UPDATE search_runs SET status = ?, scope_json = ?, failure_json = ?, finished_at = ? "
        "WHERE id = ?",
        (status, json_dumps(scope), json_dumps(failures), utc_now(), run_id),
    )


def get_run(run_id: int) -> dict | None:
    row = get_db().query_one("SELECT * FROM search_runs WHERE id = ?", (run_id,))
    if not row:
        return None
    row["plan"] = json_loads(row.pop("plan_json", "{}"), {})
    row["scope"] = json_loads(row.pop("scope_json", "{}"), {})
    row["failures"] = json_loads(row.pop("failure_json", "[]"), [])
    return row


def list_runs(project_id: int | None = None, limit: int = 25) -> list[dict]:
    if project_id:
        rows = get_db().query(
            "SELECT * FROM search_runs WHERE project_id = ? ORDER BY id DESC LIMIT ?",
            (project_id, max(1, limit)),
        )
    else:
        rows = get_db().query(
            "SELECT * FROM search_runs ORDER BY id DESC LIMIT ?", (max(1, limit),)
        )
    out = []
    for row in rows:
        row["plan"] = json_loads(row.pop("plan_json", "{}"), {})
        row["scope"] = json_loads(row.pop("scope_json", "{}"), {})
        row["failures"] = json_loads(row.pop("failure_json", "[]"), [])
        out.append(row)
    return out


# ---------------------------------------------------------------------------
# search ledger
# ---------------------------------------------------------------------------


def add_ledger_entry(
    run_id: int,
    *,
    source: str,
    family: str,
    query: str,
    page: int,
    result_count: int,
    new_records: int,
    duration_ms: int,
    status: str,
    exclusions: list[dict] | None = None,
    detail: str = "",
) -> int:
    return get_db().execute(
        "INSERT INTO ledger_entries(run_id, source, family, query, page, result_count, "
        "new_records, duration_ms, status, exclusion_json, detail, created_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            run_id,
            source,
            family,
            query,
            page,
            result_count,
            new_records,
            duration_ms,
            status,
            json_dumps(exclusions or []),
            detail,
            utc_now(),
        ),
    )


def get_ledger(run_id: int) -> list[dict]:
    rows = get_db().query(
        "SELECT * FROM ledger_entries WHERE run_id = ? ORDER BY id ASC", (run_id,)
    )
    for row in rows:
        row["exclusions"] = json_loads(row.pop("exclusion_json", "[]"), [])
    return rows


def ledger_summary(run_id: int) -> dict:
    rows = get_db().query(
        "SELECT source, family, status, result_count, new_records, duration_ms, page, query, detail "
        "FROM ledger_entries WHERE run_id = ?",
        (run_id,),
    )
    by_source: dict[str, dict] = {}
    for row in rows:
        entry = by_source.setdefault(
            row["source"],
            {
                "source": row["source"],
                "families": set(),
                "queries": 0,
                "pages": 0,
                "results": 0,
                "new_records": 0,
                "errors": 0,
                "empty": 0,
                "duration_ms": 0,
                "statuses": {},
                "error_detail": "",
            },
        )
        entry["families"].add(row["family"])
        entry["queries"] += 1
        entry["pages"] += 1
        entry["results"] += row["result_count"] or 0
        entry["new_records"] += row["new_records"] or 0
        entry["duration_ms"] += row["duration_ms"] or 0
        entry["statuses"][row["status"]] = entry["statuses"].get(row["status"], 0) + 1
        if row["status"] == "error":
            entry["errors"] += 1
            entry["error_detail"] = row["detail"] or entry["error_detail"]
        if row["status"] == "empty":
            entry["empty"] += 1
    sources = []
    for entry in by_source.values():
        entry["families"] = sorted(entry["families"])
        sources.append(entry)
    sources.sort(key=lambda item: item["source"])
    return {
        "run_id": run_id,
        "entries": len(rows),
        "sources": sources,
        "total_results": sum(row["result_count"] or 0 for row in rows),
        "total_new_records": sum(row["new_records"] or 0 for row in rows),
        "failed_sources": [entry["source"] for entry in sources if entry["errors"]],
    }


# ---------------------------------------------------------------------------
# records
# ---------------------------------------------------------------------------


def save_record(
    *,
    run_id: int,
    project_id: int,
    record: dict,
    relevance: float,
    dedupe_key: str,
) -> int:
    return get_db().execute(
        "INSERT INTO records(run_id, project_id, kind, source, source_id, doi, title, abstract, "
        "authors_json, year, published_date, venue, publisher, url, pdf_url, license, access, "
        "citation_count, reference_dois, concepts_json, modality, dataset_json, raw_json, "
        "relevance, retrieved_at, dedupe_key, created_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            run_id,
            project_id,
            record.get("kind", "paper"),
            record.get("source", ""),
            record.get("source_id", ""),
            record.get("doi") or None,
            record.get("title", ""),
            record.get("abstract") or None,
            json_dumps(record.get("authors") or []),
            record.get("year"),
            record.get("published_date"),
            record.get("venue") or "",
            record.get("publisher") or "",
            record.get("url") or "",
            record.get("pdf_url") or "",
            record.get("license") or "",
            record.get("access") or "unknown",
            record.get("citation_count"),
            json_dumps(record.get("reference_dois") or []),
            json_dumps(record.get("concepts") or []),
            record.get("modality") or "",
            json_dumps(record.get("dataset") or {}),
            json_dumps(record.get("raw") or {}),
            float(relevance),
            record.get("retrieved_at") or utc_now(),
            dedupe_key,
            utc_now(),
        ),
    )


def mark_duplicate(record_id: int, canonical_id: int) -> None:
    get_db().execute("UPDATE records SET duplicate_of = ? WHERE id = ?", (canonical_id, record_id))


def set_relevance(record_id: int, relevance: float) -> None:
    get_db().execute("UPDATE records SET relevance = ? WHERE id = ?", (float(relevance), record_id))


_RECORD_SELECT = """
SELECT id, run_id, project_id, kind, source, source_id, doi, title, abstract, authors_json, year,
       published_date, venue, publisher, url, pdf_url, license, access, citation_count,
       reference_dois, concepts_json, modality, dataset_json, raw_json, relevance,
       verification_status, retrieved_at, dedupe_key, duplicate_of, created_at
FROM records
"""


def _decode_record(row: dict) -> dict:
    row["authors"] = json_loads(row.pop("authors_json", "[]"), [])
    row["reference_dois"] = json_loads(row.pop("reference_dois", "[]"), [])
    row["concepts"] = json_loads(row.pop("concepts_json", "[]"), [])
    row["dataset"] = json_loads(row.pop("dataset_json", "{}"), {})
    row["raw"] = json_loads(row.pop("raw_json", "{}"), {})
    row["record_key"] = f"{row.get('source')}:{row.get('source_id')}"
    row["is_duplicate"] = row.get("duplicate_of") is not None
    return row


def list_records(
    *,
    run_id: int | None = None,
    project_id: int | None = None,
    kind: str | None = None,
    include_duplicates: bool = False,
    limit: int = 500,
    offset: int = 0,
    order_by: str = "relevance",
    ids: Sequence[int] | None = None,
) -> list[dict]:
    clauses = []
    params: list[Any] = []
    if run_id is not None:
        clauses.append("run_id = ?")
        params.append(run_id)
    if project_id is not None:
        clauses.append("project_id = ?")
        params.append(project_id)
    if kind:
        clauses.append("kind = ?")
        params.append(kind)
    if not include_duplicates:
        clauses.append("duplicate_of IS NULL")
    if ids:
        clauses.append(f"id IN ({','.join('?' for _ in ids)})")
        params.extend(ids)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    order = "relevance DESC, citation_count DESC, year DESC" if order_by == "relevance" else "id ASC"
    sql = f"{_RECORD_SELECT} {where} ORDER BY {order} LIMIT ? OFFSET ?"
    params.extend([max(1, limit), max(0, offset)])
    return [_decode_record(row) for row in get_db().query(sql, params)]


def get_record(record_id: int) -> dict | None:
    row = get_db().query_one(f"{_RECORD_SELECT} WHERE id = ?", (record_id,))
    return _decode_record(row) if row else None


def records_by_ids(record_ids: Iterable[int]) -> dict[int, dict]:
    ids = [int(rid) for rid in record_ids]
    if not ids:
        return {}
    rows = get_db().query(
        f"{_RECORD_SELECT} WHERE id IN ({','.join('?' for _ in ids)})", ids
    )
    return {row["id"]: _decode_record(row) for row in rows}


def record_kind_counts(run_id: int) -> dict:
    rows = get_db().query(
        "SELECT kind, COUNT(*) AS total, SUM(CASE WHEN duplicate_of IS NULL THEN 1 ELSE 0 END) "
        "AS unique_records FROM records WHERE run_id = ? GROUP BY kind",
        (run_id,),
    )
    return {
        row["kind"]: {"total": row["total"], "unique": row["unique_records"] or 0} for row in rows
    }


# ---------------------------------------------------------------------------
# verification, evidence, dataset state
# ---------------------------------------------------------------------------


def save_verification(record_id: int, check: dict, *, overall: str) -> None:
    get_db().execute(
        "INSERT INTO verifications(record_id, check_name, status, method, deterministic, detail, "
        "evidence_url, checked_at) VALUES(?,?,?,?,?,?,?,?) "
        "ON CONFLICT(record_id, check_name, method) DO UPDATE SET status=excluded.status, "
        "detail=excluded.detail, evidence_url=excluded.evidence_url, checked_at=excluded.checked_at",
        (
            record_id,
            check.get("check_name", ""),
            check.get("status", ""),
            check.get("method", ""),
            1 if check.get("deterministic", True) else 0,
            check.get("detail", ""),
            check.get("evidence_url", ""),
            utc_now(),
        ),
    )
    get_db().execute(
        "UPDATE records SET verification_status = ? WHERE id = ?", (overall, record_id)
    )


def get_verifications(record_id: int) -> list[dict]:
    rows = get_db().query(
        "SELECT check_name, status, method, deterministic, detail, evidence_url, checked_at "
        "FROM verifications WHERE record_id = ? ORDER BY id ASC",
        (record_id,),
    )
    for row in rows:
        row["deterministic"] = bool(row["deterministic"])
    return rows


def verifications_for_records(record_ids: Sequence[int]) -> dict[int, list[dict]]:
    if not record_ids:
        return {}
    rows = get_db().query(
        "SELECT record_id, check_name, status, method, deterministic, detail, evidence_url, checked_at "
        f"FROM verifications WHERE record_id IN ({','.join('?' for _ in record_ids)}) ORDER BY id ASC",
        list(record_ids),
    )
    out: dict[int, list[dict]] = {}
    for row in rows:
        row["deterministic"] = bool(row["deterministic"])
        out.setdefault(row.pop("record_id"), []).append(row)
    return out


def save_evidence(record_id: int, project_id: int, item: dict) -> int:
    return get_db().execute(
        "INSERT INTO evidence(record_id, project_id, claim, passage, locator, field, url, "
        "retrieved_at, origin, verified) VALUES(?,?,?,?,?,?,?,?,?,?)",
        (
            record_id,
            project_id,
            item.get("claim", ""),
            item.get("passage", ""),
            item.get("locator", ""),
            item.get("field_name") or item.get("field", ""),
            item.get("url", ""),
            item.get("retrieved_at") or utc_now(),
            item.get("origin", "source_metadata"),
            1 if item.get("verified") else 0,
        ),
    )


def get_evidence(project_id: int | None = None, record_ids: Sequence[int] | None = None) -> list[dict]:
    clauses = []
    params: list[Any] = []
    if project_id is not None:
        clauses.append("project_id = ?")
        params.append(project_id)
    if record_ids:
        clauses.append(f"record_id IN ({','.join('?' for _ in record_ids)})")
        params.extend(record_ids)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    rows = get_db().query(
        f"SELECT * FROM evidence {where} ORDER BY id ASC LIMIT 5000", params
    )
    for row in rows:
        row["verified"] = bool(row["verified"])
    return rows


def evidence_counts(project_id: int) -> dict[int, int]:
    rows = get_db().query(
        "SELECT record_id, COUNT(*) AS total FROM evidence WHERE project_id = ? GROUP BY record_id",
        (project_id,),
    )
    return {row["record_id"]: row["total"] for row in rows}


def set_dataset_state(record_id: int, state: str, reason: str = "") -> None:
    get_db().execute(
        "INSERT INTO dataset_states(record_id, state, reason, updated_at) VALUES(?,?,?,?) "
        "ON CONFLICT(record_id) DO UPDATE SET state=excluded.state, reason=excluded.reason, "
        "updated_at=excluded.updated_at",
        (record_id, state, reason, utc_now()),
    )


def get_dataset_states(record_ids: Sequence[int]) -> dict[int, dict]:
    if not record_ids:
        return {}
    rows = get_db().query(
        f"SELECT * FROM dataset_states WHERE record_id IN ({','.join('?' for _ in record_ids)})",
        list(record_ids),
    )
    return {row["record_id"]: row for row in rows}


def get_dataset_state(record_id: int) -> dict | None:
    return get_db().query_one("SELECT * FROM dataset_states WHERE record_id = ?", (record_id,))


# ---------------------------------------------------------------------------
# reports, gaps, manuscripts
# ---------------------------------------------------------------------------


def save_compatibility_report(project_id: int, record_ids: list[int], report: dict) -> int:
    return get_db().execute(
        "INSERT INTO compatibility_reports(project_id, record_ids, report_json, created_at) "
        "VALUES(?,?,?,?)",
        (project_id, json_dumps(record_ids), json_dumps(report), utc_now()),
    )


def get_compatibility_report(project_id: int, report_id: int) -> dict | None:
    row = get_db().query_one(
        "SELECT * FROM compatibility_reports WHERE id = ? AND project_id = ?", (report_id, project_id)
    )
    if not row:
        return None
    row["record_ids"] = json_loads(row.pop("record_ids", "[]"), [])
    row["report"] = json_loads(row.pop("report_json", "{}"), {})
    return row


def save_gap_candidates(project_id: int, run_id: int | None, gaps: list[dict]) -> None:
    now = utc_now()
    for gap in gaps:
        get_db().execute(
            "INSERT INTO gap_candidates(project_id, run_id, gap_json, created_at) VALUES(?,?,?,?)",
            (project_id, run_id, json_dumps(gap), now),
        )


def list_gap_candidates(project_id: int) -> list[dict]:
    rows = get_db().query(
        "SELECT id, gap_json, run_id, created_at FROM gap_candidates WHERE project_id = ? ORDER BY id ASC",
        (project_id,),
    )
    out = []
    for row in rows:
        gap = json_loads(row.pop("gap_json", "{}"), {})
        gap["id"] = row["id"]
        gap["run_id"] = row["run_id"]
        gap["created_at"] = row["created_at"]
        out.append(gap)
    return out


def upsert_manuscript(project_id: int, title: str, sections: list[dict], checklist: list[dict]) -> int:
    existing = get_db().query_one("SELECT id FROM manuscripts WHERE project_id = ?", (project_id,))
    now = utc_now()
    if existing:
        get_db().execute(
            "UPDATE manuscripts SET title = ?, sections_json = ?, checklist_json = ?, updated_at = ? "
            "WHERE id = ?",
            (title, json_dumps(sections), json_dumps(checklist), now, existing["id"]),
        )
        return existing["id"]
    return get_db().execute(
        "INSERT INTO manuscripts(project_id, title, sections_json, checklist_json, updated_at, created_at) "
        "VALUES(?,?,?,?,?,?)",
        (project_id, title, json_dumps(sections), json_dumps(checklist), now, now),
    )


def get_manuscript(project_id: int) -> dict | None:
    row = get_db().query_one("SELECT * FROM manuscripts WHERE project_id = ?", (project_id,))
    if not row:
        return None
    row["sections"] = json_loads(row.pop("sections_json", "[]"), [])
    row["checklist"] = json_loads(row.pop("checklist_json", "[]"), [])
    return row


# ---------------------------------------------------------------------------
# jobs
# ---------------------------------------------------------------------------


def create_job(kind: str, request: dict, project_id: int | None = None) -> str:
    job_id = uuid.uuid4().hex[:16]
    now = utc_now()
    get_db().execute(
        "INSERT INTO jobs(id, project_id, kind, status, progress, message, request_json, "
        "created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
        (job_id, project_id, kind, "queued", 0.0, "queued", json_dumps(request), now, now),
    )
    return job_id


def update_job(
    job_id: str,
    *,
    status: str | None = None,
    progress: float | None = None,
    message: str | None = None,
    result: dict | None = None,
    error: str | None = None,
    project_id: int | None = None,
) -> None:
    sets = ["updated_at = ?"]
    params: list[Any] = [utc_now()]
    if status is not None:
        sets.append("status = ?")
        params.append(status)
    if progress is not None:
        sets.append("progress = ?")
        params.append(float(progress))
    if message is not None:
        sets.append("message = ?")
        params.append(message)
    if result is not None:
        sets.append("result_json = ?")
        params.append(json_dumps(result))
    if error is not None:
        sets.append("error = ?")
        params.append(error)
    if project_id is not None:
        sets.append("project_id = ?")
        params.append(project_id)
    params.append(job_id)
    get_db().execute(f"UPDATE jobs SET {', '.join(sets)} WHERE id = ?", params)


def request_cancel(job_id: str) -> None:
    get_db().execute("UPDATE jobs SET cancel_requested = 1 WHERE id = ?", (job_id,))


def is_cancelled(job_id: str) -> bool:
    value = get_db().query_one("SELECT cancel_requested FROM jobs WHERE id = ?", (job_id,))
    return bool(value and value["cancel_requested"])


def get_job(job_id: str) -> dict | None:
    row = get_db().query_one("SELECT * FROM jobs WHERE id = ?", (job_id,))
    if not row:
        return None
    row["request"] = json_loads(row.pop("request_json", "{}"), {})
    row["result"] = json_loads(row.pop("result_json", "{}"), {})
    row["cancel_requested"] = bool(row["cancel_requested"])
    return row


def list_jobs(limit: int = 30, active_only: bool = False) -> list[dict]:
    sql = "SELECT * FROM jobs"
    if active_only:
        sql += " WHERE status IN ('queued','running','cancelling')"
    sql += " ORDER BY rowid DESC LIMIT ?"
    rows = get_db().query(sql, (max(1, limit),))
    out = []
    for row in rows:
        row["request"] = json_loads(row.pop("request_json", "{}"), {})
        row["result"] = json_loads(row.pop("result_json", "{}"), {})
        row["cancel_requested"] = bool(row["cancel_requested"])
        out.append(row)
    return out


def requeue_stuck_jobs() -> int:
    """Mark jobs left running by a previous process as interrupted."""
    with get_db().transaction() as conn:
        cursor = conn.execute(
            "UPDATE jobs SET status='interrupted', message='process restarted while job was running', "
            "updated_at=? WHERE status IN ('queued','running','cancelling')",
            (utc_now(),),
        )
        return cursor.rowcount


# ---------------------------------------------------------------------------
# downloads
# ---------------------------------------------------------------------------


def create_download(record_id: int, project_id: int | None, url: str) -> int:
    return get_db().execute(
        "INSERT INTO downloads(record_id, project_id, url, status, created_at) VALUES(?,?,?,?,?)",
        (record_id, project_id, url, "queued", utc_now()),
    )


def update_download(download_id: int, **fields: Any) -> None:
    allowed = {
        "target_path", "status", "bytes_total", "bytes_written", "sha256", "content_type",
        "error", "attempts", "started_at", "finished_at",
    }
    sets = []
    params: list[Any] = []
    for key, value in fields.items():
        if key not in allowed:
            continue
        sets.append(f"{key} = ?")
        params.append(json_dumps(value) if isinstance(value, (list, dict)) else value)
    if not sets:
        return
    params.append(download_id)
    get_db().execute(f"UPDATE downloads SET {', '.join(sets)} WHERE id = ?", params)


def save_download_members(download_id: int, members: list[dict]) -> None:
    get_db().execute(
        "UPDATE downloads SET members_json = ? WHERE id = ?", (json_dumps(members), download_id)
    )


def get_download(download_id: int) -> dict | None:
    row = get_db().query_one("SELECT * FROM downloads WHERE id = ?", (download_id,))
    if not row:
        return None
    row["members"] = json_loads(row.pop("members_json", "[]"), [])
    return row


def list_downloads(project_id: int | None = None, record_id: int | None = None) -> list[dict]:
    clauses = []
    params: list[Any] = []
    if project_id is not None:
        clauses.append("project_id = ?")
        params.append(project_id)
    if record_id is not None:
        clauses.append("record_id = ?")
        params.append(record_id)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    rows = get_db().query(f"SELECT * FROM downloads {where} ORDER BY id ASC", params)
    for row in rows:
        row["members"] = json_loads(row.pop("members_json", "[]"), [])
    return rows


# ---------------------------------------------------------------------------
# aggregate views
# ---------------------------------------------------------------------------


def project_snapshot(project_id: int) -> dict | None:
    """Everything the workspace needs for one project, in one payload."""
    project = get_project(project_id)
    if not project:
        return None
    runs = list_runs(project_id=project_id, limit=10)
    latest_run = runs[0] if runs else None
    run_id = latest_run["id"] if latest_run else None

    records = list_records(project_id=project_id, limit=800) if run_id else []
    duplicates = (
        list_records(project_id=project_id, include_duplicates=True, limit=2000)
        if run_id
        else []
    )
    evidence_counts_map = evidence_counts(project_id)
    record_ids = [record["id"] for record in records]
    verifications = verifications_for_records(record_ids)
    dataset_states = get_dataset_states(record_ids)
    for record in records:
        record["evidence_count"] = evidence_counts_map.get(record["id"], 0)
        record["checks"] = verifications.get(record["id"], [])
        state = dataset_states.get(record["id"])
        record["dataset_state"] = state["state"] if state else (
            "discovered" if record["kind"] == "dataset" else None
        )
        record["dataset_state_reason"] = state["reason"] if state else ""

    return {
        "project": project,
        "latest_run": latest_run,
        "runs": runs,
        "ledger": ledger_summary(run_id) if run_id else None,
        "records": records,
        "duplicate_records": [r for r in duplicates if r["is_duplicate"]][:200],
        "downloads": list_downloads(project_id=project_id),
        "gaps": list_gap_candidates(project_id),
        "manuscript": get_manuscript(project_id),
        "counts": {
            "records": len(records),
            "papers": len([r for r in records if r["kind"] == "paper"]),
            "datasets": len([r for r in records if r["kind"] == "dataset"]),
            "duplicates": len([r for r in duplicates if r["is_duplicate"]]),
            "evidence": sum(evidence_counts_map.values()),
        },
    }
