"""End-to-end HTTP tests against the real FastAPI application.

FastAPI's own ``TestClient`` depends on httpx, which is not a dependency of this
project, so these tests drive the ASGI application directly with the small
harness in ``tests.support``. Nothing here touches the network.
"""

from __future__ import annotations

import unittest

from app.services import store
from app.services.evidence import INSUFFICIENT_DATASET_MESSAGE
from app.services.jobs import reset_job_runner

from tests.support import DatabaseTestCase, asgi_request


def _app():
    # Imported lazily so ``create_app()`` runs after this test's temporary
    # settings and database have been installed.
    from app.main import app

    return app


class ApiTestCase(DatabaseTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.app = _app()
        self.project_id, self.run_id = self.project_with_run("fraud detection datasets")
        store.update_project_spec(
            self.project_id,
            {"question": "fraud detection datasets", "topic": "fraud detection", "domain": "finance"},
        )
        store.finish_run(
            self.run_id,
            "completed",
            {
                "sources_searched": ["openalex", "zenodo"],
                "unique_queries": ["fraud dataset"],
                "queries_executed": 2,
                "pages_retrieved": 2,
                "sufficiency": {"status": "sufficient"},
            },
            [],
        )
        self.paper_id = self.add_record(
            self.run_id,
            self.project_id,
            kind="paper",
            source="openalex",
            source_id="W1",
            title="Fraud detection with graphs",
            doi="10.1234/abc",
            abstract="We propose a graph model. Future work should explore transfer.",
            year=2021,
        )
        self.dataset_id = self.add_record(
            self.run_id,
            self.project_id,
            kind="dataset",
            source="zenodo",
            source_id="zen123",
            title="Elliptic dataset",
            license="CC-BY-4.0",
            access="open",
            dataset={
                "samples": 200000,
                "modality": "tabular",
                "files": [{"name": "elliptic.csv", "url": "https://zenodo.org/records/zen123/elliptic.csv"}],
            },
        )
        store.save_evidence(
            self.paper_id,
            self.project_id,
            {
                "claim": "transfer is unresolved",
                "passage": "Future work should explore transfer.",
                "locator": "abstract, sentence 2",
                "field_name": "stated_future_work",
                "url": "https://example.org/1",
                "origin": "source_metadata",
            },
        )
        store.save_verification(
            self.paper_id,
            {"check_name": "registrar_resolution", "status": "verified", "method": "Crossref"},
            overall="verified",
        )

    def tearDown(self) -> None:
        from app.services.jobs import get_job_runner

        try:
            get_job_runner().shutdown(wait=True)
        finally:
            reset_job_runner()
        super().tearDown()

    # -- helpers -----------------------------------------------------------
    def get(self, path: str, **kwargs):
        return asgi_request(self.app, "GET", path, **kwargs)

    def post(self, path: str, payload=None, **kwargs):
        return asgi_request(self.app, "POST", path, payload=payload if payload is not None else {}, **kwargs)


class HealthTests(ApiTestCase):
    def test_health_reports_real_configuration_and_counts(self) -> None:
        response = self.get("/api/health")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["status"], "ok")
        self.assertEqual(body["schema_version"], "1")
        self.assertTrue(body["adapters"]["available"])
        self.assertEqual(body["counts"]["projects"], 1)
        self.assertEqual(body["counts"]["records"], 2)
        self.assertEqual(body["counts"]["evidence"], 1)

    def test_meta_exposes_the_exact_insufficient_message(self) -> None:
        body = self.get("/api/meta").json()
        self.assertEqual(body["insufficient_dataset_message"], INSUFFICIENT_DATASET_MESSAGE)
        self.assertEqual(
            body["insufficient_dataset_message"], "There is insufficient data worldwide. No dataset."
        )
        self.assertIn("never fabricates", body["policy"])
        self.assertIn("cannot_complete_yet", body["manuscript_section_statuses"])
        self.assertIn("search_incomplete", body["sufficiency_statuses"])
        self.assertIn("discovered", [item["state"] for item in body["dataset_states"]])

    def test_adapters_endpoint_lists_only_real_adapters(self) -> None:
        body = self.get("/api/adapters").json()
        names = {adapter["name"] for adapter in body["adapters"]}
        self.assertIn("openalex", names)
        self.assertIn("zenodo", names)
        self.assertIn("Only the adapters listed as available are contacted", body["note"])

    def test_logs_endpoint_returns_a_list(self) -> None:
        body = self.get("/api/logs").json()
        self.assertIsInstance(body["logs"], list)


class PlanningTests(ApiTestCase):
    def test_plan_contacts_no_source(self) -> None:
        response = self.post("/api/research/plan", {"question": "Find image datasets for skin lesions"})
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertIn("spec", body)
        self.assertIn("plan", body)
        self.assertTrue(body["plan"]["queries"])
        self.assertIn("No source has been contacted yet", body["note"])

    def test_empty_question_is_rejected_with_a_structured_error(self) -> None:
        response = self.post("/api/research/plan", {"question": "hi"})
        self.assertEqual(response.status_code, 422)
        error = response.json()["error"]
        self.assertEqual(error["status"], 422)
        self.assertEqual(error["path"], "/api/research/plan")


