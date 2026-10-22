"""Tests for the verification pipeline.

These tests pin down the refusal rules: a missing DOI is inconclusive rather
than a failure, a source that cannot be reached is not treated as a source that
does not exist, and a registrar that disagrees produces a conflict instead of a
silent overwrite.
"""

from __future__ import annotations

import unittest

from app.http_client import HttpError
from app.services import verification
from app.services.verification import (
    CONFLICT,
    FAILED,
    INACCESSIBLE,
    INCONCLUSIVE,
    VERIFIED,
    Verifier,
)


class FakeHttp:
    """Minimal stand-in for :class:`HttpClient` used by the verifier."""

    def __init__(self, statuses: dict[str, int] | None = None, errors: dict[str, Exception] | None = None) -> None:
        self.statuses = statuses or {}
        self.errors = errors or {}
        self.probed: list[str] = []

    def head_status(self, url: str, **_: object) -> int:
        self.probed.append(url)
        if url in self.errors:
            raise self.errors[url]
        return self.statuses.get(url, 200)


def record(**overrides) -> dict:
    base = {
        "record_key": "crossref:10.1234/abc",
        "source": "crossref",
        "source_id": "10.1234/abc",
        "kind": "paper",
        "title": "Fraud detection with graph neural networks",
        "url": "https://example.org/paper",
        "license": "",
        "doi": "10.1234/abc",
    }
    base.update(overrides)
    return base


class IdentifierTests(unittest.TestCase):
    def setUp(self) -> None:
        self.verifier = Verifier(FakeHttp())

    def test_valid_doi_is_verified(self) -> None:
        check = self.verifier._identifier_check(record(), "10.1234/abc")
        self.assertEqual(check.status, VERIFIED)
        self.assertEqual(check.evidence_url, "https://doi.org/10.1234/abc")

    def test_missing_doi_falls_back_to_repository_identifier(self) -> None:
        check = self.verifier._identifier_check(record(doi=None), "")
        self.assertEqual(check.status, INCONCLUSIVE)
        self.assertIn("crossref:10.1234/abc", check.detail)

    def test_malformed_doi_fails_the_syntax_check(self) -> None:
        check = self.verifier._identifier_check(record(), "not-a-doi")
        self.assertEqual(check.status, FAILED)


class RegistrarTests(unittest.TestCase):
    def setUp(self) -> None:
        self.verifier = Verifier(FakeHttp())

    def test_resolved_doi_yields_verified_and_agreement(self) -> None:
        self.verifier.crossref.resolve_doi = lambda doi: {
            "title": ["Fraud detection with graph neural networks"],
            "DOI": doi,
            "publisher": "Example Press",
            "author": [{"given": "A.", "family": "Author"}],
            "issued": {"date-parts": [[2021]]},
        }
        report = self.verifier.verify(record(), check_links=False)
        self.assertEqual(report.status_of("registrar_resolution"), VERIFIED)
        self.assertEqual(report.status_of("metadata_agreement"), VERIFIED)
        # The identifier and metadata checks passed, but the licence could not be
        # established from the registrar metadata, so the record is not reported
        # as wholly verified.
        self.assertEqual(report.status_of("license_consistency"), INCONCLUSIVE)
        self.assertEqual(report.overall, "partially_verified")
        self.assertEqual(report.resolved_metadata["publisher"], "Example Press")
        self.assertEqual(report.resolved_metadata["creators"], ["A. Author"])
        self.assertEqual(report.resolved_metadata["publication_year"], 2021)

    def test_disagreeing_registrar_title_is_a_conflict(self) -> None:
        self.verifier.crossref.resolve_doi = lambda doi: {
            "title": ["An entirely unrelated study of marine biology"]
        }
        report = self.verifier.verify(record(), check_links=False)
        self.assertEqual(report.status_of("metadata_agreement"), CONFLICT)
        self.assertEqual(report.overall, CONFLICT)

    def test_lookup_failure_is_inconclusive_not_absence(self) -> None:
        def boom(doi: str) -> dict:
            raise HttpError("connection reset", url="https://api.crossref.org", status=None)

        self.verifier.crossref.resolve_doi = boom
        report = self.verifier.verify(record(), check_links=False)
        self.assertEqual(report.status_of("registrar_resolution"), INCONCLUSIVE)
        detail = next(c.detail for c in report.checks if c.check_name == "registrar_resolution")
        self.assertIn("NOT evidence", detail)
        # The DOI syntax check is still verified, so the record is not reported as
        # wholly inconclusive; the registrar lookup itself remains unproven.
        self.assertEqual(report.status_of("stable_identifier"), VERIFIED)
        self.assertEqual(report.overall, "partially_verified")

    def test_no_registrar_record_fails_resolution(self) -> None:
        self.verifier.crossref.resolve_doi = lambda doi: None
        self.verifier.datacite.resolve_doi = lambda doi: None
        report = self.verifier.verify(record(), check_links=False)
        self.assertEqual(report.status_of("registrar_resolution"), FAILED)

    def test_record_without_doi_skips_resolution(self) -> None:
        report = self.verifier.verify(record(doi=None), check_links=False)
        self.assertEqual(report.status_of("registrar_resolution"), verification.SKIPPED)


class LicenseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.verifier = Verifier(FakeHttp())

    def test_disagreeing_licences_produce_a_conflict(self) -> None:
        check = self.verifier._license_check(
            record(license="cc-by"), {"rightsList": [{"rightsIdentifier": "CC-BY-4.0"}]}
        )
        self.assertEqual(check.status, CONFLICT)
        self.assertIn("cc-by", check.detail)

    def test_absent_license_is_inconclusive_not_permissive(self) -> None:
        check = self.verifier._license_check(record(license=""), None)
        self.assertEqual(check.status, INCONCLUSIVE)
        self.assertIn("not assumed", check.detail)

    def test_declared_license_is_verified(self) -> None:
        check = self.verifier._license_check(record(license="CC-BY-4.0"), None)
        self.assertEqual(check.status, VERIFIED)


class LinkTests(unittest.TestCase):
    def _verify(self, url: str, **status_kwargs) -> "verification.VerificationReport":
        http = FakeHttp(**status_kwargs)
        verifier = Verifier(http)
        verifier.crossref.resolve_doi = lambda doi: None
        verifier.datacite.resolve_doi = lambda doi: None
        return verifier.verify(record(url=url, doi=None), check_links=True)

    def test_reachable_link_is_verified(self) -> None:
        report = self._verify("https://example.org/paper", statuses={"https://example.org/paper": 200})
        self.assertEqual(report.status_of("source_link"), VERIFIED)

    def test_controlled_access_is_labelled_inaccessible(self) -> None:
        report = self._verify("https://example.org/paper", statuses={"https://example.org/paper": 403})
        self.assertEqual(report.status_of("source_link"), INACCESSIBLE)
        detail = next(c.detail for c in report.checks if c.check_name == "source_link")
        self.assertIn("does not bypass access controls", detail)

    def test_missing_link_fails(self) -> None:
        report = self._verify("https://example.org/gone", statuses={"https://example.org/gone": 404})
        self.assertEqual(report.status_of("source_link"), FAILED)

    def test_server_error_is_inconclusive(self) -> None:
        report = self._verify("https://example.org/paper", statuses={"https://example.org/paper": 500})
        self.assertEqual(report.status_of("source_link"), INCONCLUSIVE)

    def test_probe_failure_leaves_reachability_unknown(self) -> None:
        report = self._verify(
            "https://example.org/paper",
            errors={"https://example.org/paper": HttpError("timed out", url="https://example.org/paper")},
        )
        self.assertEqual(report.status_of("source_link"), INCONCLUSIVE)
        detail = next(c.detail for c in report.checks if c.check_name == "source_link")
        self.assertIn("Reachability is unknown", detail)

    def test_dataset_file_is_probed_when_published(self) -> None:
        dataset = record(
            kind="dataset",
            doi=None,
            url="https://zenodo.org/records/1",
            dataset={"files": [{"name": "iris.csv", "url": "https://zenodo.org/records/1/iris.csv"}]},
        )
        http = FakeHttp(statuses={"https://zenodo.org/records/1/iris.csv": 200})
        verifier = Verifier(http)
        report = verifier.verify(dataset, check_links=True)
        self.assertIn("https://zenodo.org/records/1/iris.csv", http.probed)
        self.assertEqual(report.status_of("dataset_file_reachable"), VERIFIED)

    def test_dataset_without_file_url_is_inconclusive(self) -> None:
        dataset = record(
            kind="dataset", doi=None, url="https://zenodo.org/records/1", dataset={"files": [{"name": "iris.csv"}]}
        )
        verifier = Verifier(FakeHttp())
        report = verifier.verify(dataset, check_links=True)
        self.assertEqual(report.status_of("dataset_file_reachable"), INCONCLUSIVE)


class ReportShapeTests(unittest.TestCase):
    def test_overall_prefers_conflict_then_failure(self) -> None:
        report = verification.VerificationReport(record_key="x")
        report.checks.append(verification.CheckResult("a", VERIFIED, "m"))
        self.assertEqual(report.overall, VERIFIED)
        report.checks.append(verification.CheckResult("b", INACCESSIBLE, "m"))
        self.assertEqual(report.overall, "partially_verified")
        report.checks.append(verification.CheckResult("c", CONFLICT, "m"))
        self.assertEqual(report.overall, CONFLICT)

    def test_report_is_serializable_and_status_lookup_defaults_to_skipped(self) -> None:
        report = verification.VerificationReport(record_key="x")
        self.assertEqual(report.status_of("missing"), verification.SKIPPED)
        self.assertEqual(report.overall, INCONCLUSIVE)
        payload = report.as_dict()
        self.assertEqual(payload["record_key"], "x")
        self.assertEqual(payload["checks"], [])


if __name__ == "__main__":
    unittest.main()
