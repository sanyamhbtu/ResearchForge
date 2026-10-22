"""Evidence-backed manuscript workspace.

Twelve sections are assembled from retrieved records, verification results,
evidence passages, dataset reports and gap candidates. Every section carries an
explicit status so the researcher can see what is supported, what needs their
input, what needs more research and what cannot be completed yet.

The workspace never invents results, numbers, citations, ethics approvals or
novelty claims. Where information is missing it emits a checklist item instead of
prose.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Any

from ..config import get_settings
from ..logging_utils import get_logger
from . import store
from .docx_writer import DocxDocument
from .gaps import NOT_REPORTED, build_evidence_matrix, discover_gaps

log = get_logger("services.manuscript")

STATUS_EVIDENCE = "evidence_supported"
STATUS_USER_INPUT = "needs_user_input"
STATUS_MORE_RESEARCH = "needs_research"
STATUS_CANNOT_COMPLETE = "cannot_complete_yet"

STATUS_LABELS = {
    STATUS_EVIDENCE: "Supported by retrieved evidence",
    STATUS_USER_INPUT: "Requires researcher input",
    STATUS_MORE_RESEARCH: "Requires additional research",
    STATUS_CANNOT_COMPLETE: "Cannot be completed yet",
}

SECTION_TITLES = [
    ("research_question", "1. Research question and objectives"),
    ("literature_review", "2. Literature review and related work"),
    ("candidate_gap", "3. Candidate research gap and novelty assessment"),
    ("methodology", "4. Research methodology and experimental design"),
    ("datasets", "5. Dataset sources and selection criteria"),
    ("preprocessing", "6. Preprocessing and data-quality checks"),
    ("baselines", "7. Baselines and evaluation metrics"),
    ("results", "8. Experiment logs and actual results"),
    ("discussion", "9. Discussion and comparison with existing work"),
    ("limitations", "10. Limitations, ethics and potential bias"),
    ("conclusion", "11. Conclusion and future work"),
    ("references", "12. References, acknowledgements and disclosures"),
]

GUIDELINES = {
    "clinical": [
        "CONSORT if a randomised trial is reported; STROBE for observational designs",
        "PRISMA for the systematic-search component",
        "Trial/ethics registration identifiers must be supplied by the researcher",
    ],
    "ml": [
        "ML reproducibility checklist: data provenance, splits, hyperparameters, compute, code",
        "Report dataset versions, licences and preprocessing exactly",
        "State all metrics with their definitions and aggregation",
    ],
    "default": [
        "PRISMA-style reporting of search scope, screening and inclusion decisions",
        "Report every search source, query and retrieval date",
    ],
}


@dataclass
class Section:
    key: str
    title: str
    status: str
    summary: str
    paragraphs: list[str] = field(default_factory=list)
    citations: list[str] = field(default_factory=list)
    evidence_links: list[dict] = field(default_factory=list)
    pending_items: list[str] = field(default_factory=list)
    table: dict | None = None

    def as_dict(self) -> dict:
        data = asdict(self)
        data["status_label"] = STATUS_LABELS.get(self.status, self.status)
        return data


def build_manuscript(project_id: int, run_id: int | None = None, store_result: bool = True) -> dict:
    snapshot = store.project_snapshot(project_id)
    if not snapshot:
        raise ValueError(f"project {project_id} does not exist")

    project = snapshot["project"]
    run = snapshot["latest_run"] or {}
    scope = run.get("scope") or {}
    sufficiency = (run.get("scope") or {}).get("sufficiency") or {}
    spec = project.get("spec") or {}
    records = snapshot["records"]
    papers = [record for record in records if record["kind"] == "paper"]
    datasets = [record for record in records if record["kind"] == "dataset"]

    matrix = build_evidence_matrix(records)
    gap_result = discover_gaps(matrix, scope)
    gaps = gap_result["gaps"]

    references, citation_keys = _build_references(papers + datasets)
    sections = [
        _section_research_question(spec, scope, sufficiency),
        _section_literature_review(matrix, citation_keys, gap_result),
        _section_candidate_gap(gaps, citation_keys),
        _section_methodology(spec, matrix),
        _section_datasets(datasets, spec, citation_keys),
        _section_preprocessing(datasets, snapshot["downloads"]),
        _section_baselines(matrix, citation_keys),
        _section_results(snapshot["downloads"]),
        _section_discussion(matrix, datasets, citation_keys),
        _section_limitations(matrix, scope, sufficiency, citation_keys),
        _section_conclusion(gaps),
        _section_references(references, scope, sufficiency, gap_result),
    ]

    manuscript = {
        "project_id": project_id,
        "title": f"Draft manuscript — {project['title']}",
        "question": project["question"],
        "generated_at": run.get("finished_at") or run.get("started_at"),
        "run_id": run.get("id"),
        "sections": [section.as_dict() for section in sections],
        "references": references,
        "checklist": build_checklist(spec, scope, sufficiency, records, datasets, references),
        "status_counts": _status_counts(sections),
        "disclaimer": (
            "This is a draft assembled from retrieved, cited evidence. It is not a guarantee of "
            "publication, it contains no fabricated results, and every section marked as pending "
            "must be completed by the researcher. Experiments reported as pending were never run by "
            "ResearchForge."
        ),
    }
    if store_result:
        store.upsert_manuscript(
            project_id,
            manuscript["title"],
            manuscript["sections"],
            manuscript["checklist"],
        )
    log.info("manuscript_built", project_id=project_id, sections=len(sections))
    return manuscript


def _status_counts(sections: list[Section]) -> dict:
    counts = {status: 0 for status in STATUS_LABELS}
    for section in sections:
        counts[section.status] = counts.get(section.status, 0) + 1
    return counts


# ---------------------------------------------------------------------------
# sections
# ---------------------------------------------------------------------------


def _section_research_question(spec: dict, scope: dict, sufficiency: dict) -> Section:
    paragraphs = [
        f"Research question as submitted: {spec.get('question', '')}",
        f"Interpreted topic: {spec.get('topic', '')}",
        f"Objective(s) identified from the request: {spec.get('objective', '')}",
        f"Domain: {spec.get('domain', 'unspecified')} | Intent: {spec.get('intent', '')}",
        f"Data types/modalities of interest: {', '.join(spec.get('modalities') or []) or 'not specified'}",
        f"Geography: {spec.get('geography') or 'not specified'} | Time range: {_time_range(spec)}",
        f"Stated constraints: {'; '.join(spec.get('constraints') or []) or 'none'}",
    ]
    pending = []
    if spec.get("assumptions"):
        pending.append("Confirm the assumptions recorded during parsing: " + "; ".join(spec["assumptions"]))
    if spec.get("clarification_question"):
        pending.append(spec["clarification_question"])
    pending.append("Convert the objective into measurable hypotheses — this requires the researcher.")
    return Section(
        key="research_question",
        title="1. Research question and objectives",
        status=STATUS_USER_INPUT if pending else STATUS_EVIDENCE,
        summary="Parsed from the submitted request; the measurable hypotheses still need the researcher.",
        paragraphs=paragraphs,
        pending_items=pending,
        evidence_links=[{"scope": scope, "sufficiency": sufficiency}] if scope else [],
    )


def _section_literature_review(matrix: list[dict], citation_keys: dict, gap_result: dict) -> Section:
    if not matrix:
        return Section(
            key="literature_review",
            title="2. Literature review and related work",
            status=STATUS_MORE_RESEARCH,
            summary="No literature records with retrievable content were available.",
            pending_items=[
                "Run the search with an intent that includes literature, or add a literature source.",
                gap_result.get("reason", ""),
            ],
        )

    groups: dict[str, list[dict]] = {}
    for entry in matrix:
        key = _theme(entry)
        groups.setdefault(key, []).append(entry)

    paragraphs: list[str] = []
    citations: list[str] = []
    for theme, entries in sorted(groups.items(), key=lambda item: -len(item[1]))[:8]:
        parts = []
        for entry in entries[:6]:
            contribution = entry.get("claimed_contribution")
            if contribution == NOT_REPORTED:
                contribution = (
                    f"the retrieved abstract does not state a contribution "
                    f"(title: “{entry['title'][:120]}”)"
                )
            parts.append(f"{contribution} {citation_keys.get(entry['record_key'], '')}".strip())
            citations.append(citation_keys.get(entry["record_key"], ""))
        paragraphs.append(f"Theme “{theme}”: " + " ".join(parts))

    methods = [entry for entry in matrix if entry.get("methods") != NOT_REPORTED]
    if methods:
        paragraphs.append(
            "Methods reported in the retrieved abstracts: "
            + "; ".join(str(entry["methods"])[:180] for entry in methods[:6])
        )
    pending = []
    if len(matrix) < 5:
        pending.append(
            "Only a small number of records were retrieved; extend the search before treating this "
            "review as representative."
        )
    pending.append("Verify that each cited claim is supported by the cited passage — see the evidence viewer.")
    return Section(
        key="literature_review",
        title="2. Literature review and related work",
        status=STATUS_EVIDENCE,
        summary=f"{len(matrix)} record(s) with retrievable content, grouped into {len(groups)} theme(s).",
        paragraphs=paragraphs,
        citations=sorted(set(c for c in citations if c)),
        pending_items=pending,
    )


def _section_candidate_gap(gaps: list[dict], citation_keys: dict) -> Section:
    if not gaps:
        return Section(
            key="candidate_gap",
            title="3. Candidate research gap and novelty assessment",
            status=STATUS_MORE_RESEARCH,
            summary="No gap candidate could be supported by the retrieved evidence.",
            pending_items=[
                "Widen the search scope (more sources, greater depth) and re-run before claiming novelty.",
                "Novelty cannot be established from a limited search — record the search scope as a limitation.",
            ],
        )
    paragraphs = []
    citations = []
    for gap in gaps:
        support = ", ".join(
            f"{record['title'][:80]} ({citation_keys.get(record['record_key'], '')})"
            for record in gap.get("supporting_records", [])[:4]
        )
        counter = "; ".join(
            f"{item['title'][:80]} — {item.get('claim', '')[:120]}"
            for item in gap.get("counterevidence", [])[:3]
        )
        paragraphs.append(
            f"[{gap['gap_type']}] {gap['proposed_gap']} Supporting evidence: {support}. "
            f"Counter-evidence found in the same search: {counter or 'none found in the retrieved set'}. "
            f"Feasible research question: {gap['feasible_research_question']} "
            f"Methodology: {gap['proposed_methodology']} "
            f"Additional requirements: {'; '.join(gap.get('additional_requirements') or [])}. "
            f"Uncertainty: {'; '.join(gap.get('uncertainty') or [])}"
        )
        citations.extend(citation_keys.get(record["record_key"], "") for record in gap.get("supporting_records", []))
    return Section(
        key="candidate_gap",
        title="3. Candidate research gap and novelty assessment",
        status=STATUS_EVIDENCE,
        summary=(
            f"{len(gaps)} gap candidate(s), each with supporting passages and counter-evidence. "
            "Novelty is asserted relative to the recorded search scope only."
        ),
        paragraphs=paragraphs,
        citations=sorted(set(c for c in citations if c)),
        pending_items=[
            "Confirm that no pre-print or non-indexed work already covers the candidate gap.",
            "State the search scope explicitly whenever novelty is claimed.",
        ],
    )


def _section_methodology(spec: dict, matrix: list[dict]) -> Section:
    reported = [
        entry["methods"]
        for entry in matrix
        if entry.get("methods") != NOT_REPORTED
    ]
    paragraphs = [
        "ResearchForge cannot design the study for you; it can show what comparable work used.",
        "Reported methods in the retrieved set: "
        + ("; ".join(str(method)[:180] for method in reported[:6]) if reported else "none reported in the retrieved abstracts"),
    ]
    pending = [
        "State the design (experimental, observational, computational), the unit of analysis and the sampling frame.",
        "Define inclusion/exclusion criteria and the analysis plan before data collection.",
        "Pre-register the protocol if the design permits.",
        "Specify the statistical test and the smallest effect size of interest.",
    ]
    return Section(
        key="methodology",
        title="4. Research methodology and experimental design",
        status=STATUS_USER_INPUT,
        summary="Researcher-owned section; only comparable reported methods are surfaced as input.",
        paragraphs=paragraphs,
        pending_items=pending,
    )


def _section_datasets(datasets: list[dict], spec: dict, citation_keys: dict) -> Section:
    if not datasets:
        return Section(
            key="datasets",
            title="5. Dataset sources and selection criteria",
            status=STATUS_MORE_RESEARCH,
            summary="No dataset records were retrieved.",
            pending_items=[
                "Run the search with the dataset intent, or enable more dataset sources.",
                "If no dataset satisfies the requirements, the manuscript must record that a data "
                "collection step is required.",
            ],
        )
    rows = []
    citations = []
    for record in datasets[:25]:
        dataset = record.get("dataset") or {}
        rows.append(
            [
                record["title"][:70],
                record["source"],
                record.get("license") or "unknown",
                record.get("access") or "unknown",
                str(dataset.get("samples") if dataset.get("samples") is not None else "not published"),
                record.get("dataset_state") or "discovered",
                citation_keys.get(record["record_key"], ""),
            ]
        )
        citations.append(citation_keys.get(record["record_key"], ""))
    pending = []
    if any(not (record.get("license") or "").strip() for record in datasets):
        pending.append("Resolve missing licences before using or redistributing any dataset.")
    if any((record.get("dataset_state") or "discovered") == "discovered" for record in datasets):
        pending.append("Complete the download/verification stage for datasets still at 'discovered'.")
    pending.append("Record the selection criteria you apply, and the datasets you rejected and why.")
    return Section(
        key="datasets",
        title="5. Dataset sources and selection criteria",
        status=STATUS_EVIDENCE,
        summary=f"{len(datasets)} dataset candidate(s) with provenance, licence and verification state.",
        paragraphs=[
            "Only values actually published by the source are shown; 'not published' means the source "
            "did not state it and it was not inferred.",
        ],
        table={
            "headers": ["Dataset", "Source", "Licence", "Access", "Samples", "State", "Citation"],
            "rows": rows,
        },
        citations=sorted(set(c for c in citations if c)),
        pending_items=pending,
    )


def _section_preprocessing(datasets: list[dict], downloads: list[dict]) -> Section:
    inspected = [item for item in downloads if item["status"] == "verified"]
    paragraphs = []
    if not inspected:
        paragraphs.append(
            "No dataset file has been downloaded and inspected, so no preprocessing facts can be "
            "reported. Any preprocessing statement here would be unsupported."
        )
    for item in inspected[:10]:
        profile = (item.get("members") or [{}])[0]
        checks = profile.get("checks") or []
        dimensions = profile.get("dimensions") or []
        paragraphs.append(
            f"{item.get('target_path') or item['url']}: SHA-256 {item.get('sha256')}; "
            f"{len(checks)} integrity check(s); "
            + "; ".join(
                f"{dimension.get('name')}: {json.dumps(dimension.get('value'))[:160]}"
                if isinstance(dimension.get("value"), (dict, list))
                else f"{dimension.get('name')}: {dimension.get('note', '')[:160]}"
                for dimension in dimensions[:3]
            )
        )
    pending = [
        "Document the exact preprocessing steps applied and version them.",
        "Report how missing values, duplicates and outliers are handled.",
        "Specify the train/validation/test split rule and seed.",
    ]
    if not inspected:
        pending.append("Download and inspect at least one dataset before writing this section.")
    return Section(
        key="preprocessing",
        title="6. Preprocessing and data-quality checks",
        status=STATUS_EVIDENCE if inspected else STATUS_CANNOT_COMPLETE,
        summary=(
            f"{len(inspected)} inspected file(s)."
            if inspected
            else "No downloaded content to inspect, so this section cannot be completed."
        ),
        paragraphs=paragraphs,
        pending_items=pending,
    )


def _section_baselines(matrix: list[dict], citation_keys: dict) -> Section:
    with_metrics = [entry for entry in matrix if entry.get("evaluation_metrics") != NOT_REPORTED]
    rows = []
    citations = []
    for entry in with_metrics[:20]:
        rows.append(
            [
                entry["title"][:70],
                entry.get("methods", NOT_REPORTED)[:120],
                entry.get("evaluation_metrics", NOT_REPORTED)[:120],
                citation_keys.get(entry["record_key"], ""),
            ]
        )
        citations.append(citation_keys.get(entry["record_key"], ""))
    return Section(
        key="baselines",
        title="7. Baselines and evaluation metrics",
        status=STATUS_EVIDENCE if with_metrics else STATUS_USER_INPUT,
        summary=(
            f"{len(with_metrics)} record(s) state evaluation metrics in the retrieved text."
            if with_metrics
            else "No metric statements were found in the retrieved text; baselines must be chosen by the researcher."
        ),
        paragraphs=[
            "Metrics below are quoted from the retrieved text. ResearchForge does not compute, round or "
            "interpolate results.",
        ]
        if with_metrics
        else [],
        table={
            "headers": ["Study", "Method", "Reported evaluation", "Citation"],
            "rows": rows,
        }
        if rows
        else None,
        citations=sorted(set(c for c in citations if c)),
        pending_items=[
            "Choose the primary metric and justify it before running experiments.",
            "Reproduce at least one reported baseline on your own split.",
        ],
    )


def _section_results(downloads: list[dict]) -> Section:
    return Section(
        key="results",
        title="8. Experiment logs and actual results",
        status=STATUS_CANNOT_COMPLETE,
        summary="Pending: no experiments have been executed by ResearchForge, so no results exist.",
        paragraphs=[
            "This section is intentionally empty. ResearchForge does not run experiments and will not "
            "generate synthetic results, significance tests or sample sizes."
        ],
        pending_items=[
            "Run the experiments and record: command, environment, seeds, hardware, runtime.",
            "Log every metric with its definition, aggregation and confidence interval.",
            "Report negative and failed runs as well as successful ones.",
            "Attach raw logs as a reproducibility appendix.",
        ],
    )


def _section_discussion(matrix: list[dict], datasets: list[dict], citation_keys: dict) -> Section:
    paragraphs = []
    limitations = [
        (entry, limitation)
        for entry in matrix
        for limitation in (entry.get("stated_limitations") or [])
    ][:10]
    if limitations:
        paragraphs.append(
            "Author-stated limitations collected from the retrieved abstracts: "
            + " ".join(
                f"“{limitation}” {citation_keys.get(entry['record_key'], '')}"
                for entry, limitation in limitations
            )
        )
    conflicts = [entry for entry in matrix if entry.get("conflicting_findings") != NOT_REPORTED]
    if conflicts:
        paragraphs.append(
            "Statements indicating disagreement or failed reproduction: "
            + " ".join(
                f"“{entry['conflicting_findings']}” {citation_keys.get(entry['record_key'], '')}"
                for entry in conflicts[:6]
            )
        )
    else:
        paragraphs.append(
            "No retrieved abstract contained a statement of conflicting findings. Absence of such a "
            "statement is not evidence that results are consistent."
        )
    return Section(
        key="discussion",
        title="9. Discussion and comparison with existing work",
        status=STATUS_EVIDENCE if paragraphs else STATUS_MORE_RESEARCH,
        summary=f"{len(limitations)} limitation statement(s) and {len(conflicts)} conflict statement(s) available.",
        paragraphs=paragraphs,
        citations=sorted(
            set(citation_keys.get(entry["record_key"], "") for entry, _ in limitations if citation_keys.get(entry["record_key"]))
        ),
        pending_items=[
            "Compare your results against the reproduced baselines, not against quoted numbers.",
            "Where sources disagree, report the disagreement and explain the implication.",
        ],
        evidence_links=[{"dataset_count": len(datasets)}],
    )


def _section_limitations(matrix: list[dict], scope: dict, sufficiency: dict, citation_keys: dict) -> Section:
    searched = ", ".join(scope.get("sources_searched") or []) or "no source"
    paragraphs = [
        "System-level limitations of this draft:",
        f"• Search scope: {searched}; {scope.get('queries_executed', 0)} query execution(s) across "
        f"{scope.get('pages_retrieved', 0)} retrieved page(s). Coverage is limited to these adapters.",
        f"• Evidence sufficiency assessment at generation time: {sufficiency.get('status', 'unknown')}.",
        "• Evidence is metadata-level unless a full text was retrievable and is labelled "
        "'retrieved_full_text'; every other passage is quoted from source metadata.",
        "• Records with no retrievable content are listed with no claims attached.",
    ]
    if scope.get("sources_failed"):
        paragraphs.append(
            "• Sources that failed during the run: "
            + ", ".join(str(item.get("adapter")) for item in scope["sources_failed"])
        )
    paragraphs.append(
        "• Research ethics approval, consent, data-protection compliance and bias assessment must be "
        "supplied by the researcher and were not assessed here."
    )
    return Section(
        key="limitations",
        title="10. Limitations, ethics and potential bias",
        status=STATUS_EVIDENCE,
        summary="Limitations of the search and of the draft, plus placeholders the researcher must complete.",
        paragraphs=paragraphs,
        pending_items=[
            "Assess dataset bias with respect to the intended population.",
            "State ethics approval, consent and data-protection status (or explicitly state that none was required).",
            "Declare funding, competing interests and author contributions.",
        ],
    )


def _section_conclusion(gaps: list[dict]) -> Section:
    return Section(
        key="conclusion",
        title="11. Conclusion and future work",
        status=STATUS_CANNOT_COMPLETE,
        summary="Pending: a conclusion requires results that have not been produced.",
        paragraphs=[
            "ResearchForge will not write a conclusion before experiments exist. The candidate gaps "
            "below are starting points, not findings:"
        ]
        + [f"• {gap['proposed_gap'][:220]}" for gap in gaps[:5]],
        pending_items=[
            "Write the conclusion only after the results section is complete.",
            "Future work should reference the candidate gaps with their evidence links.",
        ],
    )


def _section_references(
    references: list[dict], scope: dict, sufficiency: dict, gap_result: dict
) -> Section:
    unverified = [reference for reference in references if reference["verification"] in {"unverified", "failed", "conflict"}]
    paragraphs = [
        f"{len(references)} bibliographic record(s) were collected; each carries a source URL and "
        "retrieval timestamp. References were resolved against Crossref/DataCite where a DOI exists.",
        "Acknowledgements: acknowledge data providers and repository operators whose terms apply.",
        "Disclosures: declare AI assistance, funding and competing interests.",
    ]
    return Section(
        key="references",
        title="12. References, acknowledgements and disclosures",
        status=STATUS_EVIDENCE if references else STATUS_MORE_RESEARCH,
        summary=f"{len(references)} reference(s); {len(unverified)} still need verification before submission.",
        paragraphs=paragraphs,
        table={
            "headers": ["#", "Reference", "Identifier", "Verification"],
            "rows": [
                [reference["index"], reference["text"], reference["identifier"], reference["verification"]]
                for reference in references[:60]
            ],
        }
        if references
        else None,
        pending_items=(
            ["Verify or remove the references that are not yet confirmed."] if unverified else []
        )
        + [
            "Check every citation supports the sentence it is attached to — a valid DOI is not proof of support.",
        ],
        evidence_links=[{"scope_statement": gap_result.get("scope_statement", "")}],
    )


# ---------------------------------------------------------------------------
# references and checklist
# ---------------------------------------------------------------------------


def _build_references(records: list[dict]) -> tuple[list[dict], dict]:
    references: list[dict] = []
    citation_keys: dict[str, str] = {}
    for record in sorted(records, key=lambda item: (item.get("year") or 0), reverse=True):
        index = len(references) + 1
        authors = record.get("authors") or []
        author_text = ", ".join(str(author) for author in authors[:6]) or "author not published"
        if len(authors) > 6:
            author_text += " et al."
        text = (
            f"{author_text} ({record.get('year') or 'n.d.'}). {record.get('title', '')}. "
            f"{record.get('venue') or record.get('publisher') or record.get('source')}."
        )
        identifier = record.get("doi") or f"{record.get('source')}:{record.get('source_id')}"
        references.append(
            {
                "index": index,
                "key": record["record_key"],
                "text": text,
                "identifier": identifier,
                "url": record.get("url") or "",
                "license": record.get("license") or "",
                "retrieved_at": record.get("retrieved_at") or "",
                "verification": record.get("verification_status") or "unverified",
                "kind": record["kind"],
            }
        )
        citation_keys[record["record_key"]] = f"[{index}]"
    return references, citation_keys


def build_checklist(
    spec: dict,
    scope: dict,
    sufficiency: dict,
    records: list[dict],
    datasets: list[dict],
    references: list[dict],
) -> list[dict]:
    domain = (spec.get("domain") or "").lower()
    if any(term in domain for term in ("healthcare", "clinical")):
        guideline = GUIDELINES["clinical"]
    elif any(term in domain for term in ("vision", "nlp", "graph", "finance", "energy", "security", "education")):
        guideline = GUIDELINES["ml"]
    else:
        guideline = GUIDELINES["default"]

    unverified = [r for r in references if r["verification"] in {"unverified", "failed", "conflict"}]
    items = [
        {
            "item": "Search scope, sources and queries are documented",
            "satisfied": bool(scope.get("sources_searched")) and bool(scope.get("unique_queries")),
            "evidence": f"{len(scope.get('unique_queries') or [])} query execution(s) recorded in the ledger",
        },
        {
            "item": "Evidence sufficiency was assessed before reporting findings",
            "satisfied": bool(sufficiency),
            "evidence": sufficiency.get("status", "not assessed"),
        },
        {
            "item": "Every citation resolves to a verified bibliographic record",
            "satisfied": not unverified,
            "evidence": f"{len(unverified)} of {len(references)} reference(s) not yet verified",
        },
        {
            "item": "Dataset provenance, licence and version are recorded for each dataset used",
            "satisfied": bool(datasets) and all(record.get("url") for record in datasets),
            "evidence": f"{len(datasets)} dataset record(s) with source URLs",
        },
        {
            "item": "Dataset licences are known and compatible with the intended use",
            "satisfied": bool(datasets) and all((record.get("license") or "").strip() for record in datasets),
            "evidence": "every selected dataset must declare a licence",
        },
        {
            "item": "Experiment results exist and are reproducible from recorded artefacts",
            "satisfied": False,
            "evidence": "no experiments were executed by ResearchForge",
        },
        {
            "item": "Reporting guideline applied",
            "satisfied": False,
            "evidence": " | ".join(guideline),
        },
        {
            "item": "Ethics, consent, funding and competing interests declared",
            "satisfied": False,
            "evidence": "researcher-supplied; not assessed by ResearchForge",
        },
    ]
    return items


# ---------------------------------------------------------------------------
# export
# ---------------------------------------------------------------------------


def export_manuscript(project_id: int, fmt: str = "docx") -> tuple[bytes, str, str]:
    manuscript = build_manuscript(project_id, store_result=False)
    fmt = (fmt or "docx").lower()
    if fmt == "md":
        return manuscript_markdown(manuscript).encode("utf-8"), "manuscript.md", "text/markdown"
    if fmt == "tex":
        return manuscript_latex(manuscript).encode("utf-8"), "manuscript.tex", "application/x-tex"
    if fmt == "json":
        return json.dumps(manuscript, indent=2, ensure_ascii=False).encode("utf-8"), "manuscript.json", "application/json"
    return manuscript_docx(manuscript), "manuscript.docx", (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    )


def manuscript_markdown(manuscript: dict) -> str:
    lines = [f"# {manuscript['title']}", "", f"> {manuscript['disclaimer']}", ""]
    for section in manuscript["sections"]:
        lines.append(f"## {section['title']}")
        lines.append(f"*Status: {section['status_label']}*")
        lines.append("")
        lines.append(section["summary"])
        lines.append("")
        for paragraph in section.get("paragraphs") or []:
            lines.append(paragraph)
            lines.append("")
        table = section.get("table")
        if table:
            lines.append("| " + " | ".join(table["headers"]) + " |")
            lines.append("|" + "---|" * len(table["headers"]))
            for row in table["rows"]:
                lines.append("| " + " | ".join(str(cell).replace("|", "\\|") for cell in row) + " |")
            lines.append("")
        if section.get("pending_items"):
            lines.append("**Pending / requires input**")
            lines.extend(f"- {item}" for item in section["pending_items"])
            lines.append("")
    lines.append("## Readiness checklist")
    lines.append("")
    for item in manuscript["checklist"]:
        marker = "x" if item["satisfied"] else " "
        lines.append(f"- [{marker}] {item['item']} — {item['evidence']}")
    lines.append("")
    lines.append("## References")
    lines.append("")
    for reference in manuscript["references"]:
        lines.append(
            f"{reference['index']}. {reference['text']} {reference['identifier']} "
            f"{reference['url']} (retrieved {reference['retrieved_at']}; verification: {reference['verification']})"
        )
    return "\n".join(lines)


def manuscript_latex(manuscript: dict) -> str:
    def escape_tex(text: str) -> str:
        replacements = {
            "\\": r"\textbackslash{}", "&": r"\&", "%": r"\%", "$": r"\$", "#": r"\#",
            "_": r"\_", "{": r"\{", "}": r"\}", "~": r"\textasciitilde{}", "^": r"\textasciicircum{}",
        }
        return "".join(replacements.get(char, char) for char in str(text))

    lines = [
        r"\documentclass[11pt]{article}",
        r"\usepackage[utf8]{inputenc}",
        r"\usepackage[margin=1in]{geometry}",
        r"\usepackage{booktabs}",
        r"\usepackage{hyperref}",
        r"\begin{document}",
        rf"\title{{{escape_tex(manuscript['title'])}}}",
        r"\maketitle",
        rf"\noindent\textit{{{escape_tex(manuscript['disclaimer'])}}}",
        r"\medskip",
    ]
    for section in manuscript["sections"]:
        lines.append(rf"\section*{{{escape_tex(section['title'])}}}")
        lines.append(rf"\textit{{Status: {escape_tex(section['status_label'])}}}\par")
        lines.append(escape_tex(section["summary"]))
        lines.append("")
        for paragraph in section.get("paragraphs") or []:
            lines.append(escape_tex(paragraph))
            lines.append("")
        table = section.get("table")
        if table:
            columns = "l" * len(table["headers"])
            lines.append(rf"\begin{{tabular}}{{{columns}}}")
            lines.append(r"\toprule")
            lines.append(" & ".join(escape_tex(header) for header in table["headers"]) + r" \\")
            lines.append(r"\midrule")
            for row in table["rows"]:
                lines.append(" & ".join(escape_tex(cell) for cell in row) + r" \\")
            lines.append(r"\bottomrule")
            lines.append(r"\end{tabular}")
            lines.append("")
        if section.get("pending_items"):
            lines.append(r"\paragraph{Pending / requires input}")
            lines.append(r"\begin{itemize}")
            lines.extend(rf"\item {escape_tex(item)}" for item in section["pending_items"])
            lines.append(r"\end{itemize}")
    lines.append(r"\section*{Readiness checklist}")
    lines.append(r"\begin{itemize}")
    for item in manuscript["checklist"]:
        mark = "OK" if item["satisfied"] else "PENDING"
        lines.append(rf"\item [{mark}] {escape_tex(item['item'])} — {escape_tex(item['evidence'])}")
    lines.append(r"\end{itemize}")
    lines.append(r"\section*{References}")
    lines.append(r"\begin{enumerate}")
    for reference in manuscript["references"]:
        lines.append(
            rf"\item {escape_tex(reference['text'])} \texttt{{{escape_tex(reference['identifier'])}}} "
            rf"\url{{{reference['url']}}} (verification: {escape_tex(reference['verification'])})"
        )
    lines.append(r"\end{enumerate}")
    lines.append(r"\end{document}")
    return "\n".join(lines)


def manuscript_docx(manuscript: dict) -> bytes:
    document = DocxDocument()
    document.title(manuscript["title"])
    document.paragraph(manuscript["disclaimer"], italic=True)
    document.paragraph("")
    for section in manuscript["sections"]:
        document.heading(section["title"], level=1)
        document.paragraph(f"Status: {section['status_label']}", bold=True)
        document.paragraph(section["summary"])
        for paragraph in section.get("paragraphs") or []:
            document.paragraph(paragraph)
        table = section.get("table")
        if table:
            document.table(table["headers"], table["rows"][:60])
        if section.get("pending_items"):
            document.heading("Pending / requires input", level=2)
            for item in section["pending_items"]:
                document.bullet(item)
    document.heading("Readiness checklist", level=1)
    for item in manuscript["checklist"]:
        document.bullet(f"[{'x' if item['satisfied'] else ' '}] {item['item']} — {item['evidence']}")
    document.heading("References", level=1)
    for reference in manuscript["references"]:
        document.paragraph(
            f"{reference['index']}. {reference['text']} {reference['identifier']} {reference['url']} "
            f"(retrieved {reference['retrieved_at']}; verification: {reference['verification']})"
        )
    return document.render()


def _theme(entry: dict) -> str:
    concepts = entry.get("methods") or entry.get("title") or "general"
    words = [word for word in str(concepts).replace(",", " ").split() if len(word) > 4]
    return " ".join(words[:3]).lower() or "general"


def _time_range(spec: dict) -> str:
    span = spec.get("time_range") or {}
    start = span.get("from_year")
    end = span.get("to_year")
    if not start and not end:
        return "not restricted"
    return f"{start or 'any'}–{end or 'present'}"


def manuscript_path(project_id: int, fmt: str) -> str:
    settings = get_settings()
    return str(settings.data_dir / f"manuscript_project_{project_id}.{fmt}")
