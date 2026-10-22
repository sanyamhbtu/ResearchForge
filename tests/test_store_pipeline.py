"""Persistence-layer tests: ledger, records, evidence, dataset state, snapshot.

These assert the audit trail the platform promises — every retained record keeps
its identifier, URL, license and retrieval timestamp, and the ledger can explain
which source failed.
"""

from __future__ import annotations

import unittest

from app.services import store

from tests.support import DatabaseTestCase


class LedgerTests(DatabaseTestCase):
    def test_ledger_summary_aggregates_per_source_and_flags_failures(self) -> None:
        project_id, run_id = self.project_with_run()
        store.add_ledger_entry(
            run_id,
            source="zenodo",
            family="datasets",
            query="fraud dataset",
            page=1,
            result_count=10,
            new_records=8,
            duration_ms=120,
            status="ok",
        )
        store.add_ledger_entry(
            run_id,
            source="uci",
            family="datasets",
            query="fraud dataset",
            page=1,
            result_count=0,
            new_records=0,
            duration_ms=300,
            status="error",
            detail="HTTP 503",
        )
        summary = store.ledger_summary(run_id)
        self.assertEqual(summary["entries"], 2)
        self.assertEqual(summary["total_results"], 10)
        self.assertEqual(summary["total_new_records"], 8)
        self.assertEqual(summary["failed_sources"], ["uci"])
        by_source = {entry["source"]: entry for entry in summary["sources"]}
        self.assertEqual(by_source["uci"]["errors"], 1)
        self.assertEqual(by_source["uci"]["error_detail"], "HTTP 503")


class RecordTests(DatabaseTestCase):
    def test_saved_record_preserves_identity_license_and_timestamp(self) -> None:
        project_id, run_id = self.project_with_run()
        record_id = self.add_record(
            run_id,
            project_id,
            source="zenodo",
            source_id="zenodo:1234",
            kind="dataset",
            title="Fraud transactions dataset",
            url="https://zenodo.org/records/1234",
            license="CC-BY-4.0",
            access="open",
            year=2021,
            retrieved_at="2026-01-01T00:00:00Z",
        )
        record = store.get_record(record_id)
        self.assertEqual(record["source"], "zenodo")
        self.assertEqual(record["source_id"], "zenodo:1234")
        self.assertEqual(record["record_key"], "zenodo:zenodo:1234")
        self.assertEqual(record["license"], "CC-BY-4.0")
        self.assertEqual(record["retrieved_at"], "2026-01-01T00:00:00Z")
        self.assertFalse(record["is_duplicate"])

    def test_duplicates_are_hidden_by_default_and_shown_on_request(self) -> None:
        project_id, run_id = self.project_with_run()
        first = self.add_record(run_id, project_id, source_id="W1", title="Fraud detection study of banking")
        second = self.add_record(run_id, project_id, source_id="W2", title="Fraud detection study of banking")
        store.mark_duplicate(second, first)

        visible = store.list_records(project_id=project_id)
        self.assertEqual([record["id"] for record in visible], [first])

        everything = store.list_records(project_id=project_id, include_duplicates=True)
        self.assertEqual(len(everything), 2)
        self.assertTrue(any(record["is_duplicate"] for record in everything))

    def test_kind_counts_report_total_and_unique(self) -> None:
        project_id, run_id = self.project_with_run()
        first = self.add_record(run_id, project_id, source_id="W1", kind="paper")
        second = self.add_record(run_id, project_id, source_id="W2", kind="paper")
        self.add_record(run_id, project_id, source_id="z1", kind="dataset")
        store.mark_duplicate(second, first)
        counts = store.record_kind_counts(run_id)
        self.assertEqual(counts["paper"], {"total": 2, "unique": 1})
        self.assertEqual(counts["dataset"], {"total": 1, "unique": 1})


class EvidenceTests(DatabaseTestCase):
    def test_evidence_keeps_claim_passage_locator_and_url(self) -> None:
        project_id, run_id = self.project_with_run()
        record_id = self.add_record(run_id, project_id, source_id="W1", title="A paper")
        store.save_evidence(
            record_id,
            project_id,
            {
                "claim": "AUC is above 0.9",
                "passage": "We report an AUC of 0.94 on the held-out split.",
                "locator": "abstract, sentence 3",
                "field": "results_reported",
                "url": "https://example.org/paper",
                "origin": "abstract",
                "verified": True,
            },
        )
        items = store.get_evidence(project_id=project_id)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["locator"], "abstract, sentence 3")
        self.assertEqual(items[0]["url"], "https://example.org/paper")
        self.assertTrue(items[0]["verified"])
        self.assertEqual(store.evidence_counts(project_id)[record_id], 1)


class DatasetStateTests(DatabaseTestCase):
    def test_state_is_stored_and_surfaced_in_the_snapshot(self) -> None:
        project_id, run_id = self.project_with_run()
        store.finish_run(run_id, "completed", {"completed": True}, [])
        record_id = self.add_record(run_id, project_id, source_id="z1", kind="dataset", title="Dataset")
        store.set_dataset_state(record_id, "download_verified", "one file validated")
        snapshot = store.project_snapshot(project_id)
        record = next(item for item in snapshot["records"] if item["id"] == record_id)
        self.assertEqual(record["dataset_state"], "download_verified")
        self.assertEqual(record["dataset_state_reason"], "one file validated")

    def test_paper_records_have_no_dataset_state(self) -> None:
        project_id, run_id = self.project_with_run()
        store.finish_run(run_id, "completed", {"completed": True}, [])
        record_id = self.add_record(run_id, project_id, source_id="W1", kind="paper")
        snapshot = store.project_snapshot(project_id)
        record = next(item for item in snapshot["records"] if item["id"] == record_id)
        self.assertIsNone(record["dataset_state"])


class SnapshotTests(DatabaseTestCase):
    def test_snapshot_counts_and_scope_are_exposed(self) -> None:
        project_id, run_id = self.project_with_run()
        store.finish_run(
            run_id,
            "completed",
            {"completed": True, "sufficiency": {"status": "insufficient_evidence"}},
            [{"adapter": "zenodo", "reason": "HTTP 503"}],
        )
        self.add_record(run_id, project_id, source_id="W1", kind="paper")
        self.add_record(run_id, project_id, source_id="z1", kind="dataset", title="Dataset")

        snapshot = store.project_snapshot(project_id)
        self.assertEqual(snapshot["counts"]["records"], 2)
        self.assertEqual(snapshot["counts"]["papers"], 1)
        self.assertEqual(snapshot["counts"]["datasets"], 1)
        self.assertEqual(snapshot["latest_run"]["scope"]["sufficiency"]["status"], "insufficient_evidence")
        self.assertEqual(snapshot["latest_run"]["failures"][0]["adapter"], "zenodo")

    def test_missing_project_returns_none(self) -> None:
        self.assertIsNone(store.project_snapshot(9999))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
