"""Retrieval orchestration.

One research run is: parse → plan → search several sources in parallel with
pagination → filter with recorded reasons → deduplicate and merge → verify →
extract evidence → assess sufficiency → persist everything with an audit trail.

Design rules enforced here:

* Adapters run independently; a failing source is recorded as a failure and
  never blocks or corrupts the rest of the run.
* Nothing is dropped silently: every rejected record appears in the ledger with
  a reason.
* "Search complete" is only claimed when every planned query answered without
  transport failure.
"""

from __future__ import annotations

import math
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Any, Callable

from ..config import get_settings
from ..db import utc_now
from ..http_client import HttpClient
from ..logging_utils import get_logger
from ..sources import AdapterRegistry, AdapterError, SearchPage, SearchQuery, get_registry
from .dedupe import DedupeResolver, cluster_summary, dedupe_key, normalize_doi
from .evidence import (
    DocumentRetriever,
    assess_dataset_sufficiency,
    assess_literature_sufficiency,
    evidence_from_record,
)
from .llm import get_llm
from .parser import ResearchSpec, parse_request
from .planning import SearchPlan, build_plan
from . import store
from .verification import Verifier

log = get_logger("services.orchestration")

MAX_TASKS = 240
MAX_EXCLUSION_SAMPLES = 40
EVIDENCE_RECORD_LIMIT = 200
FULLTEXT_RECORD_LIMIT = 6


@dataclass
class RunRequest:
    question: str
    project_id: int | None = None
    title: str = ""
    depth: int = 1
    page_size: int = 25
    adapters: list[str] | None = None
    intent: str | None = None
    verify_limit: int = 20
    max_results_per_source: int = 120
    max_queries_per_adapter: int = 4
    follow_citations: bool = True
    fetch_fulltext: bool = True
    use_llm: bool = True


@dataclass
class RunOutcome:
    project_id: int
    run_id: int
    status: str
    spec: ResearchSpec
    plan: SearchPlan
    scope: dict
    sufficiency: dict
    counts: dict
    ledger: dict
    failures: list[dict] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "project_id": self.project_id,
            "run_id": self.run_id,
            "status": self.status,
            "spec": self.spec.as_dict(),
            "plan": self.plan.as_dict(),
            "scope": self.scope,
            "sufficiency": self.sufficiency,
            "counts": self.counts,
            "ledger": self.ledger,
            "failures": self.failures,
            "warnings": self.warnings,
        }


ProgressCallback = Callable[[float, str], None]
CancelCheck = Callable[[], bool]
ProjectCallback = Callable[[int], None]


