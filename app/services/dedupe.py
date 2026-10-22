"""Deduplication and entity resolution.

Two records are the same work when they share a normalized DOI. When no DOI is
available they match on normalized title + year, then on high token-overlap
title similarity within the same year. Merging is aggressive *only* for exact
identifiers; fuzzy matches are recorded with the similarity score so the user
can audit the decision.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from ..logging_utils import get_logger

log = get_logger("services.dedupe")

PUNCT_RE = re.compile(r"[^a-z0-9\s]")
SPACE_RE = re.compile(r"\s+")
FUZZY_THRESHOLD = 0.87


def normalize_doi(doi: str | None) -> str:
    if not doi:
        return ""
    text = str(doi).strip().lower()
    for prefix in ("https://doi.org/", "http://doi.org/", "doi:", "https://dx.doi.org/", "info:doi/"):
        if text.startswith(prefix):
            text = text[len(prefix):]
    return text.strip().rstrip(".")


def normalize_title(title: str | None) -> str:
    if not title:
        return ""
    text = str(title).lower()
    text = re.sub(r"<[^>]{1,80}>", " ", text)
    text = PUNCT_RE.sub(" ", text)
    text = SPACE_RE.sub(" ", text).strip()
    # drop boilerplate that varies between indexes
    for noise in ("a systematic review", "systematic review", "a survey", "dataset"):
        if text.endswith(f" {noise}"):
            text = text[: -len(noise) - 1]
    return text


def title_tokens(title: str) -> set[str]:
    return {token for token in normalize_title(title).split() if len(token) > 2}


def jaccard(left: set[str], right: set[str]) -> float:
    if not left or not right:
        return 0.0
    intersection = len(left & right)
    union = len(left | right)
    return intersection / union if union else 0.0


def dedupe_key(record: dict) -> str:
    doi = normalize_doi(record.get("doi"))
    if doi:
        return f"doi:{doi}"
    title = normalize_title(record.get("title"))
    year = record.get("year") or ""
    if len(title) >= 20:
        return f"title:{title}|{year}"
    return f"src:{record.get('source')}:{record.get('source_id')}"


@dataclass
class MergeDecision:
    record_key: str
    canonical_key: str
    reason: str
    similarity: float = 0.0


@dataclass
class DedupeResult:
    groups: list[dict] = field(default_factory=list)
    decisions: list[MergeDecision] = field(default_factory=list)
    unique_count: int = 0

    def as_dict(self) -> dict:
        return {
            "unique_count": self.unique_count,
            "duplicate_count": len(self.decisions),
            "groups": self.groups,
            "decisions": [
                {
                    "record_key": d.record_key,
                    "canonical_key": d.canonical_key,
                    "reason": d.reason,
                    "similarity": round(d.similarity, 3),
                }
                for d in self.decisions
            ],
        }


class DedupeResolver:
    """Incremental entity resolution over a stream of records."""

    def __init__(self) -> None:
        self._by_key: dict[str, dict] = {}
        self._by_doi: dict[str, dict] = {}
        self._by_title_year: dict[tuple[str, int | None], dict] = {}
        self.result = DedupeResult()

    def resolve(self, record: dict) -> tuple[dict | None, MergeDecision | None]:
        """Return (canonical_record, decision) when ``record`` is a duplicate."""
        doi = normalize_doi(record.get("doi"))
        if doi and doi in self._by_doi:
            canonical = self._by_doi[doi]
            decision = MergeDecision(
                record_key=record.get("record_key", ""),
                canonical_key=canonical.get("record_key", ""),
                reason="identical normalized DOI",
                similarity=1.0,
            )
            self.result.decisions.append(decision)
            return canonical, decision

        key = dedupe_key(record)
        if key in self._by_key:
            canonical = self._by_key[key]
            decision = MergeDecision(
                record_key=record.get("record_key", ""),
                canonical_key=canonical.get("record_key", ""),
                reason="identical normalized title and year",
                similarity=1.0,
            )
            self.result.decisions.append(decision)
            return canonical, decision

        title = normalize_title(record.get("title"))
        year = record.get("year")
        if len(title) >= 20:
            tokens = title_tokens(record.get("title"))
            # fuzzy pass across the same year only
            for (candidate_title, candidate_year), candidate in self._by_title_year.items():
                if year is not None and candidate_year is not None and year != candidate_year:
                    continue
                similarity = jaccard(tokens, set(candidate_title.split()))
                if similarity >= FUZZY_THRESHOLD:
                    decision = MergeDecision(
                        record_key=record.get("record_key", ""),
                        canonical_key=candidate.get("record_key", ""),
                        reason=f"title similarity {similarity:.2f} ≥ {FUZZY_THRESHOLD} (same year)",
                        similarity=similarity,
                    )
                    self.result.decisions.append(decision)
                    return candidate, decision

        # not a duplicate: register
        self._by_key[key] = record
        if doi:
            self._by_doi[doi] = record
        if len(title) >= 20:
            self._by_title_year.setdefault((title, year), record)
        return None, None

    def add_group_member(self, canonical: dict, duplicate: dict) -> None:
        for group in self.result.groups:
            if group["canonical"] == canonical.get("record_key"):
                group["members"].append(duplicate.get("record_key"))
                group["sources"].append(duplicate.get("source"))
                return
        self.result.groups.append(
            {
                "canonical": canonical.get("record_key"),
                "canonical_title": canonical.get("title"),
                "members": [duplicate.get("record_key")],
                "sources": [canonical.get("source"), duplicate.get("source")],
            }
        )

    def finish(self) -> DedupeResult:
        self.result.unique_count = len(self._by_key)
        log.info(
            "dedupe_complete",
            unique=self.result.unique_count,
            merged=len(self.result.decisions),
        )
        return self.result


def cluster_summary(dedupe_result: DedupeResult, clusters: list[dict]) -> dict:
    """Auditable summary of one deduplication pass over a run."""
    return {
        "unique_count": len(clusters),
        "duplicate_count": sum(len(cluster.get("duplicates") or []) for cluster in clusters),
        "groups": dedupe_result.groups,
        "decisions": [
            {
                "record_key": decision.record_key,
                "canonical_key": decision.canonical_key,
                "reason": decision.reason,
                "similarity": round(decision.similarity, 3),
            }
            for decision in dedupe_result.decisions
        ],
        "rule": (
            "Exact normalized DOI, then normalized title+year, then title token similarity "
            f"≥ {FUZZY_THRESHOLD} within the same year. The richest record becomes canonical; "
            "missing fields of the canonical record are filled from duplicates and recorded in "
            "raw.merged_from."
        ),
    }


def has_independent_confirmation(group: dict, records_by_key: dict[str, dict]) -> dict:
    """Decide whether a duplicate group is *independent* confirmation.

    Two indexes that merely copy the same registered metadata are not
    independent evidence. Independence requires either an exact identifier match
    plus a distinct registrar, or a genuinely different repository record.
    """
    members = [group.get("canonical")] + list(group.get("members") or [])
    sources = []
    for key in members:
        record = records_by_key.get(key) or {}
        if record.get("source"):
            sources.append(record["source"])
    distinct = sorted(set(sources))
    metadata_only = {"crossref", "datacite", "openalex"}
    independent = [s for s in distinct if s not in metadata_only]
    return {
        "sources": distinct,
        "count": len(distinct),
        "independent_sources": independent,
        "independent_confirmation": len(independent) >= 1,
        "note": (
            "Multiple bibliographic indexes carrying copied metadata do not count as "
            "independent confirmation."
        ),
    }
