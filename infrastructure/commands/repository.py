"""Implement Make's repository/check and repository/image/acquire commands.

This module selects the checker image, unittest command and public source
mounts. Docker client admission, confinement and resource retirement are
handled by the shared Docker modules. Repository tests never run on the host.
"""

from __future__ import annotations

import os
import signal
import stat
import sys
from pathlib import Path

from infrastructure.docker import client as docker_client
from infrastructure.docker import finite_job


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
CHECKER_IMAGE_REFERENCE = (
    "docker.io/library/python:3.12.16-slim-bookworm@"
    "sha256:623674a14ef93d052bb9b0de4dbf5f28eb578a35263838941cc8aa43213c1487"
)
CHECKER_IMAGE_ID = "sha256:37ccb536022bb36c4570856b55ebfa85bf07827e3c2fa3eba37f0dac62eebc05"
PUBLIC_ROOTS = frozenset({
    "Makefile", "notes", "infrastructure", "tests", ".github",
    ".gitignore", "README.md", "pyproject.toml",
    "project.mk", "src", "uv.lock",
})
# Local editor metadata is not source material and must not enter the checker.
PRIVATE_ROOTS = frozenset({".git", ".agents", ".codex", ".idea", ".vscode", ".aws", ".venv", ".pytest_cache", "__pycache__", ".env", "secrets", "build", "dist"})

# The stdlib-only checker runs common contracts. Application-specific tests run
# with their own locked dependencies through app/tests/run.
UNITTEST_DISCOVERY_ARGS = (
    "-s", "tests/infrastructure",
    "-s", "tests/repo-checks",
    "-t", ".",
    "-p", "test_*.py",
)
UNITTEST_COMMAND = finite_job.Command(
    executable="python3",
    arguments=("-I", "-B", "/workspace/infrastructure/testing/unittest_runner.py", *UNITTEST_DISCOVERY_ARGS),
)


def public_mounts(root: Path) -> tuple[finite_job.ReadOnlyMount, ...]:
    """Select the checkout entries that tests may read inside Docker.

Require the core source roots and reject unclassified top-level entries, links
and filenames unreadable by the container user. Private roots such as .git and
secrets are never mounted."""
    if any(ch in str(root) for ch in (",", "\n", "\r", "\x00")):
        raise docker_client.OperationError("Repository path cannot be represented safely in Docker mounts")
    root_entries = {source_entry.name: source_entry for source_entry in root.iterdir()}
    unclassified_roots = sorted(root_entries.keys() - PUBLIC_ROOTS - PRIVATE_ROOTS)
    if unclassified_roots:
        raise docker_client.OperationError(
            f"Unclassified {unclassified_roots = }. "
            "Next: move unrelated entries outside the checkout, or explicitly classify them "
            "in PUBLIC_ROOTS/PRIVATE_ROOTS. Do not mount unreviewed files."
        )

    for required_root in ("Makefile", "infrastructure", "tests"):
        if required_root not in root_entries:
            raise docker_client.OperationError(f"Missing {required_root = }")

    def require_checker_readable(source: Path) -> None:
        """The unprivileged checker cannot rely on the host owner's permissions."""
        source_mode = source.lstat().st_mode

        if stat.S_ISDIR(source_mode):
            required_other_permissions = 0o005
        elif stat.S_ISREG(source_mode):
            required_other_permissions = 0o004
        else:
            raise docker_client.OperationError(f"Refusing link or non-file {source = }")

        if source_mode & required_other_permissions != required_other_permissions:
            raise docker_client.OperationError(f"Checker cannot read non-public {source = }")

    read_only_mounts = []

    for root_name in sorted(PUBLIC_ROOTS & root_entries.keys()):
        source_entry = root_entries[root_name]
        require_checker_readable(source_entry)

        for (walk_directory, subdirectories, filenames) in os.walk(source_entry, followlinks=False):
            for entry_name in (*subdirectories, *filenames):
                require_checker_readable(Path(walk_directory) / entry_name)

        read_only_mounts.append(finite_job.ReadOnlyMount(source_entry, f"/workspace/{root_name}"))

    return tuple(read_only_mounts)


