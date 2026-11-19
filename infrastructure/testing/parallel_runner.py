"""Adapt pinned unittest-parallel to multi-root, failure-last repository checks.

The third-party runner executes module suites in separate worker processes.
This adapter only selects discovery roots and reports their observed results;
it does not perform Docker operations or acquire Python dependencies.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys


SUMMARY = re.compile(r"(?m)^-{70}\nRan \d+ tests? in [^\n]+\n\n(?:OK|FAILED)[^\n]*")
DETAILS = re.compile(r"(?m)^={70}$")
BANNER = re.compile(r"(?m)^Running \d+ test suites \((\d+) total tests\)")


def separate_failure_details(report: str) -> tuple[str, str]:
    """Keep test progress and the final summary before complete failure details."""
    summary = SUMMARY.search(report)
    if summary is None:
        return report, ""
    preceding = report[:summary.start()]
    failure_start = DETAILS.search(preceding)
    if failure_start is None:
        return report, ""
    progress = preceding[:failure_start.start()]
    failures = preceding[failure_start.start():]
    return progress + report[summary.start():summary.end()] + report[summary.end():], failures


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("-j", "--jobs", type=int, required=True)
    parser.add_argument("-s", "--start-directory", action="append", required=True)
    parser.add_argument("-t", "--top-level-directory", required=True)
    parser.add_argument("-p", "--pattern", default="test_*.py")
    arguments = parser.parse_args(argv)
    if not 1 <= arguments.jobs <= 4:
        parser.error("Worker count must be 1–4.")

    status = 0
    failed_details = []
    for source in arguments.start_directory:
        command = [
            "unittest-parallel", "-j", str(arguments.jobs), "--level=module",
            "-s", source, "-t", arguments.top_level_directory,
            "-p", arguments.pattern,
        ]
        try:
            result = subprocess.run(
                command, capture_output=True, text=True, timeout=150, check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            print(f"PARALLEL RUNNER FAILURE: {type(error).__name__}: {error}", file=sys.stderr)
            return 2
        if result.stdout:
            print(result.stdout, end="" if result.stdout.endswith("\n") else "\n")
        report, failures = separate_failure_details(result.stderr)
        if report:
            print(report, end="" if report.endswith("\n") else "\n", file=sys.stderr)
        if failures:
            failed_details.append(f"{source}:\n{failures}")
        discovered = BANNER.search(result.stderr)
        if discovered is None:
            status = 1
            failed_details.append(f"{source}: parallel runner did not report test discovery")
        elif int(discovered.group(1)) == 0:
            status = 1
            failed_details.append(f"{source}: NO TESTS RAN")
        if result.returncode:
            status = 1
            if not failures:
                failed_details.append(f"{source}: runner exited with status {result.returncode}")

    if failed_details:
        print("\nFailure details (end of parallel run):", file=sys.stderr)
        for detail in failed_details:
            print(detail, file=sys.stderr)
    return status


if __name__ == "__main__":
    raise SystemExit(main())
