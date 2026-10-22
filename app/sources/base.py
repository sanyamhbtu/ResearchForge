"""Source-adapter framework.

Every external research source is implemented as an independent adapter that
normalizes its results into :class:`NormalizedRecord` while preserving the
source's own identifiers, URLs, license text and retrieval timestamp.

The orchestrator treats adapters as untrusted, individually-failing units: one
adapter raising or rate-limiting must never corrupt or block the others.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass, field
from typing import Any, Iterable

from ..http_client import HttpClient, HttpError
from ..logging_utils import get_logger

LITERATURE = "literature"
DATASETS = "datasets"
REPOSITORY = "repository"
CLINICAL = "life_sciences"
GOVERNMENT = "government_open_data"


class AdapterError(RuntimeError):
    """Raised when a source cannot fulfil a search. Never fatal to a run."""

    def __init__(self, source: str, message: str, *, detail: str = "") -> None:
        super().__init__(f"[{source}] {message}")
        self.source = source
        self.detail = detail


@dataclass
class SearchQuery:
    """One concrete query sent to one adapter."""

    text: str
    limit: int = 25
    offset: int = 0
    filters: dict[str, Any] = field(default_factory=dict)
    origin: str = "expanded"  # 'original' | 'expanded' | 'follow_up'
    intent: str = "literature"  # 'literature' | 'datasets' | 'both'

    def describe(self) -> dict:
        return {
            "text": self.text,
            "limit": self.limit,
            "offset": self.offset,
            "filters": dict(self.filters),
            "origin": self.origin,
            "intent": self.intent,
        }


@dataclass
class NormalizedRecord:
    """Common internal schema. Extra source fields live in ``raw``."""

    source: str
    source_id: str
    kind: str  # 'paper' | 'dataset'
    title: str
    doi: str = ""
    abstract: str = ""
    authors: list[str] = field(default_factory=list)
    year: int | None = None
    published_date: str | None = None
    venue: str = ""
    publisher: str = ""
    url: str = ""
    pdf_url: str = ""
    license: str = ""
    access: str = "unknown"  # 'open' | 'restricted' | 'unknown'
    citation_count: int | None = None
    reference_dois: list[str] = field(default_factory=list)
    concepts: list[str] = field(default_factory=list)
    modality: str = ""
    dataset: dict[str, Any] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)
    retrieved_at: str = ""
    source_family: str = ""

    def key(self) -> str:
        return f"{self.source}:{self.source_id}"

    def as_dict(self) -> dict:
        return {
            "record_key": self.key(),
            "source": self.source,
            "source_id": self.source_id,
            "kind": self.kind,
            "title": self.title,
            "doi": self.doi,
            "abstract": self.abstract,
            "authors": list(self.authors),
            "year": self.year,
            "published_date": self.published_date,
            "venue": self.venue,
            "publisher": self.publisher,
            "url": self.url,
            "pdf_url": self.pdf_url,
            "license": self.license,
            "access": self.access,
            "citation_count": self.citation_count,
            "reference_dois": list(self.reference_dois),
            "concepts": list(self.concepts),
            "modality": self.modality,
            "dataset": dict(self.dataset),
            "raw": dict(self.raw),
            "retrieved_at": self.retrieved_at,
            "source_family": self.source_family,
        }


@dataclass
class SearchPage:
    """Result of one adapter call."""

    records: list[NormalizedRecord] = field(default_factory=list)
    total_available: int | None = None
    next_offset: int | None = None
    warnings: list[str] = field(default_factory=list)
    raw_result_count: int = 0

    def __len__(self) -> int:  # pragma: no cover - convenience
        return len(self.records)


class SourceAdapter(abc.ABC):
    """Base class for all research source connectors."""

    name: str = "unnamed"
    label: str = ""
    kind: str = "paper"
    families: tuple[str, ...] = (LITERATURE,)
    homepage: str = ""
    docs_url: str = ""
    requires_credentials: bool = False
    credential_hint: str = ""
    notes: str = ""
    supports_datasets: bool = False
    default_page_size: int = 25

    def __init__(self, http: HttpClient | None = None) -> None:
        self.http = http or HttpClient(logger=get_logger(f"source.{self.name}"))
        self.log = get_logger(f"source.{self.name}")

    # -- public API --------------------------------------------------------
    @abc.abstractmethod
    def search(self, query: SearchQuery) -> SearchPage:
        """Execute one query against this source."""

    def describe(self) -> dict:
        return {
            "name": self.name,
            "label": self.label or self.name,
            "kind": self.kind,
            "families": list(self.families),
            "homepage": self.homepage,
            "docs_url": self.docs_url,
            "requires_credentials": self.requires_credentials,
            "credential_hint": self.credential_hint,
            "credentials_present": self.credentials_present(),
            "notes": self.notes,
            "supports_datasets": self.supports_datasets,
        }

    def credentials_present(self) -> bool:
        return True

    def supports_intent(self, intent: str) -> bool:
        if intent == "both":
            return True
        if intent == "datasets":
            return self.kind == "dataset" or self.supports_datasets
        return self.kind == "paper" or self.kind == "both"

    # -- helpers for subclasses -------------------------------------------
    def _fetch_json(self, url: str, **kwargs: Any) -> Any:
        try:
            return self.http.get_json(url, **kwargs)
        except HttpError as exc:
            raise AdapterError(self.name, str(exc), detail=url) from exc

    @staticmethod
    def _clean_text(value: Any) -> str:
        if not value:
            return ""
        text = str(value)
        # strip JATS/HTML markup that some APIs embed in titles and abstracts
        for token in ("<jats:p>", "</jats:p>", "<jats:title>", "</jats:title>"):
            text = text.replace(token, " ")
        if "<" in text and ">" in text:
            import re

            text = re.sub(r"<[^>]{1,80}>", " ", text)
        return " ".join(text.split())

    @staticmethod
    def _year_from(value: Any) -> int | None:
        if value is None:
            return None
        if isinstance(value, int):
            return value if 1500 < value < 2200 else None
        text = str(value)
        import re

        match = re.search(r"(1[5-9]\d{2}|20\d{2}|21\d{2})", text)
        return int(match.group(1)) if match else None

    @staticmethod
    def _first(items: Iterable[Any], default: Any = None) -> Any:
        for item in items:
            if item:
                return item
        return default
