"""Use one explicitly selected local Docker engine for an operation.

The caller names a Docker context. This module checks the context's Unix
socket and the rootless daemon, then sends every command to that endpoint
with a fresh, empty Docker client configuration. It discards inherited
Docker and proxy settings. Container ownership and cleanup belong elsewhere.
"""

from __future__ import annotations

import json
import os
import re
import signal
import stat
import subprocess
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


CLIENT_TIMEOUT = 20
ACQUISITION_TIMEOUT = 180
MAX_OUTPUT = 1024 * 1024
CONTEXT_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")


class OperationError(RuntimeError):
    """The Docker operation failed, or its outcome could not be established."""


class Cancelled(OperationError):
    """The caller interrupted an operation; owned resources still need cleanup."""


@dataclass(frozen=True)
class Result:
    """The exit status and captured output of one client process.
    
    When truncated is true, output holds the most recent bytes, not the
    beginning of the process's output."""
    status: int
    output: str
    truncated: bool = False

class Executor(Protocol):
    """Replaceable boundary for invoking a Docker client.
    
    A timeout bounds the local process. It does not establish whether a
    request already reached the daemon or changed its state."""
    def run(self, argv: list[str], env: dict[str, str], timeout: int) -> Result: ...


class NativeExecutor:
    """Run a client without blocking on its output or leaving it running.

The output buffer holds at most the last MAX_OUTPUT bytes. If the client process
outlives its deadline, terminate its process group; callers must separately
check any daemon-side effects."""

    def run(self, argv: list[str], env: dict[str, str], timeout: int) -> Result:
        """Use the supplied environment and deadline for one subprocess.
        
        Standard output and error share a bounded buffer. Timeout raises after
        attempting to stop the local process; it is not a Docker cleanup result."""
        import selectors

        docker_client_process = subprocess.Popen(
            argv,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        captured_output = bytearray()
        truncated = False
        client_deadline = time.monotonic() + timeout
        try:
            with selectors.DefaultSelector() as output_selector:
                assert docker_client_process.stdout is not None
                output_selector.register(docker_client_process.stdout, selectors.EVENT_READ)
                while output_selector.get_map():
                    remaining_seconds = client_deadline - time.monotonic()
                    if remaining_seconds <= 0:
                        raise TimeoutError(f"Docker client exceeded {timeout}s")
                    for key, _ in output_selector.select(min(remaining_seconds, 0.5)):
                        output_chunk = os.read(key.fileobj.fileno(), 65536)
                        if not output_chunk:
                            output_selector.unregister(key.fileobj)
                            continue
                        captured_output.extend(output_chunk)
                        if len(captured_output) > MAX_OUTPUT:
                            del captured_output[: len(captured_output) - MAX_OUTPUT]
                            truncated = True
            remaining_seconds = client_deadline - time.monotonic()
            if remaining_seconds <= 0:
                raise TimeoutError(f"Docker client exceeded {timeout}s")
            client_exit_status = docker_client_process.wait(timeout=remaining_seconds)
            return Result(client_exit_status, captured_output.decode("utf-8", errors="replace"), truncated)
        finally:
            if docker_client_process.poll() is None:
                # A dead client does not prove that Docker stopped its container.
                try:
                    os.killpg(docker_client_process.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                try:
                    docker_client_process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    try:
                        os.killpg(docker_client_process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    docker_client_process.wait(timeout=2)
            if docker_client_process.stdout is not None:
                docker_client_process.stdout.close()


def clean_environment(original: dict[str, str]) -> dict[str, str]:
    """Keep the minimum host environment needed to find and run Docker.

Caller-provided Docker endpoints, builder settings, proxies and credentials
must not silently change which daemon or registry a command reaches."""
    allowed_host_variables = ("PATH", "HOME", "XDG_RUNTIME_DIR", "LANG", "LC_ALL", "LC_CTYPE", "TMPDIR")
    return {variable_name: original[variable_name] for variable_name in allowed_host_variables if variable_name in original}


def required_context(environ: dict[str, str]) -> str:
    """Require the context explicitly forwarded by Make.
    
    Do not fall back to the process's ambient DOCKER_CONTEXT or Docker's
    default context."""
    selected_context = environ.get("HELLO_WORLD_DOCKER_CONTEXT")
    if selected_context is None or not CONTEXT_NAME.fullmatch(selected_context):
        raise OperationError(
            "Specify DOCKER_CONTEXT=<local rootless context> on the make command line. "
            "An empty or malformed context is not accepted."
        )
    return selected_context


def checked_result(result: Result, purpose: str) -> str:
    """Return a successful client's text, or report its original failure.
    
    A nonzero exit retains the recent output as context. A truncated successful
    response is rejected because its complete value cannot be trusted."""
    if result.status != 0:
        failure_output = result.output.strip()[-1200:]
        raise OperationError(f"{purpose} failed (exit {result.status}): {failure_output}")
    if result.truncated:
        raise OperationError(f"{purpose} emitted more than {MAX_OUTPUT} bytes")
    return result.output.strip()


def socket_path(endpoint: str) -> Path:
    """Accept only absolute Unix-socket endpoints, not TCP or SSH.
    
    This parses a context's declared address; verify_socket also checks the
    actual filesystem object."""
    if not endpoint.startswith("unix://") or re.search(r"[\x00-\x1f\x7f]", endpoint):
        raise OperationError("Selected Docker context must use a local Unix socket")
    socket_address = endpoint[len("unix://"):]
    if not socket_address.startswith("/") or "?" in socket_address or "#" in socket_address:
        raise OperationError("Selected Docker endpoint must be an absolute local socket path")
    return Path(socket_address)


def verify_socket(endpoint: str) -> None:
    """Require an existing Unix socket owned by the calling user.
    
    Inspect without following a symlink. A context name alone is not
    evidence that it selects the intended local engine."""
    selected_socket_path = socket_path(endpoint)
    try:
        socket_metadata = selected_socket_path.lstat()
    except OSError as error:
        raise OperationError(f"Cannot inspect selected Docker socket {selected_socket_path}: {error}") from error
    if not stat.S_ISSOCK(socket_metadata.st_mode) or socket_metadata.st_uid != os.geteuid():
        raise OperationError("Selected Docker socket must be user-owned and a Unix socket")


class DockerClient:
    """Send commands to one admitted engine using a private client configuration.
    
    Once the context is resolved, later operations reuse the stored endpoint;
    inherited Docker settings cannot redirect individual calls."""
    def __init__(self, executor: Executor, environment: dict[str, str]):
        self.executor = executor
        self.environment = clean_environment(environment)
        self.endpoint: str | None = None
        self.config: Path | None = None

    def context(self, name: str) -> str:
        """Resolve the named context and retain its verified Unix-socket endpoint.
        
        This reads the caller's context definitions but rejects nonlocal or
        non-user-owned sockets rather than silently choosing another engine."""
        context_endpoint = checked_result(
            self.executor.run(
                ["docker", "context", "inspect", "--format", "{{.Endpoints.docker.Host}}", name],
                self.environment, CLIENT_TIMEOUT,
            ),
            "Docker context inspection",
        )
        socket_path(context_endpoint)
        verify_socket(context_endpoint)
        self.endpoint = context_endpoint
        return context_endpoint

    def call(self, *args: str, timeout: int = CLIENT_TIMEOUT) -> Result:
        """Run an operation on the already selected engine.
        
        The endpoint and private configuration are explicit arguments to Docker.
        The caller remains responsible for interpreting the returned exit status."""
        assert self.endpoint is not None and self.config is not None
        return self.executor.run(
            ["docker", "--config", str(self.config), "--host", self.endpoint, *args],
            self.environment, timeout,
        )

    def engine(self) -> str:
        """Admit only the rootless Linux/amd64 engine this runner supports.
        
        Require systemd with cgroup v2 for resource limits and return the daemon's
        reported ID. This does not inspect individual container restrictions."""
        engine_info_json = checked_result(self.call("info", "--format", "{{json .}}"), "Docker engine inspection")
        try:
            engine_info = json.loads(engine_info_json)
        except (TypeError, ValueError) as error:
            raise OperationError("Docker engine returned malformed information") from error
        if not isinstance(engine_info, dict):
            raise OperationError("Docker engine returned a non-object description")
        engine_security_options = engine_info.get("SecurityOptions")
        if not isinstance(engine_security_options, list) or "name=rootless" not in engine_security_options:
            raise OperationError("Confined operations require a rootless Docker engine")
        if engine_info.get("CgroupDriver") != "systemd" or engine_info.get("CgroupVersion") != "2":
            raise OperationError("Rootless engine must enforce resource limits through systemd cgroup v2")
        if engine_info.get("OSType") != "linux":
            raise OperationError("Confined operations require a Linux Docker engine")
        if engine_info.get("Architecture") not in ("x86_64", "amd64"):
            raise OperationError("Pinned runtime requires a linux/amd64 Docker engine")
        engine_identity = engine_info.get("ID")
        if not isinstance(engine_identity, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9:._-]{0,127}", engine_identity):
            raise OperationError("Docker daemon ID is missing or invalid")
        return engine_identity

    def image(self, reference: str, expected_id: str, acquire_hint: str) -> str:
        """Check that the pinned image exists locally with the expected config ID.
        
        No download is attempted. Missing or mismatched images stop execution;
        a missing image includes the caller's explicit acquisition command."""
        image_inspection_result = self.call("image", "inspect", "--format", "{{.Id}}", reference)
        if image_inspection_result.status != 0:
            raise OperationError(f"Pinned image is missing on the selected engine. {acquire_hint}")
        actual_image_id = checked_result(image_inspection_result, "Pinned image inspection")
        if actual_image_id != expected_id:
            raise OperationError(f"Pinned image configuration differs: {actual_image_id!r}")
        return actual_image_id

    def acquire(self, reference: str, expected_id: str) -> str:
        """Pull the specified linux/amd64 image and verify its local config ID.
        
        This is the explicit networked acquisition path. Ordinary jobs only
        inspect already acquired images."""
        checked_result(
            self.call("pull", "--platform=linux/amd64", reference, timeout=ACQUISITION_TIMEOUT),
            "Pinned image acquisition",
        )
        return self.image(reference, expected_id, "Acquired image could not be verified")


@contextmanager
def docker_client(environ: dict[str, str], executor: Executor | None = None):
    """Admit the selected engine for the duration of an operation.

Refuse root callers, create a private client configuration, and remove it
when the context exits. This does not own or retire Docker containers."""
    if os.geteuid() == 0 or os.getegid() == 0:
        raise OperationError("Confined operations must be invoked by a non-root user")
    selected_context_name = required_context(environ)
    selected_docker_client = DockerClient(executor or NativeExecutor(), environ)
    selected_docker_client.context(selected_context_name)
    with tempfile.TemporaryDirectory(prefix="hello-world-docker-client-") as client_config_directory:
        selected_docker_client.config = Path(client_config_directory)
        (selected_docker_client.config / "config.json").write_text("{}\n", encoding="utf-8")
        selected_docker_client.engine()
        yield selected_docker_client

