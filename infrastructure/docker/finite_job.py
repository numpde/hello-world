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
    Cancelled,
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
    
    This finite-job policy does not admit writable host mounts."""
    source: Path
    target: str


@dataclass(frozen=True)
class Command:
    """The entry point and literal arguments run inside the selected image.
    
    The job lifecycle is independent of whether this invokes unittest,
    unittest-parallel, a compiler, or another finite command."""
    executable: str
    arguments: tuple[str, ...] = ()


@dataclass(frozen=True)
class Job:
    """Inputs for one bounded container run.
    
    Callers select the image, command, read-only mounts and ordinary environment.
    They cannot add Docker flags that weaken the runner's confinement policy."""
    image_id: str
    command: Command
    mounts: tuple[ReadOnlyMount, ...]
    environment: tuple[str, ...] = ()
    workdir: str = "/workspace"


@dataclass(frozen=True)
class Outcome:
    """Execution evidence and cleanup evidence for a finished attempt.
    
    status is nonzero for a failed command or unconfirmed cleanup. The recovery
    path remains when ownership or retirement cannot safely be established."""
    status: int
    output: str
    failure: str | None
    diagnostics: tuple[str, ...]
    recovery: Path | None

def container_arguments(job: Job, token: str, owner: str) -> list[str]:
    """Build the create command from admitted job inputs.

Reject ambiguous mount destinations and malformed arguments before touching
Docker. Network, privileges, resources and writable storage are fixed here,
not selectable through an arbitrary list of Docker flags."""
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", job.image_id):
        raise OperationError("A verified immutable image ID is required")
    executable = job.command.executable
    if not executable or (not executable.startswith("/") and "/" in executable):
        raise OperationError("Container entrypoint must be a name or absolute path")
    if not job.workdir.startswith("/") or any(ch in job.workdir for ch in ("\n", "\r", "\x00")):
        raise OperationError("Container workdir must be an absolute path")
    mounts = []
    seen = set()
    for mount in job.mounts:
        source = str(mount.source)
        if not mount.source.is_absolute() or not mount.target.startswith("/") or any(
            ch in source + mount.target for ch in (",", "\n", "\r", "\x00")
        ):
            raise OperationError(f"Unsafe read-only mount: {mount}")
        if mount.target in seen:
            raise OperationError(f"Duplicate container destination: {mount.target}")
        seen.add(mount.target)
        mounts.extend(["--mount", f"type=bind,source={source},target={mount.target},readonly"])
    environment = []
    for item in job.environment:
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", item, flags=re.DOTALL) or "\x00" in item:
            raise OperationError(f"Malformed container environment declaration: {item!r}")
        environment.append(f"--env={item}")
    return [
        "create", "--pull=never",
        "--label", f"{OPERATION_LABEL}={token}",
        "--label", f"{IDENTITY_LABEL}={owner}",
        "--network=none", "--read-only", "--cap-drop=ALL",
        "--security-opt=no-new-privileges:true", "--user=65532:65532",
        "--cpus=2", "--memory=512m", "--memory-swap=512m", "--pids-limit=64",
        "--ulimit=nofile=256:256", "--log-driver=none",
        "--tmpfs=/tmp:rw,nosuid,nodev,noexec,size=67108864",
        f"--workdir={job.workdir}", "--platform=linux/amd64",
        *environment, *mounts,
        f"--entrypoint={executable}", job.image_id, *job.command.arguments,
    ]


