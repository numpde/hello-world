"""Repository test outcomes remain useful without custom unittest runners."""

import contextlib
import io
import unittest

from tools.repository import checks


class ResultContracts(unittest.TestCase):
    def report(self, *cases):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            status = checks.run(unittest.TestSuite(cases))
        return status, output.getvalue()

    def test_success(self):
        class Passing(unittest.TestCase):
            def runTest(self):
                self.assertTrue(True)

        status, output = self.report(Passing())
        self.assertEqual(status, 0)
        self.assertIn("Ran 1 test", output)
        self.assertEqual(output.splitlines()[-1], "OK")

    def test_failures_errors_and_unexpected_successes_follow_summary(self):
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
        errors = io.StringIO()
        with contextlib.redirect_stderr(errors):
            status = checks.run(unittest.TestSuite())
        self.assertEqual(status, 1)
        self.assertIn("NO TESTS RAN", errors.getvalue())


if __name__ == "__main__":
    unittest.main()
