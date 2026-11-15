"""Shared job invocation and source-admission contracts for application adapters."""

from __future__ import annotations

import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from infrastructure.commands import job_execution
from infrastructure.docker import client, finite_job


IMAGE = "sha256:" + "a" * 64
JOB = finite_job.Job(IMAGE, finite_job.Command("true"), ())


class JobExecutionContracts(unittest.TestCase):
    def test_success_prints_work_output_once_and_returns(self):
        outcome = finite_job.Outcome(0, "work completed\n", None, (), None)
        output, diagnostics = io.StringIO(), io.StringIO()
        with (
            patch.object(job_execution.finite_job, "run_container", return_value=outcome) as run,
            contextlib.redirect_stdout(output),
            contextlib.redirect_stderr(diagnostics),
        ):
            job_execution.run(object(), "generic", JOB)
        run.assert_called_once()
        self.assertEqual(output.getvalue(), "work completed\n")
        self.assertEqual(diagnostics.getvalue(), "")

    def test_work_failure_and_secondary_cleanup_evidence_are_reported_last(self):
        outcome = finite_job.Outcome(
            1, "test output\n", "Container exited with 7",
            ("Output was truncated", "CLEANUP UNCONFIRMED; recovery at /tmp/owned"), Path("/tmp/owned"),
        )
        output, diagnostics = io.StringIO(), io.StringIO()
        with (
            patch.object(job_execution.finite_job, "run_container", return_value=outcome),
            contextlib.redirect_stdout(output),
            contextlib.redirect_stderr(diagnostics),
            self.assertRaisesRegex(client.OperationError, "Application finite job failed"),
        ):
            job_execution.run(object(), "generic", JOB)

        self.assertEqual(output.getvalue(), "test output\n")
        report = diagnostics.getvalue()
        self.assertIn("Container exited with 7", report)
        self.assertLess(report.index("Container exited with 7"), report.index("Output was truncated"))
        self.assertLess(report.index("Output was truncated"), report.index("CLEANUP UNCONFIRMED"))

    def test_temporary_mount_owner_is_the_dispatcher_when_present(self):
        with tempfile.TemporaryDirectory() as root:
            with finite_job.temporary_job_directory(prefix="job-", parent=Path(root)) as owned:
                job = finite_job.Job(IMAGE, JOB.command, (),
                    writable_mounts=(finite_job.WritableMount(owned.path, "/output"),))
                with (
                    patch.object(owned, "run", return_value=finite_job.Outcome(0, "", None, (), None)) as run,
                    patch.object(job_execution.finite_job, "run_container") as direct,
                ):
                    job_execution.run(object(), "generic", job, temporary_mount=owned)
                run.assert_called_once()
                direct.assert_not_called()

    def test_missing_linked_or_unusable_read_only_sources_are_rejected(self):
        with tempfile.TemporaryDirectory() as root:
            directory = Path(root)
            regular = directory / "input"
            regular.write_text("content")
            folder = directory / "folder"
            folder.mkdir()
            symlink = directory / "linked"
            symlink.symlink_to(regular)
            self.assertEqual(
                finite_job.read_only_mount(regular, "/input").source, regular,
            )
            self.assertEqual(
                finite_job.read_only_mount(folder, "/dir").source, folder,
            )
            for source in (symlink, directory / "absent"):
                with self.subTest(source=str(source)):
                    with self.assertRaisesRegex(client.OperationError, "regular application input"):
                        finite_job.read_only_mount(source, "/input")


if __name__ == "__main__":
    unittest.main()
