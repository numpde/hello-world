"""Run repository checks through the shared finite-job container finite_job."""

from __future__ import annotations

import os
import signal
import stat
import sys
from pathlib import Path

from tools.container import finite_job


ROOT = Path(__file__).resolve().parents[2]
IMAGE = (
    "docker.io/library/python:3.12.16-slim-bookworm@"
    "sha256:623674a14ef93d052bb9b0de4dbf5f28eb578a35263838941cc8aa43213c1487"
)
IMAGE_ID = "sha256:37ccb536022bb36c4570856b55ebfa85bf07827e3c2fa3eba37f0dac62eebc05"
PUBLIC_ROOTS = frozenset({"Makefile", "notes", "tools", "tests", ".github", ".gitignore", "README.md", "pyproject.toml"})
PRIVATE_ROOTS = frozenset({".git", ".agents", ".codex", ".aws", ".venv", ".pytest_cache", "__pycache__", ".env", "secrets", "build", "dist"})

# Discovery args are shared with unittest-parallel's CLI. Changing runners
# changes Command and the pinned checker image, not the Docker lifecycle.
UNITTEST_DISCOVERY_ARGS = ("-s", "tests/repository", "-t", ".", "-p", "test_*.py")
UNITTEST_COMMAND = finite_job.Command(
    executable="python3",
    arguments=("-I", "-B", "/workspace/tools/repository/unittest_runner.py", *UNITTEST_DISCOVERY_ARGS),
)


def public_mounts(root: Path) -> tuple[finite_job.ReadOnlyMount, ...]:
    """Grant only classified public project inputs to repository tests."""
    if any(ch in str(root) for ch in (",", "\n", "\r", "\x00")):
        raise finite_job.OperationError("Repository path cannot be represented safely in Docker mounts")
    discovered = {entry.name: entry for entry in root.iterdir()}
    unknown = sorted(discovered.keys() - PUBLIC_ROOTS - PRIVATE_ROOTS)
    if unknown:
        raise finite_job.OperationError(f"Unclassified repository roots: {', '.join(unknown)}")
    for required in ("Makefile", "tools", "tests"):
        if required not in discovered:
            raise finite_job.OperationError(f"Required repository input missing: {required}")

    mounts = []
    for name in sorted(PUBLIC_ROOTS & discovered.keys()):
        entry = discovered[name]
        for directory, directories, files in os.walk(entry, followlinks=False):
            for child in [Path(directory), *(Path(directory) / part for part in directories + files)]:
                mode = child.lstat().st_mode
                if not (stat.S_ISREG(mode) or stat.S_ISDIR(mode)):
                    raise finite_job.OperationError(f"Refusing link or non-file source: {child}")
                if (stat.S_ISDIR(mode) and mode & 0o005 != 0o005) or (
                    stat.S_ISREG(mode) and mode & 0o004 != 0o004
                ):
                    raise finite_job.OperationError(f"Checker cannot read non-public source: {child}")
        mode = entry.lstat().st_mode
        if not (stat.S_ISREG(mode) or stat.S_ISDIR(mode)):
            raise finite_job.OperationError(f"Refusing link or non-file source: {entry}")
        if (stat.S_ISDIR(mode) and mode & 0o005 != 0o005) or (
            stat.S_ISREG(mode) and mode & 0o004 != 0o004
        ):
            raise finite_job.OperationError(f"Checker cannot read non-public source: {entry}")
        mounts.append(finite_job.ReadOnlyMount(entry, f"/workspace/{name}"))
    return tuple(mounts)


def repository_test_job(root: Path, image_id: str, command: finite_job.Command = UNITTEST_COMMAND) -> finite_job.Job:
    return finite_job.Job(
        image_id=image_id,
        command=command,
        mounts=public_mounts(root),
        environment=("HOME=/tmp", "PYTHONDONTWRITEBYTECODE=1"),
    )


def run_repository_command(
    action: str,
    environ: dict[str, str],
    executor: finite_job.Executor | None = None,
    root: Path = ROOT,
) -> int:
    if action not in {"check", "acquire"}:
        raise finite_job.OperationError(f"Unknown repository operation: {action}")
    context_name = finite_job.required_context(environ)
    with finite_job.docker_client(environ, executor) as docker:
        if action == "acquire":
            docker.acquire(IMAGE, IMAGE_ID)
            print(f"Acquired checker image {IMAGE_ID} on Docker context {context_name}.")
            return 0
        image_id = docker.image(
            reference=IMAGE,
            expected_id=IMAGE_ID,
            acquire_hint="Run: make repository/image/acquire DOCKER_CONTEXT=<context>",
        )
        outcome = finite_job.run_container(docker, context_name, repository_test_job(root, image_id))

    if outcome.output:
        print(outcome.output, end="" if outcome.output.endswith("\n") else "\n")
    if outcome.failure:
        failure = outcome.failure
        if failure.startswith("Container exited with status "):
            failure = f"Repository checks failed (container exit {failure.rsplit(' ', 1)[-1]})"
        print(f"REPOSITORY CHECK FAILURE: {failure}", file=sys.stderr)
    for diagnostic in outcome.diagnostics:
        print(diagnostic, file=sys.stderr)
    return outcome.status


def main(argv: list[str] | None = None) -> int:
    actions = sys.argv[1:] if argv is None else argv
    if len(actions) != 1:
        print("Usage: python3 -m tools.repository.checker check|acquire", file=sys.stderr)
        return 2

    def cancel(signum: int, _frame: object) -> None:
        raise finite_job.Cancelled(f"Caller requested cancellation ({signal.Signals(signum).name})")

    signals = (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)
    previous = {number: signal.signal(number, cancel) for number in signals}
    try:
        return run_repository_command(actions[0], dict(os.environ))
    except (finite_job.OperationError, OSError, TimeoutError, KeyboardInterrupt) as error:
        print(f"REPOSITORY OPERATION FAILURE: {error}", file=sys.stderr)
        return 2
    finally:
        for number, handler in previous.items():
            signal.signal(number, handler)


if __name__ == "__main__":
    raise SystemExit(main())
