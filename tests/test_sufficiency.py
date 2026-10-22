"""Evidence-sufficiency gate tests.

This is the policy the whole platform is judged on: when the search scope was
explored and nothing suitable exists, the platform must return the exact message
``"There is insufficient data worldwide. No dataset."`` — and it must *not*
convert an API failure into a claim of absence.
"""

from __future__ import annotations

import unittest

from app.services.evidence import (
    INSUFFICIENT_DATASET_MESSAGE,
    STATUS_INCOMPLETE,
    STATUS_INSUFFICIENT,
    STATUS_SUFFICIENT,
    STATUS_UNVERIFIED,
    assess_dataset_sufficiency,
)


def scope(**overrides) -> dict:
    value = {
        "sources_searched": ["zenodo", "uci"],
        "sources_failed": [],
        "pages_retrieved": 4,
        "queries_executed": 6,
        "completed": True,
    }
    value.update(overrides)
    return value


def candidate(access: str = "open", overall: str = "verified") -> dict:
    return {"access": access, "verification": {"overall": overall}, "title": "candidate"}


class ExactMessageTests(unittest.TestCase):
    def test_message_constant_is_the_required_sentence(self) -> None:
        self.assertEqual(
            INSUFFICIENT_DATASET_MESSAGE, "There is insufficient data worldwide. No dataset."
        )

    def test_completed_search_with_no_candidates_uses_the_exact_message(self) -> None:
        result = assess_dataset_sufficiency(
            scope=scope(), candidates=[], requirements={"stated": ["tabular"]}, queries=["fraud datasets"]
        )
        self.assertEqual(result.status, STATUS_INSUFFICIENT)
        self.assertEqual(result.message, INSUFFICIENT_DATASET_MESSAGE)
        self.assertTrue(result.exact_message_used)
        # the explanation must state what was searched and what remains open
        joined = " ".join(result.explanation)
        self.assertIn("zenodo", joined)
        self.assertIn("uci", joined)
        self.assertTrue(result.next_steps)

    def test_candidates_that_all_fail_verification_also_use_the_exact_message(self) -> None:
        result = assess_dataset_sufficiency(
            scope=scope(),
            candidates=[candidate(overall="failed"), candidate(access="restricted")],
            requirements={"unmet": ["open licence"]},
            queries=["fraud datasets"],
        )
        self.assertEqual(result.status, STATUS_INSUFFICIENT)
        self.assertEqual(result.message, INSUFFICIENT_DATASET_MESSAGE)
        self.assertTrue(result.exact_message_used)
        self.assertIn("open licence", " ".join(result.explanation))


class IncompleteScopeTests(unittest.TestCase):
    def test_an_incomplete_search_never_claims_absence(self) -> None:
        result = assess_dataset_sufficiency(
            scope=scope(completed=False, sources_failed=[{"adapter": "zenodo", "reason": "HTTP 503"}]),
            candidates=[],
            requirements={},
            queries=["fraud datasets"],
        )
        self.assertEqual(result.status, STATUS_INCOMPLETE)
        self.assertNotEqual(result.message, INSUFFICIENT_DATASET_MESSAGE)
        self.assertFalse(result.exact_message_used)
        joined = " ".join(result.explanation)
        self.assertIn("not evidence of absence", joined)
        self.assertIn("zenodo", joined)
        self.assertTrue(result.next_steps)


class SufficientScopeTests(unittest.TestCase):
    def test_verified_usable_candidates_are_sufficient(self) -> None:
        result = assess_dataset_sufficiency(
            scope=scope(), candidates=[candidate()], requirements={}, queries=["fraud datasets"]
        )
        self.assertEqual(result.status, STATUS_SUFFICIENT)
        self.assertEqual(result.verified_candidate_count, 1)
        self.assertFalse(result.exact_message_used)

    def test_unverified_candidates_are_reported_as_unverified(self) -> None:
        result = assess_dataset_sufficiency(
            scope=scope(),
            candidates=[candidate(), candidate(overall="partially_verified"), candidate(overall="unverified")],
            requirements={},
            queries=["fraud datasets"],
        )
        self.assertEqual(result.status, STATUS_UNVERIFIED)
        self.assertEqual(result.verified_candidate_count, 2)
        self.assertIn("unverified", result.message)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
