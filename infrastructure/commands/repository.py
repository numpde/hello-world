"""Implement Make's repository/check and repository/image/acquire commands.

This module selects the checker image, unittest command and public source
mounts. Docker client admission, confinement and resource retirement are
handled by the shared Docker modules. Repository tests never run on the host.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import signal
import stat
import sys
from pathlib import Path

from infrastructure.docker import client as docker_client
from infrastructure.docker import finite_job


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
PARALLEL_DOCKERFILE = REPOSITORY_ROOT / "containers/parallel-checker/Dockerfile"
PARALLEL_IMAGE_LABEL = "org.numpde.hello-world.parallel-checker-source-sha256"
CHECKER_IMAGE_REFERENCE = (
    "docker.io/library/python:3.12.16-slim-bookworm@"
    "sha256:623674a14ef93d052bb9b0de4dbf5f28eb578a35263838941cc8aa43213c1487"
)
CHECKER_IMAGE_ID = "sha256:37ccb536022bb36c4570856b55ebfa85bf07827e3c2fa3eba37f0dac62eebc05"
PUBLIC_ROOTS = frozenset({
    "Makefile", "notes", "infrastructure", "tests", ".github",
    ".gitignore", "README.md", "pyproject.toml",
    "project.mk", "src", "uv.lock", "app", "containers",
    "package.json", "package-lock.json",
    "examples",
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

APPLICATION_OPERATION_COMMAND = finite_job.Command(
    executable="python3",
    arguments=(
        "-I", "-B", "/workspace/infrastructure/testing/unittest_runner.py",
        "-s", "tests/application_ops", "-t", ".", "-p", "test_*.py",
    ),
)


def parallel_test_command(workers: int) -> finite_job.Command:
    """Use one adapter for both stdlib repository discovery roots."""
    return finite_job.Command(
        executable="python3",
        arguments=(
            "-I", "-B", "/workspace/infrastructure/testing/parallel_runner.py",
            "-j", str(workers), *UNITTEST_DISCOVERY_ARGS,
        ),
    )


def parallel_image_definition() -> tuple[str, str]:
    """Derive the local image name from reviewed Dockerfile and base pins."""
    fingerprint = hashlib.sha256(
        PARALLEL_DOCKERFILE.read_bytes() + CHECKER_IMAGE_REFERENCE.encode("utf-8")
    ).hexdigest()
    return f"localhost/hello-world/parallel-checker:{fingerprint[:20]}", fingerprint


def parallel_image(docker: docker_client.DockerClient, context: str) -> str:
    """Only admit a locally prepared image with matching source identity."""
    tag, expected_fingerprint = parallel_image_definition()
    inspection = docker.call("image", "inspect", "--format", "{{json .}}", tag)
    if inspection.status:
        raise docker_client.OperationError(
            f"Parallel checker image is absent or inaccessible. "
            f"Next: make repository/image/acquire DOCKER_CONTEXT={context} TEST_RUNNER=parallel"
        )
    try:
        metadata = json.loads(docker_client.checked_result(inspection, "Parallel checker inspection"))
        image_id = metadata["Id"]
        source_fingerprint = metadata["Config"]["Labels"][PARALLEL_IMAGE_LABEL]
    except (KeyError, TypeError, ValueError) as error:
        raise docker_client.OperationError("Docker returned malformed parallel checker metadata") from error
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", image_id) or source_fingerprint != expected_fingerprint:
        raise docker_client.OperationError("Parallel checker image does not match its pinned source definition")
    return image_id


def acquire_parallel_image(docker: docker_client.DockerClient, context: str) -> str:
    """Explicitly build the parallel checker from SHA-256-pinned wheel URLs.

    No ordinary repository check may reach this networked Docker build path.
    """
    docker.acquire(CHECKER_IMAGE_REFERENCE, CHECKER_IMAGE_ID)
    tag, fingerprint = parallel_image_definition()
    docker_client.checked_result(
        docker.call(
            "build", "--pull=false", "--network=default",
            "--platform=linux/amd64",
            "--file", str(PARALLEL_DOCKERFILE),
            "--tag", tag,
            "--label", f"{PARALLEL_IMAGE_LABEL}={fingerprint}",
            "--build-arg", f"PYTHON_IMAGE={CHECKER_IMAGE_REFERENCE}",
            str(PARALLEL_DOCKERFILE.parent),
            timeout=600,
        ),
        "Parallel checker acquisition",
    )
    return parallel_image(docker, context)


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
    if action not in {"check", "acquire", "application-check"}:
        raise docker_client.OperationError(f"Unknown repository operation: {action}")
    selected_context_name = docker_client.required_context(environ)
    if action == "application-check":
        if (root / "project.mk").is_symlink() or not (root / "project.mk").is_file():
            raise docker_client.OperationError(
                "An application operation check requires a regular project.mk."
            )
        suite = root / "tests" / "application_ops"
        if suite.is_symlink() or not suite.is_dir():
            raise docker_client.OperationError(
                "Missing tests/application_ops. Add application-only stdlib contracts."
            )

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
            repository_test_job(
                root,
                verified_image_id,
                APPLICATION_OPERATION_COMMAND if action == "application-check" else UNITTEST_COMMAND,
            ),
        )

    if job_outcome.output:
        print(job_outcome.output, end="" if job_outcome.output.endswith("\n") else "\n")
    if job_outcome.failure:
        category = "APPLICATION OPERATION CHECK" if action == "application-check" else "REPOSITORY CHECK"
        print(f"{category} FAILURE: {job_outcome.failure}", file=sys.stderr)

    for operation_diagnostic in job_outcome.diagnostics:
        print(operation_diagnostic, file=sys.stderr)

    if job_outcome.recovery is not None:
        print(f"Next: inspect {job_outcome.recovery / 'recovery.txt'} before retrying.", file=sys.stderr)
    elif job_outcome.status != 0:
        print(
            f"Next: resolve the failure above, then make "
            f"{'repository/application/check' if action == 'application-check' else 'repository/check'} "
            f"DOCKER_CONTEXT={selected_context_name}",
            file=sys.stderr,
        )

    return job_outcome.status


def main(argv: list[str] | None = None) -> int:
    """Parse the Make-facing action and turn operator failures into an exit code.
    
    Signal handlers translate interruption into the same cleanup path used
    for other failed jobs, and are restored before returning."""
    selected_actions = sys.argv[1:] if argv is None else argv
    if len(selected_actions) != 1:
        print("Usage: infrastructure/host-python -m infrastructure.commands.repository "
              "check|acquire|application-check", file=sys.stderr)
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
