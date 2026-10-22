"""Tests for the evidence matrix, gap discovery and the manuscript workspace.

The point of these tests is negative as much as positive: the workspace must
never produce a results section, a conclusion or a gap claim that the retrieved
text does not support.
"""

from __future__ import annotations

import unittest

from app.services import store
from app.services.gaps import (
    GAP_TYPE_AUTHOR_STATED,
    GAP_TYPE_STATED_LIMITATION,
    NOT_REPORTED,
    build_and_store_gaps,
    build_evidence_matrix,
    discover_gaps,
)
from app.services.manuscript import (
    SECTION_TITLES,
    STATUS_CANNOT_COMPLETE,
    build_manuscript,
    export_manuscript,
    manuscript_markdown,
)

from tests.support import DatabaseTestCase

ABSTRACT = (
    "We investigate fraudulent transaction detection in banking. "
    "We propose a graph neural network model. "
    "We evaluate on the Elliptic dataset. "
    "Our model reaches accuracy of 0.95. "
    "However, the results cannot generalise to other banks. "
    "Future work should address cross-institution transfer. "
    "The data are publicly available upon request. "
    "Code is available at https://github.com/example/repo. "
    "The effect may differ across populations. "
    "This is contrary to earlier reports."
)

# Same study, but with no code-availability sentence, so the abstract-level
# replication signal is present.
NO_CODE_ABSTRACT = (
    "We investigate fraudulent transaction detection in banking. "
    "We propose a graph neural network model. "
    "We evaluate on the Elliptic dataset. "
    "Our model reaches accuracy of 0.95."
)


def paper(record_id: int, **overrides) -> dict:
    record = {
        "id": record_id,
        "kind": "paper",
        "source": "openalex",
        "source_id": f"W{record_id}",
        "title": f"Study {record_id}",
        "url": f"https://example.org/{record_id}",
        "year": 2021,
        "abstract": ABSTRACT,
        "retrieved_at": "2026-01-01T00:00:00Z",
    }
    record.update(overrides)
    return record


SCOPE = {"sources_searched": ["openalex"], "queries_executed": 3, "pages_retrieved": 2}


class EvidenceMatrixTests(unittest.TestCase):
    def test_only_paper_records_enter_the_matrix(self) -> None:
        entries = build_evidence_matrix([paper(1), {"id": 2, "kind": "dataset"}])
        self.assertEqual([entry["record_id"] for entry in entries], [1])

    def test_record_without_content_reports_every_field_as_not_reported(self) -> None:
        entry = build_evidence_matrix([paper(1, abstract=None)])[0]
        self.assertEqual(entry["claimed_contribution"], NOT_REPORTED)
        self.assertEqual(entry["methods"], NOT_REPORTED)
        self.assertIn("No abstract or full text", entry["retrieval_note"])
        self.assertEqual(entry["passages"], [])

    def test_fields_are_extracted_verbatim_with_a_locator(self) -> None:
        entry = build_evidence_matrix([paper(1)])[0]
        self.assertIn("investigate", entry["research_question"])
        self.assertTrue(entry["claimed_contribution"].startswith("We propose"))
        self.assertTrue(entry["methods"].startswith("We evaluate"))
        self.assertIn("Elliptic", entry["datasets_used"])
        self.assertIn("0.95", entry["evaluation_metrics"])
        self.assertIn("contrary to earlier reports", entry["conflicting_findings"])
        self.assertIn("github.com", entry["code_availability"])
        self.assertTrue(any("However" in item for item in entry["stated_limitations"]))
        self.assertTrue(any("Future work" in item for item in entry["stated_future_work"]))
        self.assertIn("may differ", entry["uncertainty"])
        locators = {passage["locator"] for passage in entry["passages"]}
        self.assertTrue(any(locator.startswith("abstract, sentence") for locator in locators))
        self.assertEqual(entry["passages"][0]["url"], "https://example.org/1")


class GapDiscoveryTests(unittest.TestCase):
    def test_empty_matrix_is_insufficient_evidence(self) -> None:
        result = discover_gaps([], SCOPE)
        self.assertEqual(result["status"], "insufficient_evidence")
        self.assertEqual(result["gaps"], [])
        self.assertIn("no gap can be supported", result["reason"])
        self.assertIn("not evidence that a topic is globally unexplored", result["scope_statement"])

    def test_gaps_come_only_from_author_statements(self) -> None:
        matrix = build_evidence_matrix([paper(1)])
        result = discover_gaps(matrix, SCOPE)
        self.assertEqual(result["status"], "evidence_supported")
        types = {gap["gap_type"] for gap in result["gaps"]}
        self.assertIn(GAP_TYPE_AUTHOR_STATED, types)
        self.assertIn(GAP_TYPE_STATED_LIMITATION, types)
        for gap in result["gaps"]:
            self.assertTrue(gap["supporting_passages"])
            self.assertTrue(gap["uncertainty"])
            self.assertTrue(gap["feasible_research_question"])
            self.assertIn("search scope", " ".join(gap["uncertainty"]).lower())

    def test_abstract_without_signals_produces_no_gap(self) -> None:
        matrix = build_evidence_matrix(
            [paper(1, abstract="We describe a system. The system works well.")]
        )
        result = discover_gaps(matrix, SCOPE)
        self.assertEqual(result["status"], "insufficient_evidence")
        self.assertEqual(result["gaps"], [])

    def test_identical_statements_are_grouped_into_one_gap(self) -> None:
        matrix = build_evidence_matrix([paper(1), paper(2)])
        result = discover_gaps(matrix, SCOPE)
        future_gaps = [gap for gap in result["gaps"] if gap["gap_type"] == GAP_TYPE_AUTHOR_STATED]
        self.assertEqual(len(future_gaps), 1)
        self.assertEqual(len(future_gaps[0]["supporting_records"]), 2)

    def test_concentration_and_replication_gaps_need_enough_records(self) -> None:
        one = build_evidence_matrix([paper(1)])
        # A single record cannot demonstrate evaluation concentration.
        self.assertFalse(
            any(gap["gap_type"] == "evaluation_concentration" for gap in discover_gaps(one, SCOPE)["gaps"])
        )
        many = build_evidence_matrix(
            [paper(index, abstract=NO_CODE_ABSTRACT) for index in range(1, 6)]
        )
        types = {gap["gap_type"] for gap in discover_gaps(many, SCOPE)["gaps"]}
        self.assertIn("evaluation_concentration", types)
        self.assertIn("replication_uncertainty", types)


