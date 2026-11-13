"""Execute one finite command in a confined Docker container.

The caller supplies an immutable image ID, literal command and mounts.
Networkless, read-only jobs are the default. Explicit acquisition jobs can
select bridge networking and writable output directories without changing
the repository checker's grants. Confinement is verified before starting;
uncertain effects retain recovery evidence.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import signal
import stat
import tempfile
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace
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
UNSAFE_MOUNT_PATH = re.compile(r"[,\n\r\x00]")

# Use one fixed policy for Docker arguments and the expected daemon state.
# Inspection still checks what the daemon applied, not what was requested.
CONTAINER_USER = "65532:65532"
CPU_LIMIT = 2
MEMORY_LIMIT_MIB = 512
PID_LIMIT = 64
NOFILE_LIMIT = 256
TMPFS_OPTIONS = ("rw", "nosuid", "nodev", "noexec", "size=67108864")


@dataclass(frozen=True)
class ReadOnlyMount:
    """A host source made available inside a job without write permission."""

    source: Path
    target: str


@dataclass(frozen=True)
class WritableMount:
    """An existing user-owned directory for explicit output or dependency caches."""

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

    Default jobs remain networkless and read-only. Acquisition callers may
    explicitly select bridge networking and writable output/cache directories;
    no other Docker flags are configurable.
    """

    image_id: str
    command: Command
    mounts: tuple[ReadOnlyMount, ...]
    environment: tuple[str, ...] = ()
    workdir: str = "/workspace"
    writable_mounts: tuple[WritableMount, ...] = ()
    network: str = "none"
    timeout: int = WORK_TIMEOUT


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


@dataclass
class TemporaryJobDirectory:
    """A private mount source held until its one finite job is retired.

    Call run() inside temporary_job_directory(). A failed workload with
    confirmed retirement releases the directory; uncertain retirement
    retains its contents alongside the container recovery evidence.
    """

    path: Path
    _used: bool = False
    _may_release: bool = True

    def run(self, docker: DockerClient, context: str, job: Job) -> Outcome:
        """Execute one job mounting this directory and observe its retirement."""
        if self._used:
            raise OperationError("Temporary directory already ran a finite job")
        if not any(
            mount.source == self.path
            for mount in (*job.mounts, *job.writable_mounts)
        ):
            raise OperationError("Finite job does not mount its owned temporary directory")

        self._used = True
        self._may_release = False
        try:
            outcome = run_container(docker, context, job)
        except BaseException as error:
            # An unexpected runner exception cannot establish daemon retirement.
            error.add_note(f"Retained temporary mount: {self.path}")
            raise

        self._may_release = outcome.recovery is None
        if self._may_release:
            return outcome

        retained_mount = f"Retained temporary mount: {self.path}"
        diagnostics = [retained_mount]
        try:
            with (outcome.recovery / "recovery.txt").open("a", encoding="utf-8") as receipt:
                receipt.write(retained_mount + "\n")
        except OSError as error:
            diagnostics.append(f"Cannot update mount recovery record: {error}")

        return replace(outcome, diagnostics=outcome.diagnostics + tuple(diagnostics))


@contextmanager
def temporary_job_directory(
    prefix: str,
    parent: Path | None = None,
) -> Iterator[TemporaryJobDirectory]:
    """Create one private mount source and release it only after safe retirement.

    Processing the output stays inside this context. Directories retained for
    recovery are not deleted by the context manager when an exception unwinds.
    """
    path = Path(tempfile.mkdtemp(prefix=prefix, dir=parent))
    owner = TemporaryJobDirectory(path)
    try:
        yield owner
    finally:
        if owner._may_release:
            shutil.rmtree(path)


