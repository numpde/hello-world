"""Execute one finite command in a confined Docker container.

The caller supplies an immutable image ID, the executable and literal
arguments, and read-only inputs. This module fixes the security options,
checks the created container before starting it, and removes only resources
whose ownership it can verify. An uncertain create or cleanup leaves a
private recovery receipt. This is not a service or image-building runner.
"""

from __future__ import annotations

import json
import re
import secrets
import signal
import tempfile
import time
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from infrastructure.docker.client import (
    CLIENT_TIMEOUT,
    MAX_OUTPUT,
    DockerClient,
    OperationError,
    checked_result,
)


OPERATION_LABEL = "org.numpde.hello-world.operation"
IDENTITY_LABEL = "org.numpde.hello-world.owner"
WORK_TIMEOUT = 180
CLEANUP_TIMEOUT = 25
CONTAINER_ID = re.compile(r"[0-9a-f]{64}\Z")


@dataclass(frozen=True)
class ReadOnlyMount:
    """A host source made available inside a job without write permission.

    This finite-job policy does not admit writable host mounts.
    """

    source: Path
    target: str


@dataclass(frozen=True)
class Command:
    """An executable and its literal arguments inside the selected image.

    The container lifecycle does not depend on which program is run.
    """

    executable: str
    arguments: tuple[str, ...] = ()


@dataclass(frozen=True)
class Job:
    """Inputs for one bounded container run.

    Callers select the image, command, read-only mounts and ordinary environment.
    They cannot add Docker flags that weaken the runner's confinement policy.
    """

    image_id: str
    command: Command
    mounts: tuple[ReadOnlyMount, ...]
    environment: tuple[str, ...] = ()
    workdir: str = "/workspace"


@dataclass(frozen=True)
class Outcome:
    """Execution evidence and cleanup evidence for a finished attempt.

    status is nonzero for a failed command or unconfirmed cleanup. The recovery
    path remains when ownership or retirement cannot safely be established.
    """

    status: int
    output: str
    failure: str | None
    diagnostics: tuple[str, ...]
    recovery: Path | None


def container_arguments(job: Job, token: str, owner: str) -> list[str]:
    """Build the create command from the job's declared inputs.

    Reject ambiguous mount destinations and malformed arguments before touching
    Docker. Network, privileges, resources and writable storage are fixed here,
    not selectable through an arbitrary list of Docker flags.
    """
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", job.image_id):
        raise OperationError(f"A verified immutable image ID is required: {job.image_id = }")

    executable = job.command.executable
    if not executable or (not executable.startswith("/") and "/" in executable):
        raise OperationError(f"Container entrypoint must be a name or absolute path: {executable = }")

    if not job.workdir.startswith("/"):
        raise OperationError(f"Container workdir must be absolute: {job.workdir = }")
    if any(character in job.workdir for character in ("\n", "\r", "\x00")):
        raise OperationError(f"Control character in container workdir: {job.workdir = }")

    mount_arguments = []
    mounted_targets = set()

    for mount in job.mounts:
        source_path = str(mount.source)

        if not mount.source.is_absolute() or not mount.target.startswith("/"):
            raise OperationError(f"Mount paths must be absolute: {mount.source = }, {mount.target = }")

        # Docker splits --mount options on commas. A comma or control character
        # inside a path could change which mount options Docker interprets.
        if any(character in source_path + mount.target for character in (",", "\n", "\r", "\x00")):
            raise OperationError(f"Invalid mount path: {mount.source = }, {mount.target = }")

        if mount.target in mounted_targets:
            raise OperationError(f"Duplicate container destination: {mount.target = }")

        mounted_targets.add(mount.target)
        mount_arguments.extend(["--mount", f"type=bind,source={source_path},target={mount.target},readonly"])

    # The value may contain credentials; reject malformed declarations without
    # echoing the supplied setting into diagnostic output.
    environment_arguments = []
    for setting in job.environment:
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", setting, flags=re.DOTALL) or "\x00" in setting:
            raise OperationError("Malformed container environment declaration (expected NAME=value without NUL)")
        environment_arguments.append(f"--env={setting}")

    return [
        "create", "--pull=never",
        "--label", f"{OPERATION_LABEL}={token}",
        "--label", f"{IDENTITY_LABEL}={owner}",

        # Workloads never receive host networking or elevated privileges.
        "--network=none", "--read-only", "--cap-drop=ALL",
        "--security-opt=no-new-privileges:true", "--user=65532:65532",

        # Limit each job's resource use and give it bounded temporary storage.
        "--cpus=2", "--memory=512m", "--memory-swap=512m", "--pids-limit=64",
        "--ulimit=nofile=256:256", "--log-driver=none",
        "--tmpfs=/tmp:rw,nosuid,nodev,noexec,size=67108864",

        # Only validated environment and mount arguments precede the image;
        # the command arguments follow it and cannot change Docker grants.
        f"--workdir={job.workdir}", "--platform=linux/amd64",
        *environment_arguments, *mount_arguments,
        f"--entrypoint={executable}", job.image_id, *job.command.arguments,
    ]


