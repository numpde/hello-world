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
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            status = unittest_runner.run(unittest.TestSuite(cases))
        return status, output.getvalue()

    def test_success(self):
        """A passing suite prints its total and finishes with OK."""
        class Passing(unittest.TestCase):
            def runTest(self):
                self.assertTrue(True)

        status, output = self.report(Passing())
        self.assertEqual(status, 0)
        self.assertIn("Ran 1 test", output)
        self.assertEqual(output.splitlines()[-1], "OK")

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

        status, output = self.report(Failure(), Error(), Unexpected())
        self.assertEqual(status, 1)
        summary = output.index("FAILED (failures=1, errors=1, unexpected successes=1)")
        details = output.index("Failure details")
        self.assertLess(summary, details)
        self.assertIn("distinctive-causal-failure", output[details:])
        self.assertIn("distinctive-causal-error", output[details:])
        self.assertIn("UNEXPECTED SUCCESS:", output[details:])
        self.assertTrue(output.rstrip().endswith("End of failures: failures=1, errors=1, unexpected_successes=1"))

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

        status, output = self.report(Skipped(), Expected())
        self.assertEqual(status, 0)
        self.assertEqual(output.splitlines()[-1], "OK")

    def test_no_tests_is_an_error(self):
        """An empty selection must not be reported as a green test run."""
        errors = io.StringIO()
        with contextlib.redirect_stderr(errors):
            status = unittest_runner.run(unittest.TestSuite())
        self.assertEqual(status, 1)
        self.assertIn("NO TESTS RAN", errors.getvalue())


if __name__ == "__main__":
    unittest.main()