class ResearchRunner:
    def __init__(
        self,
        *,
        registry: AdapterRegistry | None = None,
        http: HttpClient | None = None,
        progress: ProgressCallback | None = None,
        cancel_check: CancelCheck | None = None,
        project_callback: ProjectCallback | None = None,
    ) -> None:
        self.settings = get_settings()
        self.registry = registry or get_registry()
        self.http = http or HttpClient(logger=get_logger("services.orchestration.http"))
        self.verifier = Verifier(self.http)
        self.retriever = DocumentRetriever(self.http)
        self.progress = progress
        self.cancel_check = cancel_check
        self.project_callback = project_callback

    # -- helpers -----------------------------------------------------------
    def _report(self, fraction: float, message: str) -> None:
        if self.progress:
            try:
                self.progress(max(0.0, min(1.0, fraction)), message)
            except Exception:  # progress must never break a run
                log.warning("progress_callback_failed", message=message)

    def _cancelled(self) -> bool:
        return bool(self.cancel_check and self.cancel_check())

    # -- main entry point --------------------------------------------------
    def run(self, request: RunRequest) -> RunOutcome:
        started = time.monotonic()
        self._report(0.02, "Parsing the research request")

        spec = parse_request(request.question, use_llm=request.use_llm)
        if request.intent in {"literature", "datasets", "both"}:
            spec.intent = request.intent

        plan = build_plan(
            spec,
            registry=self.registry,
            depth=request.depth or self.settings.default_search_depth,
            page_size=request.page_size or self.settings.default_page_size,
            adapter_names=request.adapters,
            max_results_per_source=request.max_results_per_source,
        )

        project_id = request.project_id
        if project_id:
            store.update_project_spec(project_id, spec.as_dict())
        else:
            project_id = store.create_project(request.title or spec.topic, request.question, spec.as_dict())
        run_id = store.create_run(project_id, request.question, plan.as_dict())
        if self.project_callback:
            try:
                self.project_callback(project_id)
            except Exception:  # a UI hint must never break a run
                log.warning("project_callback_failed", project_id=project_id)

        self._report(0.06, f"Planned {len(plan.queries)} query variant(s) across {len(plan.adapters)} source(s)")

        # ---- search -------------------------------------------------------
        raw_records, failures, ledger_stats = self._execute_searches(run_id, spec, plan, request)
        self._report(0.62, f"Retrieved {len(raw_records)} raw records")

        if self._cancelled():
            return self._finish_cancelled(project_id, run_id, spec, plan, ledger_stats, failures)

        # ---- filter + deduplicate + merge ---------------------------------
        kept, excluded = self._apply_filters(raw_records, spec)
        clusters, dedupe_info = _cluster(kept)
        merged = [cluster["record"] for cluster in clusters]
        self._report(
            0.70,
            f"Kept {len(kept)} of {len(raw_records)} records; {dedupe_info['duplicate_count']} duplicate(s) merged into {len(merged)} cluster(s)",
        )

        # ---- persist ------------------------------------------------------
        persisted = self._persist_records(
            run_id=run_id,
            project_id=project_id,
            clusters=clusters,
            spec=spec,
            excluded=excluded,
        )
        self._report(0.76, f"Persisted {len(persisted['canonical_ids'])} unique record(s)")

        # ---- citation-graph expansion (bounded) ---------------------------
        if request.follow_citations and not self._cancelled():
            extra = self._follow_citations(run_id, project_id, spec, persisted["canonical_ids"], request)
            if extra:
                self._report(0.80, f"Added {extra} record(s) from the citation graph")

        # ---- verification -------------------------------------------------
        if not self._cancelled():
            verified = self._verify(
                project_id, persisted["canonical_ids"], spec, request.verify_limit
            )
            self._report(0.86, f"Verified {verified} record(s) against registrars and live links")

        # ---- evidence -----------------------------------------------------
        if not self._cancelled():
            evidence_count = self._extract_evidence(
                project_id, persisted["canonical_ids"], spec, request.fetch_fulltext
            )
            self._report(0.92, f"Stored {evidence_count} evidence reference(s)")

        # ---- sufficiency gate ---------------------------------------------
        scope = self._build_scope(plan, ledger_stats, failures)
        sufficiency = self._assess_sufficiency(project_id, run_id, spec, scope)
        scope["sufficiency"] = sufficiency
        self._report(0.97, f"Evidence sufficiency: {sufficiency['status']}")

        status = "completed" if scope["completed"] else "completed_with_failures"
        if self._cancelled():
            status = "cancelled"
        store.finish_run(run_id, status, scope, failures)

        counts = store.record_kind_counts(run_id)
        outcome = RunOutcome(
            project_id=project_id,
            run_id=run_id,
            status=status,
            spec=spec,
            plan=plan,
            scope=scope,
            sufficiency=sufficiency,
            counts=counts,
            ledger=store.ledger_summary(run_id),
            failures=failures,
            warnings=_warnings(plan, scope, failures, dedupe_info),
        )
        self._report(1.0, f"Run finished in {time.monotonic() - started:.1f}s with status '{status}'")
        log.info(
            "run_completed",
            run_id=run_id,
            project_id=project_id,
            status=status,
            records=counts,
            sufficiency=sufficiency["status"],
        )
        return outcome

    # -- search phase ------------------------------------------------------
    def _execute_searches(
        self, run_id: int, spec: ResearchSpec, plan: SearchPlan, request: RunRequest
    ) -> tuple[list[dict], list[dict], dict]:
        tasks: list[tuple[Any, Any, int]] = []
        for adapter_name in plan.adapters:
            adapter = self.registry.get(adapter_name)
            if adapter is None:
                continue
            intent_key = "datasets" if adapter.kind == "dataset" else "literature"
            queries = [query for query in plan.queries if query.intent in {"both", intent_key}]
            if not queries:
                queries = plan.queries[:1]
            for query in queries[: request.max_queries_per_adapter]:
                for page_index in range(plan.depth):
                    tasks.append((adapter, query, page_index))
        tasks = tasks[:MAX_TASKS]

        total_tasks = len(tasks)
        fetched: list[dict] = []
        failures: list[dict] = []
        stats = {
            "pages_retrieved": 0,
            "queries_executed": 0,
            "results": 0,
            "empty_pages": 0,
            "unique_queries": [],
            "per_source_new": {},
        }
        done = 0

        if not total_tasks:
            return fetched, [
                {
                    "adapter": "none",
                    "reason": "no adapter matched the requested intent",
                    "kind": "configuration",
                }
            ], stats

        with ThreadPoolExecutor(max_workers=max(2, self.settings.max_concurrent_requests)) as pool:
            futures = {
                pool.submit(self._run_one_search, adapter, query, page_index, plan): (adapter, query, page_index)
                for adapter, query, page_index in tasks
            }
            for future in as_completed(futures):
                adapter, query, page_index = futures[future]
                done += 1
                if self._cancelled():
                    for pending in futures:
                        pending.cancel()
                    break
                try:
                    result = future.result()
                except Exception as exc:  # defensive: never lose the run
                    result = _FailedSearch(str(exc))
                if isinstance(result, _FailedSearch):
                    failures.append(
                        {"adapter": adapter.name, "reason": result.message, "query": query.text}
                    )
                    store.add_ledger_entry(
                        run_id,
                        source=adapter.name,
                        family=adapter.families[0],
                        query=query.text,
                        page=page_index + 1,
                        result_count=0,
                        new_records=0,
                        duration_ms=result.duration_ms,
                        status="rate_limited" if result.rate_limited else "error",
                        exclusions=[],
                        detail=result.message,
                    )
                    log.warning(
                        "source_failed", source=adapter.name, query=query.text, error=result.message
                    )
                else:
                    page, duration_ms = result
                    accepted, rejected = _filter_page(page.records, spec)
                    fetched.extend(accepted)
                    stats["pages_retrieved"] += 1
                    stats["queries_executed"] += 1
                    stats["results"] += page.raw_result_count or len(page.records)
                    if not page.records:
                        stats["empty_pages"] += 1
                    if query.text not in stats["unique_queries"]:
                        stats["unique_queries"].append(query.text)
                    stats["per_source_new"][adapter.name] = (
                        stats["per_source_new"].get(adapter.name, 0) + len(accepted)
                    )
                    store.add_ledger_entry(
                        run_id,
                        source=adapter.name,
                        family=adapter.families[0],
                        query=query.text,
                        page=page_index + 1,
                        result_count=len(page.records),
                        new_records=len(accepted),
                        duration_ms=duration_ms,
                        status="ok" if page.records else "empty",
                        exclusions=rejected[:MAX_EXCLUSION_SAMPLES],
                        detail="; ".join(page.warnings)[:500],
                    )
                self._report(
                    0.06 + 0.55 * (done / total_tasks),
                    f"Searched {done}/{total_tasks}: {adapter.name} · page {page_index + 1}",
                )

        # deduplicate failures per source for reporting
        unique_failures: list[dict] = []
        seen_sources: set[str] = set()
        for failure in failures:
            if failure["adapter"] in seen_sources:
                continue
            seen_sources.add(failure["adapter"])
            unique_failures.append(failure)
        return fetched, unique_failures, stats

    def _run_one_search(
        self, adapter: Any, query: Any, page_index: int, plan: SearchPlan
    ) -> tuple[SearchPage, int] | "_FailedSearch":
        started = time.monotonic()
        offset = page_index * plan.page_size
        search_query = SearchQuery(
            text=query.text,
            limit=plan.page_size,
            offset=offset,
            filters=plan.filters,
            origin=query.origin,
            intent=query.intent if query.intent != "both" else plan.spec.intent,
        )
        try:
            page = adapter.search(search_query)
        except AdapterError as exc:
            return _FailedSearch(
                str(exc),
                rate_limited="429" in str(exc) or "rate limited" in str(exc).lower(),
                duration_ms=int((time.monotonic() - started) * 1000),
            )
        except Exception as exc:  # unexpected adapter bug
            return _FailedSearch(
                f"unexpected adapter failure: {exc}",
                duration_ms=int((time.monotonic() - started) * 1000),
            )
        for record in page.records:
            if not record.retrieved_at:
                record.retrieved_at = utc_now()
        return page, int((time.monotonic() - started) * 1000)

    # -- filtering ---------------------------------------------------------
    def _apply_filters(self, records: list[dict], spec: ResearchSpec) -> tuple[list[dict], dict]:
        kept: list[dict] = []
        rejected: dict[str, list[dict]] = {}
        for record in records:
            reason = _rejection_reason(record, spec)
            if reason:
                rejected.setdefault(reason, []).append(
                    {
                        "title": record.get("title", "")[:160],
                        "source": record.get("source"),
                        "source_id": record.get("source_id"),
                    }
                )
                continue
            kept.append(record)
        summary = {
            reason: {"count": len(items), "samples": items[:5]}
            for reason, items in sorted(rejected.items(), key=lambda item: -len(item[1]))
        }
        return kept, summary

    # -- persistence -------------------------------------------------------
    def _persist_records(
        self,
        *,
        run_id: int,
        project_id: int,
        clusters: list[dict],
        spec: ResearchSpec,
        excluded: dict,
    ) -> dict:
        canonical_ids: list[int] = []
        duplicate_count = 0
        by_key: dict[str, int] = {}
        for cluster in clusters:
            canonical = cluster["record"]
            relevance = score_relevance(canonical, spec)
            canonical_id = store.save_record(
                run_id=run_id,
                project_id=project_id,
                record=canonical,
                relevance=relevance,
                dedupe_key=dedupe_key(canonical),
            )
            canonical_ids.append(canonical_id)
            by_key[canonical["record_key"]] = canonical_id
            for duplicate in cluster.get("duplicates", []):
                duplicate_id = store.save_record(
                    run_id=run_id,
                    project_id=project_id,
                    record=duplicate,
                    relevance=score_relevance(duplicate, spec),
                    dedupe_key=dedupe_key(duplicate),
                )
                store.mark_duplicate(duplicate_id, canonical_id)
                duplicate_count += 1
        return {
            "canonical_ids": canonical_ids,
            "by_key": by_key,
            "duplicates": duplicate_count,
            "excluded": excluded,
        }

    # -- citation graph ----------------------------------------------------
    def _follow_citations(
        self,
        run_id: int,
        project_id: int,
        spec: ResearchSpec,
        canonical_ids: list[int],
        request: RunRequest,
    ) -> int:
        adapter = self.registry.get("semantic_scholar")
        if adapter is None or spec.intent == "datasets":
            return 0
        records = store.records_by_ids(canonical_ids)
        papers = [r for r in records.values() if r["kind"] == "paper"]
        papers.sort(key=lambda r: (r.get("citation_count") or 0), reverse=True)
        targets = [p for p in papers if (p.get("raw") or {}).get("paper_id")][:3]
        if not targets:
            return 0

        known_keys = {f"{r['source']}:{r['source_id']}" for r in records.values()}
        added = 0
        started = time.monotonic()
        for paper in targets:
            if self._cancelled() or added >= 25:
                break
            paper_id = (paper.get("raw") or {}).get("paper_id")
            try:
                references = adapter.references(paper_id, limit=20)
            except Exception as exc:
                store.add_ledger_entry(
                    run_id,
                    source=adapter.name,
                    family="citation_graph",
                    query=f"references of {paper_id}",
                    page=1,
                    result_count=0,
                    new_records=0,
                    duration_ms=int((time.monotonic() - started) * 1000),
                    status="error",
                    exclusions=[],
                    detail=str(exc),
                )
                continue
            new_here = 0
            for cited in references:
                if added >= 25:
                    break
                payload = _citation_record(adapter, cited)
                if payload is None:
                    continue
                key = f"{payload['source']}:{payload['source_id']}"
                if key in known_keys:
                    continue
                known_keys.add(key)
                payload["raw"]["expansion"] = f"cited by {paper['title'][:80]}"
                store.save_record(
                    run_id=run_id,
                    project_id=project_id,
                    record=payload,
                    relevance=score_relevance(payload, spec) * 0.6,
                    dedupe_key=dedupe_key(payload),
                )
                added += 1
                new_here += 1
            store.add_ledger_entry(
                run_id,
                source=adapter.name,
                family="citation_graph",
                query=f"references of {paper.get('title', '')[:80]}",
                page=1,
                result_count=len(references),
                new_records=new_here,
                duration_ms=int((time.monotonic() - started) * 1000),
                status="ok" if references else "empty",
                exclusions=[],
                detail="citation-graph expansion (bounded to 25 records per run)",
            )
        return added

    # -- verification ------------------------------------------------------
    def _verify(
        self, project_id: int, canonical_ids: list[int], spec: ResearchSpec, limit: int
    ) -> int:
        records = store.records_by_ids(canonical_ids)
        ordered = sorted(records.values(), key=lambda r: r.get("relevance") or 0, reverse=True)
        targets = ordered[: max(0, limit)]
        for record in targets:
            if self._cancelled():
                break
            report = self.verifier.verify(record)
            for check in report.checks:
                store.save_verification(record["id"], check.as_dict(), overall=report.overall)
            if record["kind"] == "dataset":
                store.set_dataset_state(
                    record["id"],
                    "metadata_verified" if report.overall in {"verified", "partially_verified"} else "discovered",
                    f"verification overall: {report.overall}",
                )
        return len(targets)

    # -- evidence ----------------------------------------------------------
    def _extract_evidence(
        self, project_id: int, canonical_ids: list[int], spec: ResearchSpec, fetch_fulltext: bool
    ) -> int:
        records = store.records_by_ids(canonical_ids)
        ordered = sorted(records.values(), key=lambda r: r.get("relevance") or 0, reverse=True)
        total = 0
        fulltext_budget = FULLTEXT_RECORD_LIMIT if fetch_fulltext else 0
        for index, record in enumerate(ordered[:EVIDENCE_RECORD_LIMIT]):
            if self._cancelled():
                break
            items = evidence_from_record(record)
            if fulltext_budget > 0 and record["kind"] == "paper":
                passages, status = self.retriever.fetch_fulltext_passages(record, spec.keywords)
                if passages:
                    items.extend(passages)
                    fulltext_budget -= 1
                elif index < 3 and status:
                    log.info(
                        "fulltext_not_retrieved",
                        record_id=record["id"],
                        status=status,
                        note="evidence remains metadata-only for this record",
                    )
            for item in items:
                store.save_evidence(record["id"], project_id, item.as_dict())
                total += 1
        return total

    # -- scope and sufficiency --------------------------------------------
    def _build_scope(self, plan: SearchPlan, stats: dict, failures: list[dict]) -> dict:
        failed_names = {failure["adapter"] for failure in failures}
        planned = list(plan.adapters)
        searched_all = [name for name in planned if name not in failed_names]
        completed = not failures and len(planned) > 0 and stats["pages_retrieved"] > 0
        return {
            "completed": completed,
            "sources_planned": planned,
            "sources_searched": sorted(set(searched_all)),
            "sources_failed": failures,
            "pages_retrieved": stats["pages_retrieved"],
            "queries_executed": stats["queries_executed"],
            "unique_queries": stats["unique_queries"],
            "results_seen": stats["results"],
            "empty_pages": stats["empty_pages"],
            "depth_requested": plan.depth,
            "page_size": plan.page_size,
            "excluded_adapters": plan.excluded_adapters,
            "coverage_statement": (
                "Coverage is limited to the adapters listed in sources_planned. "
                "ResearchForge does not search the entire internet, and a small result set is "
                "reported as a limited search scope rather than as evidence of absence."
            ),
        }

    def _assess_sufficiency(
        self, project_id: int, run_id: int, spec: ResearchSpec, scope: dict
    ) -> dict:
        records = store.list_records(run_id=run_id, limit=800)
        counts = store.record_kind_counts(run_id)

        if spec.intent in {"datasets", "both"}:
            datasets = [r for r in records if r["kind"] == "dataset"]
            candidates = []
            for record in datasets:
                candidates.append(
                    {
                        "record_id": record["id"],
                        "title": record["title"],
                        "source": record["source"],
                        "access": record["access"],
                        "verification": {"overall": record.get("verification_status", "unverified")},
                        "modality": record.get("modality") or (record.get("dataset") or {}).get("modality") or "",
                        "license": record.get("license") or "",
                        "year": record.get("year"),
                    }
                )
            requirements = _dataset_requirements(spec, candidates)
            assessment = assess_dataset_sufficiency(
                scope=scope,
                candidates=candidates,
                requirements=requirements,
                queries=scope.get("unique_queries") or [],
            )
            assessment_dict = assessment.as_dict()
            if spec.intent == "both":
                literature = assess_literature_sufficiency(scope=scope, records=records).as_dict()
                assessment_dict["literature"] = literature
            return assessment_dict

        assessment = assess_literature_sufficiency(scope=scope, records=records)
        return assessment.as_dict()

    def _finish_cancelled(
        self,
        project_id: int,
        run_id: int,
        spec: ResearchSpec,
        plan: SearchPlan,
        stats: dict,
        failures: list[dict],
    ) -> RunOutcome:
        scope = self._build_scope(plan, stats, failures)
        scope["completed"] = False
        scope["cancelled"] = True
        sufficiency = {
            "status": "search_incomplete",
            "message": "The run was cancelled by the user before the configured scope was explored.",
            "explanation": [
                f"Pages retrieved before cancellation: {stats['pages_retrieved']}.",
                "No sufficiency conclusion is drawn from a cancelled search.",
            ],
            "sources_searched": scope["sources_searched"],
            "sources_failed": failures,
            "candidate_count": 0,
            "verified_candidate_count": 0,
        }
        scope["sufficiency"] = sufficiency
        store.finish_run(run_id, "cancelled", scope, failures)
        return RunOutcome(
            project_id=project_id,
            run_id=run_id,
            status="cancelled",
            spec=spec,
            plan=plan,
            scope=scope,
            sufficiency=sufficiency,
            counts=store.record_kind_counts(run_id),
            ledger=store.ledger_summary(run_id),
            failures=failures,
        )


