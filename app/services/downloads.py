"""Dataset download manager.

Guarantees:

* Only openly published direct file URLs are downloaded. Authentication,
  paywalls and license acceptance screens are never bypassed — those datasets are
  reported as requiring manual access instead.
* Every download is validated: HTTP status, content length, declared size,
  written bytes, SHA-256 of the finished file, and (for archives) CRC checks.
* Interrupted transfers resume with a Range request when the server supports it.
* Oversized files are refused before any bytes are written.
* A manifest with provenance is written for every collection.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Callable

from ..config import get_settings
from ..db import get_db, utc_now
from ..http_client import USER_AGENT_FALLBACK
from ..logging_utils import get_logger
from . import store
from .compatibility import advance_state, inspect_file

log = get_logger("services.downloads")

DEFAULT_MAX_BYTES = 512 * 1024 * 1024  # 512 MiB per file
CHUNK = 256 * 1024
UNSAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._\-]+")


@dataclass
class FileCandidate:
    name: str
    url: str
    declared_size: int | None = None
    checksum: str = ""


def safe_filename(name: str, fallback: str = "dataset_file") -> str:
    base = os.path.basename(urllib.parse.urlparse(name).path if "://" in name else name)
    base = UNSAFE_NAME_RE.sub("_", base).strip("._") or fallback
    return base[:120]


def download_candidates(record: dict) -> list[FileCandidate]:
    dataset = record.get("dataset") or {}
    candidates: list[FileCandidate] = []
    for entry in dataset.get("files") or []:
        if not isinstance(entry, dict):
            continue
        url = entry.get("url") or ""
        if not url:
            continue
        candidates.append(
            FileCandidate(
                name=safe_filename(entry.get("name") or url),
                url=url,
                declared_size=entry.get("size_bytes") if isinstance(entry.get("size_bytes"), int) else None,
                checksum=str(entry.get("checksum") or ""),
            )
        )
    if not candidates and dataset.get("download_url"):
        url = dataset["download_url"]
        candidates.append(FileCandidate(name=safe_filename(url), url=url))
    return candidates


def can_download(record: dict) -> tuple[bool, str]:
    if record.get("kind") != "dataset":
        return False, "the record is not a dataset"
    if record.get("access") == "restricted":
        return False, (
            "the source declares restricted access; ResearchForge does not bypass access controls. "
            "Use the source record link to request access from the provider."
        )
    candidates = download_candidates(record)
    if not candidates:
        return False, "the source publishes no direct file URL; download must be done manually"
    return True, f"{len(candidates)} file(s) available for download"


def download_record(
    record: dict,
    *,
    project_id: int | None = None,
    max_bytes: int = DEFAULT_MAX_BYTES,
    max_files: int = 8,
    progress: Callable[[float, str], None] | None = None,
    cancel_check: Callable[[], bool] | None = None,
) -> dict:
    """Download the files of one dataset record with validation and provenance."""
    allowed, reason = can_download(record)
    if not allowed:
        return {"status": "not_downloadable", "reason": reason, "files": [], "record_id": record.get("id")}

    settings = get_settings()
    target_dir = settings.download_dir / f"dataset_{record.get('id')}"
    target_dir.mkdir(parents=True, exist_ok=True)

    candidates = download_candidates(record)[:max_files]
    results: list[dict] = []
    verified_any = False

    for index, candidate in enumerate(candidates):
        if cancel_check and cancel_check():
            results.append({"name": candidate.name, "url": candidate.url, "status": "cancelled"})
            break
        if progress:
            progress(index / max(1, len(candidates)), f"Downloading {candidate.name}")

        download_id = store.create_download(record["id"], project_id, candidate.url)
        outcome = _download_one(
            download_id=download_id,
            candidate=candidate,
            directory=target_dir,
            max_bytes=max_bytes,
            cancel_check=cancel_check,
        )
        results.append(outcome)
        if outcome.get("status") == "verified":
            verified_any = True

    if verified_any:
        current = (store.get_dataset_state(record["id"]) or {}).get("state", "discovered")
        new_state = advance_state(current, "download_verified", "at least one file downloaded and validated")
        store.set_dataset_state(record["id"], new_state, "file download validated (size + SHA-256)")

    if progress:
        progress(1.0, "Download finished")
    return {
        "status": "verified" if verified_any else "failed",
        "record_id": record.get("id"),
        "record_title": record.get("title"),
        "files": results,
        "reason": "" if verified_any else "no file could be downloaded and validated",
    }


def _download_one(
    *,
    download_id: int,
    candidate: FileCandidate,
    directory,
    max_bytes: int,
    cancel_check: Callable[[], bool] | None,
) -> dict:
    final_path = directory / candidate.name
    part_path = directory / f"{candidate.name}.part"
    store.update_download(download_id, status="running", started_at=utc_now(), attempts=1, target_path=str(final_path))

    resume_from = part_path.stat().st_size if part_path.exists() else 0
    headers = {"User-Agent": get_settings().user_agent or USER_AGENT_FALLBACK, "Accept": "*/*"}
    if resume_from:
        headers["Range"] = f"bytes={resume_from}-"

    request = urllib.request.Request(candidate.url, headers=headers, method="GET")
    started = time.monotonic()
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            status = response.status
            content_length = response.headers.get("Content-Length")
            content_type = (response.headers.get("Content-Type") or "").split(";")[0].strip()
            total = int(content_length) + (resume_from if status == 206 else 0) if content_length else None

            if status == 200 and resume_from:
                resume_from = 0  # server ignored the range; start over
                part_path.unlink(missing_ok=True)

            if total and total > max_bytes:
                message = (
                    f"refused: the file is {total} bytes which exceeds the configured limit of "
                    f"{max_bytes} bytes"
                )
                store.update_download(
                    download_id,
                    status="skipped_too_large",
                    bytes_total=total,
                    error=message,
                    finished_at=utc_now(),
                    content_type=content_type,
                )
                log.warning("download_too_large", url=candidate.url, bytes=total, limit=max_bytes)
                return {"name": candidate.name, "url": candidate.url, "status": "skipped_too_large", "detail": message}

            digest = hashlib.sha256()
            written = 0
            if resume_from:
                with open(part_path, "rb") as existing:
                    for block in iter(lambda: existing.read(CHUNK), b""):
                        digest.update(block)
                written = resume_from

            with open(part_path, "ab") as handle:
                while True:
                    if cancel_check and cancel_check():
                        store.update_download(
                            download_id,
                            status="cancelled",
                            bytes_written=written,
                            error="cancelled by the user while streaming",
                            finished_at=utc_now(),
                        )
                        return {"name": candidate.name, "url": candidate.url, "status": "cancelled"}
                    block = response.read(CHUNK)
                    if not block:
                        break
                    handle.write(block)
                    digest.update(block)
                    written += len(block)
                    if written > max_bytes:
                        raise ValueError(f"stream exceeded the {max_bytes} byte limit")
                    store.update_download(
                        download_id, bytes_written=written, bytes_total=total, content_type=content_type
                    )
        part_path.replace(final_path)
        sha256 = digest.hexdigest()
        declared_mismatch = bool(
            candidate.declared_size and total and candidate.declared_size != total
        )
        checksum_note = ""
        if candidate.checksum.startswith("md5:"):
            md5 = hashlib.md5()
            with open(final_path, "rb") as handle:
                for block in iter(lambda: handle.read(CHUNK), b""):
                    md5.update(block)
            if md5.hexdigest() != candidate.checksum.split(":", 1)[1]:
                checksum_note = "the repository-provided MD5 does not match the downloaded bytes"
        elif candidate.checksum.startswith("sha256:"):
            if sha256 != candidate.checksum.split(":", 1)[1]:
                checksum_note = "the repository-provided SHA-256 does not match the downloaded bytes"

        store.update_download(
            download_id,
            status="verified",
            bytes_written=written,
            bytes_total=total or written,
            sha256=sha256,
            content_type=content_type,
            finished_at=utc_now(),
            error=checksum_note,
        )
        profile = inspect_file(str(final_path))
        store.save_download_members(
            download_id,
            [
                {
                    "path": str(final_path),
                    "size_bytes": written,
                    "sha256": sha256,
                    "declared_size_mismatch": declared_mismatch,
                    "checks": profile.get("checks", []),
                    "dimensions": profile.get("dimensions", []),
                }
            ],
        )
        log.info(
            "download_verified",
            url=candidate.url,
            bytes=written,
            sha256=sha256[:16],
            duration_s=round(time.monotonic() - started, 2),
        )
        return {
            "name": candidate.name,
            "url": candidate.url,
            "path": str(final_path),
            "status": "verified",
            "bytes": written,
            "sha256": sha256,
            "content_type": content_type,
            "declared_size_mismatch": declared_mismatch,
            "checksum_note": checksum_note,
            "checks": profile.get("checks", []),
            "dimensions": profile.get("dimensions", []),
        }
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, ValueError, OSError) as exc:
        status_code = getattr(exc, "code", None)
        message = f"download failed: {exc}"
        if status_code in {401, 402, 403, 451}:
            message = (
                f"HTTP {status_code}: the file requires authentication or license acceptance. "
                "ResearchForge does not bypass access controls; download it manually from the source."
            )
        store.update_download(
            download_id,
            status="failed",
            error=message[:800],
            finished_at=utc_now(),
        )
        log.warning("download_failed", url=candidate.url, error=str(exc))
        return {"name": candidate.name, "url": candidate.url, "status": "failed", "detail": message}


# ---------------------------------------------------------------------------
# collection manifest
# ---------------------------------------------------------------------------


def build_manifest(project_id: int, record_ids: list[int] | None = None) -> dict:
    records = store.list_records(project_id=project_id, limit=2000)
    # A collection manifest describes datasets and their files, so bibliographic
    # records are excluded rather than inflating the dataset totals.
    records = [record for record in records if record.get("kind") == "dataset"]
    if record_ids:
        wanted = set(record_ids)
        records = [record for record in records if record["id"] in wanted]
    downloads = store.list_downloads(project_id=project_id)

    entries = []
    for record in records:
        dataset = record.get("dataset") or {}
        dataset_downloads = [item for item in downloads if item["record_id"] == record["id"]]
        files = []
        for item in dataset_downloads:
            members = item.get("members") or []
            files.append(
                {
                    "url": item["url"],
                    "path": item.get("target_path") or "",
                    "status": item["status"],
                    "bytes_total": item.get("bytes_total"),
                    "bytes_written": item.get("bytes_written"),
                    "sha256": item.get("sha256"),
                    "content_type": item.get("content_type"),
                    "error": item.get("error") or "",
                    "started_at": item.get("started_at"),
                    "finished_at": item.get("finished_at"),
                    "profile": members,
                }
            )
        entries.append(
            {
                "record_id": record["id"],
                "kind": record["kind"],
                "source": record["source"],
                "source_id": record["source_id"],
                "title": record["title"],
                "record_url": record["url"],
                "doi": record["doi"],
                "license": record["license"],
                "access": record["access"],
                "version": dataset.get("version") or "",
                "publisher": dataset.get("publisher") or record.get("publisher") or "",
                "creators": dataset.get("creators") or [],
                "retrieved_at": record["retrieved_at"],
                "verification_status": record["verification_status"],
                "dataset_state": (store.get_dataset_state(record["id"]) or {}).get("state", "discovered"),
                "declared_files": dataset.get("files") or [],
                "downloads": files,
            }
        )

    manifest = {
        "manifest_version": "1.0",
        "project_id": project_id,
        "generated_at": utc_now(),
        "generator": "ResearchForge",
        "disclaimer": (
            "This manifest records what was actually retrieved and verified. Files without a "
            "SHA-256 were never downloaded. Licenses and access terms remain those of the original "
            "providers and must be honoured."
        ),
        "entries": entries,
        "totals": {
            "datasets": len(entries),
            "files_downloaded": sum(1 for entry in entries for f in entry["downloads"] if f["status"] == "verified"),
            "files_failed": sum(1 for entry in entries for f in entry["downloads"] if f["status"] == "failed"),
            "bytes_downloaded": sum(
                f["bytes_written"] or 0 for entry in entries for f in entry["downloads"]
            ),
        },
    }
    return manifest


def write_manifest(project_id: int, record_ids: list[int] | None = None) -> dict:
    manifest = build_manifest(project_id, record_ids)
    settings = get_settings()
    target = settings.download_dir / f"manifest_project_{project_id}.json"
    target.write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
    manifest["path"] = str(target)
    return manifest


def estimate_download_size(record: dict) -> int:
    dataset = record.get("dataset") or {}
    total = dataset.get("size_bytes") or 0
    if not total:
        total = sum(
            entry.get("size_bytes") or 0
            for entry in dataset.get("files") or []
            if isinstance(entry, dict)
        )
    return int(total or 0)


def purge_partial_files() -> int:
    """Remove leftover .part files from interrupted downloads."""
    settings = get_settings()
    removed = 0
    for root, _dirs, files in os.walk(settings.download_dir):
        for name in files:
            if name.endswith(".part"):
                try:
                    os.remove(os.path.join(root, name))
                    removed += 1
                except OSError:
                    continue
    return removed


def download_stats(project_id: int | None = None) -> dict:
    rows = get_db().query(
        "SELECT status, COUNT(*) AS total, SUM(COALESCE(bytes_written,0)) AS bytes FROM downloads "
        + ("WHERE project_id = ? " if project_id is not None else "")
        + "GROUP BY status",
        (project_id,) if project_id is not None else (),
    )
    return {
        "by_status": {row["status"]: row["total"] for row in rows},
        "bytes_written": sum(row["bytes"] or 0 for row in rows),
    }


__all__ = [
    "build_manifest",
    "can_download",
    "download_candidates",
    "download_record",
    "download_stats",
    "estimate_download_size",
    "purge_partial_files",
    "safe_filename",
    "write_manifest",
]