def container_arguments(job: Job, token: str, owner: str) -> list[str]:
    """Build the create command from the job's declared inputs.

    Reject ambiguous mount destinations and malformed arguments before Docker.
    The only optional grants are bridge networking and owner-writable bind
    directories; privileged settings and resource limits remain fixed.
    """
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", job.image_id):
        raise OperationError(f"Expected sha256 digest for {job.image_id = }")
    if job.network not in ("none", "bridge"):
        raise OperationError("Finite jobs admit only none or bridge networking")
    if type(job.timeout) is not int or not 1 <= job.timeout <= 900:
        raise OperationError("Job timeout must be between 1 and 900 seconds")

    container_entrypoint = job.command.executable
    if not container_entrypoint or (not container_entrypoint.startswith("/") and "/" in container_entrypoint):
        raise OperationError(f"Expected bare name or absolute path for {container_entrypoint = }")

    if not job.workdir.startswith("/"):
        raise OperationError(f"Expected absolute path for {job.workdir = }")
    if any(character in job.workdir for character in ("\n", "\r", "\x00")):
        raise OperationError(f"Control character in container {job.workdir = }")

    mount_arguments = []
    mounted_targets = set()

    for mount in (*job.mounts, *job.writable_mounts):
        if not mount.source.is_absolute():
            raise OperationError(f"Expected absolute {mount.source = }")
        if not mount.target.startswith("/"):
            raise OperationError(f"Expected absolute {mount.target = }")

        # Docker treats commas as --mount option delimiters. Reject commas,
        # line breaks and NUL before constructing the option string.
        if UNSAFE_MOUNT_PATH.search(str(mount.source)):
            raise OperationError(f"Comma, line break or NUL in {mount.source = }")
        if UNSAFE_MOUNT_PATH.search(mount.target):
            raise OperationError(f"Comma, line break or NUL in {mount.target = }")

        if mount.target in mounted_targets:
            raise OperationError(f"Duplicate {mount.target = }")

        mounted_targets.add(mount.target)
        if isinstance(mount, WritableMount):
            try:
                metadata = mount.source.lstat()
            except OSError as error:
                raise OperationError(f"Cannot inspect writable directory {mount.source}: {error}") from error
            if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.geteuid():
                raise OperationError("Writable mount must be an existing, user-owned directory (not a symlink)")
            mount_arguments.extend(["--mount", f"type=bind,source={mount.source},target={mount.target}"])
        else:
            mount_arguments.extend(["--mount", f"type=bind,source={mount.source},target={mount.target},readonly"])

    # Environment values may contain credentials. Reject malformed declarations
    # without repeating their contents in diagnostics.
    environment_arguments = []

    for environment_setting in job.environment:
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", environment_setting, flags=re.DOTALL) or "\x00" in environment_setting:
            raise OperationError("Expected NAME=value without NUL in container environment")
        environment_arguments.append(f"--env={environment_setting}")

    return [
        "create", "--pull=never",
        "--label", f"{OPERATION_LABEL}={token}",
        "--label", f"{IDENTITY_LABEL}={owner}",

        # Only explicit acquisition jobs can reach the bridge. On the admitted
        # rootless engine, namespaced UID 0 maps to the invoking host user, so
        # writable host directories never need world-writable permissions.
        f"--network={job.network}", "--read-only", "--cap-drop=ALL",
        "--security-opt=no-new-privileges:true",
        f"--user={'0:0' if job.writable_mounts else CONTAINER_USER}",

        # Limit each job's resource use and give it bounded temporary storage.
        f"--cpus={CPU_LIMIT}", f"--memory={MEMORY_LIMIT_MIB}m",
        f"--memory-swap={MEMORY_LIMIT_MIB}m", f"--pids-limit={PID_LIMIT}",
        f"--ulimit=nofile={NOFILE_LIMIT}:{NOFILE_LIMIT}", "--log-driver=none",
        f"--tmpfs=/tmp:{','.join(TMPFS_OPTIONS)}",

        # Only validated environment and mount arguments precede the image;
        # the command arguments follow it and cannot change Docker grants.
        f"--workdir={job.workdir}", "--platform=linux/amd64",
        *environment_arguments, *mount_arguments,
        f"--entrypoint={container_entrypoint}", job.image_id, *job.command.arguments,
    ]


