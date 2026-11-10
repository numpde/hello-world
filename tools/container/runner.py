"""Run finite, confined Docker jobs with verified container ownership and retirement.

Consumers choose an immutable image, command and read-only inputs. This module
owns engine admission, confinement, deadlines and cleanup, not test discovery.
Services and controlled networked acquisition need distinct explicit policies.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import signal
import stat
import subprocess
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass
from collections.abc import Callable
from pathlib import Path
from typing import Protocol


OPERATION_LABEL = "org.numpde.hello-world.operation"
IDENTITY_LABEL = "org.numpde.hello-world.owner"
CLIENT_TIMEOUT = 20
WORK_TIMEOUT = 180
CLEANUP_TIMEOUT = 25
MAX_OUTPUT = 1024 * 1024
CONTAINER_ID = re.compile(r"[0-9a-f]{64}\Z")
CONTEXT_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")


class OperationError(RuntimeError):
    """A selected operation could not be completed and verified."""


class Cancelled(OperationError):
    """The caller requested cancellation; cleanup still needs to run."""


@dataclass(frozen=True)
class Result:
    status: int
    output: str
    truncated: bool = False


@dataclass(frozen=True)
class ReadOnlyMount:
    source: Path
    target: str


@dataclass(frozen=True)
class Command:
    executable: str
    arguments: tuple[str, ...] = ()


@dataclass(frozen=True)
class Job:
    image_id: str
    command: Command
    mounts: tuple[ReadOnlyMount, ...]
    environment: tuple[str, ...] = ()
    workdir: str = "/workspace"


@dataclass(frozen=True)
class Outcome:
    status: int
    output: str
    failure: str | None
    diagnostics: tuple[str, ...]
    recovery: Path | None


class Executor(Protocol):
    def run(self, argv: list[str], env: dict[str, str], timeout: int) -> Result: ...


class NativeExecutor:
    """Run a Docker client with a bounded lifetime and bounded captured output."""

    def run(self, argv: list[str], env: dict[str, str], timeout: int) -> Result:
        import selectors

        process = subprocess.Popen(
            argv,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        output = bytearray()
        truncated = False
        deadline = time.monotonic() + timeout
        try:
            with selectors.DefaultSelector() as poller:
                assert process.stdout is not None
                poller.register(process.stdout, selectors.EVENT_READ)
                while poller.get_map():
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError(f"Docker client exceeded {timeout}s")
                    for key, _ in poller.select(min(remaining, 0.5)):
                        chunk = os.read(key.fileobj.fileno(), 65536)
                        if not chunk:
                            poller.unregister(key.fileobj)
                            continue
                        output.extend(chunk)
                        if len(output) > MAX_OUTPUT:
                            del output[: len(output) - MAX_OUTPUT]
                            truncated = True
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"Docker client exceeded {timeout}s")
            status = process.wait(timeout=remaining)
            return Result(status, output.decode("utf-8", errors="replace"), truncated)
        finally:
            if process.poll() is None:
                # A dead client does not prove that Docker stopped its container.
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    process.wait(timeout=2)
            if process.stdout is not None:
                process.stdout.close()


def clean_environment(original: dict[str, str]) -> dict[str, str]:
    """Keep only the host facts necessary to run a local Docker client."""
    names = ("PATH", "HOME", "XDG_RUNTIME_DIR", "LANG", "LC_ALL", "LC_CTYPE", "TMPDIR")
    return {name: original[name] for name in names if name in original}


def required_context(environ: dict[str, str]) -> str:
    selected = environ.get("HELLO_WORLD_DOCKER_CONTEXT")
    if selected is None or not CONTEXT_NAME.fullmatch(selected):
        raise OperationError(
            "Specify DOCKER_CONTEXT=<local rootless context> on the make command line. "
            "An empty or malformed context is not accepted."
        )
    return selected


def checked_result(result: Result, purpose: str) -> str:
    if result.status != 0:
        detail = result.output.strip()[-1200:]
        raise OperationError(f"{purpose} failed (exit {result.status}): {detail}")
    if result.truncated:
        raise OperationError(f"{purpose} emitted more than {MAX_OUTPUT} bytes")
    return result.output.strip()


def socket_path(endpoint: str) -> Path:
    if not endpoint.startswith("unix://") or re.search(r"[\x00-\x1f\x7f]", endpoint):
        raise OperationError("Selected Docker context must use a local Unix socket")
    raw = endpoint[len("unix://"):]
    if not raw.startswith("/") or "?" in raw or "#" in raw:
        raise OperationError("Selected Docker endpoint must be an absolute local socket path")
    return Path(raw)


def verify_socket(endpoint: str) -> None:
    path = socket_path(endpoint)
    try:
        entry = path.lstat()
    except OSError as error:
        raise OperationError(f"Cannot inspect selected Docker socket {path}: {error}") from error
    if not stat.S_ISSOCK(entry.st_mode) or entry.st_uid != os.geteuid():
        raise OperationError("Selected Docker socket must be user-owned and a Unix socket")


class Docker:
    def __init__(self, executor: Executor, environment: dict[str, str]):
        self.executor = executor
        self.environment = clean_environment(environment)
        self.endpoint: str | None = None
        self.config: Path | None = None

    def context(self, name: str) -> str:
        output = checked_result(
            self.executor.run(
                ["docker", "context", "inspect", "--format", "{{.Endpoints.docker.Host}}", name],
                self.environment, CLIENT_TIMEOUT,
            ),
            "Docker context inspection",
        )
        socket_path(output)
        verify_socket(output)
        self.endpoint = output
        return output

    def call(self, *args: str, timeout: int = CLIENT_TIMEOUT) -> Result:
        assert self.endpoint is not None and self.config is not None
        return self.executor.run(
            ["docker", "--config", str(self.config), "--host", self.endpoint, *args],
            self.environment, timeout,
        )

    def engine(self) -> str:
        info = checked_result(self.call("info", "--format", "{{json .}}"), "Docker engine inspection")
        try:
            description = json.loads(info)
        except (TypeError, ValueError) as error:
            raise OperationError("Docker engine returned malformed information") from error
        if not isinstance(description, dict):
            raise OperationError("Docker engine returned a non-object description")
        security = description.get("SecurityOptions")
        if not isinstance(security, list) or "name=rootless" not in security:
            raise OperationError("Confined operations require a rootless Docker engine")
        if description.get("CgroupDriver") != "systemd" or description.get("CgroupVersion") != "2":
            raise OperationError("Rootless engine must enforce resource limits through systemd cgroup v2")
        if description.get("OSType") != "linux":
            raise OperationError("Confined operations require a Linux Docker engine")
        if description.get("Architecture") not in ("x86_64", "amd64"):
            raise OperationError("Pinned runtime requires a linux/amd64 Docker engine")
        identity = description.get("ID")
        if not isinstance(identity, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9:._-]{0,127}", identity):
            raise OperationError("Docker daemon ID is missing or invalid")
        return identity

    def image(self, reference: str, expected_id: str, acquire_hint: str) -> str:
        result = self.call("image", "inspect", "--format", "{{.Id}}", reference)
        if result.status != 0:
            raise OperationError(f"Pinned image is missing on the selected engine. {acquire_hint}")
        identity = checked_result(result, "Pinned image inspection")
        if identity != expected_id:
            raise OperationError(f"Pinned image configuration differs: {identity!r}")
        return identity

    def acquire(self, reference: str, expected_id: str) -> str:
        checked_result(
            self.call("pull", "--platform=linux/amd64", reference, timeout=WORK_TIMEOUT),
            "Pinned image acquisition",
        )
        return self.image(reference, expected_id, "Acquired image could not be verified")


@contextmanager
def docker_client(environ: dict[str, str], executor: Executor | None = None):
    """Select one context and allocate a private, empty client configuration."""
    if os.geteuid() == 0 or os.getegid() == 0:
        raise OperationError("Confined operations must be invoked by a non-root user")
    context_name = required_context(environ)
    docker = Docker(executor or NativeExecutor(), environ)
    docker.context(context_name)
    with tempfile.TemporaryDirectory(prefix="hello-world-docker-client-") as directory:
        docker.config = Path(directory)
        (docker.config / "config.json").write_text("{}\n", encoding="utf-8")
        docker.engine()
        yield docker


def container_arguments(job: Job, token: str, owner: str) -> list[str]:
    """Construct a confined container without accepting arbitrary Docker options."""
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


def verify_container_posture(docker: Docker, identifier: str, job: Job) -> None:
    """Reject created containers whose effective grants differ from policy."""
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
    identities = [value for value in raw.splitlines() if value]
    if len(identities) != len(set(identities)) or any(not CONTAINER_ID.fullmatch(value) for value in identities):
        raise OperationError("Docker reported malformed or duplicate container identities")
    return identities


def owned_containers(
    docker: Docker, token: str, owner: str, image_id: str,
    timeout: Callable[[], int] = lambda: CLIENT_TIMEOUT,
) -> list[str]:
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


def cleanup(docker: Docker, token: str, owner: str, image_id: str) -> None:
    deadline = time.monotonic() + CLEANUP_TIMEOUT

    def remaining() -> int:
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
    """Prevent repeated terminal signals from interrupting resource retirement."""
    signals = (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)
    previous = {number: signal.signal(number, signal.SIG_IGN) for number in signals}
    try:
        yield
    finally:
        for number, handler in previous.items():
            signal.signal(number, handler)


def run_container(docker: Docker, context: str, job: Job) -> Outcome:
    """Create, admit, execute, and retire one container owned by this operation."""
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