class _FailedSearch:
    __slots__ = ("message", "rate_limited", "duration_ms")

    def __init__(self, message: str, rate_limited: bool = False, duration_ms: int = 0) -> None:
        self.message = message
        self.rate_limited = rate_limited
        self.duration_ms = duration_ms


# ---------------------------------------------------------------------------
# scoring and filtering helpers (deterministic, unit-tested)
# ---------------------------------------------------------------------------


def _citation_record(adapter: Any, cited: dict) -> dict | None:
    """Turn a Semantic Scholar reference entry into a normalized record payload.

    ``_to_record`` is the adapter's own mapper, reused here so that citation-graph
    records are normalized exactly like search results.
    """
    title = (cited.get("title") or "").strip()
    paper_id = cited.get("paperId")
    if not title or not paper_id:
        return None
    record = adapter._to_record(
        {
            "paperId": paper_id,
            "title": title,
            "year": cited.get("year"),
            "abstract": cited.get("abstract"),
            "externalIds": cited.get("externalIds") or {},
        }
    )
    return record.as_dict() if record else None


def score_relevance(record: dict, spec: ResearchSpec) -> float:
    """Deterministic 0..1 relevance score. No model, no guessing."""
    title = (record.get("title") or "").lower()
    abstract = (record.get("abstract") or "").lower()
    concepts = " ".join(str(c).lower() for c in (record.get("concepts") or []))
    if not spec.keywords:
        return 0.0

    hits = 0.0
    for keyword in spec.keywords[:24]:
        term = keyword.lower()
        if term in title:
            hits += 3.0
        elif term in concepts:
            hits += 1.5
        elif term in abstract:
            hits += 1.0
    coverage = min(1.0, hits / max(4.0, len(spec.keywords[:24]) * 0.75))

    modality_bonus = 0.0
    if spec.modalities:
        record_modality = (record.get("modality") or (record.get("dataset") or {}).get("modality") or "").lower()
        if record_modality and record_modality in spec.modalities:
            modality_bonus = 0.1

    access_bonus = 0.05 if record.get("access") == "open" else 0.0

    popularity = 0.0
    if record.get("kind") == "dataset":
        dataset = record.get("dataset") or {}
        downloads = dataset.get("downloads")
        if isinstance(downloads, int) and downloads > 0:
            popularity = min(0.15, math.log10(downloads + 1) / 20)
    else:
        citations = record.get("citation_count")
        if isinstance(citations, int) and citations > 0:
            popularity = min(0.15, math.log10(citations + 1) / 15)

    score = 0.7 * coverage + modality_bonus + access_bonus + popularity
    return round(min(1.0, score), 4)


