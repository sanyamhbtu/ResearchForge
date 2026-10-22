"""Dataset quality and compatibility engine.

Deterministic by design: every check states the criterion it applied and the
evidence it used. Nothing here asks a language model to guess.

The engine deliberately does **not** produce a single "reliability score" —
that would hide the reasoning. It reports individual dimensions with explicit
``verified`` / ``failed`` / ``unknown`` / ``not_applicable`` states, plus the
warnings that follow from them.
"""

from __future__ import annotations

import csv
import json
import os
import struct
import zipfile
from dataclasses import asdict, dataclass, field
from typing import Any, Iterable

from ..logging_utils import get_logger

log = get_logger("services.compatibility")

VERIFIED = "verified"
FAILED = "failed"
UNKNOWN = "unknown"
NOT_APPLICABLE = "not_applicable"
WARNING = "warning"

# The dataset state machine. States only ever move forward.
STATES = [
    "discovered",
    "metadata_verified",
    "download_verified",
    "contents_inspected",
    "compatibility_assessed",
    "approved",
]
STATE_DESCRIPTIONS = {
    "discovered": "The record was returned by a source search.",
    "metadata_verified": "Source identity, link and registrar metadata were checked.",
    "download_verified": "At least one file was actually downloaded and its integrity validated.",
    "contents_inspected": "The downloaded content was opened and profiled.",
    "compatibility_assessed": "Deterministic compatibility checks were executed against the intended use.",
    "approved": "A human explicitly approved the dataset for a stated intended use.",
}

TABULAR_EXTENSIONS = {"csv", "tsv", "txt"}
JSON_EXTENSIONS = {"json", "jsonl", "ndjson"}
ARCHIVE_EXTENSIONS = {"zip"}
IMAGE_EXTENSIONS = {"png", "jpg", "jpeg"}
MAX_ROWS_SCANNED = 20000
MAX_BYTES_SCANNED = 60_000_000


@dataclass
class Check:
    name: str
    status: str
    method: str
    detail: str = ""
    evidence: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return asdict(self)


def advance_state(current: str, candidate: str, reason: str = "") -> str:
    """Move a dataset forward in the state machine only."""
    if current not in STATES:
        current = "discovered"
    if candidate not in STATES:
        return current
    index_current = STATES.index(current)
    index_candidate = STATES.index(candidate)
    if index_candidate <= index_current:
        return current
    log.info("dataset_state_advanced", from_state=current, to_state=candidate, reason=reason[:200])
    return candidate


# ---------------------------------------------------------------------------
# metadata-level assessment (no download required)
# ---------------------------------------------------------------------------


