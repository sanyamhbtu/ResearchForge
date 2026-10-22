"""Typed API contracts and the HTTP surface of ResearchForge.

Every endpoint returns data that was actually produced by the pipeline. Nothing
here invents counts, citations or findings; when a request cannot be answered
from evidence the response says so explicitly (see
``/api/meta`` for the exact insufficient-evidence message).
"""

from __future__ import annotations

import json
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Query, Request, Response
from pydantic import BaseModel, Field, field_validator

from ..config import get_settings
from ..db import get_db, utc_now
from ..logging_utils import get_logger, recent_logs
from ..sources import get_registry
from ..services import store
from ..services.compatibility import STATE_DESCRIPTIONS, STATES
from ..services.downloads import (
    build_manifest,
    can_download,
    download_stats,
    estimate_download_size,
    purge_partial_files,
    write_manifest,
)
from ..services.evidence import INSUFFICIENT_DATASET_MESSAGE
from ..services.gaps import build_evidence_matrix
from ..services.jobs import get_job_runner, run_synchronously
from ..services.manuscript import export_manuscript, manuscript_path
from ..services.parser import parse_request
from ..services.planning import build_plan

log = get_logger("api")

router = APIRouter(prefix="/api")


# ---------------------------------------------------------------------------
# models
# ---------------------------------------------------------------------------

Intent = Literal["literature", "datasets", "both"]


class ResearchRequest(BaseModel):
    question: str = Field(..., min_length=3, max_length=4000)
    project_id: int | None = None
    title: str = Field("", max_length=300)
    depth: int = Field(0, ge=0, le=6, description="0 uses the configured default")
    page_size: int = Field(0, ge=0, le=200, description="0 uses the configured default")
    adapters: list[str] | None = None
    intent: Intent | None = None
    verify_limit: int = Field(20, ge=0, le=200)
    max_results_per_source: int = Field(120, ge=1, le=1000)
    max_queries_per_adapter: int = Field(4, ge=1, le=12)
    follow_citations: bool = True
    fetch_fulltext: bool = True
    use_llm: bool = True

    @field_validator("question")
    @classmethod
    def _strip(cls, value: str) -> str:
        cleaned = value.strip()
        if len(cleaned) < 3:
            raise ValueError("question must contain at least 3 non-whitespace characters")
        return cleaned


class PlanRequest(BaseModel):
    question: str = Field(..., min_length=3, max_length=4000)
    depth: int = Field(0, ge=0, le=6)
    page_size: int = Field(0, ge=0, le=200)
    adapters: list[str] | None = None
    intent: Intent | None = None
    use_llm: bool = True


class CompatibilityRequest(BaseModel):
    record_ids: list[int] = Field(..., min_length=1)
    intended_use: str = Field("", max_length=500)


class GapsRequest(BaseModel):
    run_id: int | None = None


class ManuscriptRequest(BaseModel):
    run_id: int | None = None


class DownloadRequest(BaseModel):
    record_id: int
    max_bytes: int | None = Field(None, ge=1_000_000, le=8 * 1024 * 1024 * 1024)


# ---------------------------------------------------------------------------
# health, meta, adapters
# ---------------------------------------------------------------------------


@router.get("/health")
def health() -> dict:
    settings = get_settings()
    db = get_db()
    registry = get_registry()
    schema_version = db.scalar("SELECT value FROM meta WHERE key = 'schema_version'")
    return {
        "status": "ok",
        "version": "1.0.0",
        "utc_now": utc_now(),
        "schema_version": schema_version,
        "configuration": settings.describe(),
        "database": {"path": str(settings.db_path)},
        "adapters": {
            "available": [adapter.name for adapter in registry.all()],
            "unavailable": registry.unavailable(),
        },
        "active_jobs": get_job_runner().active(),
        "counts": {
            "projects": db.scalar("SELECT COUNT(*) FROM projects") or 0,
            "runs": db.scalar("SELECT COUNT(*) FROM search_runs") or 0,
            "records": db.scalar("SELECT COUNT(*) FROM records") or 0,
            "evidence": db.scalar("SELECT COUNT(*) FROM evidence") or 0,
        },
    }