def _rejection_reason(record: dict, spec: ResearchSpec) -> str:
    """Return a human-readable reason to reject a record, or '' to keep it."""
    year = record.get("year")
    from_year = spec.time_range.get("from_year")
    to_year = spec.time_range.get("to_year")
    if year and from_year and year < from_year:
        return f"published before the requested start year {from_year}"
    if year and to_year and year > to_year:
        return f"published after the requested end year {to_year}"

    if spec.intent == "datasets" and record.get("kind") != "dataset":
        return "not a dataset record while only datasets were requested"
    if spec.intent == "literature" and record.get("kind") == "dataset":
        return "dataset record while only literature was requested"

    if spec.modalities:
        record_modality = (
            record.get("modality") or (record.get("dataset") or {}).get("modality") or ""
        ).lower()
        if record_modality and record_modality not in spec.modalities:
            return (
                f"modality '{record_modality}' does not match the requested "
                f"{', '.join(spec.modalities)}"
            )
    if "open" in spec.access_requirements and record.get("access") == "restricted":
        return "access is restricted while openly accessible sources were requested"
    return ""


def _filter_page(records: list[Any], spec: ResearchSpec) -> tuple[list[dict], list[dict]]:
    """Light filter applied while searching, to keep memory and DB small."""
    accepted: list[dict] = []
    rejected: list[dict] = []
    for record in records:
        payload = record.as_dict()
        reason = _rejection_reason(payload, spec)
        if reason:
            rejected.append(
                {
                    "title": payload.get("title", "")[:160],
                    "source": payload.get("source"),
                    "source_id": payload.get("source_id"),
                    "reason": reason,
                }
            )
            continue
        accepted.append(payload)
    return accepted, rejected


