"""Discover contained repository tests and print actionable failures last."""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path


class DeferredFailures(unittest.TextTestResult):
    def printErrors(self) -> None:
        pass

    def printErrorsAtEnd(self) -> None:
        super().printErrors()


class FailureLastRunner(unittest.TextTestRunner):
    resultclass = DeferredFailures

    def run(self, suite: unittest.TestSuite) -> unittest.TestResult:
        result = super().run(suite)
        if not result.wasSuccessful():
            self.stream.writeln("\nFailure details (end of repository run):")
            result.printErrorsAtEnd()
            for unexpected in result.unexpectedSuccesses:
                self.stream.writeln(f"UNEXPECTED SUCCESS: {unexpected.id()}")
            self.stream.writeln(
                f"End of failures: {len(result.failures)} failed, "
                f"{len(result.errors)} errors, "
                f"{len(result.unexpectedSuccesses)} unexpected successes"
            )
        return result


def main() -> int:
    if os.environ.get("HELLO_WORLD_IN_CHECKER") != "1":
        print("Repository checks must run through the confined Docker launcher", file=sys.stderr)
        return 2
    if os.geteuid() == 0 or os.getegid() == 0:
        print("Repository checker must not run as root", file=sys.stderr)
        return 2
    root = Path("/workspace")
    if not (root / "Makefile").is_file() or not (root / "tests/repository").is_dir():
        print("Checker does not have the expected read-only source grants", file=sys.stderr)
        return 2
    suite = unittest.defaultTestLoader.discover(
        start_dir=str(root / "tests/repository"),
        top_level_dir=str(root),
        pattern="test_*.py",
    )
    if suite.countTestCases() == 0:
        print("NO TESTS RAN: repository test discovery was empty", file=sys.stderr)
        return 1
    return 0 if FailureLastRunner(verbosity=2, stream=sys.stdout).run(suite).wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
