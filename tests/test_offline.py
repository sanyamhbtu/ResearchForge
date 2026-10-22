"""Guard test: the deterministic pipeline must never reach the network.

Everything outside the source adapters is pure computation over the database.
This test hard-fails if any of it silently starts using sockets, which would
make the offline test-suite and the "report only what was retrieved" promise
unreliable.
"""

from __future__ import annotations

import socket
import unittest
import urllib.request
from unittest import mock

from app.services import store
from app.services.gaps import build_evidence_matrix, discover_gaps
from app.services.manuscript import build_manuscript
from app.services.parser import parse_request
from app.services.planning import build_plan
from app.sources import get_registry

from tests.support import DatabaseTestCase

QUESTION = "Find publicly available image datasets of skin lesions for diagnosis"


class OfflinePipelineTests(DatabaseTestCase):
    def setUp(self) -> None:
        super().setUp()
        self._patches = [
            mock.patch.object(urllib.request, "urlopen", side_effect=AssertionError("network access attempted")),
            mock.patch.object(socket, "create_connection", side_effect=AssertionError("network access attempted")),
        ]
        for patch in self._patches:
            patch.start()

    def tearDown(self) -> None:
        for patch in reversed(self._patches):
            patch.stop()
        super().tearDown()

    def test_parse_and_plan_run_offline(self) -> None:
        spec = parse_request(QUESTION, use_llm=False)
        self.assertFalse(spec.llm_assisted)
        plan = build_plan(spec, registry=get_registry(), depth=1, page_size=5)
        payload = plan.as_dict()
        self.assertTrue(payload["adapters"])
        self.assertTrue(payload["queries"])

    def test_matrix_gaps_and_manuscript_run_offline(self) -> None:
        project_id, run_id = self.project_with_run(QUESTION)
        store.finish_run(
            run_id,
            "completed",
            {"sources_searched": ["openalex"], "unique_queries": ["skin lesion"], "sufficiency": {"status": "sufficient"}},
            [],
        )
        self.add_record(
            run_id,
            project_id,
            kind="paper",
            source="openalex",
            source_id="W1",
            title="Skin lesion classification",
            abstract="We propose a model. However, melanomas remain hard to detect. Future work should explore dark skin tones.",
        )
        matrix = build_evidence_matrix(store.list_records(project_id=project_id))
        self.assertTrue(matrix)
        result = discover_gaps(matrix, {"sources_searched": ["openalex"]})
        self.assertIn(result["status"], {"evidence_supported", "insufficient_evidence"})
        manuscript = build_manuscript(project_id)
        self.assertEqual(len(manuscript["sections"]), 12)

    def test_adapters_construct_without_contacting_their_hosts(self) -> None:
        registry = get_registry()
        described = registry.describe()
        self.assertTrue(described)
        for adapter in described:
            self.assertTrue(adapter["name"])
            self.assertTrue(adapter["homepage"])


if __name__ == "__main__":
    unittest.main()