def assess_metadata(record: dict) -> dict:
    dataset = record.get("dataset") or {}
    checks: list[Check] = []

    checks.append(
        _check(
            "source_identity",
            VERIFIED if record.get("source_id") else FAILED,
            "source record exposes an identifier used to re-locate the record",
            f"identifier: {record.get('source')}:{record.get('source_id')}",
        )
    )
    checks.append(
        _check(
            "record_link",
            VERIFIED if record.get("url") else FAILED,
            "the record publishes a canonical URL",
            record.get("url") or "no canonical URL was published",
        )
    )
    license_value = (record.get("license") or "").strip()
    if license_value:
        checks.append(
            _check(
                "license",
                VERIFIED,
                "license text published by the source",
                f"declared license: {license_value}",
                {"license": license_value},
            )
        )
    else:
        checks.append(
            _check(
                "license",
                UNKNOWN,
                "license text published by the source",
                "no license was published; reuse terms are unknown and must not be assumed",
            )
        )

    samples = dataset.get("samples")
    checks.append(
        _check(
            "sample_count",
            VERIFIED if isinstance(samples, int) else UNKNOWN,
            "sample/row count published by the source",
            f"declared samples: {samples}" if isinstance(samples, int) else "sample count is not published",
            {"samples": samples if isinstance(samples, int) else None},
        )
    )

    files = dataset.get("files") or []
    checks.append(
        _check(
            "file_listing",
            VERIFIED if files else UNKNOWN,
            "file inventory published by the source",
            f"{len(files)} file(s) listed" if files else "no files are listed by the source",
            {"file_count": len(files)},
        )
    )
    total_size = dataset.get("size_bytes") or sum(
        f.get("size_bytes") or 0 for f in files if isinstance(f, dict)
    )
    checks.append(
        _check(
            "size_known",
            VERIFIED if total_size else UNKNOWN,
            "byte size published by the source",
            f"{total_size} bytes" if total_size else "size is not published",
        )
    )
    checks.append(
        _check(
            "provenance",
            VERIFIED if (dataset.get("creators") or dataset.get("publisher")) else UNKNOWN,
            "creator/publisher recorded in the source metadata",
            f"creators: {', '.join(str(c) for c in (dataset.get('creators') or [])[:5]) or 'not published'}; "
            f"publisher: {dataset.get('publisher') or 'not published'}",
        )
    )
    checks.append(
        _check(
            "update_date",
            VERIFIED if dataset.get("updated") else UNKNOWN,
            "version/update information published by the source",
            f"last update: {dataset.get('updated')}" if dataset.get("updated") else "no update date published",
        )
    )
    access = record.get("access") or "unknown"
    checks.append(
        _check(
            "access_terms",
            VERIFIED if access in {"open", "restricted"} else UNKNOWN,
            "access level stated by the source",
            (
                "open access declared by the source"
                if access == "open"
                else "restricted access declared; ResearchForge will not bypass access controls"
                if access == "restricted"
                else "access terms are not stated"
            ),
        )
    )
    modality = (record.get("modality") or dataset.get("modality") or "").strip()
    checks.append(
        _check(
            "modality_declared",
            VERIFIED if modality else UNKNOWN,
            "modality declared in source metadata",
            modality or "modality is not declared by the source",
        )
    )

    warnings = [check.detail for check in checks if check.status == UNKNOWN]
    return {
        "record_id": record.get("id"),
        "title": record.get("title"),
        "modality": modality,
        "checks": [check.as_dict() for check in checks],
        "warnings": warnings,
        "unknowns": warnings,
    }


# ---------------------------------------------------------------------------
# content-level inspection of files that were actually downloaded
# ---------------------------------------------------------------------------


def inspect_file(path: str, modality_hint: str = "") -> dict:
    """Open a downloaded file and profile it. Never guesses about its content."""
    if not os.path.isfile(path):
        return {
            "path": path,
            "checks": [
                _check("file_present", FAILED, "os.path.isfile", "the file does not exist on disk").as_dict()
            ],
            "dimensions": [],
        }

    size = os.path.getsize(path)
    extension = os.path.splitext(path)[1].lstrip(".").lower()
    checks: list[Check] = [
        _check(
            "file_present",
            VERIFIED,
            "os.path.isfile + os.path.getsize",
            f"{size} bytes present on disk",
            {"size_bytes": size},
        )
    ]
    dimensions: list[dict] = []

    if extension in ARCHIVE_EXTENSIONS:
        checks.extend(_inspect_archive(path))
    elif extension in TABULAR_EXTENSIONS or extension == "txt":
        checks.extend(_inspect_tabular(path, modality_hint))
        dimensions.extend(_tabular_dimensions(path))
    elif extension in JSON_EXTENSIONS:
        checks.extend(_inspect_json(path))
    elif extension in IMAGE_EXTENSIONS:
        checks.extend(_inspect_image(path))
    else:
        checks.append(
            _check(
                "content_inspection",
                UNKNOWN,
                "modality-specific inspector",
                f"no deterministic inspector is implemented for '.{extension}' files; "
                "contents remain uninspected",
            )
        )

    return {
        "path": path,
        "size_bytes": size,
        "checks": [check.as_dict() for check in checks],
        "dimensions": dimensions,
    }


