"""Run repository tests and report their failures after the summary."""

import os
import sys
import time
import unittest
from pathlib import Path


def run(suite: unittest.TestSuite) -> int:
    if suite.countTestCases() == 0:
        print("NO TESTS RAN: repository test discovery was empty", file=sys.stderr)
        return 1

    result = unittest.TestResult()
    started = time.monotonic()
    result.startTestRun()
    try:
        suite.run(result)
    finally:
        result.stopTestRun()

    print(f"Ran {result.testsRun} test{'s' if result.testsRun != 1 else ''} "
          f"in {time.monotonic() - started:.3f}s\n")
    if result.wasSuccessful():
        print("OK")
        return 0

    failures = len(result.failures)
    errors = len(result.errors)
    unexpected = len(result.unexpectedSuccesses)
    print(f"FAILED (failures={failures}, errors={errors}, unexpected successes={unexpected})")
    print("\nFailure details (end of repository run):")
    for kind, outcomes in (("FAIL", result.failures), ("ERROR", result.errors)):
        for test, traceback in outcomes:
            print(f"\n{kind}: {test.id()}\n{traceback.rstrip()}")
    for test in result.unexpectedSuccesses:
        print(f"\nUNEXPECTED SUCCESS: {test.id()}")
    print(f"End of failures: failures={failures}, errors={errors}, unexpected_successes={unexpected}")
    return 1


def main() -> int:
    if os.geteuid() == 0 or os.getegid() == 0:
        print("Repository checker must not run as root", file=sys.stderr)
        return 2

    root = Path(__file__).resolve().parents[2]
    suite = unittest.defaultTestLoader.discover(
        start_dir=str(root / "tests/repository"),
        top_level_dir=str(root),
        pattern="test_*.py",
    )
    return run(suite)


if __name__ == "__main__":
    raise SystemExit(main())
