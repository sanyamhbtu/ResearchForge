"""Evidence extraction and the evidence-sufficiency gate.

Two responsibilities:

1. Turn retrieved content into citable evidence records (claim → verbatim
   passage → locator → URL → retrieval timestamp → origin).
2. Decide, deterministically, whether the retrieved evidence is enough to answer
   the request. When it is not, the platform says so instead of guessing.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any

from ..db import utc_now
from ..http_client import HttpClient, HttpError
from ..logging_utils import get_logger

log = get_logger("services.evidence")

INSUFFICIENT_DATASET_MESSAGE = "There is insufficient data worldwide. No dataset."

STATUS_SUFFICIENT = "sufficient"
STATUS_UNVERIFIED = "unverified_candidates"
STATUS_INSUFFICIENT = "insufficient_evidence"
STATUS_INCOMPLETE = "search_incomplete"

MAX_FULLTEXT_BYTES = 400_000
MAX_PASSAGES_PER_RECORD = 8

_TAG_RE = re.compile(r"<(script|style)[^>]*>.*?</\1>", re.S | re.I)
_ANY_TAG_RE = re.compile(r"<[^>]{1,200}>")
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")
_WS_RE = re.compile(r"[ \t\r\f\v]+")


@dataclass
class EvidenceItem:
    claim: str
    passage: str
    locator: str
    field_name: str
    url: str
    retrieved_at: str
    origin: str  # 'source_metadata' | 'retrieved_full_text'
    verified: bool = False

    def as_dict(self) -> dict:
        return asdict(self)


# ---------------------------------------------------------------------------
# evidence from metadata
# ---------------------------------------------------------------------------


def evidence_from_record(record: dict) -> list[EvidenceItem]:
    """Evidence that can be cited from the source's own metadata payload."""
    retrieved_at = record.get("retrieved_at") or utc_now()
    url = record.get("url") or ""
    items: list[EvidenceItem] = []

    def add(claim: str, passage: str, locator: str, field_name: str, verified: bool = False) -> None:
        if not passage:
            return
        items.append(
            EvidenceItem(
                claim=claim,
                passage=_truncate(passage, 1200),
                locator=locator,
                field_name=field_name,
                url=url,
                retrieved_at=retrieved_at,
                origin="source_metadata",
                verified=verified,
            )
        )

    add("The record exists in this source.", record.get("title") or "", "title", "title", True)
    if record.get("abstract"):
        add(
            "The source reports the following description/abstract.",
            record["abstract"],
            "abstract",
            "abstract",
        )
    dataset = record.get("dataset") or {}
    if dataset:
        if dataset.get("samples"):
            add(
                "The source states this sample count.",
                f"{dataset['samples']} samples/records "
                f"({dataset.get('samples_source') or 'declared by the source'}).",
                "dataset.samples",
                "dataset.samples",
            )
        if dataset.get("format"):
            add("The source lists these file formats.", str(dataset["format"]), "dataset.format", "dataset.format")
        if dataset.get("files"):
            add(
                "The source publishes this file listing.",
                "; ".join(
                    f"{f.get('name')} ({f.get('size_bytes')} bytes)"
                    if isinstance(f, dict)
                    else str(f)
                    for f in dataset["files"][:12]
                ),
                "dataset.files",
                "dataset.files",
            )
        if dataset.get("related_publications"):
            add(
                "The record links these related publications.",
                "; ".join(str(value) for value in dataset["related_publications"][:12]),
                "dataset.related_publications",
                "dataset.related_publications",
            )
        if dataset.get("updated"):
            add("The source states this last-update date.", str(dataset["updated"]), "dataset.updated", "dataset.updated")
    if record.get("license"):
        add("The source declares this license.", str(record["license"]), "license", "license")
    if record.get("authors"):
        add(
            "The source lists these authors/creators.",
            ", ".join(str(a) for a in record["authors"][:20]),
            "authors",
            "authors",
        )
    if record.get("year"):
        add("The source states this publication year.", str(record["year"]), "year", "year")
    return items