def _cluster(records: list[dict]) -> tuple[list[dict], dict]:
    """Group duplicates, choose the richest record as canonical, merge gaps."""
    ordered = sorted(records, key=_richness, reverse=True)
    resolver = DedupeResolver()
    clusters: list[dict] = []
    canonical_by_key: dict[str, dict] = {}

    for record in ordered:
        canonical, decision = resolver.resolve(record)
        if canonical is None:
            cluster = {"record": record, "duplicates": [], "reason": "unique"}
            clusters.append(cluster)
            canonical_by_key[record["record_key"]] = cluster
            continue
        cluster = canonical_by_key.get(canonical["record_key"])
        if cluster is None:  # pragma: no cover - defensive
            cluster = {"record": canonical, "duplicates": [], "reason": "unique"}
            clusters.append(cluster)
            canonical_by_key[canonical["record_key"]] = cluster
        cluster["duplicates"].append(record)
        resolver.add_group_member(canonical, record)
        merge_missing_fields(canonical, record)

    info = cluster_summary(resolver.finish(), clusters)
    return clusters, info


def _richness(record: dict) -> tuple:
    dataset = record.get("dataset") or {}
    return (
        1 if record.get("doi") else 0,
        len(record.get("abstract") or ""),
        len(dataset.get("files") or []),
        1 if record.get("license") else 0,
        len(record.get("authors") or []),
        1 if record.get("access") == "open" else 0,
    )


