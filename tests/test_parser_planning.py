"""Request parsing and search-planning tests.

The parser must extract constraints deterministically and record its assumptions
rather than inventing them, and the planner must publish an auditable scope with
an explicit disclaimer that no internet-wide completeness is claimed.
"""

from __future__ import annotations

import unittest

from app.services.parser import parse_request
from app.services.planning import build_filters, build_plan, expand_queries
from app.sources import build_registry


class ParserTests(unittest.TestCase):
    def test_dataset_request_is_detected(self) -> None:
        spec = parse_request(
            "Find publicly available datasets for detecting fraudulent financial transactions",
            use_llm=False,
        )
        self.assertEqual(spec.intent, "datasets")
        self.assertIn("fraudulent", spec.topic)
        self.assertIn("finance", spec.domain)

    def test_literature_request_is_detected(self) -> None:
        spec = parse_request(
            "Identify research gaps in graph neural networks for fraud detection", use_llm=False
        )
        self.assertEqual(spec.intent, "literature")

    def test_request_for_both_is_detected(self) -> None:
        spec = parse_request("papers and datasets about battery degradation", use_llm=False)
        self.assertEqual(spec.intent, "both")

    def test_time_range_and_geography_are_extracted(self) -> None:
        spec = parse_request(
            "Find datasets about air quality in Kenya from 2015 to 2020", use_llm=False
        )
        self.assertEqual(spec.time_range.get("from_year"), 2015)
        self.assertEqual(spec.time_range.get("to_year"), 2020)
        self.assertEqual(spec.geography, "kenya")

    def test_since_and_last_n_years_are_extracted(self) -> None:
        since = parse_request("datasets published since 2018 about fraud", use_llm=False)
        self.assertEqual(since.time_range.get("from_year"), 2018)

        last_n = parse_request("datasets from the last 3 years about fraud", use_llm=False)
        self.assertIsNotNone(last_n.time_range.get("from_year"))
        self.assertGreater(last_n.time_range["to_year"], last_n.time_range["from_year"])

    def test_open_access_requirement_is_extracted(self) -> None:
        spec = parse_request("find openly available datasets for fraud detection", use_llm=False)
        self.assertIn("open", spec.access_requirements)

    def test_modalities_are_extracted(self) -> None:
        spec = parse_request("find image datasets of skin lesions", use_llm=False)
        self.assertIn("image", spec.modalities)

    def test_missing_details_produce_assumptions_not_inventions(self) -> None:
        spec = parse_request("Find datasets for fraud detection", use_llm=False)
        self.assertEqual(spec.question, "Find datasets for fraud detection")
        self.assertTrue(spec.assumptions)
        self.assertTrue(any("geography" in assumption for assumption in spec.assumptions))
        self.assertTrue(any("modality" in assumption for assumption in spec.assumptions))

    def test_vague_request_asks_a_clarification_question(self) -> None:
        spec = parse_request("data", use_llm=False)
        self.assertTrue(spec.missing_information)
        self.assertTrue(spec.clarification_question)

    def test_no_llm_use_means_no_llm_flag(self) -> None:
        spec = parse_request("Find datasets for fraud detection", use_llm=False)
        self.assertFalse(spec.llm_assisted)


class ExpansionTests(unittest.TestCase):
    def test_original_query_is_kept_unmodified(self) -> None:
        spec = parse_request("Find datasets for fraud detection", use_llm=False)
        queries = expand_queries(spec)
        self.assertEqual(queries[0].origin, "original")
        self.assertEqual(queries[0].text, spec.topic)
        self.assertEqual(queries[0].intent, "both")

    def test_dataset_expansions_are_added_for_dataset_intent(self) -> None:
        spec = parse_request("Find datasets for fraud detection", use_llm=False)
        queries = expand_queries(spec)
        dataset_queries = [q for q in queries if q.intent == "datasets"]
        self.assertTrue(dataset_queries)
        self.assertTrue(all(q.origin == "expanded" for q in dataset_queries))
        # every expansion documents why it was added
        self.assertTrue(all(q.rationale for q in queries[1:]))

    def test_expansions_are_deduplicated(self) -> None:
        spec = parse_request("Find datasets for fraud detection", use_llm=False)
        texts = [q.text.lower() for q in expand_queries(spec)]
        self.assertEqual(len(texts), len(set(texts)))


class PlanTests(unittest.TestCase):
    def _plan(self, question: str, **kwargs):
        spec = parse_request(question, use_llm=False)
        registry = build_registry()
        return build_plan(
            spec, registry=registry, depth=1, page_size=5, adapter_names=kwargs.pop("adapters", None),
            **kwargs,
        )

    def test_plan_publishes_scope_and_disclaimer(self) -> None:
        plan = self._plan("Find datasets for fraud detection", adapters=["zenodo", "uci"])
        self.assertEqual(plan.depth, 1)
        self.assertEqual(plan.page_size, 5)
        self.assertCountEqual(plan.adapters, ["zenodo", "uci"])
        self.assertTrue(plan.queries)
        joined = " ".join(plan.notes)
        self.assertIn("no claim of internet-wide", joined)

    def test_plan_excludes_adapters_that_do_not_serve_the_intent(self) -> None:
        plan = self._plan("Find papers about fraud detection", adapters=["zenodo", "openalex"])
        intent = parse_request("Find papers about fraud detection", use_llm=False).intent
        self.assertEqual(intent, "literature")
        self.assertIn("openalex", plan.adapters)
        self.assertNotIn("zenodo", plan.adapters)
        self.assertTrue(
            any(item["kind"] == "intent_mismatch" for item in plan.excluded_adapters)
        )

    def test_excluded_adapter_families_are_reported(self) -> None:
        plan = self._plan("Find datasets for fraud detection", adapters=["zenodo", "uci"])
        self.assertTrue(plan.families)

    def test_filters_follow_the_parsed_spec(self) -> None:
        spec = parse_request("find openly available datasets from 2015 to 2020", use_llm=False)
        filters = build_filters(spec)
        self.assertTrue(filters.get("open_access_only"))
        self.assertEqual(filters.get("from_year"), 2015)
        self.assertEqual(filters.get("to_year"), 2020)

    def test_queries_for_intent_filters_correctly(self) -> None:
        plan = self._plan("Find datasets for fraud detection", adapters=["zenodo"])
        literature = plan.queries_for("literature")
        self.assertTrue(all(q.intent in {"literature", "both"} for q in literature))
        self.assertEqual(len(plan.queries_for("both")), len(plan.queries))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