# ---------------------------------------------------------------------------
# evidence from retrieved full text (only when legitimately open access)
# ---------------------------------------------------------------------------


class DocumentRetriever:
    def __init__(self, http: HttpClient | None = None) -> None:
        self.http = http or HttpClient(logger=get_logger("services.evidence.http"))

    def candidate_fulltext_url(self, record: dict) -> str:
        """Only openly accessible HTML full text is retrieved. Never paywalls."""
        raw = record.get("raw") or {}
        for key in ("fulltext_html", "full_text_html_url"):
            if raw.get(key):
                return str(raw[key])
        if record.get("access") == "open" and record.get("pdf_url", "").endswith(".pdf") is False:
            return ""
        return ""

    def fetch_fulltext_passages(
        self, record: dict, keywords: list[str], limit: int = MAX_PASSAGES_PER_RECORD
    ) -> tuple[list[EvidenceItem], str]:
        """Return (evidence, status) where status describes retrievability."""
        url = self.candidate_fulltext_url(record)
        if not url:
            return [], "no_open_full_text_url"
        try:
            response = self.http.get(
                url,
                accept="text/html,application/xhtml+xml",
                use_cache=True,
                cache_ttl=86_400,
            )
        except HttpError as exc:
            log.warning("fulltext_unavailable", url=url, error=str(exc))
            return [], f"retrieval_failed: {exc}"
        if response.status >= 400 or len(response.body) > MAX_FULLTEXT_BYTES:
            return [], f"not_retrieved (status {response.status})"

        text = _html_to_text(response.text)
        if len(text) < 200:
            return [], "content_too_small_to_be_full_text"

        passages = _select_passages(text, keywords, limit)
        items = [
            EvidenceItem(
                claim="Retrieved full-text passage containing a query term.",
                passage=passage,
                locator=f"retrieved HTML full text at {url} (character offset {offset})",
                field_name="full_text",
                url=url,
                retrieved_at=utc_now(),
                origin="retrieved_full_text",
                verified=False,
            )
            for passage, offset in passages
        ]
        log.info("fulltext_retrieved", url=url, passages=len(items))
        return items, "retrieved"


def _html_to_text(html: str) -> str:
    text = _TAG_RE.sub(" ", html)
    text = _ANY_TAG_RE.sub(" ", text)
    replacements = {
        "&amp;": "&", "&lt;": "<", "&gt;": ">", "&quot;": '"', "&#39;": "'", "&nbsp;": " ",
        "&mdash;": "—", "&ndash;": "–", "&minus;": "−",
    }
    for entity, value in replacements.items():
        text = text.replace(entity, value)
    text = _WS_RE.sub(" ", text)
    return "\n".join(line.strip() for line in text.splitlines() if line.strip())


def _select_passages(text: str, keywords: list[str], limit: int) -> list[tuple[str, int]]:
    sentences = _SENTENCE_SPLIT_RE.split(text)
    lowered_keywords = [k.lower() for k in keywords if len(k) > 3][:12]
    scored: list[tuple[int, int, str]] = []
    running_offset = 0
    for sentence in sentences:
        offset = text.find(sentence, running_offset)
        running_offset = offset + len(sentence) if offset >= 0 else running_offset
        cleaned = sentence.strip()
        if len(cleaned) < 60 or len(cleaned) > 600:
            continue
        lowered = cleaned.lower()
        score = sum(1 for keyword in lowered_keywords if keyword in lowered)
        if score:
            scored.append((score, offset, cleaned))
    scored.sort(key=lambda item: (-item[0], item[1]))
    return [(passage, offset) for _, offset, passage in scored[:limit]]


# ---------------------------------------------------------------------------
# the sufficiency gate
# ---------------------------------------------------------------------------


@dataclass
class SufficiencyAssessment:
    status: str
    message: str
    explanation: list[str] = field(default_factory=list)
    requirements_unmet: list[str] = field(default_factory=list)
    sources_searched: list[str] = field(default_factory=list)
    sources_failed: list[dict] = field(default_factory=list)
    queries_executed: int = 0
    pages_retrieved: int = 0
    candidate_count: int = 0
    verified_candidate_count: int = 0
    next_steps: list[str] = field(default_factory=list)
    exact_message_used: bool = False

    def as_dict(self) -> dict:
        return asdict(self)


