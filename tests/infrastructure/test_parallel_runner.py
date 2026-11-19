"""The parallel adapter preserves discovery and failure-last reporting."""

from __future__ import annotations

import contextlib
import io
import subprocess
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from infrastructure.testing import parallel_runner


SUCCESS = (
    "Running 2 test suites (7 total tests) across 2 workers\n"
    "----------------------------------------------------------------------\n"
    "Ran 7 tests in 0.120s\n\nOK\n"
)
FAILURE = (
    "Running 1 test suites (2 total tests) across 2 workers\n"
    "F\n"
    "======================================================================\n"
    "FAIL: test_failure (tests.infrastructure.test_example.TestExample.test_failure)\n"
    "----------------------------------------------------------------------\n"
    "Traceback (most recent call last):\n"
    "  File \"fixture.py\", line 2\n"
    "AssertionError: distinguishing evidence\n"
    "----------------------------------------------------------------------\n"
    "Ran 2 tests in 0.030s\n\nFAILED (failures=1)\n"
)
EMPTY = (
    "Running 0 test suites (0 total tests) across 1 workers\n"
    "----------------------------------------------------------------------\n"
    "Ran 0 tests in 0.001s\n\nOK\n"
)


class ParallelAdapterContracts(unittest.TestCase):
    def invoke(self, outcomes):
        calls = []

        def execute(command, **options):
            calls.append((command, options))
            outcome = outcomes[len(calls) - 1]
            if isinstance(outcome, BaseException):
                raise outcome
            return outcome

        stdout, stderr = io.StringIO(), io.StringIO()
        with (
            patch.object(parallel_runner.subprocess, "run", side_effect=execute),
            contextlib.redirect_stdout(stdout),
            contextlib.redirect_stderr(stderr),
        ):
            status = parallel_runner.main([
                "-j", "2", "-s", "tests/infrastructure", "-s", "tests/repo-checks",
                "-t", ".", "-p", "test_*.py", "--budget-seconds", "150",
            ])
        return status, stdout.getvalue(), stderr.getvalue(), calls

    def test_each_discovery_root_runs_with_same_bounded_worker_policy(self):
        outcomes = [SimpleNamespace(returncode=0, stdout="", stderr=SUCCESS)] * 2
        status, _out, _err, calls = self.invoke(outcomes)
        self.assertEqual(status, 0)
        self.assertEqual(len(calls), 2)
        for (command, options), start in zip(calls, ("tests/infrastructure", "tests/repo-checks")):
            self.assertEqual(command[0], "unittest-parallel")
            self.assertEqual(command[command.index("-j") + 1], "2")
            self.assertEqual(command[command.index("-s") + 1], start)
            self.assertEqual(command[command.index("-p") + 1], "test_*.py")
            self.assertEqual(command[command.index("-t") + 1], ".")
            self.assertIn("--level=module", command)
            self.assertTrue(options["capture_output"])

    def test_failure_is_after_summary_and_other_root_still_runs(self):
        outcomes = [
            SimpleNamespace(returncode=1, stdout="", stderr=FAILURE),
            SimpleNamespace(returncode=0, stdout="", stderr=SUCCESS),
        ]
        status, _out, report, calls = self.invoke(outcomes)
        self.assertNotEqual(status, 0)
        self.assertEqual(len(calls), 2)
        self.assertIn("Ran 2 tests", report)
        self.assertIn("Ran 7 tests", report)
        self.assertIn("distinguishing evidence", report)
        self.assertLess(report.index("Ran 7 tests"), report.index("distinguishing evidence"))

    def test_zero_tests_cannot_succeed(self):
        outcomes = [
            SimpleNamespace(returncode=0, stdout="", stderr=EMPTY),
            SimpleNamespace(returncode=0, stdout="", stderr=SUCCESS),
        ]
        status, _out, report, calls = self.invoke(outcomes)
        self.assertNotEqual(status, 0)
        self.assertEqual(len(calls), 2)
        self.assertIn("NO TESTS RAN", report)

    def test_each_root_receives_only_the_remaining_shared_budget(self):
        outcomes = [SimpleNamespace(returncode=0, stdout="", stderr=SUCCESS)] * 2
        with patch.object(parallel_runner.time, "monotonic", side_effect=(0, 0, 60)):
            status, _out, _err, calls = self.invoke(outcomes)
        self.assertEqual(status, 0)
        self.assertEqual([options["timeout"] for _command, options in calls], [150, 90])

    def test_exhausted_budget_does_not_launch_another_root(self):
        outcomes = [SimpleNamespace(returncode=0, stdout="", stderr=SUCCESS)]
        with patch.object(parallel_runner.time, "monotonic", side_effect=(0, 0, 151)):
            status, _out, report, calls = self.invoke(outcomes)
        self.assertEqual(status, 2)
        self.assertEqual(len(calls), 1)
        self.assertIn("tests/repo-checks: shared time budget exhausted", report)
        self.assertIn("Failure details (end of parallel run)", report)

    def test_timeout_retains_partial_output_after_prior_failure_details(self):
        outcomes = [
            SimpleNamespace(returncode=1, stdout="", stderr=FAILURE),
            subprocess.TimeoutExpired(
                cmd="unittest-parallel", timeout=15,
                output=b"partial worker stdout", stderr=b"partial worker stderr",
            ),
        ]
        with patch.object(parallel_runner.time, "monotonic", side_effect=(0, 0, 135)):
            status, _out, report, calls = self.invoke(outcomes)
        self.assertEqual(status, 2)
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[1][1]["timeout"], 15)
        self.assertIn("tests/repo-checks: parallel runner exceeded shared time budget", report)
        self.assertIn("partial worker stdout", report)
        self.assertIn("partial worker stderr", report)
        self.assertLess(report.index("Ran 2 tests"), report.index("distinguishing evidence"))
        self.assertLess(report.index("distinguishing evidence"), report.index("partial worker stderr"))

    def test_missing_parallel_executable_fails_without_serial_fallback(self):
        with patch.object(parallel_runner.subprocess, "run", side_effect=FileNotFoundError("unittest-parallel")):
            output = io.StringIO()
            with contextlib.redirect_stderr(output):
                status = parallel_runner.main([
                    "-j", "2", "-s", "tests/infrastructure", "-t", ".",
                    "-p", "test_*.py", "--budget-seconds", "150",
                ])
        self.assertNotEqual(status, 0)
        self.assertIn("unittest-parallel", output.getvalue())


if __name__ == "__main__":
    unittest.main()
