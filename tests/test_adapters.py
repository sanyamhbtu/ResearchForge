"""Source-adapter normalization tests.

Each adapter is driven with a canned payload that mimics the real API, so the
mapping from a source payload to the common ``NormalizedRecord`` schema is
verified without any network access. The tests assert the rules the project
promises: original identifiers preserved, licenses never invented, pagination
reported honestly, and failures surfaced as ``AdapterError`` instead of silence.
"""

from __future__ import annotations

import unittest

from app.http_client import HttpError
from app.sources.base import AdapterError, SearchQuery
from app.sources.openalex import OpenAlexAdapter, reconstruct_abstract
from app.sources.uci import UciAdapter


class FakeHttp:
    """Minimal stand-in for ``HttpClient`` that returns canned JSON."""

    def __init__(self, handler) -> None:
        self.handler = handler
        self.calls: list[tuple[str, dict]] = []

    def get_json(self, url: str, **kwargs):
        self.calls.append((url, kwargs))
        return self.handler(url, kwargs)

    def get(self, url: str, **kwargs):
        return self.get_json(url, **kwargs)


# ---------------------------------------------------------------------------
# OpenAlex
# ---------------------------------------------------------------------------

OPENALEX_PAYLOAD = {
    "meta": {"count": 42},
    "results": [
        {
            "id": "https://openalex.org/W123456",
            "doi": "https://doi.org/10.1234/ABC.Def",
            "display_name": "Fraud detection with graph neural networks",
            "publication_year": 2021,
            "publication_date": "2021-05-01",
            "type": "article",
            "open_access": {"is_oa": True, "oa_status": "gold"},
            "best_oa_location": {"pdf_url": "https://example.org/paper.pdf", "license": "cc-by"},
            "primary_location": {"source": {"display_name": "Journal of Fraud Research"}},
            "cited_by_count": 12,
            "referenced_works": ["https://openalex.org/W999", "https://openalex.org/W888"],
            "topics": [{"display_name": "Graph learning"}],
            "abstract_inverted_index": {"Fraud": [0], "detection": [1], "matters": [2]},
            "authorships": [{"author": {"display_name": "A. Author"}}],
            "updated_date": "2022-01-01",
        },
        {
            "id": "https://openalex.org/W654321",
            "display_name": "A fraud dataset",
            "publication_year": 2020,
            "type": "dataset",
            "open_access": {"is_oa": False},
            "best_oa_location": {},
        },
    ],
}


class OpenAlexTests(unittest.TestCase):
    def _adapter(self) -> OpenAlexAdapter:
        def handler(url, kwargs):
            return OPENALEX_PAYLOAD

        return OpenAlexAdapter(http=FakeHttp(handler))

    def test_paper_mapping_preserves_identifiers_and_metadata(self) -> None:
        page = self._adapter().search(SearchQuery(text="fraud detection", limit=25, offset=0))
        self.assertEqual(len(page.records), 2)
        record = page.records[0]
        self.assertEqual(record.source, "openalex")
        self.assertEqual(record.source_id, "W123456")
        self.assertEqual(record.kind, "paper")
        # the DOI is preserved on the URL field exactly as the source published it
        self.assertEqual(record.url, "https://doi.org/10.1234/ABC.Def")
        self.assertEqual(record.year, 2021)
        self.assertEqual(record.access, "open")
        self.assertEqual(record.license, "CC-BY-4.0")
        self.assertEqual(record.pdf_url, "https://example.org/paper.pdf")
        self.assertEqual(record.citation_count, 12)
        self.assertEqual(record.authors, ["A. Author"])
        self.assertEqual(record.reference_dois, ["W999", "W888"])
        self.assertIn("Graph learning", record.concepts)

    def test_inverted_abstract_is_reconstructed_in_order(self) -> None:
        page = self._adapter().search(SearchQuery(text="fraud", limit=25))
        self.assertEqual(page.records[0].abstract, "Fraud detection matters")

    def test_dataset_type_is_classified_as_dataset(self) -> None:
        page = self._adapter().search(SearchQuery(text="fraud", limit=25))
        dataset = page.records[1]
        self.assertEqual(dataset.kind, "dataset")
        self.assertTrue(dataset.dataset.get("notes"))

    def test_pagination_is_reported_from_the_source_total(self) -> None:
        page = self._adapter().search(SearchQuery(text="fraud", limit=25, offset=0))
        self.assertEqual(page.total_available, 42)
        self.assertEqual(page.next_offset, 25)

    def test_last_page_reports_no_next_offset(self) -> None:
        # The source reports 42 matches, so the page starting at offset 25 is the
        # last one and there is no offset to follow it.
        page = self._adapter().search(SearchQuery(text="fraud", limit=25, offset=25))
        self.assertEqual(page.total_available, 42)
        self.assertIsNone(page.next_offset)

    def test_matches_without_parsable_records_produce_a_warning(self) -> None:
        def handler(url, kwargs):
            return {"meta": {"count": 5}, "results": [{"id": "https://openalex.org/W1"}]}

        page = OpenAlexAdapter(http=FakeHttp(handler)).search(SearchQuery(text="x"))
        self.assertEqual(page.records, [])
        self.assertTrue(page.warnings)

    def test_reconstruct_abstract_handles_missing_input(self) -> None:
        self.assertEqual(reconstruct_abstract(None), "")
        self.assertEqual(reconstruct_abstract({}), "")
        self.assertEqual(reconstruct_abstract({"b": [1], "a": [0]}), "a b")