@router.get("/meta")
def meta() -> dict:
    """Vocabulary the UI needs so labels always match the backend's real states."""
    return {
        "insufficient_dataset_message": INSUFFICIENT_DATASET_MESSAGE,
        "dataset_states": [{"state": state, "description": STATE_DESCRIPTIONS.get(state, "")} for state in STATES],
        "dataset_reachability_states": [
            "discovered",
            "metadata_verified",
            "download_verified",
            "contents_inspected",
            "compatibility_assessed",
            "approved",
        ],
        "dataset_state_meanings": {
            "discovered": "The record exists in a source; nothing about it has been verified yet.",
            "metadata_verified": "The identifier, title and links were checked against the registrar or the source.",
            "download_verified": "At least one file was downloaded and its size and SHA-256 were recorded.",
            "contents_inspected": "The downloaded content was parsed (schema, dimensions, integrity).",
            "compatibility_assessed": "Compatibility with a concrete selection or intended use was assessed.",
            "approved": "A researcher explicitly approved the dataset after reviewing the reports.",
        },
        "verification_statuses": [
            "verified",
            "partially_verified",
            "conflict",
            "inconclusive",
            "inaccessible",
            "skipped",
            "failed",
            "unverified",
        ],
        "sufficiency_statuses": [
            "sufficient",
            "unverified_candidates",
            "insufficient_evidence",
            "search_incomplete",
        ],
        "manuscript_section_statuses": {
            "evidence_supported": "Supported by retrieved evidence",
            "needs_user_input": "Requires researcher input",
            "needs_research": "Requires additional research",
            "cannot_complete_yet": "Cannot be completed yet",
        },
        "policy": (
            "ResearchForge never fabricates data, sources, citations, dataset counts, findings, "
            "experiments or conclusions. Metadata copied from a single source is not treated as "
            "independent confirmation, and a failed source is not treated as evidence of absence."
        ),
    }


@router.get("/adapters")
def adapters() -> dict:
    registry = get_registry()
    return {
        "adapters": registry.describe(),
        "unavailable": registry.unavailable(),
        "note": (
            "Only the adapters listed as available are contacted. Each adapter has its own "
            "pagination, retry and rate-limit handling."
        ),
    }


@router.get("/logs")
def logs(limit: int = Query(200, ge=1, le=600), min_level: str = "INFO") -> dict:
    return {"logs": recent_logs(limit=limit, min_level=min_level)}


# ---------------------------------------------------------------------------
# research
# ---------------------------------------------------------------------------


@router.post("/research/plan")
def research_plan(payload: PlanRequest) -> dict:
    """Parse and plan without contacting any source.

    Lets the researcher confirm the interpretation and the clarification
    question before a live, rate-limited search is started.
    """
    settings = get_settings()
    spec = parse_request(payload.question, use_llm=payload.use_llm)
    if payload.intent:
        spec.intent = payload.intent
    plan = build_plan(
        spec,
        registry=get_registry(),
        depth=payload.depth or settings.default_search_depth,
        page_size=payload.page_size or settings.default_page_size,
        adapter_names=payload.adapters,
        max_results_per_source=settings.max_results_per_source,
    )
    return {
        "spec": spec.as_dict(),
        "plan": plan.as_dict(),
        "assumptions_stated": bool(spec.assumptions),
        "note": (
            "No source has been contacted yet. This is the interpretation and the query plan that "
            "will be executed."
        ),
    }


@router.post("/research", status_code=202)
def start_research(payload: ResearchRequest) -> dict:
    job_id = get_job_runner().submit(
        "research", payload.model_dump(), project_id=payload.project_id
    )
    return {"job_id": job_id, "status": "queued", "kind": "research"}


@router.post("/research/sync")
def research_sync(payload: ResearchRequest) -> dict:
    """Run a search inline. Used by the automated tests and for small runs."""
    result = run_synchronously("research", payload.model_dump(), project_id=payload.project_id)
    return result


# ---------------------------------------------------------------------------
# jobs
# ---------------------------------------------------------------------------