def _inspect_archive(path: str) -> list[Check]:
    checks: list[Check] = []
    try:
        with zipfile.ZipFile(path) as archive:
            names = archive.namelist()
            bad = archive.testzip()
    except zipfile.BadZipFile as exc:
        return [_check("archive_integrity", FAILED, "zipfile.ZipFile + testzip", f"bad archive: {exc}")]
    except Exception as exc:
        return [_check("archive_integrity", UNKNOWN, "zipfile.ZipFile", f"could not read archive: {exc}")]

    checks.append(
        _check(
            "archive_integrity",
            VERIFIED if bad is None else FAILED,
            "zipfile.ZipFile + testzip (CRC verification)",
            "all members passed CRC verification" if bad is None else f"corrupt member: {bad}",
            {"members": len(names), "sample": names[:20]},
        )
    )
    return checks


def _inspect_tabular(path: str, modality_hint: str) -> list[Check]:
    checks: list[Check] = []
    delimiter = "\t" if path.lower().endswith(".tsv") else ","
    try:
        with open(path, "r", encoding="utf-8", errors="replace", newline="") as handle:
            reader = csv.reader(handle, delimiter=delimiter)
            header = next(reader)
            rows = 0
            ragged = 0
            for row in reader:
                if rows >= MAX_ROWS_SCANNED:
                    break
                rows += 1
                if len(row) != len(header):
                    ragged += 1
    except StopIteration:
        return [_check("parseability", FAILED, "csv.reader", "the file has no header row")]
    except Exception as exc:
        return [_check("parseability", FAILED, "csv.reader", f"could not parse the file: {exc}")]

    checks.append(
        _check(
            "parseability",
            VERIFIED,
            f"csv.reader with delimiter '{delimiter}'",
            f"parsed {rows} data row(s) and {len(header)} column(s)",
            {"rows": rows, "columns": len(header)},
        )
    )
    checks.append(
        _check(
            "schema_consistency",
            VERIFIED if ragged == 0 else FAILED,
            "row width vs header width comparison",
            (
                "every scanned row matches the header width"
                if ragged == 0
                else f"{ragged} of {rows} scanned row(s) do not match the header width"
            ),
            {"ragged_rows": ragged, "rows_scanned": rows},
        )
    )
    if rows < 50:
        checks.append(
            _check(
                "statistical_checks",
                NOT_APPLICABLE,
                "class imbalance / leakage heuristics",
                "fewer than 50 rows were scanned, so distribution checks are not meaningful",
            )
        )
    return checks


def _tabular_dimensions(path: str) -> list[dict]:
    delimiter = "\t" if path.lower().endswith(".tsv") else ","
    try:
        with open(path, "r", encoding="utf-8", errors="replace", newline="") as handle:
            reader = csv.reader(handle, delimiter=delimiter)
            header = next(reader)
            missing = [0] * len(header)
            seen_rows: set[tuple] = set()
            duplicates = 0
            rows = 0
            for row in reader:
                if rows >= MAX_ROWS_SCANNED:
                    break
                rows += 1
                padded = row + [""] * (len(header) - len(row))
                key = tuple(padded[: len(header)])
                if key in seen_rows:
                    duplicates += 1
                else:
                    seen_rows.add(key)
                for index in range(min(len(header), len(padded))):
                    if padded[index].strip() == "":
                        missing[index] += 1
    except Exception as exc:
        return [{"name": "column_profile", "status": UNKNOWN, "note": f"could not profile columns: {exc}"}]

    dimensions: list[dict] = []
    if rows:
        dimensions.append(
            {
                "name": "missing_values",
                "status": VERIFIED,
                "method": "empty-cell count per column over the scanned rows",
                "value": {
                    header[i]: {"missing": missing[i], "ratio": round(missing[i] / rows, 4)}
                    for i in range(min(len(header), 40))
                },
                "note": f"based on the first {rows} data row(s)",
            }
        )
        dimensions.append(
            {
                "name": "duplicate_rows",
                "status": VERIFIED,
                "method": "exact duplicate detection over the scanned rows",
                "value": {"duplicates": duplicates, "ratio": round(duplicates / rows, 4)},
                "note": "near-duplicates require a similarity metric and are not claimed here",
            }
        )
    dimensions.append(
        {
            "name": "class_imbalance",
            "status": UNKNOWN,
            "method": "requires a designated label column",
            "note": (
                "No label column was specified, so class balance is reported as unknown rather "
                "than guessed from the column names."
            ),
        }
    )
    dimensions.append(
        {
            "name": "train_test_leakage",
            "status": UNKNOWN,
            "method": "requires knowledge of the intended split and identifiers",
            "note": "Leakage cannot be decided from the file alone.",
        }
    )
    return dimensions