def verify_container_posture(
    docker: DockerClient,
    identifier: str,
    job: Job,
    timeout: Callable[[], int] = lambda: CLIENT_TIMEOUT,
) -> None:
    """Inspect the engine's container settings before starting the workload.

    Check network, root filesystem, privileges, resource limits, temporary
    storage and every source mount. A requested flag alone is not proof that
    the engine applied the restriction. The timeout callback bounds inspection
    by the caller's remaining work budget.
    """
    container_inspection_json = checked_result(
        result=docker.call(
            "container", "inspect", "--format", "{{json .}}", identifier,
            timeout=timeout(),
        ),
        purpose="Container confinement inspection",
    )

    try:
        container_inspection = json.loads(container_inspection_json)
        observed_host_configuration = container_inspection["HostConfig"]
        observed_container_configuration = container_inspection["Config"]
        observed_container_mounts = container_inspection["Mounts"]
    except (KeyError, TypeError, ValueError) as error:
        raise OperationError("Docker returned malformed confinement data") from error

    if (
        not isinstance(observed_host_configuration, dict)
        or not isinstance(observed_container_configuration, dict)
        or not isinstance(observed_container_mounts, list)
    ):
        raise OperationError("Docker returned malformed confinement types")

    expected_memory_bytes = MEMORY_LIMIT_MIB * 1024 * 1024
    expected_host_configuration = {
        "NetworkMode": job.network,
        "ReadonlyRootfs": True,
        "Privileged": False,
        "NanoCpus": CPU_LIMIT * 1_000_000_000,
        "Memory": expected_memory_bytes,
        "MemorySwap": expected_memory_bytes,
        "PidsLimit": PID_LIMIT,
    }

    confinement_findings = []
    for (host_field, expected_host_value) in expected_host_configuration.items():
        observed_host_value = observed_host_configuration.get(host_field)
        if observed_host_value != expected_host_value:
            confinement_findings.append(f"{host_field = }, {observed_host_value = }, {expected_host_value = }")

    observed_user = observed_container_configuration.get("User")
    expected_user = "0:0" if job.writable_mounts else CONTAINER_USER
    if observed_user != expected_user:
        confinement_findings.append(f"{observed_user = }, {expected_user = }")
    if observed_host_configuration.get("PublishAllPorts") not in (None, False):
        confinement_findings.append("Unexpected automatic port publication")
    if observed_host_configuration.get("PortBindings") not in (None, {}):
        confinement_findings.append("Unexpected published container ports")

    observed_dropped_capabilities = observed_host_configuration.get("CapDrop")
    if not isinstance(observed_dropped_capabilities, list) or not any(
        isinstance(capability, str) and capability.lower() == "all"
        for capability in observed_dropped_capabilities
    ):
        confinement_findings.append(f"Missing all-capabilities flag in {observed_dropped_capabilities = }")

    # CapAdd can restore a capability despite an all-capabilities drop.
    observed_added_capabilities = observed_host_configuration.get("CapAdd")
    if observed_added_capabilities not in (None, []):
        confinement_findings.append(f"Unexpected {observed_added_capabilities = }")

    observed_security_options = observed_host_configuration.get("SecurityOpt")
    if not isinstance(observed_security_options, list) or "no-new-privileges:true" not in observed_security_options:
        confinement_findings.append(f"Missing no-new-privileges in {observed_security_options = }")

    observed_logging_configuration = observed_host_configuration.get("LogConfig")
    if not isinstance(observed_logging_configuration, dict) or observed_logging_configuration.get("Type") != "none":
        confinement_findings.append(f"Expected disabled logging in {observed_logging_configuration = }")

    observed_ulimits = observed_host_configuration.get("Ulimits")
    observed_nofile_limits = []

    if isinstance(observed_ulimits, list):
        observed_nofile_limits = [
            ulimit
            for ulimit in observed_ulimits
            if isinstance(ulimit, dict) and ulimit.get("Name") == "nofile"
        ]

    expected_nofile_limit = {"Name": "nofile", "Soft": NOFILE_LIMIT, "Hard": NOFILE_LIMIT}

    # Two entries for the same resource would make the effective limit ambiguous.
    if observed_nofile_limits != [expected_nofile_limit]:
        confinement_findings.append(f"{observed_nofile_limits = }, {expected_nofile_limit = }")

    observed_tmpfs_configuration = observed_host_configuration.get("Tmpfs")
    observed_tmpfs_destinations = (
        set(observed_tmpfs_configuration) if isinstance(observed_tmpfs_configuration, dict) else None
    )
    if observed_tmpfs_destinations != {"/tmp"}:
        confinement_findings.append(f"Unexpected {observed_tmpfs_destinations = }")

    reported_tmp_options = observed_tmpfs_configuration.get("/tmp") if isinstance(observed_tmpfs_configuration, dict) else None
    observed_tmp_options = set(reported_tmp_options.split(",")) if isinstance(reported_tmp_options, str) else None
    expected_tmp_options = set(TMPFS_OPTIONS)

    # Allow Docker to reorder options, but not to add contradictory permissions
    # or a second size declaration.
    if observed_tmp_options != expected_tmp_options:
        confinement_findings.append(f"{observed_tmp_options = }, {expected_tmp_options = }")

    # Docker may also include --tmpfs /tmp in Mounts. Other mounts, including
    # additional read-only binds, are unrequested grants.
    expected_bind_mounts = {
        (str(mount.source), mount.target, False) for mount in job.mounts
    } | {
        (str(mount.source), mount.target, True) for mount in job.writable_mounts
    }
    observed_bind_mounts = set()

    for observed_mount in observed_container_mounts:
        if not isinstance(observed_mount, dict):
            confinement_findings.append(f"Unexpected {observed_mount = }")
            continue

        if observed_mount.get("Type") == "tmpfs":
            if (
                observed_mount.get("Destination") != "/tmp"
                or observed_mount.get("RW") is not True
                or observed_mount.get("Source") not in ("", None)
            ):
                confinement_findings.append(f"Unexpected {observed_mount = }")
            continue

        if observed_mount.get("Type") != "bind" or type(observed_mount.get("RW")) is not bool:
            confinement_findings.append(f"Unexpected mount {observed_mount = }")
            continue

        observed_bind_mounts.add((
            observed_mount.get("Source"), observed_mount.get("Destination"), observed_mount["RW"],
        ))

    if observed_bind_mounts != expected_bind_mounts:
        confinement_findings.append(f"Mount inventory differs: {observed_bind_mounts = }, {expected_bind_mounts = }")

    if confinement_findings:
        raise OperationError(f"Docker did not apply required confinement: {', '.join(confinement_findings)}")


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
    candidate_container_listing = checked_result(
        result=docker.call(
            "ps", "--all", "--quiet", "--no-trunc",
            "--filter", f"label={OPERATION_LABEL}={token}",
            timeout=timeout(),
        ),
        purpose="Owned container discovery",
    )

    candidate_container_ids = container_ids(candidate_container_listing)

    for candidate_container_id in candidate_container_ids:
        candidate_inspection_json = checked_result(
            result=docker.call(
                "container", "inspect", "--format", "{{json .}}", candidate_container_id,
                timeout=timeout(),
            ),
            purpose="Owned container verification",
        )

        try:
            candidate_inspection = json.loads(candidate_inspection_json)
            candidate_ownership_labels = candidate_inspection["Config"]["Labels"]
            candidate_ownership_verified = (
                candidate_inspection["Id"] == candidate_container_id
                and candidate_inspection["Image"] == image_id
                and candidate_ownership_labels[OPERATION_LABEL] == token
                and candidate_ownership_labels[IDENTITY_LABEL] == owner
            )
        except (KeyError, TypeError, ValueError):
            candidate_ownership_verified = False

        if not candidate_ownership_verified:
            raise OperationError(f"Refusing to remove container with inconsistent ownership: {candidate_container_id = }")

    return candidate_container_ids