@router.get("/jobs")
def list_jobs(limit: int = Query(30, ge=1, le=200), active_only: bool = False) -> dict:
    return {
        "jobs": store.list_jobs(limit=limit, active_only=active_only),
        "active": get_job_runner().active(),
    }


@router.get("/jobs/{job_id}")
def get_job(job_id: str) -> dict:
    job = store.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail=f"job '{job_id}' does not exist")
    return job


@router.post("/jobs/{job_id}/cancel")
def cancel_job(job_id: str) -> dict:
    if not get_job_runner().cancel(job_id):
        job = store.get_job(job_id)
        if not job:
            raise HTTPException(status_code=404, detail=f"job '{job_id}' does not exist")
        return {"job_id": job_id, "cancelled": False, "status": job["status"], "reason": "job already finished"}
    return {"job_id": job_id, "cancelled": True, "status": "cancelling"}


# ---------------------------------------------------------------------------
# projects
# ---------------------------------------------------------------------------


@router.get("/projects")
def list_projects(limit: int = Query(50, ge=1, le=200)) -> dict:
    projects = store.list_projects(limit=limit)
    for project in projects:
        runs = store.list_runs(project_id=project["id"], limit=1)
        project["latest_run"] = runs[0] if runs else None
    return {"projects": projects}


@router.get("/projects/{project_id}")
def get_project(project_id: int) -> dict:
    project = store.get_project(project_id)
    if not project:
        raise HTTPException(status_code=404, detail=f"project {project_id} does not exist")
    project["runs"] = store.list_runs(project_id=project_id, limit=25)
    return project


@router.delete("/projects/{project_id}")
def delete_project(project_id: int) -> dict:
    if not store.get_project(project_id):
        raise HTTPException(status_code=404, detail=f"project {project_id} does not exist")
    store.delete_project(project_id)
    return {"deleted": True, "project_id": project_id}


@router.get("/projects/{project_id}/snapshot")
def project_snapshot(project_id: int) -> dict:
    snapshot = store.project_snapshot(project_id)
    if not snapshot:
        raise HTTPException(status_code=404, detail=f"project {project_id} does not exist")
    return snapshot


@router.get("/projects/{project_id}/ledger")
def project_ledger(project_id: int, run_id: int | None = None) -> dict:
    if run_id is None:
        runs = store.list_runs(project_id=project_id, limit=1)
        if not runs:
            raise HTTPException(status_code=404, detail="this project has no search run yet")
        run_id = runs[0]["id"]
    run = store.get_run(run_id)
    if not run or run["project_id"] != project_id:
        raise HTTPException(status_code=404, detail=f"run {run_id} does not belong to project {project_id}")
    return {
        "run": run,
        "summary": store.ledger_summary(run_id),
        "entries": store.get_ledger(run_id),
    }


@router.get("/projects/{project_id}/records")
def project_records(
    project_id: int,
    kind: str | None = None,
    q: str | None = None,
    modality: str | None = None,
    access: str | None = None,
    license_contains: str | None = None,
    min_year: int | None = None,
    max_year: int | None = None,
    min_relevance: float = 0.0,
    verification: str | None = None,
    dataset_state: str | None = None,
    include_duplicates: bool = False,
    order: Literal["relevance", "id"] = "relevance",
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
) -> dict:
    """Record list with the filters the workspace exposes.

    Filtering happens over the records actually retrieved for this project; no
    counts are estimated or extrapolated.
    """
    if not store.get_project(project_id):
        raise HTTPException(status_code=404, detail=f"project {project_id} does not exist")

    records = store.list_records(
        project_id=project_id,
        include_duplicates=include_duplicates,
        limit=2000,
        order_by=order,
    )
    evidence_map = store.evidence_counts(project_id)
    verification_map = store.verifications_for_records([record["id"] for record in records])
    state_map = store.get_dataset_states([record["id"] for record in records])
    for record in records:
        record["evidence_count"] = evidence_map.get(record["id"], 0)
        record["checks"] = verification_map.get(record["id"], [])
        state = state_map.get(record["id"])
        record["dataset_state"] = state["state"] if state else (
            "discovered" if record["kind"] == "dataset" else None
        )
        record["downloadable"], record["download_note"] = (
            can_download(record) if record["kind"] == "dataset" else (False, "not a dataset")
        )
        if record["kind"] == "dataset":
            record["declared_size_bytes"] = estimate_download_size(record)

    filtered = _apply_record_filters(
        records,
        kind=kind,
        q=q,
        modality=modality,
        access=access,
        license_contains=license_contains,
        min_year=min_year,
        max_year=max_year,
        min_relevance=min_relevance,
        verification=verification,
        dataset_state=dataset_state,
    )
    page = filtered[offset : offset + limit]
    return {
        "total_matching": len(filtered),
        "returned": len(page),
        "offset": offset,
        "limit": limit,
        "filters": {
            "kind": kind,
            "q": q,
            "modality": modality,
            "access": access,
            "license_contains": license_contains,
            "min_year": min_year,
            "max_year": max_year,
            "min_relevance": min_relevance,
            "verification": verification,
            "dataset_state": dataset_state,
        },
        "records": page,
    }


