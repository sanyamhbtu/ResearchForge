"""Dataset quality and compatibility engine tests.

The engine is deterministic, so every check can be asserted exactly: which
criterion was applied, what the outcome was, and — importantly — that
unavailable evidence is reported as ``unknown`` instead of being guessed.
"""

from __future__ import annotations

import json
import struct
import unittest
import zipfile

from app.services.compatibility import (
    FAILED,
    NOT_APPLICABLE,
    STATES,
    UNKNOWN,
    VERIFIED,
    WARNING,
    advance_state,
    assess_compatibility,
    assess_dataset,
    assess_metadata,
    inspect_file,
)

from tests.support import DatabaseTestCase


def dataset_record(**overrides) -> dict:
    record = {
        "id": 1,
        "kind": "dataset",
        "source": "uci",
        "source_id": "53",
        "title": "Iris",
        "url": "https://archive.ics.uci.edu/dataset/53/iris",
        "license": "CC-BY-4.0",
        "access": "open",
        "modality": "tabular",
        "year": 1988,
        "dataset": {
            "samples": 150,
            "files": [{"name": "iris.csv", "url": "https://example.org/iris.csv", "size_bytes": 4600}],
            "creators": ["R. Fisher"],
            "publisher": "UCI",
            "updated": "2023-01-01",
        },
    }
    record.update(overrides)
    return record


def status_of(report: dict, name: str) -> str | None:
    for check in report["checks"]:
        if check["name"] == name:
            return check["status"]
    return None


class StateMachineTests(unittest.TestCase):
    def test_states_only_move_forward(self) -> None:
        self.assertEqual(advance_state("discovered", "metadata_verified"), "metadata_verified")
        self.assertEqual(advance_state("contents_inspected", "metadata_verified"), "contents_inspected")
        self.assertEqual(advance_state("approved", "discovered"), "approved")

    def test_unknown_inputs_degrade_safely(self) -> None:
        self.assertEqual(advance_state("not-a-state", "metadata_verified"), "metadata_verified")
        self.assertEqual(advance_state("discovered", "not-a-state"), "discovered")

    def test_state_vocabulary_is_ordered(self) -> None:
        self.assertEqual(STATES[0], "discovered")
        self.assertEqual(STATES[-1], "approved")


class MetadataAssessmentTests(unittest.TestCase):
    def test_complete_metadata_is_verified(self) -> None:
        report = assess_metadata(dataset_record())
        self.assertEqual(status_of(report, "source_identity"), VERIFIED)
        self.assertEqual(status_of(report, "record_link"), VERIFIED)
        self.assertEqual(status_of(report, "license"), VERIFIED)
        self.assertEqual(status_of(report, "sample_count"), VERIFIED)
        self.assertEqual(status_of(report, "file_listing"), VERIFIED)
        self.assertEqual(report["warnings"], [])

    def test_missing_license_is_unknown_and_warned_not_assumed(self) -> None:
        report = assess_metadata(dataset_record(license=""))
        self.assertEqual(status_of(report, "license"), UNKNOWN)
        self.assertTrue(any("license" in warning for warning in report["warnings"]))

    def test_missing_source_id_fails_identity(self) -> None:
        report = assess_metadata(dataset_record(source_id=""))
        self.assertEqual(status_of(report, "source_identity"), FAILED)

    def test_absent_sample_count_is_unknown(self) -> None:
        record = dataset_record()
        record["dataset"]["samples"] = None
        report = assess_metadata(record)
        self.assertEqual(status_of(report, "sample_count"), UNKNOWN)

    def test_restricted_access_is_recorded_not_bypassed(self) -> None:
        report = assess_metadata(dataset_record(access="restricted"))
        self.assertEqual(status_of(report, "access_terms"), VERIFIED)
        detail = next(c["detail"] for c in report["checks"] if c["name"] == "access_terms")
        self.assertIn("will not bypass access controls", detail)