def cleanup(docker: DockerClient, token: str, owner: str, image_id: str) -> None:
    """Retire verified containers within a separate cleanup deadline.

    Attempt removal, then list them again to prove they are gone. Preserve
    failures and raise if removal or the final inspection is inconclusive.
    """
    cleanup_deadline = time.monotonic() + CLEANUP_TIMEOUT

    def remaining_cleanup_timeout() -> int:
        """Bound each cleanup request by the time left for the whole cleanup."""
        remaining_cleanup_seconds = int(cleanup_deadline - time.monotonic())

        if remaining_cleanup_seconds <= 0:
            raise TimeoutError("Docker cleanup budget exhausted")

        return min(CLIENT_TIMEOUT, remaining_cleanup_seconds)

    owned_container_ids = owned_containers(docker, token, owner, image_id, timeout=remaining_cleanup_timeout)

    removal_failures = []
    for container_id in owned_container_ids:
        removal_result = docker.call("rm", "--force", "--volumes", container_id, timeout=remaining_cleanup_timeout())
        if removal_result.status != 0:
            removal_failures.append(f"{container_id = }, {removal_result.status = }")

    remaining_container_ids = owned_containers(docker, token, owner, image_id, timeout=remaining_cleanup_timeout)

    # Failed rm --volumes may leave anonymous volumes even if the container
    # disappears. An empty container listing alone cannot confirm retirement.
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
    image_id: str,
) -> None:
    """Save the selected engine and ownership evidence before Docker create.

    If a client times out after sending a request, operators can identify
    possible daemon-side resources without guessing or globally pruning.
    """
    recovery_receipt = directory / "recovery.txt"
    recovery_receipt.write_text(
        f"Context: {context}\n"
        f"Endpoint: {endpoint}\n"
        f"Image ID: {image_id}\n"
        f"Operation label: {OPERATION_LABEL}={token}\n"
        f"Owner label: {IDENTITY_LABEL}={owner}\n"
        f"Inspect: docker --context {context} ps -a --no-trunc "
        f"--filter label={OPERATION_LABEL}={token}\n"
        "If container.cid exists, inspect its full ID as well; a partial create "
        "might not have applied the labels.\n"
        "Verify image and both ownership labels before removing a container. Do not use global prune.\n",
        encoding="utf-8",
    )
    recovery_receipt.chmod(0o600)