def _apply_record_filters(records: list[dict], **filters: Any) -> list[dict]:
    text = (filters.get("q") or "").strip().lower()
    out = []
    for record in records:
        if filters.get("kind") and record["kind"] != filters["kind"]:
            continue
        if text:
            haystack = " ".join(
                [record.get("title") or "", record.get("abstract") or "", record.get("venue") or ""]
            ).lower()
            if text not in haystack:
                continue
        modality = filters.get("modality")
        if modality:
            value = (record.get("modality") or (record.get("dataset") or {}).get("modality") or "").lower()
            if value != modality.lower():
                continue
        if filters.get("access") and (record.get("access") or "") != filters["access"]:
            continue
        if filters.get("license_contains"):
            if filters["license_contains"].lower() not in (record.get("license") or "").lower():
                continue
        year = record.get("year")
        if filters.get("min_year") is not None and (year or 0) < filters["min_year"]:
            continue
        if filters.get("max_year") is not None and (year or 9999) > filters["max_year"]:
            continue
        if filters.get("min_relevance") and (record.get("relevance") or 0.0) < filters["min_relevance"]:
            continue
        if filters.get("verification") and (record.get("verification_status") or "unverified") != filters["verification"]:
            continue
        if filters.get("dataset_state") and (record.get("dataset_state") or "") != filters["dataset_state"]:
            continue
        out.append(record)
    return out


@router.get("/records/{record_id}")
def get_record(record_id: int) -> dict:
    record = store.get_record(record_id)
    if not record:
        raise HTTPException(status_code=404, detail=f"record {record_id} does not exist")
    state = store.get_dataset_state(record_id)
    return {
        "record": record,
        "verifications": store.get_verifications(record_id),
        "evidence": store.get_evidence(record_ids=[record_id]),
        "dataset_state": state,
        "downloads": store.list_downloads(record_id=record_id),
        "downloadable": can_download(record) if record["kind"] == "dataset" else (False, "not a dataset"),
    }


@router.get("/records/{record_id}/evidence")
def get_record_evidence(record_id: int) -> dict:
    if not store.get_record(record_id):
        raise HTTPException(status_code=404, detail=f"record {record_id} does not exist")
    items = store.get_evidence(record_ids=[record_id])
    return {
        "record_id": record_id,
        "evidence": items,
        "note": (
            "Every passage records where it came from. 'source_metadata' means the text is the "
            "source's own metadata, not an independent confirmation."
        ),
    }


@router.get("/records/{record_id}/verifications")
def get_record_verifications(record_id: int) -> dict:
    if not store.get_record(record_id):
        raise HTTPException(status_code=404, detail=f"record {record_id} does not exist")
    return {"record_id": record_id, "verifications": store.get_verifications(record_id)}


# ---------------------------------------------------------------------------
# evidence matrix and gaps
# ---------------------------------------------------------------------------


