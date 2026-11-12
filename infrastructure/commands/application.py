"""Delegate app/ goals without including application Make rules in common Make."""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
APPLICATION_GOAL = re.compile(r"app/[a-z0-9][a-z0-9_-]*(?:/[a-z0-9][a-z0-9_-]*)*\Z")
INHERITED_MAKE_STATE = (
    "MAKEFLAGS", "GNUMAKEFLAGS", "MFLAGS", "MAKEOVERRIDES", "MAKEFILES",
    "MAKELEVEL", "HELLO_WORLD_APP_TARGET",
)


def run_application(
    goal: str,
    root: Path = REPOSITORY_ROOT,
    environment: dict[str, str] | None = None,
) -> int:
    """Run one literal application target in an independently parsed Makefile."""
    if not APPLICATION_GOAL.fullmatch(goal):
        print(f"Invalid {goal = !r}. Next: make help.", file=sys.stderr)
        return 2

    project_makefile = root / "project.mk"
    if project_makefile.is_symlink() or not project_makefile.is_file():
        print(
            "Missing regular project.mk at the repository root. "
            "Next: select an example branch or add an application Makefile.",
            file=sys.stderr,
        )
        return 2

    child_environment = dict(os.environ if environment is None else environment)
    for variable in INHERITED_MAKE_STATE:
        child_environment.pop(variable, None)

    try:
        operation = subprocess.run(
            ["make", "--no-print-directory", "-f", "project.mk", goal],
            cwd=root,
            env=child_environment,
            check=False,
        )
    except OSError as error:
        print(f"Cannot execute application Makefile: {error}", file=sys.stderr)
        return 2

    exit_status = operation.returncode if operation.returncode >= 0 else 128 - operation.returncode
    if exit_status:
        print(f"Application target failed ({goal = !r}, {exit_status = }). Next: make help.", file=sys.stderr)
    return exit_status


def main() -> int:
    """Use the automatic target variable exported by the root Make pattern."""
    return run_application(os.environ.get("HELLO_WORLD_APP_TARGET", ""))


if __name__ == "__main__":
    raise SystemExit(main())