def repository_test_job(root: Path, image_id: str, command: finite_job.Command = UNITTEST_COMMAND) -> finite_job.Job:
    """Describe the repository test workload without running it.
    
    The command can be replaced by unittest-parallel when its pinned checker
    image is available; source grants and Docker policy stay unchanged."""
    return finite_job.Job(
        image_id=image_id,
        command=command,
        mounts=public_mounts(root),
        environment=("HOME=/tmp", "PYTHONDONTWRITEBYTECODE=1"),
    )


def run_repository_command(
    action: str,
    environ: dict[str, str],
    executor: docker_client.Executor | None = None,
    root: Path = REPOSITORY_ROOT,
) -> int:
    """Run the selected Make action against one explicit Docker context.
    
    Acquisition may download the pinned image. Checking uses the local image,
    runs tests in a confined job, and reports execution and cleanup failures
    separately. No host-side test fallback is permitted."""
    if action not in {"check", "acquire"}:
        raise docker_client.OperationError(f"Unknown repository operation: {action}")
    selected_context_name = docker_client.required_context(environ)

    with docker_client.docker_client(environ, executor) as selected_engine_client:
        if action == "acquire":
            observed_image_id = selected_engine_client.acquire(CHECKER_IMAGE_REFERENCE, CHECKER_IMAGE_ID)
            print(f"Acquired checker image on Docker context {selected_context_name} ({observed_image_id = }).")
            print(f"Next: make repository/check DOCKER_CONTEXT={selected_context_name}")
            return 0
        verified_image_id = selected_engine_client.image(
            reference=CHECKER_IMAGE_REFERENCE,
            expected_id=CHECKER_IMAGE_ID,
            acquire_hint=(
                f"make repository/image/acquire DOCKER_CONTEXT={selected_context_name} && "
                f"make repository/check DOCKER_CONTEXT={selected_context_name}"
            ),
        )
        job_outcome = finite_job.run_container(
            selected_engine_client,
            selected_context_name,
            repository_test_job(root, verified_image_id),
        )

    if job_outcome.output:
        print(job_outcome.output, end="" if job_outcome.output.endswith("\n") else "\n")
    if job_outcome.failure:
        print(f"REPOSITORY CHECK FAILURE: {job_outcome.failure}", file=sys.stderr)

    for operation_diagnostic in job_outcome.diagnostics:
        print(operation_diagnostic, file=sys.stderr)

    if job_outcome.recovery is not None:
        print(f"Next: inspect {job_outcome.recovery / 'recovery.txt'} before retrying.", file=sys.stderr)
    elif job_outcome.status != 0:
        print(
            f"Next: resolve the failure above, then make repository/check DOCKER_CONTEXT={selected_context_name}",
            file=sys.stderr,
        )

    return job_outcome.status


def main(argv: list[str] | None = None) -> int:
    """Parse the Make-facing action and turn operator failures into an exit code.
    
    Signal handlers translate interruption into the same cleanup path used
    for other failed jobs, and are restored before returning."""
    selected_actions = sys.argv[1:] if argv is None else argv
    if len(selected_actions) != 1:
        print("Usage: python3 -m infrastructure.commands.repository check|acquire", file=sys.stderr)
        return 2

    def cancel(signal_number: int, _frame: object) -> None:
        """Raise cancellation so an in-flight job can enter its cleanup path."""
        raise docker_client.Cancelled(f"Caller requested cancellation ({signal.Signals(signal_number).name})")

    terminal_signals = (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)
    previous_handlers = {}

    try:
        # A later signal installation may fail; restore those already changed.
        for signal_number in terminal_signals:
            previous_handlers[signal_number] = signal.signal(signal_number, cancel)

        return run_repository_command(selected_actions[0], dict(os.environ))
    except (docker_client.OperationError, OSError, TimeoutError, KeyboardInterrupt) as error:
        print(f"REPOSITORY OPERATION FAILURE: {error}", file=sys.stderr)
        return 2
    finally:
        for (signal_number, previous_handler) in previous_handlers.items():
            signal.signal(signal_number, previous_handler)


if __name__ == "__main__":
    raise SystemExit(main())