@router.get("/projects/{project_id}/evidence-matrix")
def evidence_matrix(project_id: int) -> dict:
    if not store.get_project(project_id):
        raise HTTPException(status_code=404, detail=f"project {project_id} does not exist")
    records = store.list_records(project_id=project_id, limit=1000)
    matrix = build_evidence_matrix(records)
    return {
        "project_id": project_id,
        "entries": matrix,
        "note": (
            "Fields are extracted verbatim from retrieved text and carry their locator. Fields that "
            "were not found are reported as 'not reported in the retrieved text' rather than inferred."
        ),
    }


@router.get("/projects/{project_id}/gaps")
def list_gaps(project_id: int) -> dict:
    if not store.get_project(project_id):
        raise HTTPException(status_code=404, detail=f"project {project_id} does not exist")
    return {"project_id": project_id, "gaps": store.list_gap_candidates(project_id)}


@router.post("/projects/{project_id}/gaps", status_code=202)
def compute_gaps(project_id: int, payload: GapsRequest) -> dict:
    if not store.get_project(project_id):
        raise HTTPException(status_code=404, detail=f"project {project_id} does not exist")
    job_id = get_job_runner().submit(
        "gaps", {"project_id": project_id, "run_id": payload.run_id}, project_id=project_id
    )
    return {"job_id": job_id, "kind": "gaps", "status": "queued"}


# ---------------------------------------------------------------------------
# downloads and compatibility
# ---------------------------------------------------------------------------


@router.post("/datasets/{record_id}/download", status_code=202)
def download_dataset(record_id: int, payload: DownloadRequest) -> dict:
    record = store.get_record(record_id)
    if not record:
        raise HTTPException(status_code=404, detail=f"record {record_id} does not exist")
    allowed, reason = can_download(record)
    if not allowed:
        raise HTTPException(status_code=409, detail=reason)
    job_id = get_job_runner().submit(
        "download",
        {
            "record_id": record_id,
            "project_id": record.get("project_id"),
            "max_bytes": payload.max_bytes,
        },
        project_id=record.get("project_id"),
    )
    return {"job_id": job_id, "kind": "download", "status": "queued"}


@router.post("/datasets/{record_id}/download/sync")
def download_dataset_sync(record_id: int) -> dict:
    record = store.get_record(record_id)
    if not record:
        raise HTTPException(status_code=404, detail=f"record {record_id} does not exist")
    return run_synchronously("download", {"record_id": record_id}, project_id=record.get("project_id"))


@router.get("/downloads")
def list_downloads(project_id: int | None = None, record_id: int | None = None) -> dict:
    return {
        "downloads": store.list_downloads(project_id=project_id, record_id=record_id),
        "stats": download_stats(project_id),
    }


@router.get("/downloads/{download_id}")
def get_download(download_id: int) -> dict:
    item = store.get_download(download_id)
    if not item:
        raise HTTPException(status_code=404, detail=f"download {download_id} does not exist")
    return item


@router.post("/downloads/purge-partial")
def purge_partial() -> dict:
    return {"removed_partial_files": purge_partial_files()}


@router.post("/projects/{project_id}/compatibility", status_code=202)
def start_compatibility(project_id: int, payload: CompatibilityRequest) -> dict:
    if not store.get_project(project_id):
        raise HTTPException(status_code=404, detail=f"project {project_id} does not exist")
    job_id = get_job_runner().submit(
        "compatibility",
        {"project_id": project_id, "record_ids": payload.record_ids, "intended_use": payload.intended_use},
        project_id=project_id,
    )
    return {"job_id": job_id, "kind": "compatibility", "status": "queued"}


@router.post("/projects/{project_id}/compatibility/sync")
def compatibility_sync(project_id: int, payload: CompatibilityRequest) -> dict:
    if not store.get_project(project_id):
        raise HTTPException(status_code=404, detail=f"project {project_id} does not exist")
    return run_synchronously(
        "compatibility",
        {"project_id": project_id, "record_ids": payload.record_ids, "intended_use": payload.intended_use},
        project_id=project_id,
    )