class FileInspectionTests(DatabaseTestCase):
    def test_missing_file_is_reported_as_failed(self) -> None:
        report = inspect_file(str(self.settings.download_dir / "nope.csv"))
        self.assertEqual(status_of(report, "file_present"), FAILED)

    def test_clean_csv_is_parsed_and_profiled(self) -> None:
        path = self.write_file("clean.csv", "a,b\n1,2\n3,4\n")
        report = inspect_file(str(path))
        self.assertEqual(status_of(report, "parseability"), VERIFIED)
        self.assertEqual(status_of(report, "schema_consistency"), VERIFIED)
        # too few rows for distribution checks
        self.assertEqual(status_of(report, "statistical_checks"), NOT_APPLICABLE)
        dimensions = {dimension["name"]: dimension for dimension in report["dimensions"]}
        self.assertEqual(dimensions["duplicate_rows"]["value"]["duplicates"], 0)

    def test_ragged_csv_fails_schema_consistency(self) -> None:
        path = self.write_file("ragged.csv", "a,b\n1,2\n3\n4,5,6\n")
        report = inspect_file(str(path))
        self.assertEqual(status_of(report, "schema_consistency"), FAILED)

    def test_missing_values_and_duplicates_are_counted(self) -> None:
        path = self.write_file("dirty.csv", "x,y\n1,\n,2\n3,4\n3,4\n")
        report = inspect_file(str(path))
        dimensions = {dimension["name"]: dimension for dimension in report["dimensions"]}
        missing = dimensions["missing_values"]["value"]
        self.assertEqual(missing["x"]["missing"], 1)
        self.assertEqual(missing["y"]["missing"], 1)
        self.assertEqual(dimensions["duplicate_rows"]["value"]["duplicates"], 1)
        # class balance and leakage are never guessed
        self.assertEqual(dimensions["class_imbalance"]["status"], UNKNOWN)
        self.assertEqual(dimensions["train_test_leakage"]["status"], UNKNOWN)

    def test_valid_archive_passes_crc_verification(self) -> None:
        path = self.settings.download_dir / "good.zip"
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("data.csv", "a,b\n1,2\n")
        report = inspect_file(str(path))
        self.assertEqual(status_of(report, "archive_integrity"), VERIFIED)

    def test_corrupt_archive_fails_integrity(self) -> None:
        path = self.write_file("bad.zip", b"this is not a zip archive")
        report = inspect_file(str(path))
        self.assertEqual(status_of(report, "archive_integrity"), FAILED)

    def test_json_and_json_lines_are_parsed(self) -> None:
        json_path = self.write_file("data.json", json.dumps([{"a": 1}, {"a": 2}]))
        self.assertEqual(status_of(inspect_file(str(json_path)), "parseability"), VERIFIED)

        lines_path = self.write_file("data.jsonl", '{"a": 1}\n{"a": 2}\n')
        self.assertEqual(status_of(inspect_file(str(lines_path)), "parseability"), VERIFIED)

    def test_invalid_json_fails_parseability(self) -> None:
        path = self.write_file("broken.json", "{not valid")
        self.assertEqual(status_of(inspect_file(str(path)), "parseability"), FAILED)

    def test_png_dimensions_are_read_from_the_header(self) -> None:
        header = b"\x89PNG\r\n\x1a\n" + b"\x00" * 8 + struct.pack(">II", 640, 480)
        path = self.write_file("image.png", header + b"\x00" * 16)
        report = inspect_file(str(path))
        self.assertEqual(status_of(report, "image_header"), VERIFIED)
        detail = next(c for c in report["checks"] if c["name"] == "image_header")
        self.assertEqual(detail["evidence"]["width"], 640)
        self.assertEqual(detail["evidence"]["height"], 480)

    def test_unknown_extension_is_reported_as_uninspected(self) -> None:
        path = self.write_file("weights.bin", b"\x00\x01\x02")
        self.assertEqual(status_of(inspect_file(str(path)), "content_inspection"), UNKNOWN)


class CompatibilityTests(unittest.TestCase):
    def test_no_selection_produces_an_explicit_statement(self) -> None:
        report = assess_compatibility([])
        self.assertEqual(report["checks"], [])
        self.assertTrue(report["warnings"])

    def test_mixed_modalities_warn(self) -> None:
        report = assess_compatibility(
            [dataset_record(modality="tabular"), dataset_record(id=2, modality="image", title="Images")]
        )
        self.assertEqual(status_of(report, "modality_consistency"), WARNING)
        self.assertTrue(any("modalities" in warning for warning in report["warnings"]))

    def test_unknown_license_blocks_a_clean_verdict(self) -> None:
        report = assess_compatibility([dataset_record(license=""), dataset_record(id=2, title="B")])
        self.assertEqual(status_of(report, "license_compatibility"), UNKNOWN)
        self.assertTrue(any("licence" in warning or "license" in warning for warning in report["warnings"]))

    def test_noncommercial_license_conflicts_with_commercial_use(self) -> None:
        report = assess_compatibility([dataset_record(license="CC-BY-NC-4.0")], intended_use="commercial product")
        self.assertEqual(status_of(report, "intended_use_license_conflict"), FAILED)
        self.assertTrue(any("commercial" in warning for warning in report["warnings"]))

    def test_restricted_access_is_flagged(self) -> None:
        report = assess_compatibility([dataset_record(access="restricted")])
        self.assertTrue(any("access-restricted" in warning for warning in report["warnings"]))

    def test_wide_temporal_span_warns(self) -> None:
        report = assess_compatibility([dataset_record(year=2000), dataset_record(id=2, title="B", year=2020)])
        self.assertEqual(status_of(report, "temporal_span"), WARNING)

    def test_shared_identifiers_warn_about_contamination(self) -> None:
        left = dataset_record()
        right = dataset_record(id=2, title="B")
        left["dataset"]["doi"] = "10.1/shared"
        right["dataset"]["doi"] = "10.1/shared"
        report = assess_compatibility([left, right])
        self.assertEqual(status_of(report, "train_test_contamination"), WARNING)

    def test_schema_overlap_is_measured(self) -> None:
        left = dataset_record()
        right = dataset_record(id=2, title="B")
        left["dataset"]["labels"] = ["a", "b", "c"]
        right["dataset"]["labels"] = ["a", "b", "d"]
        report = assess_compatibility([left, right])
        check = next(c for c in report["checks"] if c["name"] == "schema_compatibility")
        self.assertEqual(check["status"], VERIFIED)
        self.assertEqual(check["evidence"]["overlap_ratio"], 0.5)

    def test_report_never_claims_a_single_score(self) -> None:
        report = assess_compatibility([dataset_record()])
        self.assertIn("not a single", report["statement"])
        self.assertNotIn("score", report)


class AssessDatasetTests(DatabaseTestCase):
    def test_metadata_only_record_reaches_metadata_verified(self) -> None:
        report = assess_dataset(dataset_record())
        self.assertEqual(report["achieved_state"], "metadata_verified")

    def test_inspected_file_promotes_the_state(self) -> None:
        path = self.write_file("iris.csv", "a,b\n1,2\n")
        report = assess_dataset(dataset_record(), local_files=[str(path)])
        self.assertEqual(report["achieved_state"], "contents_inspected")
        self.assertTrue(any(check["name"] == "file_present" for check in report["checks"]))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