def merge_missing_fields(canonical: dict, duplicate: dict) -> None:
    """Copy over only fields the canonical record is missing. Never overwrite."""
    for field_name in ("doi", "url", "pdf_url", "license", "abstract", "venue", "publisher", "year"):
        if not canonical.get(field_name) and duplicate.get(field_name):
            canonical[field_name] = duplicate[field_name]
    if duplicate.get("citation_count") and not canonical.get("citation_count"):
        canonical["citation_count"] = duplicate["citation_count"]
    if duplicate.get("modality") and not canonical.get("modality"):
        canonical["modality"] = duplicate["modality"]
    if duplicate.get("concepts"):
        existing = {str(c).lower() for c in canonical.get("concepts") or []}
        for concept in duplicate["concepts"]:
            if str(concept).lower() not in existing:
                canonical.setdefault("concepts", []).append(concept)
    canonical_dataset = canonical.setdefault("dataset", {})
    duplicate_dataset = duplicate.get("dataset") or {}
    for key, value in duplicate_dataset.items():
        if not canonical_dataset.get(key) and value:
            canonical_dataset[key] = value
    if duplicate.get("authors"):
        existing = {str(a).lower() for a in canonical.get("authors") or []}
        for author in duplicate["authors"]:
            if str(author).lower() not in existing:
                canonical.setdefault("authors", []).append(author)
    merged = canonical.setdefault("raw", {}).setdefault("merged_from", [])
    merged.append(
        {
            "source": duplicate.get("source"),
            "source_id": duplicate.get("source_id"),
            "url": duplicate.get("url"),
        }
    )
    if canonical.get("doi") and not canonical.get("doi_normalized"):
        canonical["doi"] = normalize_doi(canonical["doi"])