@router.get("/projects/{project_id}/compatibility")
def get_compatibility(project_id: int, report_id: int | None = None) -> dict:
    if not store.get_project(project_id):
        raise HTTPException(status_code=404, detail=f"project {project_id} does not exist")
    if report_id is not None:
        report = store.get_compatibility_report(project_id, report_id)
        if not report:
            raise HTTPException(status_code=404, detail=f"report {report_id} does not exist")
        return report
    rows = get_db().query(
        "SELECT id, record_ids, created_at FROM compatibility_reports WHERE project_id = ? ORDER BY id DESC",
        (project_id,),
    )
    for row in rows:
        row["record_ids"] = json.loads(row["record_ids"])
    return {"project_id": project_id, "reports": rows}


# ---------------------------------------------------------------------------
# manifest
# ---------------------------------------------------------------------------


@router.get("/projects/{project_id}/manifest")
def get_manifest(project_id: int, write: bool = False) -> dict:
    if not store.get_project(project_id):
        raise HTTPException(status_code=404, detail=f"project {project_id} does not exist")
    return write_manifest(project_id) if write else build_manifest(project_id)


@router.get("/projects/{project_id}/manifest/download")
def download_manifest(project_id: int) -> Response:
    if not store.get_project(project_id):
        raise HTTPException(status_code=404, detail=f"project {project_id} does not exist")
    manifest = build_manifest(project_id)
    body = json.dumps(manifest, indent=2, ensure_ascii=False).encode("utf-8")
    return Response(
        content=body,
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="manifest_project_{project_id}.json"'},
    )


# ---------------------------------------------------------------------------
# manuscript
# ---------------------------------------------------------------------------


@router.get("/projects/{project_id}/manuscript")
def get_manuscript(project_id: int) -> dict:
    if not store.get_project(project_id):
        raise HTTPException(status_code=404, detail=f"project {project_id} does not exist")
    manuscript = store.get_manuscript(project_id)
    if not manuscript:
        return {
            "project_id": project_id,
            "manuscript": None,
            "note": "No manuscript has been assembled for this project yet. POST to build one.",
        }
    return {"project_id": project_id, "manuscript": manuscript}


@router.post("/projects/{project_id}/manuscript", status_code=202)
def build_manuscript_endpoint(project_id: int, payload: ManuscriptRequest) -> dict:
    if not store.get_project(project_id):
        raise HTTPException(status_code=404, detail=f"project {project_id} does not exist")
    job_id = get_job_runner().submit(
        "manuscript", {"project_id": project_id, "run_id": payload.run_id}, project_id=project_id
    )
    return {"job_id": job_id, "kind": "manuscript", "status": "queued"}


@router.post("/projects/{project_id}/manuscript/sync")
def build_manuscript_sync(project_id: int, payload: ManuscriptRequest) -> dict:
    if not store.get_project(project_id):
        raise HTTPException(status_code=404, detail=f"project {project_id} does not exist")
    return run_synchronously(
        "manuscript", {"project_id": project_id, "run_id": payload.run_id}, project_id=project_id
    )


@router.get("/projects/{project_id}/manuscript/export")
def export(project_id: int, fmt: Literal["docx", "md", "tex", "json"] = "docx") -> Response:
    if not store.get_project(project_id):
        raise HTTPException(status_code=404, detail=f"project {project_id} does not exist")
    try:
        body, filename, media_type = export_manuscript(project_id, fmt)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if fmt == "docx":
        target = manuscript_path(project_id, "docx")
        try:
            with open(target, "wb") as handle:
                handle.write(body)
        except OSError as exc:  # exporting must still succeed if the cache write fails
            log.warning("manuscript_cache_write_failed", project_id=project_id, error=str(exc))
    return Response(
        content=body,
        media_type=media_type,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ---------------------------------------------------------------------------
# error shape
# ---------------------------------------------------------------------------


async def http_exception_handler(request: Request, exc: HTTPException) -> Response:
    return Response(
        content=json.dumps(
            {"error": {"status": exc.status_code, "message": exc.detail, "path": str(request.url.path)}},
            ensure_ascii=False,
        ),
        status_code=exc.status_code,
        media_type="application/json",
    )
