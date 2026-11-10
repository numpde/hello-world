"""Run unittest discovery and report full failures after the summary."""

import argparse
import os
import sys
import time
import unittest


def run(suite: unittest.TestSuite) -> int:
    if suite.countTestCases() == 0:
        print("NO TESTS RAN: unittest discovery was empty", file=sys.stderr)
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
    print("\nFailure details (end of unittest run):")
    for kind, outcomes in (("FAIL", result.failures), ("ERROR", result.errors)):
        for test, traceback in outcomes:
            print(f"\n{kind}: {test.id()}\n{traceback.rstrip()}")
    for test in result.unexpectedSuccesses:
        print(f"\nUNEXPECTED SUCCESS: {test.id()}")
    print(f"End of failures: failures={failures}, errors={errors}, unexpected_successes={unexpected}")
    return 1


def main(argv: list[str] | None = None) -> int:
    if os.geteuid() == 0 or os.getegid() == 0:
        print("Unittest runner must not run as root", file=sys.stderr)
        return 2

    parser = argparse.ArgumentParser(description="Run repository tests and report failures last")
    parser.add_argument("-s", "--start-directory", required=True)
    parser.add_argument("-t", "--top-level-directory", required=True)
    parser.add_argument("-p", "--pattern", required=True)
    args = parser.parse_args(argv)
    suite = unittest.defaultTestLoader.discover(
        start_dir=args.start_directory,
        top_level_dir=args.top_level_directory,
        pattern=args.pattern,
    )
    return run(suite)


if __name__ == "__main__":
    raise SystemExit(main())