def _dataset_requirements(spec: ResearchSpec, candidates: list[dict]) -> dict:
    stated: list[str] = []
    unmet: list[str] = []
    if spec.modalities:
        stated.append(f"data modality in {', '.join(spec.modalities)}")
        if candidates and not any(
            (candidate.get("modality") or "").lower() in spec.modalities for candidate in candidates
        ):
            unmet.append(f"data modality in {', '.join(spec.modalities)}")
    if spec.time_range.get("from_year"):
        stated.append(f"published/updated from {spec.time_range['from_year']}")
        if candidates and not any(
            (candidate.get("year") or 0) >= spec.time_range["from_year"] for candidate in candidates
        ):
            unmet.append(f"published/updated from {spec.time_range['from_year']}")
    if spec.intent != "literature":
        stated.append("a verifiable source record with a stable identifier or repository link")
        if candidates and not any(candidate.get("verification", {}).get("overall") != "unverified" for candidate in candidates):
            unmet.append("independent verification of the source record")
    if spec.geography:
        stated.append(f"geography: {spec.geography}")
        unmet.append(f"geography: {spec.geography} (not stated in retrieved metadata)")
    for requirement in spec.access_requirements:
        stated.append(f"access: {requirement}")
    return {"stated": stated, "unmet": unmet}


def _warnings(plan: SearchPlan, scope: dict, failures: list[dict], dedupe_info: dict) -> list[str]:
    warnings: list[str] = []
    if failures:
        warnings.append(
            "Some sources failed; results are partial. Failed sources: "
            + ", ".join(sorted({failure["adapter"] for failure in failures}))
        )
    if not scope.get("completed"):
        warnings.append("The configured search scope was not fully explored.")
    if dedupe_info.get("duplicate_count"):
        warnings.append(
            f"{dedupe_info['duplicate_count']} duplicate record(s) were merged; the merge reasons are listed per group."
        )
    if plan.excluded_adapters:
        warnings.append(
            "Excluded adapters: "
            + ", ".join(f"{item['adapter']} ({item['reason']})" for item in plan.excluded_adapters)
        )
    warnings.append(scope.get("coverage_statement", ""))
    return [warning for warning in warnings if warning]
