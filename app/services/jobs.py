"""Background job runner.

Long operations (research runs, dataset downloads, compatibility assessments,
gap discovery, manuscript assembly) are executed on a small thread pool so the
HTTP layer stays responsive. Every job is persisted in the ``jobs`` table with
progress, a human-readable message, a structured result and an error string, so
progress, cancellation and post-mortem inspection all work across restarts.

Cancellation is cooperative: the runner flips a flag in the database and each
handler polls it, leaving whatever was already retrieved and verified intact.
"""

from __future__ import annotations

import threading
import traceback
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable

from ..logging_utils import get_logger
from . import store
from .compatibility import advance_state, assess_compatibility, assess_dataset
from .downloads import download_record
from .gaps import build_and_store_gaps
from .manuscript import build_manuscript
from .orchestration import ResearchRunner, RunRequest

log = get_logger("services.jobs")

JOB_KINDS = ("research", "download", "compatibility", "gaps", "manuscript")

ProgressFn = Callable[[float, str], None]
CancelFn = Callable[[], bool]
Handler = Callable[[dict, ProgressFn, CancelFn], dict]

# Set while a handler runs so it can attach created resources (for example the
# project id) to the persisted job, letting the UI follow a run live.
_current_job = threading.local()


def report_project(project_id: int) -> None:
    job_id = getattr(_current_job, "job_id", None)
    if job_id:
        store.update_job(job_id, project_id=int(project_id))


# ---------------------------------------------------------------------------
# handlers
# ---------------------------------------------------------------------------


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def research_handler(request: dict, progress: ProgressFn, cancel: CancelFn) -> dict:
    run_request = RunRequest(
        question=(request.get("question") or "").strip(),
        project_id=_as_int(request.get("project_id")) or None,
        title=request.get("title") or "",
        # 0 means "use the configured default" — ResearchRunner resolves that.
        depth=_as_int(request.get("depth")),
        page_size=_as_int(request.get("page_size")),
        adapters=request.get("adapters") or None,
        intent=request.get("intent") or None,
        verify_limit=_as_int(request.get("verify_limit"), 20) or 20,
        max_results_per_source=_as_int(request.get("max_results_per_source")) or 120,
        max_queries_per_adapter=_as_int(request.get("max_queries_per_adapter")) or 4,
        follow_citations=bool(request.get("follow_citations", True)),
        fetch_fulltext=bool(request.get("fetch_fulltext", True)),
        use_llm=bool(request.get("use_llm", True)),
    )
    runner = ResearchRunner(progress=progress, cancel_check=cancel, project_callback=report_project)
    outcome = runner.run(run_request)
    payload = outcome.as_dict()
    payload["cancelled"] = outcome.status == "cancelled"
    return payload


def download_handler(request: dict, progress: ProgressFn, cancel: CancelFn) -> dict:
    record_id = _as_int(request.get("record_id"))
    record = store.get_record(record_id)
    if not record:
        raise ValueError(f"record {record_id} does not exist")
    project_id = _as_int(request.get("project_id")) or record.get("project_id")
    max_bytes = _as_int(request.get("max_bytes")) or 512 * 1024 * 1024
    result = download_record(
        record,
        project_id=project_id,
        max_bytes=max_bytes,
        progress=progress,
        cancel_check=cancel,
    )
    result["cancelled"] = any(
        item.get("status") == "cancelled" for item in result.get("files") or []
    )
    return result


def compatibility_handler(request: dict, progress: ProgressFn, cancel: CancelFn) -> dict:
    project_id = _as_int(request.get("project_id"))
    record_ids = [_as_int(value) for value in (request.get("record_ids") or [])]
    record_ids = [value for value in record_ids if value]
    if not record_ids:
        raise ValueError("at least one record_id is required")

    intended_use = request.get("intended_use") or ""
    records = store.records_by_ids(record_ids)
    ordered = [records[key] for key in record_ids if key in records]
    if not ordered:
        raise ValueError("none of the requested records exist")

    progress(0.2, f"Assessing {len(ordered)} dataset(s) at metadata level")
    report = assess_compatibility(ordered, intended_use=intended_use)

    per_dataset = []
    for record in ordered:
        if record.get("kind") != "dataset":
            continue
        if cancel():
            break
        local_files = [
            item["target_path"]
            for item in store.list_downloads(record_id=record["id"])
            if item.get("status") == "verified" and item.get("target_path")
        ]
        detail = assess_dataset(record, local_files=local_files, intended_use=intended_use)
        per_dataset.append(detail)

        current = (store.get_dataset_state(record["id"]) or {}).get("state", "discovered")
        if local_files:
            new_state = advance_state(
                current, "compatibility_assessed", "compatibility assessed on downloaded content"
            )
        else:
            new_state = advance_state(
                current, "metadata_verified", "compatibility assessed on metadata only"
            )
        store.set_dataset_state(record["id"], new_state, "compatibility assessment recorded")

    progress(0.9, "Recording the compatibility report")
    report["per_dataset"] = per_dataset
    report["intended_use"] = intended_use
    report["record_ids"] = record_ids
    report["report_id"] = store.save_compatibility_report(project_id, record_ids, report)
    report["cancelled"] = cancel()
    return report