def verify_container_posture(docker: DockerClient, identifier: str, job: Job) -> None:
    """Inspect the engine's container settings before starting the workload.

    Check network, root filesystem, privileges, resource limits, temporary
    storage and every source mount. A requested flag alone is not proof that
    the engine applied the restriction.
    """
    inspection_json = checked_result(
        result=docker.call("container", "inspect", "--format", "{{json .}}", identifier),
        purpose="Container confinement inspection",
    )

    try:
        inspection = json.loads(inspection_json)
        host_configuration = inspection["HostConfig"]
        container_configuration = inspection["Config"]
        reported_mounts = inspection["Mounts"]
    except (KeyError, TypeError, ValueError) as error:
        raise OperationError("Docker returned malformed confinement data") from error

    if (
        not isinstance(host_configuration, dict)
        or not isinstance(container_configuration, dict)
        or not isinstance(reported_mounts, list)
    ):
        raise OperationError("Docker returned malformed confinement types")

    expected_configuration = {
        "NetworkMode": "none",
        "ReadonlyRootfs": True,
        "Privileged": False,
        "NanoCpus": 2_000_000_000,
        "Memory": 536_870_912,
        "MemorySwap": 536_870_912,
        "PidsLimit": 64,
    }
    violations = []
    for (field_name, expected_value) in expected_configuration.items():
        observed_value = host_configuration.get(field_name)
        if observed_value != expected_value:
            violations.append(f"{field_name}: {observed_value = }, {expected_value = }")

    expected_user = "65532:65532"
    observed_user = container_configuration.get("User")
    if observed_user != expected_user:
        violations.append(f"user: {observed_user = }, {expected_user = }")

    dropped_capabilities = host_configuration.get("CapDrop")
    if not isinstance(dropped_capabilities, list) or not any(
        isinstance(capability, str) and capability.lower() == "all"
        for capability in dropped_capabilities
    ):
        violations.append(f"capabilities: {dropped_capabilities = }")

    security_options = host_configuration.get("SecurityOpt")
    if not isinstance(security_options, list) or "no-new-privileges:true" not in security_options:
        violations.append(f"no-new-privileges: {security_options = }")

    logging_configuration = host_configuration.get("LogConfig")
    if not isinstance(logging_configuration, dict) or logging_configuration.get("Type") != "none":
        violations.append(f"logging: {logging_configuration = }")

    temporary_filesystems = host_configuration.get("Tmpfs")
    observed_tmp_options = temporary_filesystems.get("/tmp") if isinstance(temporary_filesystems, dict) else None
    expected_tmp_options = {"rw", "nosuid", "nodev", "noexec", "size=67108864"}

    if (
        not isinstance(observed_tmp_options, str)
        or not expected_tmp_options.issubset(observed_tmp_options.split(","))
    ):
        violations.append(f"temporary storage: {observed_tmp_options = }, {expected_tmp_options = }")

    # Compare the daemon's complete mount list, not just the requested binds.
    # An additional read-only mount is still a grant this job did not request.
    expected_bind_mounts = {(str(mount.source), mount.target) for mount in job.mounts}
    observed_bind_mounts = set()

    for observed_mount in reported_mounts:
        if (
            not isinstance(observed_mount, dict)
            or observed_mount.get("Type") != "bind"
            or observed_mount.get("RW") is not False
        ):
            violations.append(f"source mount type or writable permission: {observed_mount = }")
            continue
        observed_bind_mounts.add((observed_mount.get("Source"), observed_mount.get("Destination")))

    if observed_bind_mounts != expected_bind_mounts:
        violations.append(f"source mount inventory: {observed_bind_mounts = }, {expected_bind_mounts = }")

    if violations:
        raise OperationError(f"Docker did not apply required confinement: {', '.join(violations)}")


def container_ids(raw: str) -> list[str]:
    """Parse the full, unique IDs returned by Docker container listing.

    Reject truncated or duplicate identities rather than risk acting on an
    ambiguous container during cleanup.
    """
    container_identifiers = [value for value in raw.splitlines() if value]

    if len(container_identifiers) != len(set(container_identifiers)):
        raise OperationError("Docker reported duplicate container identities")
    if any(not CONTAINER_ID.fullmatch(identifier) for identifier in container_identifiers):
        raise OperationError("Docker reported malformed container identities")

    return container_identifiers