# ---------------------------------------------------------------------------
# UCI Machine Learning Repository
# ---------------------------------------------------------------------------

UCI_LIST = {"status": 200, "data": [{"id": 53, "name": "Iris"}, {"id": 54, "name": "Wine"}]}
UCI_DETAIL = {
    "status": 200,
    "data": {
        "uci_id": 53,
        "name": "Iris",
        "repository_url": "https://archive.ics.uci.edu/dataset/53/iris",
        "data_url": "https://archive.ics.uci.edu/static/public/53/iris.zip",
        "abstract": "Classic multi-class classification dataset.",
        "num_instances": 150,
        "num_features": 4,
        "variables": [{"name": "sepal_length", "role": "Feature"}, {"name": "class", "role": "Target"}],
        "tasks": ["classification"],
        "area": "Life Sciences",
        "creators": ["R. Fisher"],
        "year_of_dataset_creation": 1988,
        "last_updated": "2023-01-01",
        "dataset_doi": "10.24432/C56C76",
    },
}


class UciTests(unittest.TestCase):
    def _adapter(self, list_payload=None, detail_payload=None) -> UciAdapter:
        list_payload = UCI_LIST if list_payload is None else list_payload
        detail_payload = UCI_DETAIL if detail_payload is None else detail_payload

        def handler(url, kwargs):
            if "list" in url:
                return list_payload
            if kwargs.get("params", {}).get("id") is not None:
                return detail_payload
            return {"status": 200, "data": detail_payload.get("data")}

        return UciAdapter(http=FakeHttp(handler))

    def test_dataset_fields_come_only_from_the_source_payload(self) -> None:
        page = self._adapter().search(SearchQuery(text="iris", limit=25))
        self.assertEqual(page.total_available, 2)
        record = page.records[0]
        self.assertEqual(record.kind, "dataset")
        self.assertEqual(record.source_id, "53")
        self.assertEqual(record.url, "https://archive.ics.uci.edu/dataset/53/iris")
        self.assertEqual(record.access, "open")
        self.assertEqual(record.year, 1988)
        self.assertEqual(record.dataset["samples"], 150)
        self.assertEqual(record.dataset["num_features"], 4)
        self.assertEqual(record.dataset["files"][0]["name"], "iris.zip")
        self.assertEqual(record.dataset["doi"], "10.24432/C56C76")

    def test_license_is_left_empty_rather_than_assumed(self) -> None:
        record = self._adapter().search(SearchQuery(text="iris")).records[0]
        self.assertEqual(record.license, "")
        self.assertEqual(record.dataset["license"], "")

    def test_pagination_is_applied_by_slicing_the_catalogue(self) -> None:
        page = self._adapter().search(SearchQuery(text="a", limit=1, offset=0))
        self.assertEqual(len(page.records), 1)
        self.assertEqual(page.next_offset, 1)
        page = self._adapter().search(SearchQuery(text="a", limit=1, offset=1))
        self.assertIsNone(page.next_offset)

    def test_detail_failure_falls_back_to_catalogue_fields_with_a_warning(self) -> None:
        def handler(url, kwargs):
            if "list" in url:
                return UCI_LIST
            raise HttpError("HTTP 500", url=url, status=500)

        page = UciAdapter(http=FakeHttp(handler)).search(SearchQuery(text="iris", limit=25))
        self.assertEqual(len(page.records), 2)
        self.assertTrue(any("detail" in warning for warning in page.warnings))
        # the catalogue-level name and id are still preserved
        self.assertEqual(page.records[0].source_id, "53")
        self.assertEqual(page.records[0].title, "Iris")

    def test_unusable_list_endpoint_falls_back_to_the_detail_endpoint(self) -> None:
        page = self._adapter(list_payload={"status": 404, "data": []}).search(
            SearchQuery(text="iris", limit=25)
        )
        self.assertEqual(len(page.records), 1)
        self.assertTrue(any("list endpoint unavailable" in warning for warning in page.warnings))

    def test_total_failure_raises_adapter_error(self) -> None:
        def handler(url, kwargs):
            raise HttpError("connection refused", url=url)

        with self.assertRaises(AdapterError):
            UciAdapter(http=FakeHttp(handler)).search(SearchQuery(text="iris"))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
