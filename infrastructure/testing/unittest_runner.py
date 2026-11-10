"""Run unittest discovery and print complete failures after the summary.

This program runs inside the test container, not in the Docker launcher.
It accepts standard discovery options and reports an empty suite as failure.
Tracebacks come last so the end of the log contains actionable errors.
"""

import argparse
import os
import sys
import time
import unittest


def run(suite: unittest.TestSuite) -> int:
    """Execute one suite and report its result without custom unittest classes.
    
    Collect failures, errors and unexpected successes, print totals first,
    then print complete tracebacks. Skips and expected failures are not failures.
    A suite containing no tests is rejected."""
    if suite.countTestCases() == 0:
        print("NO TESTS RAN: unittest discovery was empty", file=sys.stderr)
        return 1

    test_result = unittest.TestResult()
    test_start_time = time.monotonic()
    test_result.startTestRun()
    try:
        suite.run(test_result)
    finally:
        test_result.stopTestRun()

    print(f"Ran {test_result.testsRun} test{'s' if test_result.testsRun != 1 else ''} "
          f"in {time.monotonic() - test_start_time:.3f}s\n")
    if test_result.wasSuccessful():
        print("OK")
        return 0

    failures = len(test_result.failures)
    errors = len(test_result.errors)
    unexpected = len(test_result.unexpectedSuccesses)
    print(f"FAILED (failures={failures}, errors={errors}, unexpected successes={unexpected})")
    print("\nFailure details (end of unittest run):")
    for outcome_kind, outcome_records in (("FAIL", test_result.failures), ("ERROR", test_result.errors)):
        for test_case, failure_traceback in outcome_records:
            print(f"\n{outcome_kind}: {test_case.id()}\n{failure_traceback.rstrip()}")
    for test_case in test_result.unexpectedSuccesses:
        print(f"\nUNEXPECTED SUCCESS: {test_case.id()}")
    print(f"End of failures: failures={failures}, errors={errors}, unexpected_successes={unexpected}")
    return 1


def main(argv: list[str] | None = None) -> int:
    """Parse standard unittest discovery flags and run the selected suite.
    
    The caller supplies the start directory, top-level directory and filename
    pattern. Refuse root execution; container admission is checked elsewhere."""
    if os.geteuid() == 0 or os.getegid() == 0:
        print("Unittest runner must not run as root", file=sys.stderr)
        return 2

    discovery_parser = argparse.ArgumentParser(description="Run repository tests and report failures last")
    discovery_parser.add_argument("-s", "--start-directory", required=True)
    discovery_parser.add_argument("-t", "--top-level-directory", required=True)
    discovery_parser.add_argument("-p", "--pattern", required=True)
    discovery_args = discovery_parser.parse_args(argv)
    discovered_suite = unittest.defaultTestLoader.discover(
        start_dir=discovery_args.start_directory,
        top_level_dir=discovery_args.top_level_directory,
        pattern=discovery_args.pattern,
    )
    return run(discovered_suite)


if __name__ == "__main__":
    raise SystemExit(main())