def verify_container_posture(docker: DockerClient, identifier: str, job: Job) -> None:
    """Inspect the engine's container settings before starting the workload.

Check network, root filesystem, privileges, resource limits, temporary
storage and every source mount. A requested flag alone is not proof that
the engine applied the restriction."""
    raw = checked_result(
        docker.call("container", "inspect", "--format", "{{json .}}", identifier),
        "Checker confinement inspection",
    )
    try:
        description = json.loads(raw)
        host = description["HostConfig"]
        config = description["Config"]
        mounts = description["Mounts"]
    except (KeyError, TypeError, ValueError) as error:
        raise OperationError("Docker returned malformed confinement data") from error
    if not isinstance(host, dict) or not isinstance(config, dict) or not isinstance(mounts, list):
        raise OperationError("Docker returned malformed confinement types")
    failures = []
    expected = {
        "NetworkMode": "none", "ReadonlyRootfs": True, "Privileged": False,
        "NanoCpus": 2_000_000_000, "Memory": 536_870_912,
        "MemorySwap": 536_870_912, "PidsLimit": 64,
    }
    for field, value in expected.items():
        if host.get(field) != value:
            failures.append(field)
    if config.get("User") != "65532:65532":
        failures.append("user")
    if not isinstance(host.get("CapDrop"), list) or "all" not in (
        value.lower() for value in host["CapDrop"] if isinstance(value, str)
    ):
        failures.append("capabilities")
    if not isinstance(host.get("SecurityOpt"), list) or "no-new-privileges:true" not in host["SecurityOpt"]:
        failures.append("no-new-privileges")
    logging = host.get("LogConfig")
    if not isinstance(logging, dict) or logging.get("Type") != "none":
        failures.append("logging")
    temporary = host.get("Tmpfs")
    if not isinstance(temporary, dict) or not isinstance(temporary.get("/tmp"), str) or not {
        "rw", "nosuid", "nodev", "noexec", "size=67108864"
    }.issubset(set(temporary["/tmp"].split(","))):
        failures.append("temporary storage")
    expected_mounts = {(str(mount.source), mount.target) for mount in job.mounts}
    actual_mounts = set()
    for mount in mounts:
        if not isinstance(mount, dict) or mount.get("Type") != "bind" or mount.get("RW") is not False:
            failures.append("source mount type or writable permission")
            continue
        actual_mounts.add((mount.get("Source"), mount.get("Destination")))
    if actual_mounts != expected_mounts:
        failures.append("source mount inventory")
    if failures:
        raise OperationError(f"Docker did not apply required confinement: {', '.join(failures)}")


def container_ids(raw: str) -> list[str]:
    """Parse the full, unique IDs returned by Docker container listing.
    
    Reject truncated or duplicate identities rather than risk acting on an
    ambiguous container during cleanup."""
    identities = [value for value in raw.splitlines() if value]
    if len(identities) != len(set(identities)) or any(not CONTAINER_ID.fullmatch(value) for value in identities):
        raise OperationError("Docker reported malformed or duplicate container identities")
    return identities


def owned_containers(
    docker: DockerClient, token: str, owner: str, image_id: str,
    timeout: Callable[[], int] = lambda: CLIENT_TIMEOUT,
) -> list[str]:
    """Discover containers with this run's label and verify each candidate.
    
    A matching search label does not grant deletion authority: the full ID,
    image ID and both ownership labels must also agree."""
    found = checked_result(
        docker.call("ps", "--all", "--quiet", "--no-trunc", "--filter", f"label={OPERATION_LABEL}={token}", timeout=timeout()),
        "Owned container discovery",
    )
    candidates = container_ids(found)
    for candidate in candidates:
        details = checked_result(
            docker.call("container", "inspect", "--format", "{{json .}}", candidate, timeout=timeout()),
            "Owned container verification",
        )
        try:
            description = json.loads(details)
            labels = description["Config"]["Labels"]
            verified = (
                description["Id"] == candidate
                and description["Image"] == image_id
                and labels[OPERATION_LABEL] == token
                and labels[IDENTITY_LABEL] == owner
            )
        except (KeyError, TypeError, ValueError):
            verified = False
        if not verified:
            raise OperationError(f"Refusing to remove container with inconsistent ownership: {candidate}")
    return candidates


def cleanup(docker: DockerClient, token: str, owner: str, image_id: str) -> None:
    """Retire verified containers within a separate cleanup deadline.
    
    Attempt removal, then list them again to prove they are gone. Preserve
    failures and raise if removal or the final inspection is inconclusive."""
    deadline = time.monotonic() + CLEANUP_TIMEOUT

    def remaining() -> int:
        """Bound each cleanup request by the time left for the whole cleanup."""
        seconds = deadline - time.monotonic()
        if seconds <= 0:
            raise TimeoutError("Docker cleanup budget exhausted")
        return min(CLIENT_TIMEOUT, max(1, int(seconds)))

    candidates = owned_containers(docker, token, owner, image_id, remaining)
    failures = []
    for candidate in candidates:
        result = docker.call("rm", "--force", "--volumes", candidate, timeout=remaining())
        if result.status != 0:
            failures.append(f"remove {candidate}: exit {result.status}")
    still_present = owned_containers(docker, token, owner, image_id, remaining)
    if still_present or failures:
        raise OperationError(
            f"Container cleanup unconfirmed: remaining={still_present!r}, errors={failures!r}"
        )