def assess_dataset_sufficiency(
    *,
    scope: dict,
    candidates: list[dict],
    requirements: dict,
    queries: list[str],
) -> SufficiencyAssessment:
    """Decide whether the dataset search produced defensible answers.

    ``scope`` must describe what was actually searched — never an assumption of
    global coverage.
    """
    searched = list(scope.get("sources_searched") or [])
    failed = list(scope.get("sources_failed") or [])
    pages = int(scope.get("pages_retrieved") or 0)
    completed = bool(scope.get("completed"))
    queries_executed = int(scope.get("queries_executed") or 0)

    usable = [
        candidate
        for candidate in candidates
        if candidate.get("access") in {"open", "unknown"}
        and candidate.get("verification", {}).get("overall") in {"verified", "partially_verified"}
    ]
    unverified = [candidate for candidate in candidates if candidate not in usable]

    base_explanation = [
        f"Sources searched: {', '.join(searched) if searched else 'none succeeded'}.",
        f"Queries executed: {queries_executed} across {pages} retrieved page(s).",
    ]
    if failed:
        base_explanation.append(
            "Sources that failed: "
            + "; ".join(f"{item.get('adapter')} ({item.get('reason')})" for item in failed)
        )

    if not completed:
        explanation = base_explanation + [
            "The configured search scope could not be fully explored, so absence of results is "
            "not evidence of absence worldwide.",
            "No failure of an API is treated as proof that no data exists.",
        ]
        return SufficiencyAssessment(
            status=STATUS_INCOMPLETE,
            message="The search is incomplete; no sufficiency conclusion can be drawn yet.",
            explanation=explanation,
            requirements_unmet=list(requirements.get("unmet") or []),
            sources_searched=searched,
            sources_failed=failed,
            queries_executed=queries_executed,
            pages_retrieved=pages,
            candidate_count=len(candidates),
            verified_candidate_count=len(usable),
            next_steps=[
                "Re-run the search — one or more sources failed to respond.",
                "Reduce the search depth per source, or remove sources that are rate limiting.",
                "Add credentials (for example SEMANTIC_SCHOLAR_API_KEY) to raise rate limits.",
            ],
        )

    if not candidates:
        return SufficiencyAssessment(
            status=STATUS_INSUFFICIENT,
            message=INSUFFICIENT_DATASET_MESSAGE,
            explanation=base_explanation
            + [
                "The search scope above was explored without API failure and returned no candidate "
                "dataset records at all.",
                "Nothing unrelated is shown in place of the missing dataset.",
            ],
            requirements_unmet=list(requirements.get("unmet") or requirements.get("stated") or []),
            sources_searched=searched,
            sources_failed=failed,
            queries_executed=queries_executed,
            pages_retrieved=pages,
            candidate_count=0,
            next_steps=_dataset_next_steps(searched),
            exact_message_used=True,
        )

    if not usable:
        unmet = list(requirements.get("unmet") or [])
        return SufficiencyAssessment(
            status=STATUS_INSUFFICIENT,
            message=INSUFFICIENT_DATASET_MESSAGE,
            explanation=base_explanation
            + [
                f"{len(candidates)} candidate record(s) were found, but none satisfied the "
                "requirements combined with verifiability.",
                "Requirements that could not be satisfied: "
                + (", ".join(unmet) if unmet else "none explicitly stated — candidates failed verification"),
                "Candidates that failed verification are listed separately instead of being "
                "presented as satisfying the request.",
            ],
            requirements_unmet=unmet,
            sources_searched=searched,
            sources_failed=failed,
            queries_executed=queries_executed,
            pages_retrieved=pages,
            candidate_count=len(candidates),
            verified_candidate_count=0,
            next_steps=_dataset_next_steps(searched) + [
                "Relax a stated requirement (modality, license, time range or geography) and retry.",
            ],
            exact_message_used=True,
        )

    return SufficiencyAssessment(
        status=STATUS_SUFFICIENT if not unverified else STATUS_UNVERIFIED,
        message=(
            f"{len(usable)} candidate dataset(s) satisfy the stated requirements and passed "
            "metadata-level verification."
            if not unverified
            else (
                f"{len(usable)} verified candidate dataset(s); {len(unverified)} further "
                "candidate(s) remain unverified and are shown as such."
            )
        ),
        explanation=base_explanation
        + [
            "Candidate suitability reflects the checks that were actually executed; "
            "unexecuted checks are reported as unknown.",
        ],
        sources_searched=searched,
        sources_failed=failed,
        queries_executed=queries_executed,
        pages_retrieved=pages,
        candidate_count=len(candidates),
        verified_candidate_count=len(usable),
    )