class ProjectTests(ApiTestCase):
    def test_project_list_includes_the_latest_run(self) -> None:
        body = self.get("/api/projects").json()
        self.assertEqual(len(body["projects"]), 1)
        self.assertEqual(body["projects"][0]["latest_run"]["id"], self.run_id)

    def test_project_detail_and_missing_project(self) -> None:
        body = self.get(f"/api/projects/{self.project_id}").json()
        self.assertEqual(body["id"], self.project_id)
        self.assertEqual(len(body["runs"]), 1)
        self.assertEqual(self.get("/api/projects/424242").status_code, 404)

    def test_snapshot_exposes_counts_and_sufficiency(self) -> None:
        body = self.get(f"/api/projects/{self.project_id}/snapshot").json()
        self.assertEqual(body["counts"]["papers"], 1)
        self.assertEqual(body["counts"]["datasets"], 1)
        self.assertEqual(body["counts"]["evidence"], 1)
        self.assertEqual(body["latest_run"]["scope"]["sufficiency"]["status"], "sufficient")

    def test_ledger_endpoint_returns_the_search_record(self) -> None:
        body = self.get(f"/api/projects/{self.project_id}/ledger").json()
        self.assertEqual(body["run"]["id"], self.run_id)
        self.assertIn("sources", body["summary"])

    def test_ledger_without_a_run_is_404(self) -> None:
        empty_project = store.create_project("empty", "nothing", {})
        response = self.get(f"/api/projects/{empty_project}/ledger")
        self.assertEqual(response.status_code, 404)

    def test_delete_removes_the_project(self) -> None:
        response = asgi_request(self.app, "DELETE", f"/api/projects/{self.project_id}")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["deleted"])
        self.assertIsNone(store.get_project(self.project_id))


class RecordTests(ApiTestCase):
    def test_records_can_be_filtered_by_kind(self) -> None:
        body = self.get(f"/api/projects/{self.project_id}/records", query="kind=dataset").json()
        self.assertEqual(body["total_matching"], 1)
        self.assertEqual(body["records"][0]["kind"], "dataset")
        self.assertTrue(body["records"][0]["dataset_state"])

    def test_dataset_records_report_downloadability(self) -> None:
        body = self.get(f"/api/projects/{self.project_id}/records", query="kind=dataset").json()
        self.assertTrue(body["records"][0]["downloadable"])

    def test_record_detail_includes_evidence_and_checks(self) -> None:
        body = self.get(f"/api/records/{self.paper_id}").json()
        self.assertEqual(body["record"]["id"], self.paper_id)
        self.assertEqual(len(body["evidence"]), 1)
        self.assertEqual(body["record"]["verification_status"], "verified")

    def test_evidence_endpoint_labels_the_origin(self) -> None:
        body = self.get(f"/api/records/{self.paper_id}/evidence").json()
        self.assertEqual(body["evidence"][0]["origin"], "source_metadata")
        self.assertIn("not an independent confirmation", body["note"])

    def test_unknown_record_is_404(self) -> None:
        self.assertEqual(self.get("/api/records/98765").status_code, 404)
        self.assertEqual(self.get("/api/records/98765/evidence").status_code, 404)
        self.assertEqual(self.get("/api/records/98765/verifications").status_code, 404)


class GapAndMatrixTests(ApiTestCase):
    def test_evidence_matrix_uses_the_not_reported_placeholder(self) -> None:
        body = self.get(f"/api/projects/{self.project_id}/evidence-matrix").json()
        self.assertEqual(len(body["entries"]), 1)
        self.assertIn("not reported in the retrieved text", body["entries"][0]["methods"])
        self.assertIn("verbatim", body["note"])

    def test_gaps_are_empty_before_they_are_computed(self) -> None:
        body = self.get(f"/api/projects/{self.project_id}/gaps").json()
        self.assertEqual(body["gaps"], [])
        self.assertEqual(self.post(f"/api/projects/{self.project_id}/gaps", {}).status_code, 202)


class ManifestTests(ApiTestCase):
    def test_manifest_reports_a_version_and_totals(self) -> None:
        body = self.get(f"/api/projects/{self.project_id}/manifest").json()
        self.assertEqual(body["manifest_version"], "1.0")
        self.assertEqual(body["totals"]["datasets"], 1)
        self.assertEqual(len(body["entries"]), 1)
        self.assertEqual(body["totals"]["files_downloaded"], 0)
        self.assertIn("never downloaded", body["disclaimer"])


class ManuscriptApiTests(ApiTestCase):
    def test_no_manuscript_yet(self) -> None:
        body = self.get(f"/api/projects/{self.project_id}/manuscript").json()
        self.assertIsNone(body["manuscript"])
        self.assertIn("No manuscript has been assembled", body["note"])

    def test_sync_build_returns_twelve_sections(self) -> None:
        response = self.post(f"/api/projects/{self.project_id}/manuscript/sync", {})
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(len(body["sections"]), 12)
        self.assertTrue(body["progress_log"])
        self.assertIsNotNone(store.get_manuscript(self.project_id))

    def test_export_json_is_downloadable(self) -> None:
        response = self.get(f"/api/projects/{self.project_id}/manuscript/export", query="fmt=json")
        self.assertEqual(response.status_code, 200)
        self.assertIn("application/json", response.headers["content-type"])
        self.assertIn("attachment", response.headers["content-disposition"])
        self.assertEqual(len(response.json()["sections"]), 12)


class JobEndpointTests(ApiTestCase):
    def test_jobs_list_is_available(self) -> None:
        body = self.get("/api/jobs").json()
        self.assertIn("jobs", body)
        self.assertIsInstance(body["active"], list)

    def test_unknown_job_is_404(self) -> None:
        self.assertEqual(self.get("/api/jobs/does-not-exist").status_code, 404)


class StaticFrontendTests(ApiTestCase):
    def test_index_page_is_served(self) -> None:
        response = self.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertIn("ResearchForge", response.text)

    def test_stylesheet_is_served(self) -> None:
        response = self.get("/static/styles.css")
        self.assertEqual(response.status_code, 200)


if __name__ == "__main__":
    unittest.main()
