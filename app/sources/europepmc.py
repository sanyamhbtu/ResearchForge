"""Europe PMC adapter — biomedical and life-sciences literature.

Docs consulted: https://europepmc.org/developers and
https://www.ebi.ac.uk/europepmc/webservices/rest/search (Europe PMC RESTful API).

Search: GET /search?query=&format=json&pageSize=&cursorMark=&resultType=core
``resultType=core`` returns the full metadata block (authors, journal, MeSH
headings, open-access URLs, license and citation count).

Pagination: Europe PMC's ``page`` parameter is deprecated/ignored (every page
returns the first results), so this adapter uses cursor-based paging via
``cursorMark`` (``cursorMark=*`` first, then ``nextCursorMark`` from each
response). To keep real offset semantics required by the framework, it walks
the cursor forward, discarding ``SearchQuery.offset`` items and collecting
``SearchQuery.limit`` of them.

A search that legitimately matches nothing returns ``hitCount == 0`` and an
empty ``resultList``; that is a normal empty page, not an error.
"""

from __future__ import annotations

from typing import Any

from .base import CLINICAL, LITERATURE, NormalizedRecord, SearchPage, SearchQuery, SourceAdapter

SEARCH_URL = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"
MAX_PAGE_SIZE = 100
MAX_CURSOR_PAGES = 30


