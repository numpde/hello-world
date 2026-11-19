"""Runner selection is explicit, bounded, and independent of Docker."""

from __future__ import annotations

import unittest

from infrastructure.docker.client import OperationError
from infrastructure.commands import runner_selection


class RunnerSelectionContracts(unittest.TestCase):
    def test_default_and_explicit_serial_do_not_request_parallel_workers(self):
        for environment in ({}, {"HELLO_WORLD_TEST_RUNNER": "serial"}):
            with self.subTest(environment=environment):
                option = runner_selection.select(environment, "check")
                self.assertEqual((option.name, option.workers), ("serial", None))

    def test_parallel_requires_explicit_mode_and_bounds_worker_count(self):
        option = runner_selection.select({"HELLO_WORLD_TEST_RUNNER": "parallel"}, "check")
        self.assertEqual((option.name, option.workers), ("parallel", 2))
        for workers in ("1", "2", "4"):
            with self.subTest(workers=workers):
                option = runner_selection.select({
                    "HELLO_WORLD_TEST_RUNNER": "parallel",
                    "HELLO_WORLD_TEST_WORKERS": workers,
                }, "check")
                self.assertEqual(option.workers, int(workers))

    def test_bad_modes_and_worker_counts_fail_without_fallback(self):
        candidates = (
            {"HELLO_WORLD_TEST_RUNNER": ""},
            {"HELLO_WORLD_TEST_RUNNER": "PARALLEL"},
            {"HELLO_WORLD_TEST_RUNNER": "other"},
            {"HELLO_WORLD_TEST_WORKERS": "2"},
            {"HELLO_WORLD_TEST_RUNNER": "serial", "HELLO_WORLD_TEST_WORKERS": "2"},
            *({
                "HELLO_WORLD_TEST_RUNNER": "parallel",
                "HELLO_WORLD_TEST_WORKERS": workers,
            } for workers in ("", "0", "5", "-1", "1.5", " 2", "02", "unlimited")),
        )
        for environment in candidates:
            with self.subTest(environment=environment), self.assertRaises(OperationError):
                runner_selection.select(environment, "check")

    def test_application_operation_check_does_not_implicitly_use_parallel(self):
        self.assertEqual(runner_selection.select({}, "application-check").name, "serial")
        with self.assertRaises(OperationError):
            runner_selection.select({"HELLO_WORLD_TEST_RUNNER": "parallel"}, "application-check")


if __name__ == "__main__":
    unittest.main()
