"""Crossref adapter — authoritative bibliographic metadata and DOI resolution.

Docs: https://api.crossref.org/swagger-ui/index.html
The ``/works/{doi}`` endpoint is the primary tool used by the verification
pipeline to confirm that a DOI exists and matches a title.
"""

from __future__ import annotations

from typing import Any

from ..config import get_settings
from .base import AdapterError, LITERATURE, NormalizedRecord, SearchPage, SearchQuery, SourceAdapter

BASE = "https://api.crossref.org/works"
MEMBER_AGENT = "ResearchForge/1.0 (research verification; mailto:{email})"


class CrossrefAdapter(SourceAdapter):
    name = "crossref"
    label = "Crossref"
    kind = "paper"
    families = (LITERATURE,)
    homepage = "https://www.crossref.org"
    docs_url = "https://api.crossref.org/swagger-ui/index.html"
    notes = (
        "Registered DOI metadata (publisher-deposited). Provide RF_CONTACT_EMAIL "
        "to join the polite pool."
    )

    def search(self, query: SearchQuery) -> SearchPage:
        settings = get_settings()
        rows = min(max(query.limit, 1), 100)
        params: dict[str, Any] = {
            "query.bibliographic": query.text,
            "rows": rows,
            "offset": query.offset,
            "select": (
                "DOI,title,abstract,author,issued,container-title,publisher,"
                "license,URL,link,is-referenced-by-count,reference,subject,type,"
                "created,indexed"
            ),
            "mailto": settings.contact_email or None,
        }
        filters: list[str] = []
        if query.filters.get("from_year"):
            filters.append(f"from-pub-date:{query.filters['from_year']}-01-01")
        if query.filters.get("to_year"):
            filters.append(f"until-pub-date:{query.filters['to_year']}-12-31")
        if query.filters.get("crossref_type"):
            filters.append(f"type:{query.filters['crossref_type']}")
        if filters:
            params["filter"] = ",".join(filters)

        payload = self._fetch_json(BASE, params=params)
        message = (payload or {}).get("message") or {}
        items = message.get("items") or []
        total = message.get("total-results")

        records = [r for r in (self._to_record(item) for item in items) if r]
        next_offset = None
        if isinstance(total, int) and query.offset + rows < min(total, 10000):
            next_offset = query.offset + rows

        return SearchPage(
            records=records,
            total_available=total,
            next_offset=next_offset,
            raw_result_count=len(items),
        )

    def resolve_doi(self, doi: str) -> dict | None:
        """Fetch authoritative metadata for a DOI. Returns None for 404."""
        from ..http_client import HttpError

        try:
            payload = self._fetch_json(f"{BASE}/{doi}")
        except AdapterError as exc:
            if "404" in str(exc):
                return None
            raise
        except HttpError:
            return None
        return (payload or {}).get("message")

    def describe(self) -> dict:
        info = super().describe()
        info["capabilities"] = ["search", "doi_resolution"]
        return info

    # -- mapping -----------------------------------------------------------
    def _to_record(self, item: dict) -> NormalizedRecord | None:
        titles = item.get("title") or []
        title = self._clean_text(titles[0] if titles else "")
        if not title:
            return None
        doi = (item.get("DOI") or "").lower()
        issued = _date_parts(item.get("issued"))
        authors = []
        for author in (item.get("author") or [])[:60]:
            name = " ".join(
                part
                for part in [
                    self._clean_text(author.get("given")),
                    self._clean_text(author.get("family")),
                ]
                if part
            )
            if name:
                authors.append(name)
        licenses = item.get("license") or []
        license_url = licenses[0].get("URL") if licenses else ""
        pdf_url = ""
        for link in item.get("link") or []:
            if (link.get("content-type") or "").endswith("pdf"):
                pdf_url = link.get("URL") or ""
                break
        references = [
            (ref.get("DOI") or "").lower()
            for ref in (item.get("reference") or [])
            if ref.get("DOI")
        ]
        container = item.get("container-title") or []
        return NormalizedRecord(
            source=self.name,
            source_id=doi or self._clean_text(item.get("URL")),
            kind="paper",
            title=title,
            doi=doi,
            abstract=self._clean_text(item.get("abstract")),
            authors=authors,
            year=issued.get("year"),
            published_date=issued.get("date"),
            venue=self._clean_text(container[0] if container else ""),
            publisher=self._clean_text(item.get("publisher")),
            url=item.get("URL") or (f"https://doi.org/{doi}" if doi else ""),
            pdf_url=pdf_url,
            license=_normalize_license_url(license_url),
            access="open" if license_url else "unknown",
            citation_count=item.get("is-referenced-by-count"),
            reference_dois=references,
            concepts=[self._clean_text(s) for s in (item.get("subject") or [])[:10]],
            modality="",
            raw={
                "crossref_type": item.get("type"),
                "deposited": (item.get("created") or {}).get("date-time"),
                "indexed": (item.get("indexed") or {}).get("date-time"),
            },
            retrieved_at=(item.get("indexed") or {}).get("date-time") or "",
            source_family=LITERATURE,
        )


def _date_parts(value: Any) -> dict:
    if not value or not isinstance(value, dict):
        return {}
    parts = value.get("date-parts") or []
    if not parts or not parts[0]:
        return {}
    first = parts[0]
    year = first[0] if len(first) > 0 else None
    month = first[1] if len(first) > 1 else 1
    day = first[2] if len(first) > 2 else 1
    if not year:
        return {}
    try:
        return {"year": int(year), "date": f"{int(year):04d}-{int(month):02d}-{int(day):02d}"}
    except (TypeError, ValueError):
        return {"year": int(year), "date": None}


def _normalize_license_url(url: str) -> str:
    if not url:
        return ""
    text = url.strip().rstrip("/")
    if "creativecommons.org/licenses/" in text:
        tail = text.split("creativecommons.org/licenses/")[-1].replace("/", "-")
        return f"CC-{tail.upper()}"
    if "creativecommons.org/publicdomain/" in text:
        return "CC0-1.0"
    return text
