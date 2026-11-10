"""Select one local rootless Docker engine and run sanitized, bounded client calls.

No repository-specific facts, test choices or container-job lifecycle belong
here. Acquisition is explicit and separated from offline execution.
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
    """A selected operation could not be completed and verified."""


class Cancelled(OperationError):
    """The caller requested cancellation; cleanup still needs to run."""


@dataclass(frozen=True)
class Result:
    status: int
    output: str
    truncated: bool = False

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


class DockerClient:
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
            self.call("pull", "--platform=linux/amd64", reference, timeout=ACQUISITION_TIMEOUT),
            "Pinned image acquisition",
        )
        return self.image(reference, expected_id, "Acquired image could not be verified")


@contextmanager
def docker_client(environ: dict[str, str], executor: Executor | None = None):
    """Select one context and allocate a private, empty client configuration."""
    if os.geteuid() == 0 or os.getegid() == 0:
        raise OperationError("Confined operations must be invoked by a non-root user")
    context_name = required_context(environ)
    docker = DockerClient(executor or NativeExecutor(), environ)
    docker.context(context_name)
    with tempfile.TemporaryDirectory(prefix="hello-world-docker-client-") as directory:
        docker.config = Path(directory)
        (docker.config / "config.json").write_text("{}\n", encoding="utf-8")
        docker.engine()
        yield docker

