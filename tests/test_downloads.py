"""Dataset download-manager tests.

The manager must never bypass access controls, must refuse oversized files
before writing them, must validate what it wrote, and must record an honest
provenance manifest. All of this is tested with a fake ``urlopen``.
"""

from __future__ import annotations

import unittest
import urllib.error
from unittest import mock

from app.services import store
from app.services.downloads import (
    build_manifest,
    can_download,
    download_candidates,
    download_record,
    download_stats,
    purge_partial_files,
    safe_filename,
    write_manifest,
)

from tests.support import DatabaseTestCase

CSV_BODY = b"a,b\n1,2\n3,4\n"


class FakeStream:
    def __init__(self, status: int, body: bytes, headers: dict | None = None) -> None:
        self.status = status
        self._body = body
        self._pos = 0
        self.headers = headers or {}

    def read(self, size: int | None = -1) -> bytes:
        if size is None or size < 0:
            chunk, self._pos = self._body[self._pos:], len(self._body)
            return chunk
        chunk = self._body[self._pos:self._pos + size]
        self._pos += len(chunk)
        return chunk

    def __enter__(self) -> "FakeStream":
        return self

    def __exit__(self, *exc: object) -> bool:
        return False


class SafeFilenameTests(unittest.TestCase):
    def test_url_is_reduced_to_a_basename(self) -> None:
        self.assertEqual(safe_filename("https://example.org/data/iris.csv"), "iris.csv")

    def test_unsafe_characters_are_replaced(self) -> None:
        cleaned = safe_filename("weird name!!.zip")
        self.assertNotIn(" ", cleaned)
        self.assertTrue(cleaned.endswith(".zip"))

    def test_path_traversal_is_stripped(self) -> None:
        self.assertEqual(safe_filename("../../etc/passwd"), "passwd")

    def test_fallback_is_used_for_empty_input(self) -> None:
        self.assertEqual(safe_filename("...", fallback="fallback.csv"), "fallback.csv")


class EligibilityTests(DatabaseTestCase):
    def _record(self, **overrides) -> dict:
        project_id, run_id = self.project_with_run()
        record = {
            "source": "zenodo",
            "source_id": "z1",
            "kind": "dataset",
            "title": "Dataset",
            "url": "https://zenodo.org/records/1",
            "access": "open",
            "dataset": {
                "files": [{"name": "data.csv", "url": "https://example.org/data.csv"}],
                "download_url": "https://example.org/data.csv",
            },
        }
        record.update(overrides)
        record_id = self.add_record(run_id, project_id, **record)
        return store.get_record(record_id)

    def test_non_dataset_records_are_not_downloadable(self) -> None:
        allowed, reason = can_download(self._record(kind="paper"))
        self.assertFalse(allowed)
        self.assertIn("not a dataset", reason)

    def test_restricted_access_is_refused_not_bypassed(self) -> None:
        allowed, reason = can_download(self._record(access="restricted"))
        self.assertFalse(allowed)
        self.assertIn("does not bypass access controls", reason)

    def test_record_without_a_file_url_cannot_be_downloaded(self) -> None:
        record = self._record()
        record["dataset"] = {"files": []}
        allowed, reason = can_download(record)
        self.assertFalse(allowed)
        self.assertIn("no direct file URL", reason)

    def test_download_candidates_fall_back_to_the_dataset_download_url(self) -> None:
        record = self._record()
        record["dataset"] = {"files": [], "download_url": "https://example.org/whole-dataset.zip"}
        candidates = download_candidates(record)
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].name, "whole-dataset.zip")

    def test_open_dataset_with_files_is_downloadable(self) -> None:
        allowed, reason = can_download(self._record())
        self.assertTrue(allowed)
        self.assertIn("available for download", reason)


