"""Deduplication / entity-resolution tests.

These assert the exact merge rule the platform publishes, including the
important negative case: copied metadata from two bibliographic indexes is *not*
independent confirmation.
"""

from __future__ import annotations

import unittest

from app.services.dedupe import (
    FUZZY_THRESHOLD,
    DedupeResolver,
    cluster_summary,
    dedupe_key,
    has_independent_confirmation,
    jaccard,
    normalize_doi,
    normalize_title,
    title_tokens,
)


def record(source: str, source_id: str, title: str, *, doi: str = "", year: int | None = None) -> dict:
    return {
        "source": source,
        "source_id": source_id,
        "record_key": f"{source}:{source_id}",
        "title": title,
        "doi": doi,
        "year": year,
    }


class NormalizationTests(unittest.TestCase):
    def test_normalize_doi_strips_every_prefix_form(self) -> None:
        for value in (
            "https://doi.org/10.1000/XYZ",
            "http://doi.org/10.1000/XYZ",
            "doi:10.1000/XYZ",
            "https://dx.doi.org/10.1000/XYZ",
            "info:doi/10.1000/XYZ",
            "  10.1000/XYZ.  ",
        ):
            self.assertEqual(normalize_doi(value), "10.1000/xyz", value)
        self.assertEqual(normalize_doi(None), "")
        self.assertEqual(normalize_doi(""), "")

    def test_normalize_title_strips_markup_and_boilerplate(self) -> None:
        self.assertEqual(
            normalize_title("<jats:p>Fraud Detection</jats:p> in Banking: A Survey"),
            "fraud detection in banking",
        )
        self.assertEqual(normalize_title("Graph Neural Networks - a systematic review"),
                         "graph neural networks")

    def test_title_tokens_drop_short_words(self) -> None:
        tokens = title_tokens("Graph neural network for fraud detection")
        self.assertIn("fraud", tokens)
        self.assertIn("graph", tokens)
        # tokens of two characters or fewer carry no matching signal and are dropped
        self.assertNotIn("of", title_tokens("Detection of fraud"))

    def test_jaccard_bounds(self) -> None:
        self.assertEqual(jaccard(set(), {"a"}), 0.0)
        self.assertEqual(jaccard({"a", "b"}, {"a", "b"}), 1.0)
        self.assertAlmostEqual(jaccard({"a", "b"}, {"b", "c"}), 1 / 3, places=6)

    def test_dedupe_key_prefers_doi_then_title_then_source(self) -> None:
        with_doi = record("openalex", "W1", "Some long enough title here for matching", doi="10.1/Ab")
        self.assertEqual(dedupe_key(with_doi), "doi:10.1/ab")

        without_doi = record("crossref", "10.1/Ab", "A sufficiently long title for the rule to engage", year=2020)
        self.assertTrue(dedupe_key(without_doi).startswith("title:"))

        short = record("uci", "53", "Iris", year=1988)
        self.assertEqual(dedupe_key(short), "src:uci:53")


class ResolverTests(unittest.TestCase):
    def test_identical_doi_is_merged(self) -> None:
        resolver = DedupeResolver()
        first = record("openalex", "W1", "Fraud detection in banking", doi="10.1/x", year=2020)
        second = record("crossref", "10.1/x", "Fraud detection in banking", doi="https://doi.org/10.1/X", year=2020)

        self.assertIsNone(resolver.resolve(first)[0])
        canonical, decision = resolver.resolve(second)
        self.assertIsNotNone(canonical)
        self.assertEqual(canonical["record_key"], "openalex:W1")
        self.assertEqual(decision.reason, "identical normalized DOI")
        self.assertEqual(decision.similarity, 1.0)

    def test_identical_title_and_year_is_merged(self) -> None:
        resolver = DedupeResolver()
        title = "Detecting fraudulent financial transactions with graph learning"
        resolver.resolve(record("openalex", "W2", title, year=2021))
        canonical, decision = resolver.resolve(record("zenodo", "99", title, year=2021))
        self.assertIsNotNone(canonical)
        self.assertEqual(decision.reason, "identical normalized title and year")

    def test_fuzzy_title_within_same_year_is_merged_and_scored(self) -> None:
        resolver = DedupeResolver()
        left = "Detecting fraudulent financial transactions with graph representation learning"
        right = left + " methods"
        resolver.resolve(record("openalex", "W3", left, year=2022))
        canonical, decision = resolver.resolve(record("semantic_scholar", "S3", right, year=2022))
        self.assertIsNotNone(canonical)
        self.assertGreaterEqual(decision.similarity, FUZZY_THRESHOLD)
        self.assertIn("title similarity", decision.reason)

    def test_similar_titles_in_different_years_are_not_merged(self) -> None:
        resolver = DedupeResolver()
        title = "Detecting fraudulent financial transactions with graph representation learning"
        resolver.resolve(record("openalex", "W4", title, year=2019))
        canonical, decision = resolver.resolve(record("crossref", "C4", title, year=2024))
        self.assertIsNone(canonical)
        self.assertIsNone(decision)

    def test_unique_count_and_groups(self) -> None:
        resolver = DedupeResolver()
        for record_item in (
            record("openalex", "W1", "Alpha study of fraud detection systems", doi="10.1/a", year=2020),
            record("crossref", "10.1/a", "Alpha study of fraud detection systems", doi="10.1/a", year=2020),
            record("zenodo", "z1", "Beta dataset for fraud detection", year=2021),
        ):
            canonical, decision = resolver.resolve(record_item)
            if canonical:
                resolver.add_group_member(canonical, record_item)
        result = resolver.finish()
        self.assertEqual(result.unique_count, 2)
        self.assertEqual(len(result.decisions), 1)
        self.assertEqual(len(result.groups), 1)
        summary = cluster_summary(result, clusters=[{"duplicates": ["crossref:10.1/a"]}, {}])
        self.assertEqual(summary["unique_count"], 2)
        self.assertEqual(summary["duplicate_count"], 1)


class IndependenceTests(unittest.TestCase):
    def test_bibliographic_indexes_are_not_independent_confirmation(self) -> None:
        records = {
            "openalex:W1": {"source": "openalex"},
            "crossref:10.1/a": {"source": "crossref"},
            "datacite:10.1/a": {"source": "datacite"},
        }
        group = {"canonical": "openalex:W1", "members": ["crossref:10.1/a", "datacite:10.1/a"]}
        verdict = has_independent_confirmation(group, records)
        self.assertEqual(verdict["count"], 3)
        self.assertEqual(verdict["independent_sources"], [])
        self.assertFalse(verdict["independent_confirmation"])

    def test_repository_record_is_independent(self) -> None:
        records = {
            "openalex:W1": {"source": "openalex"},
            "zenodo:99": {"source": "zenodo"},
        }
        group = {"canonical": "openalex:W1", "members": ["zenodo:99"]}
        verdict = has_independent_confirmation(group, records)
        self.assertIn("zenodo", verdict["independent_sources"])
        self.assertTrue(verdict["independent_confirmation"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
