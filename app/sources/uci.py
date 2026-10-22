"""UCI Machine Learning Repository adapter — classic tabular ML datasets.

Docs consulted (verified live on 2026-10-22):
  * https://archive.ics.uci.edu/                     (repository home)
  * https://archive.ics.uci.edu/api/datasets/list    (JSON catalogue: search/filter)
  * https://archive.ics.uci.edu/api/dataset          (JSON detail: id=<int> or name=<str>)
  * https://github.com/uci-ml-repo/ucimlrepo         (official client, fetch.py)

Access mechanism
----------------
The modern site under https://archive.ics.uci.edu/datasets is a JavaScript app.
The reliable, documented programmatic access is the pair of JSON endpoints used
by the official ``ucimlrepo`` client:

  1. ``GET /api/datasets/list?search=<q>&area=<area>``
     -> ``{"status":200, "data":[{"id":53,"name":"Iris"}, ...]}``
  2. ``GET /api/dataset?id=<int>`` (or ``?name=<str>``)
     -> ``{"status":200, "data":{uci_id, name, repository_url, data_url,
        abstract, area, tasks, characteristics, num_instances, num_features,
        feature_types, target_col, has_missing_values, year_of_dataset_creation,
        last_updated, dataset_doi, creators, variables, additional_info, ...}}``

Deterministic fallback chain:
  1. JSON list endpoint (primary) — full name search, then per-dataset detail.
  2. JSON detail-by-name endpoint (``?name=<query>``) — used when the list
     endpoint fails; yields a single dataset record.
  3. If both fail, ``AdapterError`` is raised — no fabricated records.

The legacy directory index under ``/ml/machine-learning-databases/`` returns
HTTP 404 for live requests, so it is intentionally not used as a fallback.

Pagination: the list endpoint returns the whole match set, so ``offset`` and
``limit`` are applied by slicing that list (the repository holds only a few
hundred datasets). ``num_instances`` is reported only when the detail payload
actually contains it.

License: the JSON API exposes no license field, so ``license`` is left empty
rather than assuming CC-BY-4.0.
"""

from __future__ import annotations

from typing import Any

from .base import (
    DATASETS,
    REPOSITORY,
    AdapterError,
    NormalizedRecord,
    SearchPage,
    SearchQuery,
    SourceAdapter,
)

LIST_URL = "https://archive.ics.uci.edu/api/datasets/list"
DETAIL_URL = "https://archive.ics.uci.edu/api/dataset"
SOURCE_HOME = "https://archive.ics.uci.edu/datasets"


