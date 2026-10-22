"""OpenAlex adapter — scholarly works, citation relationships, open-access links.

Docs: https://docs.openalex.org/api-entities/works/search-works
Polite pool: pass ``mailto`` to receive higher rate limits.
"""

from __future__ import annotations

from typing import Any

from ..config import get_settings
from .base import LITERATURE, NormalizedRecord, SearchPage, SearchQuery, SourceAdapter

BASE = "https://api.openalex.org/works"

# OpenAlex type values that describe datasets rather than articles.
DATASET_TYPES = {"dataset", "peer-review", "erratum"}
LICENSE_NORMALIZATION = {
    "cc0": "CC0-1.0",
    "cc-by": "CC-BY-4.0",
    "cc-by-sa": "CC-BY-SA-4.0",
    "cc-by-nc": "CC-BY-NC-4.0",
    "cc-by-nc-sa": "CC-BY-NC-SA-4.0",
    "cc-by-nd": "CC-BY-ND-4.0",
    "public-domain": "Public Domain",
    "mit": "MIT",
    "apache-2.0": "Apache-2.0",
    "gpl": "GPL",
    "other-oa": "Other open license",
}


class OpenAlexAdapter(SourceAdapter):
    name = "openalex"
    label = "OpenAlex"
    kind = "paper"
    families = (LITERATURE,)
    homepage = "https://openalex.org"
    docs_url = "https://docs.openalex.org"
    notes = (
        "Bibliographic records plus citation graph. Anonymous access is allowed; "
        "a contact email raises the rate limit (polite pool)."
    )

    def search(self, query: SearchQuery) -> SearchPage:
        settings = get_settings()
        page_size = min(max(query.limit, 1), 100)
        page = (query.offset // page_size) + 1
        params: dict[str, Any] = {
            "search": query.text,
            "per-page": page_size,
            "page": page,
            "mailto": settings.contact_email or None,
        }
        filters: list[str] = []
        if query.filters.get("from_year"):
            filters.append(f"from_publication_date:{query.filters['from_year']}-01-01")
        if query.filters.get("to_year"):
            filters.append(f"to_publication_date:{query.filters['to_year']}-12-31")
        if query.filters.get("open_access_only"):
            filters.append("is_oa:true")
        if filters:
            params["filter"] = ",".join(filters)

        payload = self._fetch_json(BASE, params=params)
        results = (payload or {}).get("results") or []
        meta = (payload or {}).get("meta") or {}
        total = meta.get("count")

        records: list[NormalizedRecord] = []
        for item in results:
            record = self._to_record(item)
            if record:
                records.append(record)

        next_offset = None
        if isinstance(total, int) and query.offset + page_size < total:
            next_offset = query.offset + page_size
        warnings = []
        if not records and isinstance(total, int) and total > 0:
            warnings.append("OpenAlex reported matches but returned no parsable records.")

        return SearchPage(
            records=records,
            total_available=total,
            next_offset=next_offset,
            warnings=warnings,
            raw_result_count=len(results),
        )

    # -- mapping -----------------------------------------------------------
    def _to_record(self, item: dict) -> NormalizedRecord | None:
        title = self._clean_text(item.get("display_name") or item.get("title"))
        if not title:
            return None
        openalex_id = (item.get("id") or "").rsplit("/", 1)[-1]
        doi = _normalize_doi(item.get("doi"))
        is_dataset = (item.get("type") or "").lower() in DATASET_TYPES
        oa = item.get("open_access") or {}
        best_oa = item.get("best_oa_location") or {}
        primary = item.get("primary_location") or {}
        source_info = primary.get("source") or {}
        license_value = best_oa.get("license") or primary.get("license")

        reference_dois = []
        for ref in (item.get("referenced_works") or [])[:300]:
            ref_id = str(ref).rsplit("/", 1)[-1]
            if ref_id.startswith("W"):
                reference_dois.append(ref_id)  # stored as OpenAlex work ids
            elif ref_id.startswith("10."):
                reference_dois.append(ref_id)

        concepts = []
        for topic in (item.get("topics") or [])[:8]:
            if topic.get("display_name"):
                concepts.append(self._clean_text(topic["display_name"]))
        if not concepts:
            for concept in (item.get("concepts") or [])[:8]:
                if concept.get("display_name"):
                    concepts.append(self._clean_text(concept["display_name"]))

        dataset_meta: dict[str, Any] = {}
        if is_dataset:
            dataset_meta = {
                "format": "",
                "files": [],
                "download_url": best_oa.get("pdf_url") or "",
                "source_record_url": item.get("doi") or item.get("id") or "",
                "version": "",
                "creators": [],
                "samples": None,
                "notes": "OpenAlex classifies this work as a dataset record.",
            }

        return NormalizedRecord(
            source=self.name,
            source_id=openalex_id or doi or title[:80],
            kind="dataset" if is_dataset else "paper",
            title=title,
            doi=doi,
            abstract=reconstruct_abstract(item.get("abstract_inverted_index")),
            authors=[
                self._clean_text((a.get("author") or {}).get("display_name"))
                for a in (item.get("authorships") or [])[:60]
                if (a.get("author") or {}).get("display_name")
            ],
            year=item.get("publication_year"),
            published_date=item.get("publication_date"),
            venue=self._clean_text(source_info.get("display_name")),
            publisher=self._clean_text((source_info.get("host_organization_name")) or ""),
            url=item.get("doi") or item.get("id") or "",
            pdf_url=best_oa.get("pdf_url") or "",
            license=_normalize_license(license_value),
            access="open" if oa.get("is_oa") else ("restricted" if oa.get("is_oa") is False else "unknown"),
            citation_count=item.get("cited_by_count"),
            reference_dois=reference_dois,
            concepts=concepts,
            modality="",
            dataset=dataset_meta,
            raw={
                "openalex_type": item.get("type"),
                "oa_status": oa.get("oa_status"),
                "is_retracted": item.get("is_retracted"),
                "language": item.get("language"),
            },
            retrieved_at=item.get("updated_date") or "",
            source_family=LITERATURE,
        )


def reconstruct_abstract(inverted: dict | None) -> str:
    """OpenAlex returns abstracts as an inverted index; rebuild readable text."""
    if not inverted or not isinstance(inverted, dict):
        return ""
    positions: list[tuple[int, str]] = []
    for word, indexes in inverted.items():
        if not isinstance(indexes, list):
            continue
        for index in indexes:
            if isinstance(index, int):
                positions.append((index, word))
    positions.sort(key=lambda pair: pair[0])
    return " ".join(word for _, word in positions)


def _normalize_doi(value: Any) -> str:
    if not value:
        return ""
    text = str(value).strip()
    for prefix in ("https://doi.org/", "http://doi.org/", "doi:", "https://dx.doi.org/"):
        if text.lower().startswith(prefix):
            text = text[len(prefix):]
    return text.lower()


def _normalize_license(value: Any) -> str:
    if not value:
        return ""
    text = str(value).strip().lower().rstrip("/")
    if text in LICENSE_NORMALIZATION:
        return LICENSE_NORMALIZATION[text]
    if "creativecommons.org" in text:
        tail = text.rsplit("/", 1)[-1] if "/" in text else text
        return LICENSE_NORMALIZATION.get(tail, text)
    return text
