"""Tests for the background job runner.

Jobs are persisted, so progress, results, failures and cancellation must all
survive inspection from another thread. A restarted process must never leave a
job looking like it is still running.
"""

from __future__ import annotations

import time
import unittest

from app.services import store
from app.services.jobs import (
    JobRunner,
    get_job_runner,
    report_project,
    reset_job_runner,
    run_synchronously,
)
from app.services.store import requeue_stuck_jobs

from tests.support import DatabaseTestCase

TERMINAL = {"completed", "failed", "cancelled", "interrupted"}


def wait_for(job_id: str, timeout: float = 5.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = store.get_job(job_id)
        if job and job["status"] in TERMINAL:
            return job
        time.sleep(0.01)
    raise AssertionError(f"job {job_id} did not finish: {store.get_job(job_id)}")


class JobRunnerTests(DatabaseTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.runner = JobRunner(max_workers=2)

    def tearDown(self) -> None:
        self.runner.shutdown(wait=True)
        reset_job_runner()
        super().tearDown()

    def test_successful_job_persists_progress_and_result(self) -> None:
        seen: list[tuple[float, str]] = []

        def handler(request, progress, cancel):
            progress(0.5, "halfway")
            seen.append((0.5, "halfway"))
            return {"ok": True, "value": request["value"]}

        job_id = self.runner.submit("custom", {"value": 7}, handler=handler)
        job = wait_for(job_id)
        self.assertEqual(job["status"], "completed")
        self.assertEqual(job["progress"], 1.0)
        self.assertEqual(job["result"], {"ok": True, "value": 7})
        self.assertEqual(seen, [(0.5, "halfway")])

    def test_handler_reporting_a_project_is_attached_to_the_job(self) -> None:
        project_id, _ = self.project_with_run("q")

        def handler(request, progress, cancel):
            report_project(project_id)
            return {}

        job_id = self.runner.submit("custom", {}, handler=handler)
        job = wait_for(job_id)
        self.assertEqual(job["project_id"], project_id)

    def test_failing_job_records_the_error_instead_of_vanishing(self) -> None:
        def handler(request, progress, cancel):
            raise ValueError("boom")

        job_id = self.runner.submit("custom", {}, handler=handler)
        job = wait_for(job_id)
        self.assertEqual(job["status"], "failed")
        self.assertIn("boom", job["error"])
        self.assertIn("failed: boom", job["message"])

    def test_cancellation_is_cooperative_and_recorded(self) -> None:
        def handler(request, progress, cancel):
            for _ in range(400):
                if cancel():
                    return {"cancelled": True}
                time.sleep(0.005)
            return {"cancelled": False}

        job_id = self.runner.submit("custom", {}, handler=handler)
        time.sleep(0.05)
        self.assertTrue(self.runner.cancel(job_id))
        job = wait_for(job_id)
        self.assertEqual(job["status"], "cancelled")
        self.assertTrue(job["result"]["cancelled"])
        self.assertTrue(job["cancel_requested"])

    def test_cancel_refuses_unknown_and_finished_jobs(self) -> None:
        self.assertFalse(self.runner.cancel("no-such-job"))
        job_id = self.runner.submit("custom", {}, handler=lambda r, p, c: {"ok": True})
        wait_for(job_id)
        self.assertFalse(self.runner.cancel(job_id))

    def test_unknown_kind_requires_an_explicit_handler(self) -> None:
        with self.assertRaises(ValueError):
            self.runner.submit("not-a-kind", {})

    def test_active_lists_only_unfinished_jobs(self) -> None:
        gate = {"release": False}

        def handler(request, progress, cancel):
            while not gate["release"]:
                time.sleep(0.005)
            return {"ok": True}

        job_id = self.runner.submit("custom", {}, handler=handler)
        time.sleep(0.05)
        self.assertIn(job_id, self.runner.active())
        gate["release"] = True
        wait_for(job_id)
        self.assertNotIn(job_id, self.runner.active())


class RequeueTests(DatabaseTestCase):
    def test_leftover_jobs_are_marked_interrupted(self) -> None:
        running = store.create_job("research", {})
        store.update_job(running, status="running")
        queued = store.create_job("gaps", {})
        cancelling = store.create_job("download", {})
        store.update_job(cancelling, status="cancelling")
        finished = store.create_job("manuscript", {})
        store.update_job(finished, status="completed")

        self.assertEqual(requeue_stuck_jobs(), 3)
        self.assertEqual(store.get_job(running)["status"], "interrupted")
        self.assertEqual(store.get_job(queued)["status"], "interrupted")
        self.assertEqual(store.get_job(cancelling)["status"], "interrupted")
        self.assertEqual(store.get_job(finished)["status"], "completed")
        self.assertIn("process restarted", store.get_job(running)["message"])
        # A second pass finds nothing left to requeue.
        self.assertEqual(requeue_stuck_jobs(), 0)


class SynchronousTests(DatabaseTestCase):
    def test_unknown_kind_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            run_synchronously("nope", {})

    def test_manuscript_runs_inline_with_a_progress_log(self) -> None:
        project_id, _ = self.project_with_run("q")
        result = run_synchronously("manuscript", {"project_id": project_id}, project_id=project_id)
        self.assertEqual(len(result["sections"]), 12)
        self.assertTrue(result["progress_log"])
        self.assertEqual(result["project_id"], project_id)

    def test_download_of_a_missing_record_fails_fast(self) -> None:
        with self.assertRaises(ValueError):
            run_synchronously("download", {"record_id": 9999})

    def test_compatibility_requires_at_least_one_record(self) -> None:
        project_id, _ = self.project_with_run("q")
        with self.assertRaises(ValueError):
            run_synchronously("compatibility", {"project_id": project_id, "record_ids": []})


class SingletonTests(DatabaseTestCase):
    def tearDown(self) -> None:
        reset_job_runner()
        super().tearDown()

    def test_runner_is_created_once_and_resettable(self) -> None:
        first = get_job_runner()
        self.assertIs(first, get_job_runner())
        reset_job_runner()
        self.assertIsNot(first, get_job_runner())


if __name__ == "__main__":
    unittest.main()
