"""Evidence-based research-gap discovery.

A gap is only reported when the retrieved content supports it. Every extracted
field carries the verbatim sentence it came from plus its locator, URL and
retrieval date. Fields that were not found are reported as *not reported in the
retrieved text* — never inferred.

Counter-evidence is searched for before any gap is presented as promising, and
a small result set is described as a limited search scope, never as proof that a
topic is globally unexplored.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Iterable

from ..logging_utils import get_logger
from . import store
from .lexicon import STOPWORDS

log = get_logger("services.gaps")

NOT_REPORTED = "not reported in the retrieved text"

_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9])")

CONTRIBUTION_PATTERNS = (
    r"\bwe (?:propose|present|introduce|develop|design|build|release)\b",
    r"\bthis (?:paper|study|work|article) (?:proposes|presents|introduces|reports|investigates|examines)\b",
    r"\bour contribution\b",
    r"\bwe (?:show|demonstrate|find|observe)\b",
)
METHOD_VERB_PATTERNS = (
    r"\bwe (?:use|used|employ|train|fine-tune|fine tune|evaluate|compare|apply)\b",
)
METHOD_MODEL_PATTERNS = (
    r"\b(?:random forest|logistic regression|svm|support vector|xgboost|gradient boosting|"
    r"convolutional|transformer|bert|graph neural network|gan|autoencoder|k-means|lstm|cnn|rnn)\b",
)
METHOD_PATTERNS = METHOD_VERB_PATTERNS + METHOD_MODEL_PATTERNS
DATASET_PATTERNS = (
    r"\b(?:dataset|corpus|benchmark|cohort)d? (?:called |named )?[A-Z][\w\-]+",
    r"\b(?:on|using|from|with) the [A-Z][\w\-]+ (?:dataset|corpus|benchmark|cohort)\b",
    r"\b(?:MIMIC|ImageNet|CIFAR|MNIST|PhysioNet|UK Biobank|UCI|Kaggle|OpenAlex|PubMed)\b",
)
METRIC_PATTERN = re.compile(
    r"\b(?:accuracy|f1|f-1|precision|recall|auc|auroc|rmse|mae|rmse|map|iou|bleu|rouge|"
    r"sensitivity|specificity)\b[^.]{0,40}?\d",
    re.I,
)
LIMITATION_PATTERNS = (
    r"\blimitation",
    r"\bwe did not\b",
    r"\bnot (?:possible|yet|addressed|explored|investigated)\b",
    r"\bremains? (?:an? )?(?:open|unclear|unknown|challeng)",
    r"\bhowever\b",
    r"\black of\b",
    r"\bcannot\b",
    r"\bunable to\b",
    r"\bshortcoming",
    r"\bconstrain",
)
FUTURE_PATTERNS = (
    r"\bfuture work\b",
    r"\bfurther (?:research|study|work|investigation)\b",
    r"\bshould be (?:explored|investigated|studied|addressed)\b",
    r"\bwe (?:leave|recommend|suggest|encourage)\b",
    r"\bnext steps\b",
    r"\bremains? to be\b",
)
DATA_AVAILABILITY_PATTERNS = (
    r"\bdata (?:is|are) (?:publicly )?available\b",
    r"\bwe (?:release|make available|publish)\b[^.]{0,60}\b(?:data|dataset|code)\b",
    r"\b(?:dataset|data) (?:can be|is) (?:downloaded|accessed)\b",
)
CODE_AVAILABILITY_PATTERNS = (
    r"\bcode (?:is|was) (?:publicly )?available\b",
    r"github\.com/",
    r"\bwe (?:release|open-?source|publish)\b[^.]{0,40}\bcode\b",
    r"\bgitlab\.com/",
    r"\bzenodo\.org/\b",
)
REPLICATION_PATTERNS = (
    r"\bwe (?:replicate|reproduce|repeat)\b",
    r"\breplication\b",
    r"\breproducib",
)
CONFLICT_PATTERNS = (
    r"\bcontrary to\b",
    r"\binconsistent with\b",
    r"\b(?:we )?(?:failed|were unable) to reproduce\b",
    r"\bcontradicts?\b",
    r"\bdisagree",
)

GAP_TYPE_AUTHOR_STATED = "author_stated_future_work"
GAP_TYPE_STATED_LIMITATION = "stated_limitation"
GAP_TYPE_EVALUATION_CONCENTRATION = "evaluation_concentration"
GAP_TYPE_UNVERIFIED_REPLICATION = "replication_uncertainty"


@dataclass
class MatrixEntry:
    record_id: int
    record_key: str
    title: str
    source: str
    url: str
    year: int | None
    research_question: str = NOT_REPORTED
    claimed_contribution: str = NOT_REPORTED
    methods: str = NOT_REPORTED
    datasets_used: str = NOT_REPORTED
    baselines: str = NOT_REPORTED
    evaluation_metrics: str = NOT_REPORTED
    results_reported: str = NOT_REPORTED
    uncertainty: str = NOT_REPORTED
    stated_limitations: list[str] = field(default_factory=list)
    stated_future_work: list[str] = field(default_factory=list)
    data_availability: str = NOT_REPORTED
    code_availability: str = NOT_REPORTED
    replication_evidence: str = NOT_REPORTED
    conflicting_findings: str = NOT_REPORTED
    passages: list[dict] = field(default_factory=list)
    retrieval_note: str = ""

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass
class GapCandidate:
    gap_type: str
    proposed_gap: str
    supporting_records: list[dict] = field(default_factory=list)
    supporting_passages: list[dict] = field(default_factory=list)
    counterevidence: list[dict] = field(default_factory=list)
    why_it_matters: str = ""
    uncertainty: list[str] = field(default_factory=list)
    feasible_research_question: str = ""
    proposed_methodology: str = ""
    additional_requirements: list[str] = field(default_factory=list)
    confidence_basis: str = ""

    def as_dict(self) -> dict:
        return asdict(self)


def build_evidence_matrix(records: Iterable[dict]) -> list[dict]:
    matrix: list[dict] = []
    for record in records:
        if record.get("kind") != "paper":
            continue
        matrix.append(_extract_matrix_entry(record).as_dict())
    log.info("evidence_matrix_built", entries=len(matrix))
    return matrix


def _extract_matrix_entry(record: dict) -> MatrixEntry:
    abstract = (record.get("abstract") or "").strip()
    entry = MatrixEntry(
        record_id=record["id"],
        record_key=f"{record.get('source')}:{record.get('source_id')}",
        title=record.get("title") or "",
        source=record.get("source") or "",
        url=record.get("url") or "",
        year=record.get("year"),
    )
    if not abstract:
        entry.retrieval_note = (
            "No abstract or full text was retrievable for this record; every field is therefore "
            "reported as not reported. The bibliographic record itself is still verified separately."
        )
        return entry

    sentences = [sentence.strip() for sentence in _SENTENCE_SPLIT.split(abstract) if sentence.strip()]
    for index, sentence in enumerate(sentences, start=1):
        lowered = sentence.lower()
        locator = f"abstract, sentence {index}"
        if entry.research_question == NOT_REPORTED and re.search(
            r"\b(?:we|this study|this paper) (?:aim|aimed|seek|investigate|examine|study)\b", lowered
        ):
            entry.research_question = sentence
        if entry.claimed_contribution == NOT_REPORTED and _matches(sentence, CONTRIBUTION_PATTERNS):
            entry.claimed_contribution = sentence
        if entry.datasets_used == NOT_REPORTED and _matches(sentence, DATASET_PATTERNS):
            entry.datasets_used = sentence
        if entry.evaluation_metrics == NOT_REPORTED and METRIC_PATTERN.search(sentence):
            entry.evaluation_metrics = sentence
        if entry.results_reported == NOT_REPORTED and METRIC_PATTERN.search(sentence):
            entry.results_reported = sentence
        if entry.code_availability == NOT_REPORTED and _matches(sentence, CODE_AVAILABILITY_PATTERNS):
            entry.code_availability = sentence
        if entry.data_availability == NOT_REPORTED and _matches(sentence, DATA_AVAILABILITY_PATTERNS):
            entry.data_availability = sentence
        if entry.replication_evidence == NOT_REPORTED and _matches(sentence, REPLICATION_PATTERNS):
            entry.replication_evidence = sentence
        if entry.conflicting_findings == NOT_REPORTED and _matches(sentence, CONFLICT_PATTERNS):
            entry.conflicting_findings = sentence
        if _matches(sentence, LIMITATION_PATTERNS):
            entry.stated_limitations.append(sentence)
        if _matches(sentence, FUTURE_PATTERNS):
            entry.stated_future_work.append(sentence)
        if sentence not in [passage["text"] for passage in entry.passages]:
            entry.passages.append(
                {
                    "text": sentence,
                    "locator": locator,
                    "url": entry.url,
                    "retrieved_at": record.get("retrieved_at") or "",
                    "record_key": entry.record_key,
                }
            )

    # Methods are resolved in two tiers so an explicit methodological sentence
    # ("we evaluate on ...") is preferred over a sentence that merely names a
    # model while proposing the contribution.
    for patterns in (METHOD_VERB_PATTERNS, METHOD_MODEL_PATTERNS):
        if entry.methods != NOT_REPORTED:
            break
        for sentence in sentences:
            if _matches(sentence, patterns):
                entry.methods = sentence
                break

    if entry.uncertainty == NOT_REPORTED:
        for sentence in sentences:
            if re.search(r"\b(?:may|could|might|uncertain|confidence interval|p\s*[=<]|not significant)\b", sentence, re.I):
                entry.uncertainty = sentence
                break
    entry.passages = entry.passages[:40]
    return entry


def _matches(sentence: str, patterns: tuple[str, ...]) -> bool:
    return any(re.search(pattern, sentence, re.I) for pattern in patterns)


# ---------------------------------------------------------------------------
# gap discovery
# ---------------------------------------------------------------------------


def discover_gaps(matrix: list[dict], scope: dict, max_gaps: int = 6) -> dict:
    if not matrix:
        return {
            "gaps": [],
            "status": "insufficient_evidence",
            "scope_statement": _scope_statement(scope),
            "reason": "No literature records with retrievable content were available, so no gap can be supported.",
            "method": _METHOD_NOTE,
        }

    groups: list[dict] = []
    for entry in matrix:
        for statement in entry.get("stated_future_work") or []:
            _add_statement(groups, statement, entry, GAP_TYPE_AUTHOR_STATED)
        for statement in entry.get("stated_limitations") or []:
            _add_statement(groups, statement, entry, GAP_TYPE_STATED_LIMITATION)

    groups.sort(key=lambda item: (-len(item["entries"]), -len(item["statement"])))
    gaps: list[GapCandidate] = []
    for group in groups[:max_gaps]:
        gaps.append(_gap_from_group(group, matrix, scope))

    concentration = _evaluation_concentration_gap(matrix, scope)
    if concentration:
        gaps.append(concentration)

    replication = _replication_gap(matrix, scope)
    if replication:
        gaps.append(replication)

    gaps.sort(key=lambda gap: (-len(gap.supporting_records), gap.gap_type))
    log.info("gaps_discovered", gaps=len(gaps), groups=len(groups))
    return {
        "gaps": [gap.as_dict() for gap in gaps],
        "status": "evidence_supported" if gaps else "insufficient_evidence",
        "scope_statement": _scope_statement(scope),
        "reason": (
            ""
            if gaps
            else "The retrieved abstracts contained no author-stated limitation, future work or "
            "evaluation-concentration signal that could support a gap claim."
        ),
        "method": _METHOD_NOTE,
    }


_METHOD_NOTE = (
    "Gaps are derived only from statements found in retrieved source text (author-stated "
    "limitations and future work), and from distributional evidence such as repeated evaluation on "
    "the same dataset. Counter-evidence is searched for in the same retrieved set before a gap is "
    "presented. No gap is inferred from a topic simply returning few results."
)


def _add_statement(groups: list[dict], statement: str, entry: dict, gap_type: str) -> None:
    key = _statement_key(statement)
    if not key:
        return
    for group in groups:
        if group["statement_key"] == key and group["gap_type"] == gap_type:
            if entry["record_id"] not in [item["record_id"] for item in group["entries"]]:
                group["entries"].append(entry)
            return
    groups.append({"statement_key": key, "statement": statement, "gap_type": gap_type, "entries": [entry]})


def _statement_key(statement: str) -> str:
    tokens = [
        token
        for token in re.findall(r"[a-z][a-z\-]{2,}", statement.lower())
        if token not in STOPWORDS
    ]
    # Keep the most distinctive tokens so differently-worded but equivalent
    # statements are grouped together.
    return " ".join(sorted(set(tokens))[:6])


def _gap_from_group(group: dict, matrix: list[dict], scope: dict) -> GapCandidate:
    statement = group["statement"]
    entries = group["entries"]
    supporting_records = [
        {
            "record_id": entry["record_id"],
            "record_key": entry["record_key"],
            "title": entry["title"],
            "source": entry["source"],
            "url": entry["url"],
            "year": entry["year"],
        }
        for entry in entries
    ]
    passages = [
        {
            "record_key": entry["record_key"],
            "title": entry["title"],
            "text": statement,
            "locator": _locate_passage(entry, statement),
            "url": entry["url"],
        }
        for entry in entries[:1]
    ]
    for entry in entries:
        for passage in entry.get("passages") or []:
            if passage["text"] == statement:
                passages[0]["locator"] = passage["locator"]
    counterevidence = _counterevidence(entries, matrix, statement)

    gap_type_label = (
        "stated by the authors as future work"
        if group["gap_type"] == GAP_TYPE_AUTHOR_STATED
        else "stated by the authors as a limitation"
    )
    return GapCandidate(
        gap_type=group["gap_type"],
        proposed_gap=statement,
        supporting_records=supporting_records,
        supporting_passages=passages,
        counterevidence=counterevidence,
        why_it_matters=(
            "The authors of the supporting record(s) identify this as unresolved, which means the "
            "limitation is acknowledged in the literature rather than assumed by this tool."
        ),
        uncertainty=[
            f"Supported by {len(entries)} record(s) in this search only.",
            _scope_statement(scope),
            "Whether other teams have already addressed it outside the searched scope was not established.",
        ],
        feasible_research_question=_research_question(statement),
        proposed_methodology=_methodology(statement, entries),
        additional_requirements=[
            "Locate a dataset that exposes the variables named in the limitation.",
            "Establish a baseline reproduction of the supporting study before extending it.",
            "Pre-register the evaluation protocol, splits and metrics.",
        ],
        confidence_basis=gap_type_label,
    )


def _counterevidence(entries: list[dict], matrix: list[dict], statement: str) -> list[dict]:
    """Records in the same retrieved set that appear to address the same topic."""
    terms = {
        token
        for token in re.findall(r"[a-z][a-z\-]{3,}", statement.lower())
        if token not in STOPWORDS
    }
    if not terms:
        return []
    supporting_ids = {entry["record_id"] for entry in entries}
    found = []
    for entry in matrix:
        if entry["record_id"] in supporting_ids:
            continue
        haystack = " ".join(
            [
                entry.get("title") or "",
                entry.get("claimed_contribution") or "",
                entry.get("methods") or "",
            ]
        ).lower()
        overlap = sum(1 for term in terms if term in haystack)
        if overlap >= 2 and (entry.get("claimed_contribution") or NOT_REPORTED) != NOT_REPORTED:
            found.append(
                {
                    "record_id": entry["record_id"],
                    "record_key": entry["record_key"],
                    "title": entry["title"],
                    "url": entry["url"],
                    "claim": entry["claimed_contribution"],
                    "overlap_terms": sorted(term for term in terms if term in haystack)[:6],
                }
            )
    found.sort(key=lambda item: -len(item["overlap_terms"]))
    return found[:5]


def _evaluation_concentration_gap(matrix: list[dict], scope: dict) -> GapCandidate | None:
    dataset_mentions: dict[str, list[dict]] = {}
    for entry in matrix:
        statement = entry.get("datasets_used") or ""
        if statement == NOT_REPORTED:
            continue
        for name in re.findall(r"\b([A-Z][\w\-]{2,})\b", statement):
            key = name.lower()
            if key in STOPWORDS:
                continue
            dataset_mentions.setdefault(key, []).append(entry)
    if not dataset_mentions:
        return None
    total = len(matrix)
    if total < 3:
        return None
    name, entries = max(dataset_mentions.items(), key=lambda item: len(item[1]))
    if len(entries) < 3 or len(entries) / total < 0.4:
        return None
    return GapCandidate(
        gap_type=GAP_TYPE_EVALUATION_CONCENTRATION,
        proposed_gap=(
            f"Evaluation in the retrieved set concentrates on '{name}' "
            f"({len(entries)} of {total} records mention it); behaviour on other datasets is not "
            "established by this evidence."
        ),
        supporting_records=[
            {
                "record_id": entry["record_id"],
                "record_key": entry["record_key"],
                "title": entry["title"],
                "source": entry["source"],
                "url": entry["url"],
            }
            for entry in entries
        ],
        supporting_passages=[
            {
                "record_key": entry["record_key"],
                "title": entry["title"],
                "text": entry["datasets_used"],
                "locator": "abstract, dataset mention",
                "url": entry["url"],
            }
            for entry in entries[:6]
        ],
        counterevidence=[
            {
                "record_id": entry["record_id"],
                "record_key": entry["record_key"],
                "title": entry["title"],
                "url": entry["url"],
                "claim": entry.get("datasets_used") or NOT_REPORTED,
                "note": "mentions a different dataset",
            }
            for entry in matrix
            if entry not in entries and (entry.get("datasets_used") or NOT_REPORTED) != NOT_REPORTED
        ][:5],
        why_it_matters=(
            "Concentrated evaluation limits external validity: results may not transfer to other "
            "populations, sensors or collection periods."
        ),
        uncertainty=[
            "Dataset names were extracted from abstracts only; methods sections may evaluate more broadly.",
            _scope_statement(scope),
        ],
        feasible_research_question=(
            f"How do reported results change when the same models are evaluated on datasets other "
            f"than '{name}'?"
        ),
        proposed_methodology=(
            "Re-run the most comparable published models under an identical protocol on at least two "
            "independent datasets, and report metric deltas with confidence intervals."
        ),
        additional_requirements=[
            "Datasets independent from the dominant benchmark, with compatible labels.",
            "Access to the model implementations, or a faithful reimplementation.",
        ],
        confidence_basis="distributional evidence from retrieved abstracts",
    )


def _replication_gap(matrix: list[dict], scope: dict) -> GapCandidate | None:
    unreported = [entry for entry in matrix if entry.get("code_availability") == NOT_REPORTED]
    reported = [entry for entry in matrix if entry.get("code_availability") != NOT_REPORTED]
    if not unreported or len(matrix) < 3:
        return None
    if len(unreported) / len(matrix) < 0.6:
        return None
    return GapCandidate(
        gap_type=GAP_TYPE_UNVERIFIED_REPLICATION,
        proposed_gap=(
            f"{len(unreported)} of {len(matrix)} retrieved records do not state code or artefact "
            "availability in their abstract, so independent reproduction is not demonstrable from "
            "the retrieved evidence."
        ),
        supporting_records=[
            {
                "record_id": entry["record_id"],
                "record_key": entry["record_key"],
                "title": entry["title"],
                "source": entry["source"],
                "url": entry["url"],
            }
            for entry in unreported
        ][:10],
        supporting_passages=[
            {
                "record_key": entry["record_key"],
                "title": entry["title"],
                "text": "No code/artefact availability statement found in the retrieved abstract.",
                "locator": "abstract (absence of an availability statement)",
                "url": entry["url"],
            }
            for entry in unreported[:6]
        ],
        counterevidence=[
            {
                "record_id": entry["record_id"],
                "record_key": entry["record_key"],
                "title": entry["title"],
                "url": entry["url"],
                "claim": entry["code_availability"],
                "note": "does state availability",
            }
            for entry in reported
        ][:5],
        why_it_matters=(
            "Reproducibility is a precondition for relying on reported results; abstract-level "
            "absence is a signal to check the full text, not proof that artefacts do not exist."
        ),
        uncertainty=[
            "Availability statements are often in the full text or the paper's repository page, which "
            "was not retrieved for every record.",
            _scope_statement(scope),
        ],
        feasible_research_question=(
            "Are the reported results of this literature reproducible from the published artefacts, "
            "and where exactly does reproduction fail?"
        ),
        proposed_methodology=(
            "Attempt reproduction study per selected paper: obtain artefacts or contact authors, "
            "re-run with reported settings, log deviations, and report success rate."
        ),
        additional_requirements=[
            "Full-text access for each selected paper.",
            "Compute resources comparable to the original work.",
        ],
        confidence_basis="absence of availability statements in retrieved abstracts",
    )


def _locate_passage(entry: dict, statement: str) -> str:
    for passage in entry.get("passages") or []:
        if passage["text"] == statement:
            return passage["locator"]
    return "abstract"


def _research_question(statement: str) -> str:
    cleaned = statement.strip().rstrip(".")
    return (
        f"To what extent can the following stated limitation be resolved: “{cleaned}”? "
        "Formulate it as a measurable hypothesis with a defined dataset and metric."
    )


def _methodology(statement: str, entries: list[dict]) -> str:
    methods = [entry.get("methods") for entry in entries if (entry.get("methods") or NOT_REPORTED) != NOT_REPORTED]
    base = (
        "Reproduce the baseline reported by the supporting record(s) first, then introduce a "
        "controlled change that directly targets the stated limitation, holding the data splits and "
        "evaluation protocol fixed."
    )
    if methods:
        base += " Reported baseline methods in the supporting record(s): " + " | ".join(
            str(method)[:160] for method in methods[:3]
        )
    return base


def _scope_statement(scope: dict) -> str:
    searched = ", ".join(scope.get("sources_searched") or []) or "no source"
    return (
        f"Search scope: {searched}; {int(scope.get('queries_executed') or 0)} query execution(s) across "
        f"{int(scope.get('pages_retrieved') or 0)} retrieved page(s). A small result set reflects this "
        "scope and is not evidence that a topic is globally unexplored."
    )


def build_and_store_gaps(project_id: int, run_id: int | None, scope: dict) -> dict:
    records = store.list_records(project_id=project_id, limit=1000)
    matrix = build_evidence_matrix(records)
    result = discover_gaps(matrix, scope)
    if result["gaps"]:
        store.save_gap_candidates(project_id, run_id, result["gaps"])
    result["evidence_matrix"] = matrix
    result["records_considered"] = len(records)
    return result
