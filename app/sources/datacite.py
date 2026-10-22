"""DataCite adapter — DOI metadata for datasets, software and other research
outputs, and an independent DOI-resolution path used by verification.

Docs: https://support.datacite.org/docs/api
Search:  GET /dois?query=&page[size]=&page[number]=
Resolve: GET /dois/{doi}
"""

from __future__ import annotations

from typing import Any

from .base import DATASETS, REPOSITORY, AdapterError, NormalizedRecord, SearchPage, SearchQuery, SourceAdapter

BASE = "https://api.datacite.org/dois"

RESOURCE_TYPES_DATASET = {"dataset", "software", "collection", "text", "image", "audiovisual", "model"}


class DataCiteAdapter(SourceAdapter):
    name = "datacite"
    label = "DataCite"
    kind = "dataset"
    families = (DATASETS, REPOSITORY)
    homepage = "https://datacite.org"
    docs_url = "https://support.datacite.org/docs/api"
    supports_datasets = True
    notes = (
        "DOI registration agency for research data. Useful for resolving dataset "
        "DOIs and for independent cross-checks of repository metadata."
    )

    def search(self, query: SearchQuery) -> SearchPage:
        size = min(max(query.limit, 1), 100)
        page = (query.offset // size) + 1
        params: dict[str, Any] = {
            "query": query.text,
            "page[size]": size,
            "page[number]": page,
        }
        if query.intent == "datasets":
            params["resource-type-id"] = "dataset"
        payload = self._fetch_json(BASE, params=params)
        data = (payload or {}).get("data") or []
        meta = (payload or {}).get("meta") or {}
        total = meta.get("total")

        records = [r for r in (self._to_record(item) for item in data) if r]
        next_offset = None
        if isinstance(total, int) and page * size < min(total, 10000):
            next_offset = page * size
        return SearchPage(
            records=records,
            total_available=total,
            next_offset=next_offset,
            raw_result_count=len(data),
        )

    def resolve_doi(self, doi: str) -> dict | None:
        """Resolve a dataset/DOI record. Returns None when DataCite has no record."""
        try:
            payload = self._fetch_json(f"{BASE}/{doi}")
        except AdapterError as exc:
            if "404" in str(exc):
                return None
            raise
        data = (payload or {}).get("data")
        return data.get("attributes") if isinstance(data, dict) else None

    def describe(self) -> dict:
        info = super().describe()
        info["capabilities"] = ["search", "doi_resolution"]
        return info

    def _to_record(self, item: dict) -> NormalizedRecord | None:
        attributes = item.get("attributes") or {}
        titles = attributes.get("titles") or []
        title = self._clean_text(titles[0].get("title") if titles else "")
        if not title:
            return None

        doi = (attributes.get("doi") or item.get("id") or "").lower()
        resource_type = (attributes.get("types") or {}).get("resourceTypeGeneral") or ""
        descriptions = attributes.get("descriptions") or []
        abstract = ""
        for description in descriptions:
            if (description.get("descriptionType") or "").lower() in {"abstract", "other", ""}:
                abstract = self._clean_text(description.get("description"))
                break

        rights = attributes.get("rightsList") or []
        license_text = ""
        for right in rights:
            license_text = right.get("rightsIdentifier") or right.get("rights") or right.get("rightsUri") or ""
            if license_text:
                break

        formats = [self._clean_text(f) for f in (attributes.get("formats") or [])]
        sizes = attributes.get("sizes") or []
        related = attributes.get("relatedIdentifiers") or []
        related_publications = [
            rel.get("relatedIdentifier")
            for rel in related
            if isinstance(rel, dict)
            and (rel.get("relationType") or "") in {"IsSupplementTo", "IsDocumentedBy", "Cites"}
        ][:25]

        is_dataset = resource_type.lower() in RESOURCE_TYPES_DATASET
        dataset_meta = {}
        if is_dataset:
            dataset_meta = {
                "publisher": attributes.get("publisher") or "",
                "creators": [c.get("name") for c in (attributes.get("creators") or []) if c.get("name")][:40],
                "samples": None,
                "format": ", ".join(formats[:6]),
                "size_declared": ", ".join(str(s) for s in sizes[:6]),
                "files": [],
                "download_url": attributes.get("url") or "",
                "source_record_url": f"https://doi.org/{doi}" if doi else "",
                "version": attributes.get("version") or "",
                "updated": attributes.get("updated") or "",
                "doi": doi,
                "related_publications": related_publications,
                "resource_type": resource_type,
            }

        return NormalizedRecord(
            source=self.name,
            source_id=doi or title[:60],
            kind="dataset" if is_dataset else "paper",
            title=title,
            doi=doi,
            abstract=abstract,
            authors=[c.get("name") for c in (attributes.get("creators") or []) if c.get("name")][:40],
            year=attributes.get("publicationYear"),
            published_date=(attributes.get("published") or attributes.get("registered") or "")[:10] or None,
            venue=attributes.get("publisher") or "",
            publisher=attributes.get("publisher") or "",
            url=attributes.get("url") or (f"https://doi.org/{doi}" if doi else ""),
            license=str(license_text),
            access="open" if license_text else "unknown",
            concepts=[self._clean_text(s.get("subject")) for s in (attributes.get("subjects") or [])][:10],
            modality="",
            dataset=dataset_meta,
            raw={
                "resource_type": resource_type,
                "registered": attributes.get("registered"),
                "state": attributes.get("state"),
            },
            retrieved_at="",
            source_family="datasets",
        )
