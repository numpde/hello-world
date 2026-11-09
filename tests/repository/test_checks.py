"""Test selection and failure reporting have unambiguous outcomes."""

import io
import unittest

from tools.repository.checks import FailureLastRunner


class ResultContracts(unittest.TestCase):
    def test_errors_are_after_summary(self):
        class ExampleFailure(unittest.TestCase):
            def runTest(self):
                self.fail("distinctive-causal-failure")

        output = io.StringIO()
        suite = unittest.TestSuite([ExampleFailure()])
        result = FailureLastRunner(verbosity=2, stream=output).run(suite)
        self.assertFalse(result.wasSuccessful())
        text = output.getvalue()
        self.assertLess(text.index("FAILED (failures=1)"), text.index("Failure details"))
        self.assertIn("distinctive-causal-failure", text[text.index("Failure details"):])
        self.assertTrue(text.rstrip().endswith("End of failures: 1 failed, 0 errors, 0 unexpected successes"))


if __name__ == "__main__":
    unittest.main()