@contextmanager
def protected_cleanup():
    """Ignore terminal signals during main-thread resource retirement.

    Only the main thread can install signal handlers. A worker performs the
    cleanup without changing process-wide handlers; its caller owns shutdown.
    """
    if threading.current_thread() is not threading.main_thread():
        yield
        return

    terminal_signals = (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)
    previous_signal_handlers = {}

    try:
        # Restore earlier handlers even when a later installation fails.
        for signal_number in terminal_signals:
            previous_signal_handlers[signal_number] = signal.signal(signal_number, signal.SIG_IGN)
        yield
    finally:
        for (signal_number, previous_handler) in previous_signal_handlers.items():
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
    docker_create_arguments = container_arguments(job=job, token=operation_token, owner=owner_token)
    work_deadline = time.monotonic() + job.timeout

    def remaining_work_timeout(maximum_timeout: int = CLIENT_TIMEOUT) -> int:
        """Bound each Docker request by the time left for the complete work phase."""
        remaining_seconds = int(work_deadline - time.monotonic())
        if remaining_seconds <= 0:
            raise TimeoutError("Docker work budget exhausted")
        return min(maximum_timeout, remaining_seconds)

    operation_directory = Path(tempfile.mkdtemp(prefix="hello-world-operation-"))
    recovery_directory: Path | None = operation_directory
    container_creation_requested = False
    container_create_confirmed = False

    job_output = ""
    failure_reasons = []
    operation_diagnostics = []
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
            image_id=job.image_id,
        )
        cid_receipt = operation_directory / "container.cid"
        create_command = [
            "create",
            "--cidfile", str(cid_receipt),
            *docker_create_arguments[1:],
        ]
        create_timeout = remaining_work_timeout()

        # A lost reply after this call cannot prove Docker created nothing.
        container_creation_requested = True
        reported_container_id = checked_result(
            result=docker.call(*create_command, timeout=create_timeout),
            purpose="Container creation",
        )

        if not cid_receipt.is_file() or cid_receipt.is_symlink():
            raise OperationError("Docker did not write the private container identity receipt")
        container_id = cid_receipt.read_text(encoding="ascii").strip()

        if not CONTAINER_ID.fullmatch(container_id):
            raise OperationError(f"Invalid Docker CID receipt {container_id = }")
        if reported_container_id != container_id:
            raise OperationError(
                f"Docker container creation and CID receipt disagree: {reported_container_id = }, {container_id = }"
            )

        observed_container_ids = owned_containers(
            docker=docker,
            token=operation_token,
            owner=owner_token,
            image_id=job.image_id,
            timeout=remaining_work_timeout,
        )
        if observed_container_ids != [container_id]:
            raise OperationError(
                f"Docker created an unexpected container identity: {observed_container_ids = }, {container_id = }"
            )

        container_create_confirmed = True
        verify_container_posture(docker, container_id, job, timeout=remaining_work_timeout)

        docker_attachment_result = docker.call(
            "start", "--attach", container_id, timeout=remaining_work_timeout(job.timeout)
        )
        job_output = docker_attachment_result.output
        if docker_attachment_result.truncated:
            failure_reasons.append(f"Container output exceeded {MAX_OUTPUT} bytes; tail retained")

        # The attachment client may finish while the workload is still running.
        # Only the daemon's final state establishes the workload exit status.
        completion_inspection = checked_result(
            result=docker.call(
                "container", "inspect", "--format", "{{json .State}}", container_id,
                timeout=remaining_work_timeout(),
            ),
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

        if docker_attachment_result.status != 0:
            failure_reasons.append(f"Docker attachment failed ({docker_attachment_result.status = })")
        if container_exit_code != 0:
            failure_reasons.append(f"Container exited with {container_exit_code = }")

    except (OperationError, TimeoutError, OSError, KeyboardInterrupt) as error:
        failure_reasons.append(str(error) or error.__class__.__name__)
    finally:
        # Even a timed-out create can finish on the daemon. Inspect before
        # declaring retirement; keep recovery information if uncertain.
        with protected_cleanup():
            discard_operation_directory = not container_creation_requested

            if container_creation_requested:
                try:
                    cleanup(
                        docker=docker,
                        token=operation_token,
                        owner=owner_token,
                        image_id=job.image_id,
                    )
                except (OperationError, TimeoutError, OSError) as error:
                    operation_status = 1
                    operation_diagnostics.append(
                        f"CLEANUP UNCONFIRMED: {error}. "
                        f"Retained recovery record: {operation_directory}/recovery.txt"
                    )
                else:
                    if not container_create_confirmed:
                        operation_status = 1
                        operation_diagnostics.append(
                            "Create was not confirmed; retain the receipt even if discovery currently finds nothing. "
                            f"Recovery: {operation_directory}/recovery.txt"
                        )
                    else:
                        discard_operation_directory = True

            if discard_operation_directory:
                try:
                    for child in operation_directory.iterdir():
                        child.unlink()
                    operation_directory.rmdir()
                    recovery_directory = None
                except OSError as error:
                    operation_status = 1
                    operation_diagnostics.append(
                        f"Cannot remove operation record: {operation_directory = }, {error = }"
                    )

    # The inspected workload exit takes precedence. Retain earlier evidence
    # instead of overwriting it when several failures occurred.
    if failure_reasons:
        operation_status = 1
        operation_diagnostics = failure_reasons[:-1] + operation_diagnostics

    if recovery_directory is not None:
        operation_diagnostics.append(f"Retained operation: {recovery_directory}")

    return Outcome(
        status=operation_status,
        output=job_output,
        failure=failure_reasons[-1] if failure_reasons else None,
        diagnostics=tuple(operation_diagnostics),
        recovery=recovery_directory,
    )