class ManuscriptTests(DatabaseTestCase):
    def _project(self) -> int:
        project_id, run_id = self.project_with_run("fraud detection datasets")
        store.update_project_spec(
            project_id,
            {
                "question": "fraud detection datasets",
                "topic": "fraud detection",
                "objective": "find datasets",
                "domain": "finance",
                "intent": "both",
                "modalities": ["tabular"],
                "assumptions": ["assumed tabular"],
                "constraints": [],
            },
        )
        store.finish_run(
            run_id,
            "completed",
            {**SCOPE, "unique_queries": ["fraud dataset"], "sufficiency": {"status": "sufficient"}},
            [],
        )
        self.add_record(
            run_id,
            project_id,
            kind="paper",
            source="openalex",
            source_id="W1",
            title="Fraud detection on graphs",
            doi="10.1234/abc",
            abstract=ABSTRACT,
            year=2021,
            retrieved_at="2026-01-01T00:00:00Z",
        )
        self.add_record(
            run_id,
            project_id,
            kind="dataset",
            source="zenodo",
            source_id="zen123",
            title="Elliptic dataset",
            license="CC-BY-4.0",
            access="open",
            year=2020,
            dataset={"samples": 200000, "modality": "tabular"},
        )
        return project_id

    def test_all_twelve_sections_are_present_in_order(self) -> None:
        manuscript = build_manuscript(self._project())
        self.assertEqual(len(manuscript["sections"]), 12)
        self.assertEqual(
            [section["title"] for section in manuscript["sections"]],
            [title for _, title in SECTION_TITLES],
        )
        self.assertEqual(sum(manuscript["status_counts"].values()), 12)

    def test_results_section_never_contains_results(self) -> None:
        manuscript = build_manuscript(self._project())
        results = next(s for s in manuscript["sections"] if s["key"] == "results")
        self.assertEqual(results["status"], STATUS_CANNOT_COMPLETE)
        self.assertIn("no experiments have been executed", results["summary"])
        self.assertIn("does not run experiments", " ".join(results["paragraphs"]))
        conclusion = next(s for s in manuscript["sections"] if s["key"] == "conclusion")
        self.assertEqual(conclusion["status"], STATUS_CANNOT_COMPLETE)

    def test_references_carry_provenance_and_verification(self) -> None:
        manuscript = build_manuscript(self._project())
        self.assertEqual([ref["index"] for ref in manuscript["references"]], [1, 2])
        for reference in manuscript["references"]:
            self.assertTrue(reference["identifier"])
            self.assertTrue(reference["url"])
            self.assertTrue(reference["retrieved_at"])
            self.assertTrue(reference["verification"])
        paper_ref = next(ref for ref in manuscript["references"] if ref["kind"] == "paper")
        self.assertEqual(paper_ref["identifier"], "10.1234/abc")

    def test_checklist_records_unmet_requirements(self) -> None:
        manuscript = build_manuscript(self._project())
        checklist = {item["item"]: item for item in manuscript["checklist"]}
        self.assertFalse(checklist["Experiment results exist and are reproducible from recorded artefacts"]["satisfied"])
        self.assertFalse(checklist["Reporting guideline applied"]["satisfied"])
        self.assertFalse(checklist["Ethics, consent, funding and competing interests declared"]["satisfied"])
        self.assertTrue(checklist["Evidence sufficiency was assessed before reporting findings"]["satisfied"])

    def test_manuscript_converts_to_plain_dicts_and_persists(self) -> None:
        project_id = self._project()
        manuscript = build_manuscript(project_id, store_result=True)
        import json

        json.dumps(manuscript)  # the whole payload must be JSON-serializable
        self.assertIsNotNone(store.get_manuscript(project_id))
        self.assertIn("Readiness checklist", manuscript_markdown(manuscript))

    def test_export_supports_every_format(self) -> None:
        project_id = self._project()
        for fmt, media in (
            ("md", "text/markdown"),
            ("tex", "application/x-tex"),
            ("json", "application/json"),
            ("docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
        ):
            body, filename, media_type = export_manuscript(project_id, fmt)
            self.assertTrue(body)
            self.assertEqual(media_type, media)
            self.assertTrue(filename.endswith(fmt if fmt != "docx" else "docx"))
        docx, _, _ = export_manuscript(project_id, "docx")
        self.assertTrue(docx.startswith(b"PK"))  # a real zip container, not a stub

    def test_unknown_project_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            build_manuscript(9999)

    def test_build_and_store_gaps_keeps_the_evidence_matrix(self) -> None:
        project_id = self._project()
        result = build_and_store_gaps(project_id, None, SCOPE)
        self.assertTrue(result["evidence_matrix"])
        self.assertEqual(result["records_considered"], 2)
        if result["gaps"]:
            stored = store.list_gap_candidates(project_id)
            self.assertEqual(len(stored), len(result["gaps"]))


if __name__ == "__main__":
    unittest.main()