def owned_containers(
    docker: DockerClient,
    token: str,
    owner: str,
    image_id: str,
    timeout: Callable[[], int] = lambda: CLIENT_TIMEOUT,
) -> list[str]:
    """Discover containers with this run's label and verify each candidate.

    A matching search label does not grant deletion authority: the full ID,
    image ID and both ownership labels must also agree. The timeout callback
    supplies the time remaining within the caller's cleanup budget.
    """
    container_listing = checked_result(
        result=docker.call(
            "ps", "--all", "--quiet", "--no-trunc",
            "--filter", f"label={OPERATION_LABEL}={token}",
            timeout=timeout(),
        ),
        purpose="Owned container discovery",
    )

    candidate_ids = container_ids(container_listing)

    for candidate_id in candidate_ids:
        inspection_json = checked_result(
            result=docker.call(
                "container", "inspect", "--format", "{{json .}}", candidate_id,
                timeout=timeout(),
            ),
            purpose="Owned container verification",
        )

        try:
            inspection = json.loads(inspection_json)
            ownership_labels = inspection["Config"]["Labels"]
            ownership_verified = (
                inspection["Id"] == candidate_id
                and inspection["Image"] == image_id
                and ownership_labels[OPERATION_LABEL] == token
                and ownership_labels[IDENTITY_LABEL] == owner
            )
        except (KeyError, TypeError, ValueError):
            ownership_verified = False

        if not ownership_verified:
            raise OperationError(f"Refusing to remove container with inconsistent ownership: {candidate_id = }")

    return candidate_ids


def cleanup(docker: DockerClient, token: str, owner: str, image_id: str) -> None:
    """Retire verified containers within a separate cleanup deadline.

    Attempt removal, then list them again to prove they are gone. Preserve
    failures and raise if removal or the final inspection is inconclusive.
    """
    cleanup_deadline = time.monotonic() + CLEANUP_TIMEOUT

    def remaining_time() -> int:
        """Bound each cleanup request by the time left for the whole cleanup."""
        seconds = cleanup_deadline - time.monotonic()
        if seconds <= 0:
            raise TimeoutError("Docker cleanup budget exhausted")
        return min(CLIENT_TIMEOUT, max(1, int(seconds)))

    owned_container_ids = owned_containers(docker, token, owner, image_id, timeout=remaining_time)

    removal_failures = []
    for container_id in owned_container_ids:
        removal_result = docker.call("rm", "--force", "--volumes", container_id, timeout=remaining_time())
        if removal_result.status != 0:
            removal_status = removal_result.status
            removal_failures.append(f"{container_id = }, {removal_status = }")

    remaining_container_ids = owned_containers(docker, token, owner, image_id, timeout=remaining_time)

    if remaining_container_ids or removal_failures:
        raise OperationError(
            f"Container cleanup unconfirmed: {remaining_container_ids = }, {removal_failures = }"
        )


def write_receipt(
    directory: Path,
    context: str,
    endpoint: str,
    token: str,
    owner: str,
) -> None:
    """Save the selected engine and ownership labels before Docker create.

    If a client times out after sending a request, operators can identify
    possible daemon-side resources without guessing or globally pruning.
    """
    recovery_receipt = directory / "recovery.txt"
    recovery_receipt.write_text(
        f"Context: {context}\n"
        f"Endpoint: {endpoint}\n"
        f"Operation label: {OPERATION_LABEL}={token}\n"
        f"Owner label: {IDENTITY_LABEL}={owner}\n"
        "Inspect: docker --context <context> ps -a --no-trunc --filter label=<operation-label>\n"
        "Verify ownership before removing any container. Do not use global prune.\n",
        encoding="utf-8",
    )
    recovery_receipt.chmod(0o600)


@contextmanager
def protected_cleanup():
    """Temporarily ignore terminal signals while retiring owned resources.

    The caller records the primary interruption; this guard prevents another
    signal from skipping cleanup and restores the previous handlers afterward.
    """
    terminal_signals = (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)
    previous_handlers = {}
    try:
        # Installing handlers changes process state. Restore those already
        # changed even if installing a later handler raises an exception.
        for signal_number in terminal_signals:
            previous_handlers[signal_number] = signal.signal(signal_number, signal.SIG_IGN)
        yield
    finally:
        for (signal_number, previous_handler) in previous_handlers.items():
            signal.signal(signal_number, previous_handler)


