"""Temporary mount lifetime follows the finite job's observed retirement."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from infrastructure.docker import client, finite_job


IMAGE = "sha256:" + "a" * 64


class TemporaryJobMountContracts(unittest.TestCase):
    def job(self, source: Path) -> finite_job.Job:
        return finite_job.Job(
            image_id=IMAGE,
            command=finite_job.Command("true"),
            mounts=(),
            writable_mounts=(finite_job.WritableMount(source, "/output"),),
        )

    def test_success_and_work_failure_release_only_after_confirmed_retirement(self):
        for status in (0, 1):
            with self.subTest(status=status), tempfile.TemporaryDirectory() as parent:
                with finite_job.temporary_job_directory(
                    prefix="owned-test-",
                    parent=Path(parent),
                ) as temporary:
                    source = temporary.path
                    (source / "artifact").write_text("preserved until job ends")
                    outcome = finite_job.Outcome(
                        status=status,
                        output="",
                        failure="work failed" if status else None,
                        diagnostics=(),
                        recovery=None,
                    )
                    with patch.object(finite_job, "run_container", return_value=outcome) as run:
                        result = temporary.run(object(), "generic", self.job(source))
                    run.assert_called_once()
                    self.assertEqual(result, outcome)
                    self.assertTrue(source.exists())

                self.assertFalse(source.exists())

    def test_refused_removal_retains_mounted_directory_and_records_location(self):
        with tempfile.TemporaryDirectory() as parent:
            recovery = Path(parent) / "operation"
            recovery.mkdir()
            (recovery / "recovery.txt").write_text("Container ownership evidence\n")

            with finite_job.temporary_job_directory(
                prefix="owned-test-",
                parent=Path(parent),
            ) as temporary:
                source = temporary.path
                (source / "artifact").write_text("must survive refused removal")
                failure = finite_job.Outcome(
                    status=1,
                    output="",
                    failure="Container cleanup unconfirmed",
                    diagnostics=("CLEANUP UNCONFIRMED",),
                    recovery=recovery,
                )
                with patch.object(finite_job, "run_container", return_value=failure):
                    result = temporary.run(object(), "generic", self.job(source))

            self.assertTrue(source.is_dir())
            self.assertEqual((source / "artifact").read_text(), "must survive refused removal")
            self.assertEqual(result.failure, failure.failure)
            self.assertTrue(any(str(source) in line for line in result.diagnostics))
            self.assertIn(str(source), (recovery / "recovery.txt").read_text())

    def test_unexpected_runner_error_retains_mount_and_reports_location(self):
        with tempfile.TemporaryDirectory() as parent:
            with finite_job.temporary_job_directory(
                prefix="owned-test-",
                parent=Path(parent),
            ) as temporary:
                source = temporary.path
                with patch.object(
                    finite_job,
                    "run_container",
                    side_effect=RuntimeError("unexpected runner failure"),
                ):
                    with self.assertRaisesRegex(RuntimeError, "unexpected runner failure") as raised:
                        temporary.run(object(), "generic", self.job(source))

            self.assertTrue(source.exists())
            self.assertIn(str(source), " ".join(raised.exception.__notes__))

    def test_unused_or_unbound_mount_is_not_retained(self):
        with tempfile.TemporaryDirectory() as parent:
            with finite_job.temporary_job_directory(
                prefix="owned-test-",
                parent=Path(parent),
            ) as temporary:
                unused = temporary.path
            self.assertFalse(unused.exists())

            with finite_job.temporary_job_directory(
                prefix="owned-test-",
                parent=Path(parent),
            ) as temporary:
                unbound = temporary.path
                with patch.object(finite_job, "run_container") as run:
                    with self.assertRaisesRegex(client.OperationError, "does not mount"):
                        temporary.run(
                            object(),
                            "generic",
                            finite_job.Job(IMAGE, finite_job.Command("true"), ()),
                        )
                    run.assert_not_called()
            self.assertFalse(unbound.exists())

    def test_one_directory_owner_cannot_run_two_jobs(self):
        with tempfile.TemporaryDirectory() as parent:
            with finite_job.temporary_job_directory(
                prefix="owned-test-",
                parent=Path(parent),
            ) as temporary:
                job = self.job(temporary.path)
                outcome = finite_job.Outcome(0, "", None, (), None)
                with patch.object(finite_job, "run_container", return_value=outcome) as run:
                    temporary.run(object(), "generic", job)
                    with self.assertRaisesRegex(client.OperationError, "already ran"):
                        temporary.run(object(), "generic", job)
                    run.assert_called_once()


if __name__ == "__main__":
    unittest.main()