def gaps_handler(request: dict, progress: ProgressFn, cancel: CancelFn) -> dict:
    project_id = _as_int(request.get("project_id"))
    run_id = _as_int(request.get("run_id")) or None
    if not run_id:
        runs = store.list_runs(project_id=project_id, limit=1)
        run_id = runs[0]["id"] if runs else None
    progress(0.3, "Building the literature evidence matrix")
    scope: dict = {}
    if run_id:
        run = store.get_run(run_id)
        scope = (run or {}).get("scope") or {}
    result = build_and_store_gaps(project_id, run_id, scope)
    progress(0.9, f"Discovered {len(result.get('gaps') or [])} gap candidate(s)")
    return result


def manuscript_handler(request: dict, progress: ProgressFn, cancel: CancelFn) -> dict:
    project_id = _as_int(request.get("project_id"))
    progress(0.5, "Assembling the manuscript workspace from stored evidence")
    manuscript = build_manuscript(project_id, _as_int(request.get("run_id")) or None, store_result=True)
    manuscript["cancelled"] = cancel()
    return manuscript


HANDLERS: dict[str, Handler] = {
    "research": research_handler,
    "download": download_handler,
    "compatibility": compatibility_handler,
    "gaps": gaps_handler,
    "manuscript": manuscript_handler,
}


# ---------------------------------------------------------------------------
# runner
# ---------------------------------------------------------------------------


class JobRunner:
    def __init__(self, max_workers: int = 3) -> None:
        self._pool = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="rf-job")
        self._futures: dict[str, Any] = {}
        self._lock = threading.Lock()

    # -- submission --------------------------------------------------------
    def submit(
        self,
        kind: str,
        request: dict,
        *,
        project_id: int | None = None,
        handler: Handler | None = None,
    ) -> str:
        if kind not in HANDLERS and handler is None:
            raise ValueError(f"unknown job kind '{kind}'")
        job_id = store.create_job(kind, request, project_id)
        worker = handler or HANDLERS[kind]
        future = self._pool.submit(self._run, job_id, kind, request, worker)
        with self._lock:
            self._futures[job_id] = future
        log.info("job_submitted", job_id=job_id, kind=kind, project_id=project_id)
        return job_id

    def _run(self, job_id: str, kind: str, request: dict, handler: Handler) -> None:
        _current_job.job_id = job_id
        store.update_job(job_id, status="running", progress=0.01, message="started")

        def progress(fraction: float, message: str) -> None:
            try:
                store.update_job(
                    job_id,
                    status="cancelling" if store.is_cancelled(job_id) else "running",
                    progress=max(0.0, min(1.0, float(fraction))),
                    message=message,
                )
            except Exception:  # progress must never break a job
                log.warning("job_progress_failed", job_id=job_id)

        def cancel() -> bool:
            return store.is_cancelled(job_id)

        try:
            result = handler(request, progress, cancel)
        except Exception as exc:  # a failed job must be recorded, never swallowed
            log.error("job_failed", job_id=job_id, kind=kind, error=str(exc))
            store.update_job(
                job_id,
                status="failed",
                error=f"{exc}\n{traceback.format_exc()[-2000:]}",
                message=f"failed: {exc}",
            )
            _current_job.job_id = None
            return

        cancelled = bool(result.get("cancelled")) or store.is_cancelled(job_id)
        if cancelled:
            store.update_job(
                job_id, status="cancelled", progress=1.0, message="cancelled by the user", result=result
            )
        else:
            store.update_job(
                job_id, status="completed", progress=1.0, message="completed", result=result
            )

        new_project_id = result.get("project_id")
        if new_project_id:
            store.update_job(job_id, project_id=_as_int(new_project_id))
        _current_job.job_id = None
        log.info("job_finished", job_id=job_id, kind=kind, status="cancelled" if cancelled else "completed")

    # -- inspection --------------------------------------------------------
    def cancel(self, job_id: str) -> bool:
        job = store.get_job(job_id)
        if not job:
            return False
        if job["status"] in {"completed", "failed", "cancelled", "interrupted"}:
            return False
        store.request_cancel(job_id)
        store.update_job(job_id, status="cancelling", message="cancellation requested")
        return True

    def shutdown(self, wait: bool = False) -> None:
        self._pool.shutdown(wait=wait)

    def active(self) -> list[str]:
        with self._lock:
            return [
                job_id
                for job_id, future in self._futures.items()
                if not future.done()
            ]


_runner: JobRunner | None = None
_runner_lock = threading.Lock()


def get_job_runner() -> JobRunner:
    global _runner
    if _runner is None:
        with _runner_lock:
            if _runner is None:
                _runner = JobRunner()
    return _runner


def reset_job_runner() -> None:
    """Used by tests."""
    global _runner
    with _runner_lock:
        if _runner is not None:
            _runner.shutdown(wait=False)
        _runner = None


def run_synchronously(kind: str, request: dict, project_id: int | None = None) -> dict:
    """Run a job inline (no thread pool).

    Used by the synchronous API endpoints and by the automated tests so a
    failure surfaces directly instead of being hidden inside a worker thread.
    """
    if kind not in HANDLERS:
        raise ValueError(f"unknown job kind '{kind}'")
    messages: list[str] = []

    def progress(fraction: float, message: str) -> None:
        messages.append(message)

    result = HANDLERS[kind](request, progress, lambda: False)
    result["progress_log"] = messages
    if project_id is None:
        project_id = result.get("project_id")
    return result