def write_receipt(directory: Path, context: str, endpoint: str, token: str, owner: str) -> None:
    """Save the selected engine and ownership labels before Docker create.
    
    If a client times out after sending a request, operators can identify
    possible daemon-side resources without guessing or globally pruning."""
    receipt = directory / "recovery.txt"
    receipt.write_text(
        f"Context: {context}\nEndpoint: {endpoint}\nOperation label: {OPERATION_LABEL}={token}\n"
        f"Owner label: {IDENTITY_LABEL}={owner}\n"
        "Inspect: docker --context <context> ps -a --no-trunc --filter label=<operation-label>\n"
        "Verify ownership before removing any container. Do not use global prune.\n",
        encoding="utf-8",
    )
    receipt.chmod(0o600)


@contextmanager
def protected_cleanup():
    """Temporarily ignore terminal signals while retiring owned resources.

The caller records the primary interruption; this guard prevents another
signal from skipping cleanup and restores the previous handlers afterward."""
    signals = (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)
    previous = {number: signal.signal(number, signal.SIG_IGN) for number in signals}
    try:
        yield
    finally:
        for number, handler in previous.items():
            signal.signal(number, handler)


def run_container(docker: DockerClient, context: str, job: Job) -> Outcome:
    """Run a job and report both its work outcome and cleanup outcome.

Record ownership before create, require a matching container-ID receipt,
verify confinement, then start and inspect the final exit state. Always
attempt retirement. Keep the receipt when create or cleanup is uncertain;
never report a clean success merely because the Docker client exited."""
    token = secrets.token_hex(16)
    owner = secrets.token_hex(16)
    arguments = container_arguments(job, token, owner)
    directory = Path(tempfile.mkdtemp(prefix="hello-world-operation-"))
    recovery: Path | None = directory
    create_confirmed = False
    failure: str | None = None
    diagnostics = []
    output = ""
    status = 0
    try:
        write_receipt(directory, context, docker.endpoint or "", token, owner)
        args = [*arguments[:1], "--cidfile", str(directory / "container.cid"), *arguments[1:]]
        created = checked_result(docker.call(*args), "Container creation")
        receipt = directory / "container.cid"
        if not receipt.is_file() or receipt.is_symlink():
            raise OperationError("Docker did not write the private container identity receipt")
        identifier = receipt.read_text(encoding="ascii").strip()
        if not CONTAINER_ID.fullmatch(identifier) or created != identifier:
            raise OperationError("Docker container creation and CID receipt disagree")
        observed = owned_containers(docker, token, owner, job.image_id)
        if observed != [identifier]:
            raise OperationError("Docker created an unexpected container identity")
        create_confirmed = True
        verify_container_posture(docker, identifier, job)
        result = docker.call("start", "--attach", identifier, timeout=WORK_TIMEOUT)
        output = result.output
        if result.truncated:
            failure = f"Container output exceeded {MAX_OUTPUT} bytes; tail retained"
        finished = checked_result(
            docker.call("container", "inspect", "--format", "{{json .State}}", identifier),
            "Container completion inspection",
        )
        try:
            state = json.loads(finished)
            exited = isinstance(state, dict) and state.get("Running") is False
            exitcode = state["ExitCode"] if exited else None
        except (ValueError, KeyError, TypeError):
            exitcode = None
        if type(exitcode) is not int or not 0 <= exitcode <= 255:
            raise OperationError("Container stopped without a verified final exit status")
        if result.status != 0:
            failure = f"Docker attachment failed (exit {result.status})"
        if exitcode != 0:
            failure = f"Container exited with status {exitcode}"
    except (OperationError, TimeoutError, OSError, KeyboardInterrupt, Cancelled) as error:
        failure = str(error) or error.__class__.__name__
    finally:
        # Even a timed-out create can finish on the daemon. Inspect before
        # declaring retirement; keep recovery information if uncertain.
        with protected_cleanup():
            try:
                cleanup(docker, token, owner, job.image_id)
            except (OperationError, TimeoutError, OSError) as error:
                status = 1
                diagnostics.append(f"CLEANUP UNCONFIRMED: {error}. Retained recovery record: {directory}/recovery.txt")
            else:
                if not create_confirmed:
                    status = 1
                    diagnostics.append(
                        "Create was not confirmed; retain the receipt even if discovery currently finds nothing. "
                        f"Recovery: {directory}/recovery.txt"
                    )
                else:
                    try:
                        for child in directory.iterdir():
                            child.unlink()
                        directory.rmdir()
                        recovery = None
                    except OSError as error:
                        status = 1
                        diagnostics.append(f"Cannot remove operation record {directory}: {error}")
    if failure is not None:
        status = 1
    if recovery is not None:
        diagnostics.append(f"Retained operation: {recovery}")
    return Outcome(status, output, failure, tuple(diagnostics), recovery)
