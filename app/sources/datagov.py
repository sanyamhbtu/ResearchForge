"""Data.gov adapter — US federal open-data catalog.

Docs consulted (verified live on 2026-10-22):
  * CKAN Action API: https://docs.ckan.org/en/latest/api/
  * Data.gov Catalog API (current): https://resources.data.gov/catalog-api/

Access mechanism (deterministic fallback chain):

  1. Legacy CKAN Action API (as documented by Data.gov):
     GET https://catalog.data.gov/api/3/action/package_search?q=&rows=&start=
       -> {"success":true,"result":{"count":N,"results":[{title,name,notes,url,
           license_title,license_id,organization:{title},tags:[{name}],
           metadata_modified,resources:[{name,url,format,size,mimetype,...}],
           extras:[...]}]}}
     On 2026-10-22 this endpoint answered HTTP 404 (the catalog API has been
     migrated), so it is attempted first and used when available.

  2. Current GSA-hosted Catalog API (replaces the CKAN API) — used as the
     fallback and the one that serves live data today:
     GET https://api.gsa.gov/technology/datagov/v4/search?q=&per_page=&after=
       Header: X-Api-Key: <key>
       -> {"after": <cursor>, "sort": ..., "results":[{title, description,
           identifier, slug, publisher, keyword[], theme[], organization{...},
           dcat{title,description,accessLevel,license,issued,modified,
                landingPage,distribution:[{downloadURL,mediaType,title?}],...}}]}
     ``X-Api-Key`` comes from ``get_settings().datagov_api_key`` when configured;
     otherwise the officially documented ``DEMO_KEY`` is used (low rate limits)
     and a warning is attached to the page.

If both mechanisms fail, ``AdapterError`` is raised — no fabricated records.

Pagination: CKAN uses ``rows`` (page size) + ``start`` (offset). The v4 API is
cursor-based (``after``), so this adapter walks cursor pages until it has
consumed ``offset`` and collected ``limit`` records, which keeps real offset
semantics.
"""

from __future__ import annotations

from typing import Any

from ..config import get_settings
from .base import (
    DATASETS,
    GOVERNMENT,
    AdapterError,
    NormalizedRecord,
    SearchPage,
    SearchQuery,
    SourceAdapter,
)

CKAN_URL = "https://catalog.data.gov/api/3/action/package_search"
V4_URL = "https://api.gsa.gov/technology/datagov/v4/search"
DEMO_KEY = "DEMO_KEY"
MAX_ROWS = 100
MAX_CURSOR_PAGES = 10