def _dataset_next_steps(searched: list[str]) -> list[str]:
    suggestions = [
        "Broaden the query with alternative terminology (the expanded queries are listed with the run).",
        "Increase search depth to retrieve more pages per source.",
    ]
    expected = {"huggingface", "zenodo", "datacite", "uci", "datagov"}
    missing = sorted(expected - set(searched))
    if missing:
        suggestions.append(
            "Enable or repair the dataset sources that were not searched: " + ", ".join(missing) + "."
        )
    suggestions.append(
        "Check the original repositories directly, for example a national open-data portal for your "
        "specific domain, since ResearchForge only queries the adapters listed above."
    )
    return suggestions


def assess_literature_sufficiency(*, scope: dict, records: list[dict]) -> SufficiencyAssessment:
    """Equivalent gate for literature, gap and manuscript claims."""
    searched = list(scope.get("sources_searched") or [])
    failed = list(scope.get("sources_failed") or [])
    completed = bool(scope.get("completed"))
    with_evidence = [
        record for record in records if record.get("evidence_count") or record.get("abstract")
    ]
    pages = int(scope.get("pages_retrieved") or 0)
    queries_executed = int(scope.get("queries_executed") or 0)

    if not completed:
        return SufficiencyAssessment(
            status=STATUS_INCOMPLETE,
            message="The literature search is incomplete; findings cannot yet be assessed as sufficient.",
            explanation=[
                f"Sources searched: {', '.join(searched) or 'none'}.",
                "Some sources failed, so few results do not imply that little research exists.",
            ],
            sources_searched=searched,
            sources_failed=failed,
            queries_executed=queries_executed,
            pages_retrieved=pages,
            candidate_count=len(records),
        )
    if not with_evidence:
        return SufficiencyAssessment(
            status=STATUS_INSUFFICIENT,
            message="Insufficient literature evidence: no citable records with retrievable content.",
            explanation=[
                f"Sources searched: {', '.join(searched) or 'none'}.",
                f"Queries executed: {queries_executed} across {pages} page(s).",
                "No record with an abstract or retrievable content was retained.",
                "A small result set is described as a limited search scope, never as evidence that "
                "a topic is globally unexplored.",
            ],
            sources_searched=searched,
            sources_failed=failed,
            queries_executed=queries_executed,
            pages_retrieved=pages,
            candidate_count=len(records),
        )
    return SufficiencyAssessment(
        status=STATUS_SUFFICIENT,
        message=f"{len(with_evidence)} citable record(s) with source content are available.",
        explanation=[
            f"Sources searched: {', '.join(searched) or 'none'}.",
            f"Queries executed: {queries_executed} across {pages} page(s).",
            "Claims derived from these records carry a passage, a locator, a URL and a retrieval date.",
        ],
        sources_searched=searched,
        sources_failed=failed,
        queries_executed=queries_executed,
        pages_retrieved=pages,
        candidate_count=len(records),
        verified_candidate_count=len(with_evidence),
    )


def _truncate(text: Any, limit: int) -> str:
    value = " ".join(str(text).split())
    return value if len(value) <= limit else value[: limit - 1] + "…"
