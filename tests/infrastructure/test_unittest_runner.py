"""Check unittest's exit status and placement of failure details.

These small in-memory suites exercise reporting without Docker or repository
file discovery. A failing suite must leave the useful tracebacks at the end.
"""

import contextlib
import io
import unittest

from infrastructure.testing import unittest_runner


class ResultContracts(unittest.TestCase):
    """Exercise success, failure, skip and empty-suite reporting in isolation."""
    def report(self, *cases):
        """Run synthetic cases and return their exit status and printed report."""
        captured_report = io.StringIO()
        with contextlib.redirect_stdout(captured_report):
            run_status = unittest_runner.run(unittest.TestSuite(cases))
        return run_status, captured_report.getvalue()

    def test_success(self):
        """A passing suite prints its total and finishes with OK."""
        class Passing(unittest.TestCase):
            def runTest(self):
                self.assertTrue(True)

        run_status, printed_report = self.report(Passing())
        self.assertEqual(run_status, 0)
        self.assertIn("Ran 1 test", printed_report)
        self.assertEqual(printed_report.splitlines()[-1], "OK")

    def test_failures_errors_and_unexpected_successes_follow_summary(self):
        """All unsuccessful outcome types must retain their causes after the summary."""
        class Failure(unittest.TestCase):
            def runTest(self):
                self.fail("distinctive-causal-failure")

        class Error(unittest.TestCase):
            def runTest(self):
                raise RuntimeError("distinctive-causal-error")

        class Unexpected(unittest.TestCase):
            @unittest.expectedFailure
            def runTest(self):
                self.assertTrue(True)

        run_status, printed_report = self.report(Failure(), Error(), Unexpected())
        self.assertEqual(run_status, 1)
        summary_position = printed_report.index("FAILED (failures=1, errors=1, unexpected successes=1)")
        details_position = printed_report.index("Failure details")
        self.assertLess(summary_position, details_position)
        self.assertIn("distinctive-causal-failure", printed_report[details_position:])
        self.assertIn("distinctive-causal-error", printed_report[details_position:])
        self.assertIn("UNEXPECTED SUCCESS:", printed_report[details_position:])
        self.assertTrue(printed_report.rstrip().endswith("End of failures: failures=1, errors=1, unexpected_successes=1"))

    def test_skipped_and_expected_failures_are_not_reported_as_failures(self):
        """Intentional skips and expected failures must not make a successful suite fail."""
        class Skipped(unittest.TestCase):
            @unittest.skip("not relevant here")
            def runTest(self):
                self.fail("not executed")

        class Expected(unittest.TestCase):
            @unittest.expectedFailure
            def runTest(self):
                self.fail("expected")

        run_status, printed_report = self.report(Skipped(), Expected())
        self.assertEqual(run_status, 0)
        self.assertEqual(printed_report.splitlines()[-1], "OK")

    def test_no_tests_is_an_error(self):
        """An empty selection must not be reported as a green test run."""
        captured_stderr = io.StringIO()
        with contextlib.redirect_stderr(captured_stderr):
            run_status = unittest_runner.run(unittest.TestSuite())
        self.assertEqual(run_status, 1)
        self.assertIn("NO TESTS RAN", captured_stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