class DataGovAdapter(SourceAdapter):
    name = "datagov"
    label = "Data.gov (US federal open data)"
    kind = "dataset"
    families = (GOVERNMENT, DATASETS)
    homepage = "https://catalog.data.gov"
    docs_url = "https://resources.data.gov/catalog-api/"
    supports_datasets = True
    credential_hint = "DATAGOV_API_KEY (optional; the shared DEMO_KEY is used otherwise)"
    notes = (
        "US federal open-data catalog. Uses the legacy CKAN Action API when it "
        "is reachable and falls back to the current GSA Catalog API (v4)."
    )

    def credentials_present(self) -> bool:
        return True  # anonymous/DEMO_KEY access is supported

    def _api_key(self) -> str:
        return get_settings().datagov_api_key or DEMO_KEY

    def search(self, query: SearchQuery) -> SearchPage:
        ckan_page = self._search_ckan(query)
        if ckan_page is not None:
            return ckan_page
        v4_page = self._search_v4(query)
        if v4_page is not None:
            return v4_page
        raise AdapterError(
            self.name,
            "Data.gov catalog is unreachable: both the CKAN Action API and the "
            "GSA Catalog API (v4) failed.",
            detail=CKAN_URL,
        )

    # -- mechanism 1: legacy CKAN Action API ------------------------------
    def _search_ckan(self, query: SearchQuery) -> SearchPage | None:
        rows = min(max(query.limit, 1), MAX_ROWS)
        params: dict[str, Any] = {"q": query.text, "rows": rows, "start": query.offset}
        try:
            payload = self._fetch_json(CKAN_URL, params=params, headers=self._ckan_headers())
        except AdapterError:
            return None
        if not isinstance(payload, dict) or not payload.get("success"):
            return None
        result = payload.get("result") or {}
        items = result.get("results")
        if not isinstance(items, list):
            return None
        total = result.get("count")
        records = [r for r in (self._ckan_record(item) for item in items) if r]
        next_offset = None
        if isinstance(total, int) and query.offset + rows < total:
            next_offset = query.offset + rows
        return SearchPage(
            records=records,
            total_available=total if isinstance(total, int) else None,
            next_offset=next_offset,
            raw_result_count=len(items),
        )

    def _ckan_headers(self) -> dict[str, str]:
        api_key = get_settings().datagov_api_key
        return {"X-Api-Key": api_key} if api_key else {}

    def _ckan_record(self, item: dict) -> NormalizedRecord | None:
        title = self._clean_text(item.get("title"))
        name = item.get("name") or ""
        if not title and not name:
            return None
        if not title:
            title = name

        files: list[dict] = []
        formats: set[str] = set()
        for resource in item.get("resources") or []:
            if not isinstance(resource, dict):
                continue
            fmt = (resource.get("format") or "").strip()
            if fmt:
                formats.add(fmt.upper())
            size = resource.get("size")
            files.append(
                {
                    "name": resource.get("name") or "",
                    "url": resource.get("url") or "",
                    "size_bytes": size if isinstance(size, int) else None,
                    "format": fmt,
                    "mimetype": resource.get("mimetype") or "",
                    "last_modified": resource.get("last_modified") or "",
                    "hash": resource.get("hash") or "",
                }
            )

        organization = item.get("organization") or {}
        publisher = self._clean_text(organization.get("title"))
        license_text = self._clean_text(item.get("license_title")) or self._clean_text(
            item.get("license_id")
        )
        tags = [self._clean_text(t.get("name")) for t in (item.get("tags") or []) if t.get("name")]
        extras = {
            e.get("key"): e.get("value")
            for e in (item.get("extras") or [])
            if isinstance(e, dict) and e.get("key")
        }
        record_url = f"https://catalog.data.gov/dataset/{name}" if name else ""

        dataset_meta = {
            "source_record_url": record_url,
            "files": files[:200],
            "download_url": files[0]["url"] if files else "",
            "format": ", ".join(sorted(formats)),
            "samples": None,
            "size_bytes": None,
            "updated": item.get("metadata_modified") or "",
            "publisher": publisher,
            "license": license_text,
            "tags": tags[:30],
            "extras": extras,
            "notes": "US federal open-data catalog record (CKAN).",
        }

        return NormalizedRecord(
            source=self.name,
            source_id=name or str(item.get("id") or title[:60]),
            kind="dataset",
            title=title,
            abstract=self._clean_text(item.get("notes")),
            authors=[],
            year=None,
            published_date=None,
            venue="Data.gov",
            publisher=publisher,
            url=record_url or (item.get("url") or ""),
            pdf_url="",
            license=license_text,
            access="open",
            citation_count=None,
            concepts=tags[:12],
            modality="",
            dataset=dataset_meta,
            raw={
                "identifier": item.get("id"),
                "name": name,
                "url": item.get("url"),
                "license_id": item.get("license_id"),
                "license_title": item.get("license_title"),
                "metadata_created": item.get("metadata_created"),
                "metadata_modified": item.get("metadata_modified"),
                "organization": organization,
                "num_resources": item.get("num_resources"),
                "type": item.get("type"),
            },
            retrieved_at="",
            source_family=GOVERNMENT,
        )

    # -- mechanism 2: current GSA Catalog API (v4) ------------------------
    def _search_v4(self, query: SearchQuery) -> SearchPage | None:
        page_size = min(max(query.limit, 1), MAX_ROWS)
        needed = query.offset + page_size
        headers = {"X-Api-Key": self._api_key()}
        collected: list[dict] = []
        cursor: str | None = None
        exhausted = False
        pages = 0
        while len(collected) < needed and pages < MAX_CURSOR_PAGES:
            params: dict[str, Any] = {
                "q": query.text,
                "per_page": page_size,
                "sort": "relevance",
            }
            if cursor:
                params["after"] = cursor
            try:
                payload = self._fetch_json(V4_URL, params=params, headers=headers)
            except AdapterError:
                return None
            if not isinstance(payload, dict):
                return None
            results = payload.get("results")
            if not isinstance(results, list) or not results:
                exhausted = True
                break
            collected.extend(r for r in results if isinstance(r, dict))
            cursor = payload.get("after")
            pages += 1
            if not cursor:
                exhausted = True
                break

        window = collected[query.offset : query.offset + page_size]
        records = [r for r in (self._v4_record(item) for item in window) if r]

        warnings: list[str] = []
        if not get_settings().datagov_api_key:
            warnings.append(
                "Data.gov v4 API called with the shared DEMO_KEY; set "
                "DATAGOV_API_KEY for reliable rate limits."
            )
        if len(collected) < needed and not exhausted:
            warnings.append(
                "Stopped paginating the Data.gov cursor after "
                f"{MAX_CURSOR_PAGES} pages; deeper offsets are not covered."
            )

        if exhausted:
            total = len(collected)
        else:
            total = None
        next_offset = query.offset + len(window) if cursor else None
        return SearchPage(
            records=records,
            total_available=total,
            next_offset=next_offset,
            warnings=warnings,
            raw_result_count=len(collected),
        )

    def _v4_record(self, item: dict) -> NormalizedRecord | None:
        dcat = item.get("dcat") or {}
        title = self._clean_text(item.get("title") or dcat.get("title"))
        identifier = item.get("identifier") or dcat.get("identifier")
        slug = item.get("slug") or ""
        if not title and not identifier and not slug:
            return None
        if not title:
            title = slug or str(identifier)

        files: list[dict] = []
        formats: set[str] = set()
        for distribution in dcat.get("distribution") or []:
            if not isinstance(distribution, dict):
                continue
            url = distribution.get("downloadURL") or distribution.get("accessURL") or ""
            media_type = distribution.get("mediaType") or ""
            fmt = media_type.split("/")[-1] if media_type else ""
            if not fmt and url:
                fmt = url.rsplit(".", 1)[-1].split("?")[0] if "." in url else ""
            if fmt:
                formats.add(fmt.upper())
            name = distribution.get("title") or (url.rsplit("/", 1)[-1] if url else "")
            files.append(
                {
                    "name": name,
                    "url": url,
                    "size_bytes": None,
                    "format": fmt,
                    "media_type": media_type,
                    "described_by": distribution.get("describedBy") or "",
                }
            )

        organization = item.get("organization") or {}
        publisher = self._clean_text(
            organization.get("name") or item.get("publisher") or (dcat.get("publisher") or {}).get("name")
        )
        license_text = self._clean_text(dcat.get("license"))
        keywords = [self._clean_text(k) for k in (item.get("keyword") or dcat.get("keyword") or [])]
        themes = [self._clean_text(t) for t in (item.get("theme") or dcat.get("theme") or [])]

        access_level = (dcat.get("accessLevel") or item.get("access_level") or "").lower()
        if access_level == "public":
            access = "open"
        elif access_level in {"restricted public", "non-public"}:
            access = "restricted"
        else:
            access = "open"  # a published catalog record with no stated restriction

        record_url = dcat.get("landingPage") or (
            f"https://catalog.data.gov/dataset/{slug}" if slug else ""
        )
        issued = dcat.get("issued")
        modified = dcat.get("modified") or item.get("last_harvested_date")

        dataset_meta = {
            "source_record_url": record_url,
            "files": files[:200],
            "download_url": files[0]["url"] if files else "",
            "format": ", ".join(sorted(formats)),
            "samples": None,
            "size_bytes": None,
            "updated": modified or "",
            "publisher": publisher,
            "license": license_text,
            "tags": (keywords + themes)[:30],
            "temporal": dcat.get("temporal") or "",
            "spatial": dcat.get("spatial") or "",
            "accrual_periodicity": dcat.get("accrualPeriodicity") or "",
            "organization": organization,
            "harvest_record": item.get("harvest_record") or "",
            "notes": "US federal open-data catalog record (GSA Catalog API v4).",
        }

        return NormalizedRecord(
            source=self.name,
            source_id=str(identifier or slug or title[:60]),
            kind="dataset",
            title=title,
            abstract=self._clean_text(item.get("description") or dcat.get("description")),
            authors=[],
            year=self._year_from(issued),
            published_date=(issued or modified) or None,
            venue="Data.gov",
            publisher=publisher,
            url=record_url,
            pdf_url="",
            license=license_text,
            access=access,
            citation_count=None,
            concepts=(keywords + themes)[:12],
            modality="",
            dataset=dataset_meta,
            raw={
                "identifier": identifier,
                "slug": slug,
                "access_level": item.get("access_level"),
                "dcat_access_level": dcat.get("accessLevel"),
                "popularity": item.get("popularity"),
                "type": item.get("type"),
                "last_harvested_date": item.get("last_harvested_date"),
                "harvest_record": item.get("harvest_record"),
            },
            retrieved_at="",
            source_family=GOVERNMENT,
        )