def run_container(docker: DockerClient, context: str, job: Job) -> Outcome:
    """Run a job and report both its work outcome and cleanup outcome.

    Record ownership before create, require a matching container-ID receipt,
    verify confinement, then start and inspect the final exit state. Always
    attempt retirement. Keep the receipt when create or cleanup is uncertain;
    never report a clean success merely because the Docker client exited.
    """
    operation_token = secrets.token_hex(16)
    owner_token = secrets.token_hex(16)
    create_arguments = container_arguments(job=job, token=operation_token, owner=owner_token)
    operation_directory = Path(tempfile.mkdtemp(prefix="hello-world-operation-"))
    recovery_directory: Path | None = operation_directory
    create_confirmed = False

    job_failure: str | None = None
    job_output = ""
    diagnostics = []
    operation_status = 0

    try:
        # Keep recovery evidence before the first command that can create a
        # daemon resource. The CLI reply alone is not proof of its identity.
        write_receipt(
            directory=operation_directory,
            context=context,
            endpoint=docker.endpoint or "",
            token=operation_token,
            owner=owner_token,
        )
        cid_receipt = operation_directory / "container.cid"
        create_command = [
            "create",
            "--cidfile", str(cid_receipt),
            *create_arguments[1:],
        ]
        reported_container_id = checked_result(
            result=docker.call(*create_command),
            purpose="Container creation",
        )

        if not cid_receipt.is_file() or cid_receipt.is_symlink():
            raise OperationError("Docker did not write the private container identity receipt")
        container_id = cid_receipt.read_text(encoding="ascii").strip()

        if not CONTAINER_ID.fullmatch(container_id):
            raise OperationError(f"Invalid Docker CID receipt: {container_id = }")
        if reported_container_id != container_id:
            raise OperationError(
                f"Docker container creation and CID receipt disagree: {reported_container_id = }, {container_id = }"
            )

        observed_container_ids = owned_containers(
            docker=docker,
            token=operation_token,
            owner=owner_token,
            image_id=job.image_id,
        )
        if observed_container_ids != [container_id]:
            raise OperationError(
                f"Docker created an unexpected container identity: {observed_container_ids = }, {container_id = }"
            )

        create_confirmed = True
        verify_container_posture(docker, container_id, job)

        attachment_result = docker.call("start", "--attach", container_id, timeout=WORK_TIMEOUT)
        job_output = attachment_result.output
        if attachment_result.truncated:
            job_failure = f"Container output exceeded {MAX_OUTPUT} bytes; tail retained"

        # The attachment client may finish while the workload is still running.
        # Only the daemon's final state establishes the workload exit status.
        completion_inspection = checked_result(
            result=docker.call("container", "inspect", "--format", "{{json .State}}", container_id),
            purpose="Container completion inspection",
        )

        try:
            container_state = json.loads(completion_inspection)
            container_exited = isinstance(container_state, dict) and container_state.get("Running") is False
            container_exit_code = container_state["ExitCode"] if container_exited else None
        except (ValueError, KeyError, TypeError):
            container_exit_code = None

        if type(container_exit_code) is not int or not 0 <= container_exit_code <= 255:
            raise OperationError("Container stopped without a verified final exit status")

        if attachment_result.status != 0:
            job_failure = f"Docker attachment failed ({attachment_result.status = })"
        if container_exit_code != 0:
            # The command adapter currently recognizes this message prefix.
            job_failure = f"Container exited with status {container_exit_code}"

    except (OperationError, TimeoutError, OSError, KeyboardInterrupt) as error:
        job_failure = str(error) or error.__class__.__name__
    finally:
        # Even a timed-out create can finish on the daemon. Inspect before
        # declaring retirement; keep recovery information if uncertain.
        with protected_cleanup():
            try:
                cleanup(
                    docker=docker,
                    token=operation_token,
                    owner=owner_token,
                    image_id=job.image_id,
                )
            except (OperationError, TimeoutError, OSError) as error:
                operation_status = 1
                diagnostics.append(
                    f"CLEANUP UNCONFIRMED: {error}. "
                    f"Retained recovery record: {operation_directory}/recovery.txt"
                )
            else:
                if not create_confirmed:
                    operation_status = 1
                    diagnostics.append(
                        "Create was not confirmed; retain the receipt even if discovery currently finds nothing. "
                        f"Recovery: {operation_directory}/recovery.txt"
                    )
                else:
                    try:
                        for child in operation_directory.iterdir():
                            child.unlink()
                        operation_directory.rmdir()
                        recovery_directory = None
                    except OSError as error:
                        operation_status = 1
                        diagnostics.append(f"Cannot remove operation record: {operation_directory = }, {error = }")

    if job_failure is not None:
        operation_status = 1
    if recovery_directory is not None:
        diagnostics.append(f"Retained operation: {recovery_directory}")

    return Outcome(
        status=operation_status,
        output=job_output,
        failure=job_failure,
        diagnostics=tuple(diagnostics),
        recovery=recovery_directory,
    )