def _inspect_json(path: str) -> list[Check]:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            head = handle.read(MAX_BYTES_SCANNED)
    except Exception as exc:
        return [_check("parseability", FAILED, "json.loads", f"could not read the file: {exc}")]
    try:
        parsed = json.loads(head)
    except ValueError:
        lines = [line for line in head.splitlines() if line.strip()]
        try:
            for line in lines[:MAX_ROWS_SCANNED]:
                json.loads(line)
        except ValueError as exc:
            return [
                _check(
                    "parseability",
                    FAILED,
                    "json.loads / JSON-lines fallback",
                    f"the file is neither valid JSON nor valid JSON-lines: {exc}",
                )
            ]
        return [
            _check(
                "parseability",
                VERIFIED,
                "JSON-lines validation over the first records",
                f"validated {min(len(lines), MAX_ROWS_SCANNED)} JSON-lines record(s)",
                {"records": min(len(lines), MAX_ROWS_SCANNED)},
            )
        ]
    record_count = len(parsed) if isinstance(parsed, list) else 1
    return [
        _check(
            "parseability",
            VERIFIED,
            "json.loads",
            f"valid JSON; {record_count} top-level record(s)",
            {"records": record_count},
        )
    ]


def _inspect_image(path: str) -> list[Check]:
    dimensions = _image_dimensions(path)
    if not dimensions:
        return [
            _check(
                "image_header",
                UNKNOWN,
                "PNG IHDR / JPEG SOF header parsing",
                "image dimensions could not be read from the file header",
            )
        ]
    return [
        _check(
            "image_header",
            VERIFIED,
            "PNG IHDR / JPEG SOF header parsing",
            f"image dimensions: {dimensions['width']}x{dimensions['height']}",
            dimensions,
        )
    ]


def _image_dimensions(path: str) -> dict | None:
    try:
        with open(path, "rb") as handle:
            head = handle.read(32)
            if head.startswith(b"\x89PNG\r\n\x1a\n"):
                width, height = struct.unpack(">II", head[16:24])
                return {"format": "png", "width": width, "height": height}
            if head.startswith(b"\xff\xd8"):
                handle.seek(2)
                while True:
                    marker = handle.read(2)
                    if len(marker) < 2:
                        return None
                    if marker[0] != 0xFF:
                        return None
                    marker_type = marker[1]
                    if marker_type in {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB}:
                        length = struct.unpack(">H", handle.read(2))[0]
                        data = handle.read(5)
                        height, width = struct.unpack(">HH", data[1:5])
                        return {"format": "jpeg", "width": width, "height": height}
                    length_bytes = handle.read(2)
                    if len(length_bytes) < 2:
                        return None
                    length = struct.unpack(">H", length_bytes)[0]
                    handle.seek(length - 2, os.SEEK_CUR)
    except Exception:
        return None
    return None


# ---------------------------------------------------------------------------
# compatibility across several datasets
# ---------------------------------------------------------------------------

LICENSE_CONFLICTS = (
    ("cc-by-nc", "commercial"),
    ("nc-", "commercial"),
    ("noncommercial", "commercial"),
)


