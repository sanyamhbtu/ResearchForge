"""Search planning and query expansion.

Produces an explicit, auditable plan: which sources will be queried, with which
queries, filters, depth and rationale. Expansion is deterministic (synonyms and
templates) and is always reported so the user can see how the search scope was
widened.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field

from ..logging_utils import get_logger
from ..sources import AdapterRegistry
from .lexicon import (
    DATASET_TEMPLATES,
    LITERATURE_TEMPLATES,
    SYNONYM_MAP,
)
from .parser import MODALITY_WORDS_FOR_FILTERS, ResearchSpec

log = get_logger("services.planning")

MAX_EXPANSIONS_PER_INTENT = 4


@dataclass
class PlannedQuery:
    text: str
    intent: str
    origin: str  # 'original' | 'expanded'
    rationale: str
    filters: dict = field(default_factory=dict)


@dataclass
class SearchPlan:
    spec: ResearchSpec
    queries: list[PlannedQuery]
    adapters: list[str]
    excluded_adapters: list[dict]
    families: list[str]
    depth: int
    page_size: int
    max_results_per_source: int
    filters: dict
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "spec": self.spec.as_dict(),
            "queries": [asdict(q) for q in self.queries],
            "adapters": list(self.adapters),
            "excluded_adapters": list(self.excluded_adapters),
            "families": list(self.families),
            "depth": self.depth,
            "page_size": self.page_size,
            "max_results_per_source": self.max_results_per_source,
            "filters": dict(self.filters),
            "notes": list(self.notes),
        }

    def queries_for(self, intent: str) -> list[PlannedQuery]:
        """Queries applicable to a given intent; 'both' means every query."""
        if intent == "both":
            return list(self.queries)
        return [q for q in self.queries if q.intent in {intent, "both"}]


def expand_queries(spec: ResearchSpec) -> list[PlannedQuery]:
    """Original query plus deterministic synonym/template expansions."""
    base = spec.topic or spec.question
    queries: list[PlannedQuery] = [
        PlannedQuery(
            text=base,
            intent="both",
            origin="original",
            rationale="the user's own wording, unmodified",
        )
    ]
    seen = {base.lower()}
    lowered = base.lower()

    # 1. synonym substitution — keeps the meaning, varies the terminology
    for term, synonyms in SYNONYM_MAP.items():
        if term not in lowered:
            continue
        for synonym in synonyms[:2]:
            candidate = lowered.replace(term, synonym)
            if candidate.lower() in seen:
                continue
            seen.add(candidate.lower())
            queries.append(
                PlannedQuery(
                    text=candidate,
                    intent="both",
                    origin="expanded",
                    rationale=f'technical synonym: "{term}" → "{synonym}"',
                )
            )
            break

    # 2. template expansions, split by intent
    if spec.intent in {"literature", "both"}:
        for template in LITERATURE_TEMPLATES[1 : 1 + MAX_EXPANSIONS_PER_INTENT]:
            candidate = template.format(topic=spec.topic)
            if candidate.lower() in seen:
                continue
            seen.add(candidate.lower())
            queries.append(
                PlannedQuery(
                    text=candidate,
                    intent="literature",
                    origin="expanded",
                    rationale=f'literature template: "{template}"',
                )
            )
    if spec.intent in {"datasets", "both"}:
        for template in DATASET_TEMPLATES[1 : 1 + MAX_EXPANSIONS_PER_INTENT]:
            candidate = template.format(topic=spec.topic)
            if candidate.lower() in seen:
                continue
            seen.add(candidate.lower())
            queries.append(
                PlannedQuery(
                    text=candidate,
                    intent="datasets",
                    origin="expanded",
                    rationale=f'dataset template: "{template}"',
                )
            )

    # 3. user-supplied keywords as standalone recall queries (capped)
    for keyword in spec.keywords[:2]:
        if keyword.lower() in seen:
            continue
        seen.add(keyword.lower())
        queries.append(
            PlannedQuery(
                text=keyword,
                intent="both",
                origin="expanded",
                rationale="high-salience keyword extracted from the request",
            )
        )
    return queries


def build_filters(spec: ResearchSpec) -> dict:
    filters: dict = {}
    if spec.time_range.get("from_year"):
        filters["from_year"] = spec.time_range["from_year"]
    if spec.time_range.get("to_year"):
        filters["to_year"] = spec.time_range["to_year"]
    if "open" in spec.access_requirements and "restricted" not in spec.access_requirements:
        filters["open_access_only"] = True
    if spec.modalities:
        filters["modalities"] = [MODALITY_WORDS_FOR_FILTERS.get(m, m) for m in spec.modalities]
    return filters


def build_plan(
    spec: ResearchSpec,
    *,
    registry: AdapterRegistry,
    depth: int,
    page_size: int,
    adapter_names: list[str] | None = None,
    max_results_per_source: int = 120,
) -> SearchPlan:
    adapters = registry.select(names=adapter_names, intent=spec.intent)
    excluded = []
    unavailable = registry.unavailable()
    if adapter_names:
        for name in adapter_names:
            if name in unavailable:
                excluded.append(
                    {
                        "adapter": name,
                        "reason": unavailable[name],
                        "kind": "unavailable",
                    }
                )
    for adapter in adapters:
        if not adapter.supports_intent(spec.intent):
            excluded.append(
                {
                    "adapter": adapter.name,
                    "reason": f"does not serve the '{spec.intent}' intent",
                    "kind": "intent_mismatch",
                }
            )
    adapters = [a for a in adapters if a.supports_intent(spec.intent)]

    queries = expand_queries(spec)
    filters = build_filters(spec)
    families = sorted({family for adapter in adapters for family in adapter.families})

    notes = [
        "Search scope is limited to the adapters listed below; "
        "no claim of internet-wide or worldwide completeness is made.",
        f"Configured depth: up to {depth} page(s) per query per source.",
    ]
    if spec.llm_assisted:
        notes.append("Query keywords were additionally suggested by a language model and reviewed.")

    plan = SearchPlan(
        spec=spec,
        queries=queries,
        adapters=[adapter.name for adapter in adapters],
        excluded_adapters=excluded,
        families=families,
        depth=max(1, depth),
        page_size=max(5, page_size),
        max_results_per_source=max_results_per_source,
        filters=filters,
        notes=notes,
    )
    log.info(
        "plan_built",
        adapters=len(adapters),
        queries=len(queries),
        depth=plan.depth,
        intent=spec.intent,
    )
    return plan
