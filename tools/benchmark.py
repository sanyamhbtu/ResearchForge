"""Offline benchmark / evaluation harness for ResearchForge.

Runs the *real* pipeline (parse -> plan -> multi-source search -> filter ->
deduplicate -> persist -> verify -> evidence -> sufficiency gate) against
human-reviewed ground truth, and reports the metrics the project specification
requires:

* precision / recall / F1 of retrieved records against judged-relevant records,
* metadata accuracy (field-level agreement of the stored record with the judged
  metadata),
* citation / identity correctness (duplicate resolution and identifier
  preservation),
* suitability (required resources surfaced, incompatible counterexamples
  excluded),
* frequency of unsupported claims (claims with no passage, locator or URL).

It is deliberately offline: every source payload comes from
``tools/benchmark_suite.json`` and is labelled there as a synthetic fixture in
the documented shape of that source's API. A metric produced by this harness
describes the *pipeline*, not the live internet, and the report says so.

Usage::

    python tools/benchmark.py            # run, write reports, print a summary
    python tools/benchmark.py --json     # also print the machine-readable report
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urlparse

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app import http_client  # noqa: E402
from app.config import Settings, override_settings  # noqa: E402
from app.db import Database, set_db  # noqa: E402
from app.http_client import HttpClient, HttpError, HttpResponse  # noqa: E402
from app.services import store  # noqa: E402
from app.services.evidence import INSUFFICIENT_DATASET_MESSAGE  # noqa: E402
from app.services.orchestration import ResearchRunner, RunRequest  # noqa: E402
from app.sources import AdapterRegistry  # noqa: E402
from app.sources.crossref import CrossrefAdapter  # noqa: E402
from app.sources.datacite import DataCiteAdapter  # noqa: E402
from app.sources.huggingface import HuggingFaceAdapter  # noqa: E402
from app.sources.openalex import OpenAlexAdapter  # noqa: E402
from app.sources.zenodo import ZenodoAdapter  # noqa: E402

SUITE_PATH = Path(__file__).resolve().parent / "benchmark_suite.json"
REPORT_DIR = PROJECT_ROOT / "reports"

ADAPTER_FACTORIES = {
    "openalex": OpenAlexAdapter,
    "crossref": CrossrefAdapter,
    "zenodo": ZenodoAdapter,
    "huggingface": HuggingFaceAdapter,
    "datacite": DataCiteAdapter,
}


# ---------------------------------------------------------------------------
# fixture HTTP transport
# ---------------------------------------------------------------------------


def _fixture_key(method: str, url: str) -> str:
    """Stable key for a fixture: ``METHOD host/path`` (query strings ignored)."""
    parsed = urlparse(url)
    return f"{method.upper()} {parsed.netloc.lower()}{parsed.path}"


class FixtureTransport(HttpClient):
    """Serves recorded-shape payloads instead of performing network I/O.

    An un-fixtured URL raises :class:`HttpError`, exactly as a transport failure
    would. The pipeline therefore records it as a failure / inconclusive check
    rather than silently inventing a result.
    """

    def __init__(self, fixtures: dict[str, dict]) -> None:
        super().__init__(cache_enabled=False)
        self._fixtures = {_fixture_key(*key.split(" ", 1)): value for key, value in fixtures.items()}
        self.unmatched: list[str] = []
        self.calls: list[dict] = []

    def request(  # type: ignore[override]
        self,
        method: str,
        url: str,
        *,
        params: Any = None,
        headers: Any = None,
        accept: str = "application/json",
        body: bytes | None = None,
        use_cache: bool | None = None,
        cache_ttl: int | None = None,
        cache_key: str | None = None,
        raise_for_status: bool = True,
        allowed_status: tuple[int, ...] = (200, 201, 202, 203, 206),
    ) -> HttpResponse:
        full_url = url
        if params:
            filtered = {k: v for k, v in params.items() if v is not None and v != ""}
            if filtered:
                full_url = f"{url}{'&' if '?' in url else '?'}{urlencode(filtered, doseq=True)}"
        key = _fixture_key(method, url)
        self.calls.append({"method": method.upper(), "url": full_url, "fixture": key})

        entry = self._fixtures.get(key)
        if entry is None:
            self.unmatched.append(key)
            raise HttpError(f"no benchmark fixture for {key}", url=full_url)

        status = int(entry.get("status", 200))
        if "text" in entry:
            payload = str(entry["text"]).encode("utf-8")
        else:
            payload = json.dumps(entry.get("json")).encode("utf-8")
        headers_out = {"content-type": "application/json; charset=utf-8"}
        if status >= 400 and raise_for_status and status not in allowed_status:
            raise HttpError(f"HTTP {status} for {full_url}", url=full_url, status=status)
        return HttpResponse(url=full_url, status=status, headers=headers_out, body=payload)


# ---------------------------------------------------------------------------
# comparison helpers
# ---------------------------------------------------------------------------


def _norm(text: Any) -> str:
    return " ".join(str(text or "").lower().split())


def _norm_doi(value: Any) -> str:
    text = str(value or "").strip().lower()
    for prefix in ("https://doi.org/", "http://doi.org/", "doi:", "https://dx.doi.org/"):
        if text.startswith(prefix):
            text = text[len(prefix):]
    return text


def _identifier(record: dict) -> str:
    return f"{record.get('source')}:{record.get('source_id')}"


def _canonical_root(row: dict, by_id: dict[int, dict]) -> dict:
    """Follow ``duplicate_of`` links to the record this row collapsed into."""
    seen: set[int] = set()
    current = row
    while True:
        parent_id = current.get("duplicate_of")
        if not parent_id or parent_id in seen:
            return current
        seen.add(current.get("id"))
        parent = by_id.get(parent_id)
        if parent is None:
            return current
        current = parent


def _matches_ground_truth(record: dict, item: dict) -> bool:
    if item.get("doi") and _norm_doi(record.get("doi")) == _norm_doi(item["doi"]):
        return True
    return _identifier(record) == item.get("identifier")


def _field_agreement(record: dict, item: dict) -> tuple[int, int, list[str]]:
    """Compare declared ground-truth fields with the stored record."""
    matched = 0
    total = 0
    mismatches: list[str] = []
    checks = (
        ("title", lambda r: _norm(r.get("title")), lambda g: _norm(g.get("title"))),
        ("year", lambda r: r.get("year"), lambda g: g.get("year")),
        ("doi", lambda r: _norm_doi(r.get("doi")), lambda g: _norm_doi(g.get("doi"))),
        ("license", lambda r: _norm(r.get("license")), lambda g: _norm(g.get("license"))),
        ("access", lambda r: _norm(r.get("access")), lambda g: _norm(g.get("access"))),
    )
    for name, get_record, get_truth in checks:
        if not item.get(name):
            continue
        total += 1
        if get_record(record) == get_truth(item):
            matched += 1
        else:
            mismatches.append(f"{name}: expected {get_truth(item)!r}, stored {get_record(record)!r}")
    if item.get("modality"):
        total += 1
        stored = _norm(record.get("modality") or (record.get("dataset") or {}).get("modality"))
        if stored == _norm(item["modality"]):
            matched += 1
        else:
            mismatches.append(f"modality: expected {item['modality']!r}, stored {stored!r}")
    return matched, total, mismatches


# ---------------------------------------------------------------------------
# per-case benchmark
# ---------------------------------------------------------------------------


@dataclass
class CaseResult:
    case_id: str
    question: str
    status: str
    sufficiency_status: str
    sufficiency_message: str
    exact_message_used: bool
    returned_records: int
    relevant_total: int
    relevant_found: int
    true_positives: int
    false_positives: int
    precision: float | None
    recall: float | None
    f1: float | None
    metadata_matched: int
    metadata_total: int
    metadata_mismatches: list[str] = field(default_factory=list)
    duplicate_pairs_expected: int = 0
    duplicate_pairs_correct: int = 0
    unexpected_merges: int = 0
    identifier_preservation: float = 1.0
    required_total: int = 0
    required_found: int = 0
    counterexamples_excluded: int = 0
    counterexamples_excluded_total: int = 0
    claims_total: int = 0
    unsupported_claims: int = 0
    failures: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return dict(self.__dict__)


def _evaluate_case(case: dict, project_id: int, outcome) -> CaseResult:
    ground_truth = case["ground_truth"]
    relevant_items = ground_truth.get("relevant") or []
    counterexamples = ground_truth.get("counterexamples") or []
    expected_pairs = [tuple(pair) for pair in ground_truth.get("expected_duplicate_pairs") or []]

    all_records = store.list_records(project_id=project_id, include_duplicates=True, limit=1000)
    canonical = [record for record in all_records if not record.get("is_duplicate")]

    # -- precision / recall -------------------------------------------------
    matched_truth: set[int] = set()
    true_positives = 0
    for record in canonical:
        for index, item in enumerate(relevant_items):
            if _matches_ground_truth(record, item):
                matched_truth.add(index)
                true_positives += 1
                break
    false_positives = len(canonical) - true_positives
    relevant_total = len(relevant_items)
    relevant_found = len(matched_truth)
    precision = (true_positives / len(canonical)) if canonical else None
    recall = (relevant_found / relevant_total) if relevant_total else None
    f1 = None
    if precision is not None and recall is not None and (precision + recall) > 0:
        f1 = round(2 * precision * recall / (precision + recall), 4)
    if precision is not None:
        precision = round(precision, 4)
    if recall is not None:
        recall = round(recall, 4)

    # -- metadata accuracy --------------------------------------------------
    metadata_matched = 0
    metadata_total = 0
    metadata_mismatches: list[str] = []
    for index in sorted(matched_truth):
        item = relevant_items[index]
        best = None
        for record in canonical:
            if _matches_ground_truth(record, item):
                matched, total, mismatches = _field_agreement(record, item)
                if best is None or matched > best[0]:
                    best = (matched, total, mismatches)
        if best:
            metadata_matched += best[0]
            metadata_total += best[1]
            metadata_mismatches.extend(f"{item['identifier']} -> {text}" for text in best[2])

    # -- citation / identity correctness -----------------------------------
    by_id = {record.get("id"): record for record in all_records}
    by_identifier = {_identifier(record): record for record in all_records}
    duplicate_pairs_correct = 0
    for left, right in expected_pairs:
        left_row = by_identifier.get(left)
        right_row = by_identifier.get(right)
        if not left_row or not right_row or left_row.get("id") == right_row.get("id"):
            continue
        if _canonical_root(left_row, by_id).get("id") == _canonical_root(right_row, by_id).get("id"):
            duplicate_pairs_correct += 1
    expected_pair_set = {frozenset(pair) for pair in expected_pairs}
    unexpected_merges = 0
    for record in all_records:
        if not record.get("duplicate_of"):
            continue
        canonical_row = by_id.get(record["duplicate_of"])
        if canonical_row is None:
            continue
        record_identifier = _identifier(record)
        canonical_identifier = _identifier(canonical_row)
        if record_identifier == canonical_identifier:
            # The same source record retrieved by a second expanded query: merging
            # it with itself is correct behaviour, not an unexpected merge.
            continue
        if frozenset({record_identifier, canonical_identifier}) not in expected_pair_set:
            unexpected_merges += 1
    preserved = sum(
        1
        for record in canonical
        if (record.get("doi") or record.get("source_id"))
        and record.get("url")
        and record.get("retrieved_at")
    )
    identifier_preservation = round(preserved / len(canonical), 4) if canonical else 1.0

    # -- suitability --------------------------------------------------------
    required_items = [item for item in relevant_items if item.get("required")]
    required_found = sum(
        1 for item in required_items if any(_matches_ground_truth(r, item) for r in canonical)
    )
    excluded = [item for item in counterexamples if item.get("disposition") == "excluded"]
    counterexamples_excluded = sum(
        1
        for item in excluded
        if not any(_matches_ground_truth(record, item) for record in canonical)
    )

    # -- unsupported claims -------------------------------------------------
    evidence = store.get_evidence(project_id=project_id)
    unsupported = 0
    for item in evidence:
        if not (item.get("passage") and item.get("locator") and item.get("url")):
            unsupported += 1

    sufficiency = outcome.sufficiency or {}
    failures: list[str] = []
    expect = case.get("expect") or {}
    if expect.get("sufficiency_status") and sufficiency.get("status") != expect["sufficiency_status"]:
        failures.append(
            f"sufficiency status {sufficiency.get('status')!r} != expected "
            f"{expect['sufficiency_status']!r}"
        )
    if expect.get("exact_message") and sufficiency.get("message") != expect["exact_message"]:
        failures.append("the exact insufficient-evidence message was not used verbatim")
    if expect.get("min_precision") is not None and (precision is None or precision < expect["min_precision"]):
        failures.append(f"precision {precision} < required {expect['min_precision']}")
    if expect.get("min_recall") is not None and (recall is None or recall < expect["min_recall"]):
        failures.append(f"recall {recall} < required {expect['min_recall']}")
    if expect.get("max_records") is not None and len(canonical) > expect["max_records"]:
        failures.append(f"{len(canonical)} records returned, maximum expected {expect['max_records']}")
    if any(_matches_ground_truth(r, item) for item in excluded for r in canonical):
        failures.append("an incompatible counterexample survived the filters")
    if unsupported:
        failures.append(f"{unsupported} claim(s) stored without a passage, locator or URL")

    return CaseResult(
        case_id=case["id"],
        question=case["question"],
        status=outcome.status,
        sufficiency_status=sufficiency.get("status", ""),
        sufficiency_message=sufficiency.get("message", ""),
        exact_message_used=bool(sufficiency.get("exact_message_used")),
        returned_records=len(canonical),
        relevant_total=relevant_total,
        relevant_found=relevant_found,
        true_positives=true_positives,
        false_positives=false_positives,
        precision=precision,
        recall=recall,
        f1=f1,
        metadata_matched=metadata_matched,
        metadata_total=metadata_total,
        metadata_mismatches=metadata_mismatches,
        duplicate_pairs_expected=len(expected_pairs),
        duplicate_pairs_correct=duplicate_pairs_correct,
        unexpected_merges=unexpected_merges,
        identifier_preservation=identifier_preservation,
        required_total=len(required_items),
        required_found=required_found,
        counterexamples_excluded=counterexamples_excluded,
        counterexamples_excluded_total=len(excluded),
        claims_total=len(evidence),
        unsupported_claims=unsupported,
        failures=failures,
    )


def run_suite(suite: dict, *, verbose: bool = True) -> dict:
    """Run every case against a throwaway database. Returns the report dict."""
    with tempfile.TemporaryDirectory(prefix="rf_benchmark_", ignore_cleanup_errors=True) as tmp:
        root = Path(tmp)
        data = root / "data"
        data.mkdir(parents=True, exist_ok=True)
        settings = Settings(
            project_root=root,
            host="127.0.0.1",
            port=0,
            open_browser=False,
            data_dir=data,
            db_path=data / "benchmark.db",
            cache_dir=data / "cache",
            download_dir=data / "downloads",
            http_timeout=5.0,
            http_max_retries=0,
            http_backoff_base=0.0,
            cache_ttl_seconds=0,
            default_search_depth=1,
            default_page_size=10,
            max_results_per_source=20,
        )
        settings.ensure_dirs()
        override_settings(settings)
        db = Database(settings.db_path)
        db.init()
        set_db(db)
        http_client.reset_http_clients()

        results: list[CaseResult] = []
        transport_stats: dict[str, dict] = {}
        try:
            for case in suite["cases"]:
                transport = FixtureTransport(case.get("fixtures") or {})
                registry = AdapterRegistry()
                for name in case.get("adapters") or []:
                    factory = ADAPTER_FACTORIES.get(name)
                    if factory is None:
                        raise SystemExit(f"unknown adapter in benchmark suite: {name}")
                    registry.register(factory(http=transport))
                runner = ResearchRunner(registry=registry, http=transport)
                outcome = runner.run(
                    RunRequest(
                        question=case["question"],
                        title=case["id"],
                        depth=case.get("depth", 1),
                        page_size=case.get("page_size", 10),
                        adapters=case.get("adapters"),
                        intent=case.get("intent"),
                        verify_limit=case.get("verify_limit", 10),
                        max_results_per_source=20,
                        max_queries_per_adapter=2,
                        follow_citations=False,
                        fetch_fulltext=False,
                        use_llm=False,
                    )
                )
                result = _evaluate_case(case, outcome.project_id, outcome)
                results.append(result)
                transport_stats[case["id"]] = {
                    "fixture_calls": len(transport.calls),
                    "unmatched_urls": sorted(set(transport.unmatched)),
                }
                if verbose:
                    print(
                        f"  [{ 'PASS' if not result.failures else 'FAIL' }] {result.case_id}: "
                        f"{result.returned_records} record(s), sufficiency={result.sufficiency_status}"
                    )
        finally:
            conn = getattr(db._local, "conn", None)
            if conn is not None:
                try:
                    conn.close()
                finally:
                    db._local.conn = None

    return _aggregate(suite, results, transport_stats)


def _aggregate(suite: dict, results: list[CaseResult], transport_stats: dict) -> dict:
    total_canonical = sum(r.returned_records for r in results)
    total_tp = sum(r.true_positives for r in results)
    total_relevant = sum(r.relevant_total for r in results)
    total_found = sum(r.relevant_found for r in results)
    precision = round(total_tp / total_canonical, 4) if total_canonical else None
    recall = round(total_found / total_relevant, 4) if total_relevant else None
    f1 = None
    if precision is not None and recall is not None and (precision + recall) > 0:
        f1 = round(2 * precision * recall / (precision + recall), 4)

    metadata_total = sum(r.metadata_total for r in results)
    metadata_matched = sum(r.metadata_matched for r in results)
    claims_total = sum(r.claims_total for r in results)
    unsupported = sum(r.unsupported_claims for r in results)
    pairs_expected = sum(r.duplicate_pairs_expected for r in results)
    pairs_correct = sum(r.duplicate_pairs_correct for r in results)
    unexpected_merges = sum(r.unexpected_merges for r in results)
    required_total = sum(r.required_total for r in results)
    required_found = sum(r.required_found for r in results)
    excluded_total = sum(r.counterexamples_excluded_total for r in results)
    excluded_ok = sum(r.counterexamples_excluded for r in results)
    canonical_total = sum(r.returned_records for r in results)
    preserved = sum(
        int(round(r.identifier_preservation * r.returned_records)) for r in results
    )

    failures = [f"{r.case_id}: {text}" for r in results for text in r.failures]

    return {
        "suite_version": suite.get("suite_version"),
        "reviewer": suite.get("reviewer"),
        "reviewed_on": suite.get("reviewed_on"),
        "review_basis": suite.get("review_basis"),
        "cases": [r.as_dict() for r in results],
        "transport": transport_stats,
        "aggregate": {
            "cases": len(results),
            "records_returned": canonical_total,
            "precision": precision,
            "recall": recall,
            "f1": f1,
            "metadata_accuracy": (
                round(metadata_matched / metadata_total, 4) if metadata_total else None
            ),
            "metadata_fields_checked": metadata_total,
            "dedupe_pair_recall": (
                round(pairs_correct / pairs_expected, 4) if pairs_expected else None
            ),
            "dedupe_pairs_expected": pairs_expected,
            "unexpected_merges": unexpected_merges,
            "identifier_preservation": (
                round(preserved / canonical_total, 4) if canonical_total else 1.0
            ),
            "required_item_recall": (
                round(required_found / required_total, 4) if required_total else None
            ),
            "counterexample_exclusion_rate": (
                round(excluded_ok / excluded_total, 4) if excluded_total else None
            ),
            "claims_total": claims_total,
            "unsupported_claims": unsupported,
            "unsupported_claim_rate": (
                round(unsupported / claims_total, 4) if claims_total else 0.0
            ),
            "insufficient_evidence_message_verified": any(
                r.exact_message_used and r.sufficiency_message == INSUFFICIENT_DATASET_MESSAGE
                for r in results
            ),
            "failures": failures,
            "passed": not failures,
        },
    }


# ---------------------------------------------------------------------------
# report rendering
# ---------------------------------------------------------------------------


def render_markdown(report: dict) -> str:
    aggregate = report["aggregate"]

    def fmt(value: Any) -> str:
        if value is None:
            return "n/a"
        if isinstance(value, float):
            return f"{value:.4f}"
        return str(value)

    lines = [
        "# ResearchForge benchmark report",
        "",
        "This report is produced by `tools/benchmark.py` from `tools/benchmark_suite.json`.",
        "It runs the real pipeline offline against synthetic, recorded-shape source payloads",
        "and single-reviewer relevance judgements. **It measures the pipeline, not the live web.**",
        "",
        f"- Suite version: `{report.get('suite_version')}`",
        f"- Reviewer: {report.get('reviewer')}",
        f"- Reviewed on: {report.get('reviewed_on')}",
        f"- Result: **{'PASS' if aggregate['passed'] else 'FAIL'}**",
        "",
        "## Aggregate metrics",
        "",
        "| Metric | Value |",
        "| --- | --- |",
        f"| Cases | {fmt(aggregate['cases'])} |",
        f"| Records returned (canonical) | {fmt(aggregate['records_returned'])} |",
        f"| Precision (micro) | {fmt(aggregate['precision'])} |",
        f"| Recall (micro) | {fmt(aggregate['recall'])} |",
        f"| F1 (micro) | {fmt(aggregate['f1'])} |",
        f"| Metadata accuracy (field-level) | {fmt(aggregate['metadata_accuracy'])} "
        f"({aggregate['metadata_fields_checked']} fields) |",
        f"| Duplicate-resolution recall | {fmt(aggregate['dedupe_pair_recall'])} "
        f"({aggregate['dedupe_pairs_expected']} expected pair(s)) |",
        f"| Unexpected merges | {fmt(aggregate['unexpected_merges'])} |",
        f"| Identifier/link/timestamp preservation | {fmt(aggregate['identifier_preservation'])} |",
        f"| Required-resource recall | {fmt(aggregate['required_item_recall'])} |",
        f"| Counterexample exclusion rate | {fmt(aggregate['counterexample_exclusion_rate'])} |",
        f"| Claims stored | {fmt(aggregate['claims_total'])} |",
        f"| Unsupported-claim rate | {fmt(aggregate['unsupported_claim_rate'])} |",
        f"| Exact insufficient-evidence message verified | "
        f"{aggregate['insufficient_evidence_message_verified']} |",
        "",
        "## Per case",
        "",
        "| Case | Returned | P | R | F1 | Metadata | Suitability | Sufficiency | Failures |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for case in report["cases"]:
        metadata = (
            f"{case['metadata_matched']}/{case['metadata_total']}"
            if case["metadata_total"]
            else "n/a"
        )
        suitability = (
            f"req {case['required_found']}/{case['required_total']}, "
            f"excl {case['counterexamples_excluded']}/{case['counterexamples_excluded_total']}"
        )
        lines.append(
            f"| `{case['case_id']}` | {case['returned_records']} | {fmt(case['precision'])} | "
            f"{fmt(case['recall'])} | {fmt(case['f1'])} | {metadata} | {suitability} | "
            f"{case['sufficiency_status']} | {'; '.join(case['failures']) or 'none'} |"
        )

    lines += ["", "## Per-case detail", ""]
    for case in report["cases"]:
        lines += [
            f"### `{case['case_id']}`",
            "",
            f"- Question: {case['question']}",
            f"- Ground truth: {case['relevant_total']} relevant, "
            f"{case['counterexamples_excluded_total']} counterexample(s) expected excluded",
            f"- Retrieved {case['returned_records']} record(s); "
            f"{case['true_positives']} relevant, {case['false_positives']} not in the relevant set",
            f"- Sufficiency: `{case['sufficiency_status']}` — {case['sufficiency_message']}",
            f"- Claims: {case['claims_total']}, unsupported: {case['unsupported_claims']}",
        ]
        if case["metadata_mismatches"]:
            lines.append("- Metadata mismatches:")
            lines += [f"  - {text}" for text in case["metadata_mismatches"]]
        unmatched = report["transport"][case["case_id"]]["unmatched_urls"]
        if unmatched:
            lines.append(
                "- URLs with no fixture (treated as transport failures, so their checks "
                "stay inconclusive): " + ", ".join(f"`{url}`" for url in unmatched)
            )
        lines.append("")

    lines += [
        "## Honest limitations",
        "",
        "- The corpus is synthetic. Each payload follows the documented response shape of its",
        "  source API, but no request to the live APIs was made while building this report, so",
        "  these numbers say nothing about real-world coverage or precision.",
        "- Only one human reviewer judged relevance, so inter-annotator agreement is unknown.",
        "- Probe URLs that were not fixtured are reported as transport failures; the pipeline",
        "  keeps those checks inconclusive instead of assuming the link works.",
        "- The suite is small (three cases). It is a regression harness for pipeline behaviour,",
        "  not a statistically representative evaluation.",
        "",
    ]
    if aggregate["failures"]:
        lines += ["## Failures", ""] + [f"- {text}" for text in aggregate["failures"]] + [""]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--suite", type=Path, default=SUITE_PATH)
    parser.add_argument("--json", action="store_true", help="also print the JSON report")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)

    suite = json.loads(args.suite.read_text(encoding="utf-8"))
    print(f"Running benchmark suite {suite.get('suite_version')} ({len(suite['cases'])} cases)")
    report = run_suite(suite, verbose=not args.quiet)

    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    json_path = REPORT_DIR / "benchmark_report.json"
    md_path = REPORT_DIR / "benchmark_report.md"
    json_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    md_path.write_text(render_markdown(report), encoding="utf-8")

    aggregate = report["aggregate"]
    print("")
    print(f"precision={aggregate['precision']} recall={aggregate['recall']} f1={aggregate['f1']}")
    print(
        f"metadata_accuracy={aggregate['metadata_accuracy']} "
        f"dedupe_pair_recall={aggregate['dedupe_pair_recall']} "
        f"unsupported_claim_rate={aggregate['unsupported_claim_rate']}"
    )
    print(f"report: {md_path}")
    if args.json:
        print(json.dumps(report, indent=2))
    if aggregate["failures"]:
        print("\nFAILURES:")
        for failure in aggregate["failures"]:
            print(f"  - {failure}")
        return 1
    print("\nAll benchmark expectations satisfied.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