def assess_compatibility(records: Iterable[dict], intended_use: str = "") -> dict:
    datasets = list(records)
    checks: list[Check] = []
    warnings: list[str] = []

    if len(datasets) < 1:
        return {
            "datasets": [],
            "checks": [],
            "warnings": ["No datasets were selected, so no compatibility assessment was possible."],
            "dimensions": [],
        }

    modalities = {(record.get("modality") or (record.get("dataset") or {}).get("modality") or "") for record in datasets}
    modalities.discard("")
    checks.append(
        _check(
            "modality_consistency",
            VERIFIED if len(modalities) <= 1 else WARNING,
            "comparison of declared modalities across the selection",
            f"declared modalities: {', '.join(sorted(modalities)) or 'unknown'}",
            {"modalities": sorted(modalities)},
        )
    )
    if len(modalities) > 1:
        warnings.append(
            "Selected datasets declare different modalities; combined training would require an "
            "explicit multimodal design rather than simple concatenation."
        )

    licenses = {(record.get("license") or "").lower() for record in datasets}
    licenses.discard("")
    unknown_licenses = [record["title"] for record in datasets if not (record.get("license") or "").strip()]
    if unknown_licenses:
        checks.append(
            _check(
                "license_compatibility",
                UNKNOWN,
                "comparison of declared licenses across the selection",
                f"{len(unknown_licenses)} dataset(s) have no declared license: "
                + "; ".join(title[:60] for title in unknown_licenses[:5]),
            )
        )
        warnings.append(
            "At least one dataset has no declared license. Combining data with unknown reuse terms "
            "is not safe; resolve licensing before use."
        )
    else:
        checks.append(
            _check(
                "license_compatibility",
                VERIFIED,
                "comparison of declared licenses across the selection",
                f"all selected datasets declare a license: {', '.join(sorted(licenses))}",
                {"licenses": sorted(licenses)},
            )
        )
    if "commercial" in intended_use.lower():
        for record in datasets:
            license_value = (record.get("license") or "").lower()
            if any(marker in license_value for marker, _ in LICENSE_CONFLICTS):
                checks.append(
                    _check(
                        "intended_use_license_conflict",
                        FAILED,
                        "declared license vs stated intended use (commercial)",
                        f"'{record.get('license')}' for '{record.get('title', '')[:60]}' is not "
                        "compatible with a commercial intended use",
                    )
                )
                warnings.append(
                    f"License conflict for intended commercial use: {record.get('title', '')[:60]}."
                )

    access_levels = {(record.get("access") or "unknown") for record in datasets}
    checks.append(
        _check(
            "access_compatibility",
            VERIFIED if access_levels <= {"open"} else WARNING,
            "comparison of declared access levels",
            f"access levels: {', '.join(sorted(access_levels))}",
        )
    )
    restricted = [record["title"] for record in datasets if record.get("access") == "restricted"]
    if restricted:
        warnings.append(
            f"{len(restricted)} selected dataset(s) are access-restricted; automated download is not "
            "attempted and access must be obtained from the provider."
        )

    years = [record.get("year") for record in datasets if record.get("year")]
    if years:
        spread = max(years) - min(years)
        checks.append(
            _check(
                "temporal_span",
                VERIFIED if spread <= 10 else WARNING,
                "difference between the oldest and newest stated collection/publication year",
                f"stated years span {min(years)}–{max(years)} ({spread} year(s))",
                {"min_year": min(years), "max_year": max(years)},
            )
        )
        if spread > 10:
            warnings.append(
                "The selection spans more than ten years; collection conditions and definitions may "
                "differ, and unit or label drift cannot be excluded from metadata alone."
            )

    geographies = {(record.get("dataset") or {}).get("geography") or "" for record in datasets}
    geographies.discard("")
    if len(geographies) > 1:
        checks.append(
            _check(
                "population_geography",
                WARNING,
                "comparison of stated geographies",
                f"different geographies stated: {', '.join(sorted(geographies))}",
            )
        )
        warnings.append(
            "Selected datasets describe different populations/geographies; distribution shift is "
            "possible and must be assessed before pooling."
        )
    else:
        checks.append(
            _check(
                "population_geography",
                UNKNOWN,
                "comparison of stated geographies",
                "geography/population is not published for at least one dataset, so distribution "
                "comparability is unknown",
            )
        )

    overlaps = _contamination_check(datasets)
    checks.append(overlaps)
    if overlaps.status == WARNING:
        warnings.append(overlaps.detail)

    schema_note = _schema_compatibility(datasets)
    checks.append(schema_note)

    unknowns = [check.name for check in checks if check.status == UNKNOWN]
    return {
        "datasets": [
            {
                "record_id": record.get("id"),
                "title": record.get("title"),
                "source": record.get("source"),
                "modality": record.get("modality") or (record.get("dataset") or {}).get("modality") or "",
                "license": record.get("license") or "",
                "access": record.get("access") or "unknown",
            }
            for record in datasets
        ],
        "intended_use": intended_use,
        "checks": [check.as_dict() for check in checks],
        "warnings": warnings,
        "dimensions_not_established": unknowns,
        "statement": (
            "This report shows individual dimensions and the criteria used. It is not a single "
            "reliability score, and dimensions marked 'unknown' were not established from available "
            "evidence."
        ),
    }