class DownloadExecutionTests(DatabaseTestCase):
    def _record(self) -> dict:
        project_id, run_id = self.project_with_run()
        record_id = self.add_record(
            run_id,
            project_id,
            source="zenodo",
            source_id="z1",
            kind="dataset",
            title="Dataset",
            url="https://zenodo.org/records/1",
            access="open",
            license="CC-BY-4.0",
            dataset={
                "files": [{"name": "data.csv", "url": "https://example.org/data.csv"}],
                "download_url": "https://example.org/data.csv",
            },
        )
        self.project_id = project_id
        return store.get_record(record_id)

    def test_successful_download_is_validated_and_hashed(self) -> None:
        record = self._record()
        with mock.patch(
            "app.services.downloads.urllib.request.urlopen",
            lambda request, timeout=None: FakeStream(
                200, CSV_BODY, {"Content-Length": str(len(CSV_BODY)), "Content-Type": "text/csv"}
            ),
        ):
            result = download_record(record, project_id=self.project_id)

        self.assertEqual(result["status"], "verified")
        outcome = result["files"][0]
        self.assertEqual(outcome["status"], "verified")
        self.assertEqual(outcome["bytes"], len(CSV_BODY))
        self.assertEqual(len(outcome["sha256"]), 64)
        self.assertTrue(any(check["name"] == "parseability" for check in outcome["checks"]))

        state = store.get_dataset_state(record["id"])
        self.assertEqual(state["state"], "download_verified")

    def test_oversized_file_is_refused_before_writing(self) -> None:
        record = self._record()
        with mock.patch(
            "app.services.downloads.urllib.request.urlopen",
            lambda request, timeout=None: FakeStream(
                200, b"x" * 10, {"Content-Length": "10000000", "Content-Type": "application/zip"}
            ),
        ):
            result = download_record(record, project_id=self.project_id, max_bytes=1000)

        self.assertEqual(result["status"], "failed")
        outcome = result["files"][0]
        self.assertEqual(outcome["status"], "skipped_too_large")
        self.assertIn("exceeds", outcome["detail"])
        # nothing was written
        target = self.settings.download_dir / f"dataset_{record['id']}" / "data.csv"
        self.assertFalse(target.exists())

    def test_forbidden_response_is_reported_as_requiring_manual_access(self) -> None:
        record = self._record()

        def raise_403(request, timeout=None):
            raise urllib.error.HTTPError("https://example.org/data.csv", 403, "Forbidden", {}, None)

        with mock.patch("app.services.downloads.urllib.request.urlopen", raise_403):
            result = download_record(record, project_id=self.project_id)

        self.assertEqual(result["status"], "failed")
        self.assertIn("does not bypass access controls", result["files"][0]["detail"])

    def test_transport_failure_is_recorded_without_crashing(self) -> None:
        record = self._record()

        def raise_oserror(request, timeout=None):
            raise OSError("connection reset")

        with mock.patch("app.services.downloads.urllib.request.urlopen", raise_oserror):
            result = download_record(record, project_id=self.project_id)

        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["files"][0]["status"], "failed")
        stored = store.list_downloads(project_id=self.project_id)
        self.assertEqual(stored[0]["status"], "failed")
        self.assertTrue(stored[0]["error"])

    def test_cancellation_stops_before_writing(self) -> None:
        record = self._record()

        def explode(request, timeout=None):  # pragma: no cover - must never run
            raise AssertionError("nothing should be downloaded when cancelled")

        with mock.patch("app.services.downloads.urllib.request.urlopen", explode):
            result = download_record(
                record, project_id=self.project_id, cancel_check=lambda: True
            )
        self.assertEqual(result["files"][0]["status"], "cancelled")


class ManifestTests(DatabaseTestCase):
    def test_manifest_records_provenance_and_totals(self) -> None:
        project_id, run_id = self.project_with_run()
        record_id = self.add_record(
            run_id,
            project_id,
            source="zenodo",
            source_id="z1",
            kind="dataset",
            title="Dataset",
            url="https://zenodo.org/records/1",
            access="open",
            license="CC-BY-4.0",
            retrieved_at="2026-01-01T00:00:00Z",
            dataset={
                "files": [{"name": "data.csv", "url": "https://example.org/data.csv"}],
                "version": "1.2",
                "creators": ["A. Author"],
            },
        )
        record = store.get_record(record_id)
        with mock.patch(
            "app.services.downloads.urllib.request.urlopen",
            lambda request, timeout=None: FakeStream(
                200, CSV_BODY, {"Content-Length": str(len(CSV_BODY)), "Content-Type": "text/csv"}
            ),
        ):
            download_record(record, project_id=project_id)

        manifest = build_manifest(project_id)
        self.assertEqual(manifest["manifest_version"], "1.0")
        self.assertEqual(manifest["totals"]["datasets"], 1)
        self.assertEqual(manifest["totals"]["files_downloaded"], 1)
        entry = manifest["entries"][0]
        self.assertEqual(entry["license"], "CC-BY-4.0")
        self.assertEqual(entry["retrieved_at"], "2026-01-01T00:00:00Z")
        self.assertEqual(entry["version"], "1.2")
        self.assertEqual(entry["downloads"][0]["status"], "verified")
        self.assertEqual(len(entry["downloads"][0]["sha256"]), 64)

        written = write_manifest(project_id)
        self.assertTrue(written["path"])
        stats = download_stats(project_id)
        self.assertEqual(stats["by_status"].get("verified"), 1)

    def test_purge_removes_partial_files(self) -> None:
        self.write_file("dataset_1/data.csv.part", b"partial")
        self.assertEqual(purge_partial_files(), 1)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
