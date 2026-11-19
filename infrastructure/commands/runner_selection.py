"""Choose one repository test runner without inspecting Docker or installing packages."""

from __future__ import annotations

import re
from dataclasses import dataclass

from infrastructure.docker.client import OperationError


MAX_PARALLEL_WORKERS = 4
DEFAULT_PARALLEL_WORKERS = 2


@dataclass(frozen=True)
class Runner:
    name: str
    workers: int | None


def select(environment: dict[str, str], action: str) -> Runner:
    """Serial is the default; parallel requires an explicit, valid choice."""
    name = environment.get("HELLO_WORLD_TEST_RUNNER", "serial")
    workers_text = environment.get("HELLO_WORLD_TEST_WORKERS")

    if name == "serial":
        if workers_text is not None:
            raise OperationError("TEST_WORKERS requires TEST_RUNNER=parallel.")
        return Runner(name, None)
    if name != "parallel":
        raise OperationError("TEST_RUNNER must be serial or parallel.")
    if action not in {"check", "acquire"}:
        raise OperationError("Parallel runner is available for repository/check only.")

    if workers_text is None:
        workers = DEFAULT_PARALLEL_WORKERS
    elif re.fullmatch(r"[1-9][0-9]*", workers_text) is None:
        raise OperationError("TEST_WORKERS must be a positive decimal integer.")
    else:
        workers = int(workers_text)

    if not 1 <= workers <= MAX_PARALLEL_WORKERS:
        raise OperationError(f"TEST_WORKERS must be between 1 and {MAX_PARALLEL_WORKERS}.")
    return Runner(name, workers)
