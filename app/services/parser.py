"""Research-request parsing.

Deterministic extraction first (keyword lexicons, regex for time ranges and
geography). An optional LLM pass may *add* free-text keywords or phrase the
clarification question, but it can never overwrite a deterministically extracted
field, and its output is validated before use.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any

from ..logging_utils import get_logger
from .lexicon import (
    ACCESS_TRIGGERS,
    DATASET_TRIGGERS,
    DOMAIN_TERMS,
    GEOGRAPHY_TERMS,
    LITERATURE_TRIGGERS,
    MODALITY_TERMS,
    STOPWORDS,
)
from .llm import get_llm

log = get_logger("services.parser")

YEAR_RE = re.compile(r"\b(1[5-9]\d{2}|20\d{2}|21\d{2})\b")
RANGE_RE = re.compile(
    r"\b(1[5-9]\d{2}|20\d{2})\s*(?:-|–|to|until|through)\s*(1[5-9]\d{2}|20\d{2})\b", re.I
)
SINCE_RE = re.compile(r"\b(?:since|after|from|newer than)\s+(1[5-9]\d{2}|20\d{2})\b", re.I)
BEFORE_RE = re.compile(r"\b(?:before|until|older than|up to)\s+(1[5-9]\d{2}|20\d{2})\b", re.I)
LAST_N_RE = re.compile(r"\blast\s+(\d{1,2})\s+(year|years|month|months)\b", re.I)
SAMPLE_RE = re.compile(r"\b(\d[\d,]{2,})\s*(samples|records|rows|images|examples|subjects|patients|cases)\b", re.I)

MODALITY_WORDS_FOR_FILTERS = {
    "tabular": "tabular",
    "text": "text",
    "image": "image",
    "audio": "audio",
    "video": "video",
    "timeseries": "timeseries",
    "graph": "graph",
    "geospatial": "geospatial",
}


@dataclass
class ResearchSpec:
    """Structured interpretation of a research request."""

    question: str
    topic: str
    objective: str
    domain: str
    intent: str  # 'literature' | 'datasets' | 'both'
    data_types: list[str] = field(default_factory=list)
    modalities: list[str] = field(default_factory=list)
    geography: str = ""
    time_range: dict[str, int | None] = field(default_factory=dict)
    population: str = ""
    expected_samples: str = ""
    access_requirements: list[str] = field(default_factory=list)
    constraints: list[str] = field(default_factory=list)
    keywords: list[str] = field(default_factory=list)
    assumptions: list[str] = field(default_factory=list)
    missing_information: list[str] = field(default_factory=list)
    clarification_question: str = ""
    llm_assisted: bool = False

    def as_dict(self) -> dict:
        return asdict(self)


def parse_request(question: str, *, use_llm: bool = True) -> ResearchSpec:
    question = (question or "").strip()
    lowered = question.lower()
    tokens = _tokenize(question)

    intent = _detect_intent(lowered)
    domains = _detect_domains(lowered)
    modalities = _detect_modalities(lowered)
    geography = _detect_geography(lowered)
    time_range = _detect_time_range(lowered)
    access = _detect_access(lowered)
    expected = _detect_expected_samples(question)
    objective = _detect_objective(lowered)

    spec = ResearchSpec(
        question=question,
        topic=_derive_topic(question),
        objective=objective,
        domain=", ".join(domains) if domains else "unspecified",
        intent=intent,
        data_types=list(modalities),
        modalities=list(modalities),
        geography=geography,
        time_range=time_range,
        population=_detect_population(lowered),
        expected_samples=expected,
        access_requirements=access,
        constraints=[
            "No fabricated sources, dataset counts, findings or citations.",
            "Every retained record keeps its original identifier, URL and retrieval timestamp.",
        ],
        keywords=tokens[:24],
    )

    # -- assumptions and missing-information tracking ----------------------
    if not spec.geography:
        spec.assumptions.append(
            "No geography was specified, so the search is not geographically restricted."
        )
    if not time_range.get("from_year") and not time_range.get("to_year"):
        spec.assumptions.append(
            "No time range was specified, so all publication dates are eligible; "
            "recency is reported per record instead of filtered."
        )
    if intent in {"datasets", "both"} and not modalities:
        spec.assumptions.append(
            "No data modality was specified, so tabular, text, image, audio and time-series "
            "datasets are all considered."
        )
    if not access:
        spec.assumptions.append(
            "No access requirement was specified; openly accessible sources are preferred but "
            "restricted ones are listed with an explicit access note."
        )

    if len(tokens) < 2 and len(question.split()) < 4:
        spec.missing_information.append("the precise research topic")
        spec.clarification_question = _clarification_question(spec)

    if use_llm and get_llm().enabled:
        _llm_refine(spec)

    if not spec.clarification_question and spec.missing_information:
        spec.clarification_question = _clarification_question(spec)

    log.info(
        "request_parsed",
        intent=spec.intent,
        domain=spec.domain,
        modalities=spec.modalities,
        llm_assisted=spec.llm_assisted,
    )
    return spec


# ---------------------------------------------------------------------------
# deterministic extractors
# ---------------------------------------------------------------------------


def _tokenize(text: str) -> list[str]:
    words = re.findall(r"[A-Za-z][A-Za-z0-9\-\+_]{1,}", text)
    seen: set[str] = set()
    out: list[str] = []
    for word in words:
        lowered = word.lower()
        if lowered in STOPWORDS or len(lowered) < 3:
            continue
        if lowered in seen:
            continue
        seen.add(lowered)
        out.append(lowered)
    return out


def _detect_intent(lowered: str) -> str:
    wants_datasets = any(trigger in lowered for trigger in DATASET_TRIGGERS)
    wants_literature = any(trigger in lowered for trigger in LITERATURE_TRIGGERS)
    if wants_datasets and wants_literature:
        return "both"
    if wants_datasets:
        return "datasets"
    if wants_literature:
        return "literature"
    return "both"


def _detect_domains(lowered: str) -> list[str]:
    found: list[str] = []
    for domain, terms in DOMAIN_TERMS.items():
        if any(term in lowered for term in terms):
            found.append(domain)
    return found[:4]


def _detect_modalities(lowered: str) -> list[str]:
    found: list[str] = []
    for modality, terms in MODALITY_TERMS.items():
        if any(term in lowered for term in terms):
            found.append(modality)
    return found


def _detect_geography(lowered: str) -> str:
    for term in GEOGRAPHY_TERMS:
        if re.search(rf"\b{re.escape(term)}\b", lowered):
            return term
    match = re.search(r"\bin\s+([a-z][a-z\s]{2,30})\b", lowered)
    if match:
        candidate = match.group(1).strip()
        if candidate not in {"the", "a", "an", "order"}:
            return candidate
    return ""


def _detect_time_range(lowered: str) -> dict[str, int | None]:
    result: dict[str, int | None] = {"from_year": None, "to_year": None}
    range_match = RANGE_RE.search(lowered)
    if range_match:
        result["from_year"] = int(range_match.group(1))
        result["to_year"] = int(range_match.group(2))
        return result
    since = SINCE_RE.search(lowered)
    if since:
        result["from_year"] = int(since.group(1))
    before = BEFORE_RE.search(lowered)
    if before:
        result["to_year"] = int(before.group(1))
    if not since and not before:
        last_n = LAST_N_RE.search(lowered)
        if last_n:
            count = int(last_n.group(1))
            unit = last_n.group(2).lower()
            import time

            current = int(time.strftime("%Y", time.gmtime()))
            result["from_year"] = current - count if "year" in unit else current
            result["to_year"] = current
    return result


def _detect_access(lowered: str) -> list[str]:
    found: list[str] = []
    for access_type, triggers in ACCESS_TRIGGERS.items():
        if any(trigger in lowered for trigger in triggers):
            found.append(access_type)
    return found


def _detect_expected_samples(question: str) -> str:
    match = SAMPLE_RE.search(question)
    if not match:
        return ""
    return f"{match.group(1)} {match.group(2)} (as stated by the requester)"


def _detect_objective(lowered: str) -> str:
    objective_map = (
        (("find", "discover", "locate", "collect"), "identify candidate resources"),
        (("compare", "benchmark", "evaluate", "evaluation"), "compare and evaluate"),
        (("train", "training", "fine-tune", "build a model"), "support model training"),
        (("gap", "open problem", "novelty", "unexplored"), "identify defensible research gaps"),
        (("verify", "validate", "provenance", "reproduc"), "verify provenance and reproducibility"),
        (("survey", "review", "state of the art"), "survey the state of the art"),
        (("predict", "forecast", "estimation"), "support predictive modelling"),
    )
    objectives = [label for triggers, label in objective_map if any(t in lowered for t in triggers)]
    return "; ".join(objectives) if objectives else "identify and verify relevant research resources"


def _derive_topic(question: str) -> str:
    text = question.strip().rstrip("?.!")
    for prefix in (
        "find ", "search for ", "look for ", "identify ", "get ", "show me ", "list ",
        "i want ", "i need ", "help me ", "can you ",
    ):
        if text.lower().startswith(prefix):
            text = text[len(prefix):]
            break
    words = [w for w in re.findall(r"[A-Za-z0-9\-\+_]+", text) if w.lower() not in STOPWORDS]
    topic = " ".join(words[:16]) or question[:120]
    return topic[:200]


def _detect_population(lowered: str) -> str:
    populations = {
        "patients": "clinical patients",
        "students": "students",
        "children": "children",
        "adults": "adults",
        "elderly": "older adults",
        "consumers": "consumers",
        "employees": "employees",
        "citizens": "general population",
        "residents": "local residents",
    }
    for term, label in populations.items():
        if re.search(rf"\b{term}\b", lowered):
            return label
    return ""


def _clarification_question(spec: ResearchSpec) -> str:
    if spec.missing_information:
        detail = ", ".join(spec.missing_information)
        return (
            f"The request does not yet identify {detail}. "
            "Please state the precise topic, and whether you need datasets, literature, or both — "
            "for example: 'Find publicly available datasets for detecting fraudulent financial "
            "transactions, tabular, openly licensed, 2015 onwards.'"
        )
    parts = []
    if not spec.modalities and spec.intent in {"datasets", "both"}:
        parts.append("which data modality (tabular, text, image, audio, video, time-series)")
    if not spec.geography:
        parts.append("whether a geography matters")
    if not spec.time_range.get("from_year"):
        parts.append("whether a publication or collection time range matters")
    return (
        "Search can proceed with the default assumptions listed. "
        f"To narrow the results, please specify {'; '.join(parts)}."
    )


# ---------------------------------------------------------------------------
# optional LLM refinement (language-only, validated, non-authoritative)
# ---------------------------------------------------------------------------

_LLM_SYSTEM = (
    "You help structure research requests. Return ONLY JSON. "
    "Never invent sources, DOIs, dataset counts or facts. "
    "If a field is not stated or clearly implied by the user, omit it."
)


def _llm_refine(spec: ResearchSpec) -> None:
    prompt = (
        "Extract search keywords and constraints from this research request.\n"
        'Return JSON: {"keywords": [..up to 10 additional technical synonyms..], '
        '"clarification_question": "one short sentence or empty"}\n\n'
        f"Request: {spec.question}"
    )
    data = get_llm().complete_json(prompt, system=_LLM_SYSTEM)
    if not data:
        log.info("llm_parse_skipped", reason="no valid JSON returned")
        return
    added = 0
    for keyword in data.get("keywords") or []:
        if isinstance(keyword, str) and keyword.strip():
            cleaned = keyword.strip()[:60]
            if cleaned.lower() not in {k.lower() for k in spec.keywords}:
                spec.keywords.append(cleaned)
                added += 1
    question = data.get("clarification_question")
    if isinstance(question, str) and question.strip() and not spec.clarification_question:
        spec.clarification_question = question.strip()[:400]
    spec.llm_assisted = added > 0 or bool(spec.clarification_question)
    log.info("llm_parse_applied", added_keywords=added)