def _contamination_check(datasets: list[dict]) -> Check:
    identifiers: dict[str, list[str]] = {}
    for record in datasets:
        dataset = record.get("dataset") or {}
        for key in ("doi", "repo_id"):
            value = dataset.get(key)
            if value:
                identifiers.setdefault(str(value).lower(), []).append(record.get("title", ""))
    duplicates = {key: titles for key, titles in identifiers.items() if len(titles) > 1}
    if duplicates:
        return _check(
            "train_test_contamination",
            WARNING,
            "shared repository/DOI identifiers across the selection",
            "overlapping or identical sources detected: "
            + "; ".join(f"{key} → {len(titles)} records" for key, titles in duplicates.items()),
        )
    return _check(
        "train_test_contamination",
        UNKNOWN,
        "shared repository/DOI identifiers across the selection",
        "no shared identifiers were found, but record-level overlap cannot be excluded without the "
        "actual file contents",
    )


def _schema_compatibility(datasets: list[dict]) -> Check:
    schemas = []
    for record in datasets:
        dataset = record.get("dataset") or {}
        features = dataset.get("labels") or dataset.get("features") or []
        if features:
            schemas.append({str(feature).lower() for feature in features})
    if len(schemas) < 2:
        return _check(
            "schema_compatibility",
            UNKNOWN,
            "comparison of published schema/feature lists",
            "fewer than two datasets publish a schema, so field-level compatibility is unknown",
        )
    common = set.intersection(*schemas)
    union = set.union(*schemas)
    ratio = len(common) / len(union) if union else 0.0
    return _check(
        "schema_compatibility",
        VERIFIED if ratio >= 0.5 else WARNING,
        "comparison of published schema/feature lists",
        f"{len(common)} of {len(union)} published fields are shared (overlap {ratio:.2f})",
        {"shared_fields": sorted(common), "overlap_ratio": round(ratio, 3)},
    )


# ---------------------------------------------------------------------------
# full dataset report
# ---------------------------------------------------------------------------


def assess_dataset(record: dict, local_files: list[str] | None = None, intended_use: str = "") -> dict:
    metadata_report = assess_metadata(record)
    file_reports = [inspect_file(path, metadata_report.get("modality", "")) for path in (local_files or [])]

    all_checks = list(metadata_report["checks"])
    for report in file_reports:
        all_checks.extend(report["checks"])

    dimensions = []
    for report in file_reports:
        dimensions.extend(report.get("dimensions") or [])

    state = "metadata_verified" if metadata_report["checks"] else "discovered"
    if any(
        check["name"] == "file_present" and check["status"] == VERIFIED for check in all_checks
    ):
        state = "contents_inspected"

    return {
        "record_id": record.get("id"),
        "title": record.get("title"),
        "source": record.get("source"),
        "modality": metadata_report.get("modality", ""),
        "checks": all_checks,
        "dimensions": dimensions,
        "file_reports": file_reports,
        "warnings": metadata_report["warnings"],
        "unknowns": [
            check["name"] for check in all_checks if check["status"] in {UNKNOWN, NOT_APPLICABLE}
        ],
        "achieved_state": state,
        "intended_use": intended_use,
        "statement": (
            "Every row states the criterion that was applied. 'unknown' means the evidence was not "
            "available — it does not mean the dataset is good or bad."
        ),
    }


def _check(
    name: str, status: str, method: str, detail: str, evidence: dict | None = None
) -> Check:
    return Check(name=name, status=status, method=method, detail=detail, evidence=evidence or {})