class UciAdapter(SourceAdapter):
    name = "uci"
    label = "UCI Machine Learning Repository"
    kind = "dataset"
    families = (DATASETS, REPOSITORY)
    homepage = "https://archive.ics.uci.edu"
    docs_url = "https://archive.ics.uci.edu/api/datasets/list"
    supports_datasets = True
    notes = (
        "Benchmark tabular datasets with instance/feature counts, task types "
        "and a canonical CSV per dataset. No license is published by the API."
    )

    def search(self, query: SearchQuery) -> SearchPage:
        matches, warning = self._list_datasets(query.text)
        if matches is not None:
            return self._page_from_matches(query, matches, warning)
        return self._fallback_single(query)

    # -- primary: JSON list endpoint --------------------------------------
    def _list_datasets(self, text: str) -> tuple[list[dict] | None, str]:
        """Return ``(matches, warning)``; ``matches`` is None when unusable."""
        payload = self._fetch_json(LIST_URL, params={"search": text})
        payload = payload or {}
        if payload.get("status") != 200 or not isinstance(payload.get("data"), list):
            return None, ""
        matches = [m for m in payload["data"] if isinstance(m, dict) and m.get("id") is not None]
        return matches, ""

    def _page_from_matches(
        self, query: SearchQuery, matches: list[dict], warning: str
    ) -> SearchPage:
        total = len(matches)
        window = matches[query.offset : query.offset + query.limit]
        records: list[NormalizedRecord] = []
        warnings: list[str] = [warning] if warning else []
        detail_failures = 0
        for match in window:
            detail = self._fetch_detail(match.get("id"))
            if detail is None:
                detail_failures += 1
                record = self._build_record(
                    {}, fallback_id=match.get("id"), fallback_name=match.get("name")
                )
            else:
                record = self._build_record(detail)
            if record:
                records.append(record)
        if detail_failures:
            warnings.append(
                f"{detail_failures} dataset detail(s) could not be fetched; "
                "records carry catalogue-level fields only."
            )
        next_offset = query.offset + query.limit if query.offset + query.limit < total else None
        return SearchPage(
            records=records,
            total_available=total,
            next_offset=next_offset,
            warnings=warnings,
            raw_result_count=len(matches),
        )

    # -- fallback: JSON detail-by-name endpoint ---------------------------
    def _fallback_single(self, query: SearchQuery) -> SearchPage:
        payload: Any = None
        try:
            payload = self._fetch_json(DETAIL_URL, params={"name": query.text})
        except AdapterError:
            payload = None
        detail = (payload or {}).get("data") if isinstance(payload, dict) else None
        if isinstance(detail, dict) and detail.get("name"):
            record = self._build_record(detail)
            if record:
                return SearchPage(
                    records=[record],
                    total_available=1,
                    next_offset=None,
                    warnings=[
                        "UCI list endpoint unavailable; resolved the query to a "
                        "single dataset via the detail endpoint."
                    ],
                    raw_result_count=1,
                )
        raise AdapterError(
            self.name,
            "UCI Machine Learning Repository is unreachable: neither the list "
            "endpoint nor the detail endpoint returned a usable dataset.",
            detail=LIST_URL,
        )

    def _fetch_detail(self, dataset_id: Any) -> dict | None:
        try:
            payload = self._fetch_json(DETAIL_URL, params={"id": dataset_id})
        except AdapterError:
            return None
        detail = (payload or {}).get("data") if isinstance(payload, dict) else None
        if isinstance(detail, dict) and detail.get("name"):
            return detail
        return None

    # -- mapping -----------------------------------------------------------
    def _build_record(
        self, detail: dict, *, fallback_id: Any = None, fallback_name: Any = None
    ) -> NormalizedRecord | None:
        uci_id = detail.get("uci_id") or fallback_id
        name = self._clean_text(detail.get("name") or fallback_name)
        if not name:
            return None
        record_url = detail.get("repository_url") or (
            f"https://archive.ics.uci.edu/dataset/{uci_id}" if uci_id is not None else ""
        )
        data_url = detail.get("data_url") or ""
        abstract = self._clean_text(detail.get("abstract"))
        if not abstract:
            abstract = self._clean_text((detail.get("additional_info") or {}).get("summary"))

        files: list[dict] = []
        files_base_url = ""
        if data_url:
            filename = data_url.rsplit("/", 1)[-1]
            extension = filename.rsplit(".", 1)[-1].upper() if "." in filename else ""
            files.append(
                {
                    "name": filename,
                    "url": data_url,
                    "size_bytes": None,
                    "format": extension,
                }
            )
            files_base_url = data_url.rsplit("/", 1)[0] + "/"

        variables = detail.get("variables") or []
        feature_names = [
            v.get("name")
            for v in variables
            if isinstance(v, dict) and v.get("role") == "Feature" and v.get("name")
        ]
        concepts = [str(v) for v in (detail.get("tasks") or [])]
        if detail.get("area"):
            concepts.append(str(detail["area"]))
        for characteristic in detail.get("characteristics") or []:
            concepts.append(str(characteristic))

        num_instances = detail.get("num_instances")
        dataset_meta = {
            "source_record_url": record_url,
            "files_base_url": files_base_url,
            "files": files,
            "download_url": data_url,
            "format": ", ".join(f["format"] for f in files if f.get("format")),
            "samples": num_instances if isinstance(num_instances, int) else None,
            "num_features": detail.get("num_features"),
            "feature_names": feature_names[:200],
            "target_col": detail.get("target_col") or [],
            "variables_count": len(variables),
            "creator": detail.get("creators") or [],
            "area": detail.get("area") or "",
            "tasks": detail.get("tasks") or [],
            "characteristics": detail.get("characteristics") or [],
            "has_missing_values": detail.get("has_missing_values"),
            "updated": detail.get("last_updated") or "",
            "doi": detail.get("dataset_doi") or "",
            "publisher": "UCI Machine Learning Repository",
            "license": "",
            "notes": "File listing is limited to the canonical data file exposed "
            "by the repository API; no license is published.",
        }

        return NormalizedRecord(
            source=self.name,
            source_id=str(uci_id) if uci_id is not None else name[:60],
            kind="dataset",
            title=name,
            abstract=abstract,
            authors=[self._clean_text(c) for c in (detail.get("creators") or []) if c][:40],
            year=self._year_from(detail.get("year_of_dataset_creation")),
            published_date=None,
            venue="UCI Machine Learning Repository",
            publisher="UCI Machine Learning Repository",
            url=record_url,
            pdf_url="",
            license="",
            access="open",
            citation_count=None,
            concepts=concepts[:12],
            modality="",
            dataset=dataset_meta,
            raw={
                "uci_id": uci_id,
                "repository_url": record_url,
                "data_url": data_url,
                "dataset_doi": detail.get("dataset_doi"),
                "area": detail.get("area"),
                "tasks": detail.get("tasks"),
                "num_instances": num_instances,
                "num_features": detail.get("num_features"),
                "feature_types": detail.get("feature_types"),
                "last_updated": detail.get("last_updated"),
                "intro_paper": detail.get("intro_paper"),
            },
            retrieved_at="",
            source_family=DATASETS,
        )
