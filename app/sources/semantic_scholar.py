"""Semantic Scholar Graph API adapter.

Docs: https://api.semanticscholar.org/api-docs/graph
The unauthenticated endpoint is aggressively rate limited (HTTP 429); set
SEMANTIC_SCHOLAR_API_KEY in .env to raise the ceiling.
"""

from __future__ import annotations

from typing import Any

from ..config import get_settings
from ..http_client import HttpError
from .base import AdapterError, LITERATURE, NormalizedRecord, SearchPage, SearchQuery, SourceAdapter

BASE = "https://api.semanticscholar.org/graph/v1"
FIELDS = (
    "paperId,title,abstract,year,publicationDate,venue,publicationVenue,"
    "externalIds,openAccessPdf,fieldsOfStudy,s2FieldsOfStudy,citationCount,"
    "referenceCount,influentialCitationCount,authors,publicationTypes,"
    "isOpenAccess,externalIds"
)


class SemanticScholarAdapter(SourceAdapter):
    name = "semantic_scholar"
    label = "Semantic Scholar"
    kind = "paper"
    families = (LITERATURE,)
    homepage = "https://www.semanticscholar.org"
    docs_url = "https://api.semanticscholar.org/api-docs/graph"
    requires_credentials = False
    credential_hint = "SEMANTIC_SCHOLAR_API_KEY (optional; strongly recommended)"
    notes = (
        "Academic discovery with citation counts and reference graph. "
        "Anonymous requests often return HTTP 429; the adapter surfaces this "
        "as a source failure instead of fabricating results."
    )

    def credentials_present(self) -> bool:
        return bool(get_settings().semantic_scholar_api_key)

    def search(self, query: SearchQuery) -> SearchPage:
        limit = min(max(query.limit, 1), 100)
        params: dict[str, Any] = {
            "query": query.text,
            "limit": limit,
            "offset": query.offset,
            "fields": FIELDS,
        }
        headers = {}
        if get_settings().semantic_scholar_api_key:
            headers["x-api-key"] = get_settings().semantic_scholar_api_key
        year_filter = _year_filter(query.filters)
        if year_filter:
            params["year"] = year_filter

        try:
            payload = self._fetch_json(f"{BASE}/paper/search", params=params, headers=headers)
        except AdapterError as exc:
            if "429" in str(exc):
                raise AdapterError(
                    self.name,
                    "rate limited (HTTP 429) — set SEMANTIC_SCHOLAR_API_KEY for higher limits",
                    detail=str(exc),
                ) from exc
            raise

        data = (payload or {}).get("data") or []
        total = (payload or {}).get("total")
        records = [r for r in (self._to_record(item) for item in data) if r]
        next_offset = None
        if total and query.offset + limit < min(int(total), 10000):
            next_offset = query.offset + limit
        warnings = []
        if (payload or {}).get("message"):
            warnings.append(str(payload["message"]))
        return SearchPage(
            records=records,
            total_available=total,
            next_offset=next_offset,
            warnings=warnings,
            raw_result_count=len(data),
        )

    def references(self, paper_id: str, limit: int = 100) -> list[dict]:
        """Follow the citation graph for graph-based gap discovery."""
        try:
            payload = self._fetch_json(
                f"{BASE}/paper/{paper_id}/references",
                params={"limit": min(limit, 500), "fields": "paperId,title,year,externalIds,abstract"},
            )
        except HttpError as exc:
            raise AdapterError(self.name, str(exc)) from exc
        out = []
        for entry in (payload or {}).get("data") or []:
            cited = entry.get("citedPaper") or {}
            if cited.get("title"):
                out.append(cited)
        return out

    def describe(self) -> dict:
        info = super().describe()
        info["capabilities"] = ["search", "citation_graph"]
        return info

    def _to_record(self, item: dict) -> NormalizedRecord | None:
        title = self._clean_text(item.get("title"))
        if not title:
            return None
        external = item.get("externalIds") or {}
        doi = (external.get("DOI") or "").lower()
        oa_pdf = item.get("openAccessPdf") or {}
        venue = self._clean_text(item.get("venue"))
        if not venue:
            venue = self._clean_text((item.get("publicationVenue") or {}).get("name"))
        concepts = [self._clean_text(c) for c in (item.get("fieldsOfStudy") or []) if c]
        return NormalizedRecord(
            source=self.name,
            source_id=item.get("paperId") or doi or title[:80],
            kind="paper",
            title=title,
            doi=doi,
            abstract=self._clean_text(item.get("abstract")),
            authors=[
                self._clean_text((a or {}).get("name"))
                for a in (item.get("authors") or [])[:60]
                if (a or {}).get("name")
            ],
            year=item.get("year"),
            published_date=item.get("publicationDate"),
            venue=venue,
            publisher=self._clean_text((item.get("publicationVenue") or {}).get("publisher")),
            url=f"https://www.semanticscholar.org/paper/{item.get('paperId')}"
            if item.get("paperId")
            else (f"https://doi.org/{doi}" if doi else ""),
            pdf_url=oa_pdf.get("url") or "",
            license=_license_from_pdf_url(oa_pdf.get("url") or ""),
            access="open" if oa_pdf.get("url") else ("restricted" if item.get("isOpenAccess") is False else "unknown"),
            citation_count=item.get("citationCount"),
            reference_dois=[],
            concepts=concepts,
            raw={
                "reference_count": item.get("referenceCount"),
                "influential_citation_count": item.get("influentialCitationCount"),
                "arxiv": external.get("ArXiv"),
                "pubmed": external.get("PubMed"),
                "paper_id": item.get("paperId"),
            },
            retrieved_at="",
            source_family=LITERATURE,
        )


def _year_filter(filters: dict) -> str:
    start = filters.get("from_year")
    end = filters.get("to_year")
    if start and end:
        return f"{start}-{end}"
    if start:
        return f"{start}-"
    if end:
        return f"-{end}"
    return ""


def _license_from_pdf_url(url: str) -> str:
    if "arxiv.org" in url:
        return "arXiv non-exclusive license"
    if "europepmc.org" in url or "ncbi.nlm.nih.gov" in url:
        return "Open access (repository copy)"
    return ""