class EuropePmcAdapter(SourceAdapter):
    name = "europepmc"
    label = "Europe PMC"
    kind = "paper"
    families = (CLINICAL, LITERATURE)
    homepage = "https://europepmc.org"
    docs_url = "https://europepmc.org/developers"
    notes = (
        "Life-sciences and biomedical literature aggregated from PubMed, PMC, "
        "Agricola, preprints and patents, with MeSH terms, open-access links "
        "and citation counts."
    )

    def search(self, query: SearchQuery) -> SearchPage:
        page_size = min(max(query.limit, 1), MAX_PAGE_SIZE)
        text = query.text
        # Optional server-side year restriction (Europe PMC query syntax).
        if query.filters.get("from_year") or query.filters.get("to_year"):
            clauses = []
            if query.filters.get("from_year"):
                clauses.append(f"FIRST_PDATE:[{query.filters['from_year']}-01-01 TO 2999-12-31]")
            if query.filters.get("to_year"):
                clauses.append(f"FIRST_PDATE:[1000-01-01 TO {query.filters['to_year']}-12-31]")
            text = f"({text}) AND ({' AND '.join(clauses)})"
        if query.filters.get("open_access_only"):
            text = f"({text}) AND OPEN_ACCESS:Y"

        # Europe PMC ignores ``page``; walk the cursorMark to honour ``offset``.
        needed = query.offset + page_size
        collected: list[dict] = []
        cursor = "*"
        hit_count: int | None = None
        has_more = False
        pages = 0
        while len(collected) < needed and pages < MAX_CURSOR_PAGES:
            params: dict[str, Any] = {
                "query": text,
                "format": "json",
                "pageSize": page_size,
                "cursorMark": cursor,
                "resultType": "core",
            }
            payload = self._fetch_json(SEARCH_URL, params=params) or {}
            if hit_count is None and isinstance(payload.get("hitCount"), int):
                hit_count = payload["hitCount"]
            items = ((payload.get("resultList") or {}).get("result")) or []
            if not items:
                break
            collected.extend(item for item in items if isinstance(item, dict))
            next_cursor = payload.get("nextCursorMark")
            pages += 1
            if not next_cursor or next_cursor == cursor:
                has_more = False
                break
            cursor = next_cursor
            has_more = True

        window = collected[query.offset : query.offset + page_size]
        records = [r for r in (self._to_record(item) for item in window) if r]

        next_offset = query.offset + len(window) if has_more else None
        warnings: list[str] = []
        if len(collected) < needed and has_more and pages >= MAX_CURSOR_PAGES:
            warnings.append(
                f"Stopped walking the Europe PMC cursor after {MAX_CURSOR_PAGES} "
                "pages; deeper offsets are not covered."
            )
        if not records and isinstance(hit_count, int) and hit_count > 0:
            warnings.append("Europe PMC reported matches but returned no parsable records.")

        return SearchPage(
            records=records,
            total_available=hit_count,
            next_offset=next_offset,
            warnings=warnings,
            raw_result_count=len(collected),
        )

    # -- mapping -----------------------------------------------------------
    def _to_record(self, item: dict) -> NormalizedRecord | None:
        title = self._clean_text(item.get("title"))
        if not title:
            return None
        source = item.get("source") or ""
        record_id = item.get("id") or ""
        pmid = item.get("pmid") or ""
        pmcid = item.get("pmcid") or ""
        doi = (item.get("doi") or "").lower()

        journal = (item.get("journalInfo") or {}).get("journal") or {}
        authors = [
            self._clean_text(a.get("fullName"))
            for a in (item.get("authorList") or {}).get("author") or []
            if a.get("fullName")
        ][:60]

        full_text_urls = (item.get("fullTextUrlList") or {}).get("fullTextUrl") or []
        if isinstance(full_text_urls, dict):
            full_text_urls = [full_text_urls]
        pdf_url = ""
        for entry in full_text_urls:
            if not isinstance(entry, dict):
                continue
            if (entry.get("documentStyle") or "").lower() == "pdf" and entry.get("url"):
                pdf_url = entry["url"]
                break

        concepts: list[str] = []
        for heading in _as_list((item.get("meshHeadingList") or {}).get("meshHeading")):
            if isinstance(heading, dict) and heading.get("descriptorName"):
                concepts.append(self._clean_text(heading["descriptorName"]))
        for keyword in _as_list((item.get("keywordList") or {}).get("keyword")):
            if isinstance(keyword, dict) and keyword.get("keyword"):
                concepts.append(self._clean_text(keyword["keyword"]))
            elif isinstance(keyword, str):
                concepts.append(self._clean_text(keyword))
        concepts = [c for c in concepts if c][:15]

        is_oa = item.get("isOpenAccess")
        if is_oa == "Y":
            access = "open"
        elif is_oa == "N":
            access = "restricted"
        else:
            access = "unknown"

        if source and record_id:
            page_url = f"https://europepmc.org/article/{source}/{record_id}"
        elif pmid:
            page_url = f"https://europepmc.org/article/MED/{pmid}"
        elif doi:
            page_url = f"https://doi.org/{doi}"
        else:
            page_url = ""

        return NormalizedRecord(
            source=self.name,
            source_id=str(pmid or pmcid or doi or record_id or title[:80]),
            kind="paper",
            title=title,
            doi=doi,
            abstract=self._clean_text(item.get("abstractText")),
            authors=authors,
            year=self._year_from(item.get("pubYear") or item.get("firstPublicationDate")),
            published_date=item.get("firstPublicationDate") or None,
            venue=self._clean_text(journal.get("title")),
            publisher="",
            url=page_url,
            pdf_url=pdf_url,
            license=self._clean_text(item.get("license")),
            access=access,
            citation_count=item.get("citedByCount"),
            reference_dois=[],
            concepts=concepts,
            modality="",
            raw={
                "id": record_id,
                "source": source,
                "pmid": pmid,
                "pmcid": pmcid,
                "doi": doi,
                "isOpenAccess": is_oa,
                "inEPMC": item.get("inEPMC"),
                "inPMC": item.get("inPMC"),
                "journalIssn": journal.get("issn"),
                "pubYear": item.get("pubYear"),
                "firstPublicationDate": item.get("firstPublicationDate"),
                "firstIndexDate": item.get("firstIndexDate"),
            },
            retrieved_at="",
            source_family=LITERATURE,
        )


def _as_list(value: Any) -> list:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]
