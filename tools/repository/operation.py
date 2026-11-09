"""Operate one Docker-confined repository check on an explicitly selected engine.

The host is only a coordinator. Check code runs in Docker. A private run
receipt survives if retirement cannot be proven, including ambiguous creates.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import signal
import stat
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass
from collections.abc import Callable
from pathlib import Path
from typing import Protocol


ROOT = Path(__file__).resolve().parents[2]
IMAGE = (
    "docker.io/library/python:3.12.16-slim-bookworm@"
    "sha256:623674a14ef93d052bb9b0de4dbf5f28eb578a35263838941cc8aa43213c1487"
)
IMAGE_ID = "sha256:37ccb536022bb36c4570856b55ebfa85bf07827e3c2fa3eba37f0dac62eebc05"
OPERATION_LABEL = "org.numpde.hello-world.repository-operation"
IDENTITY_LABEL = "org.numpde.hello-world.repository-owner"
PUBLIC_ROOTS = frozenset({"Makefile", "notes", "tools", "tests", ".github", ".gitignore", "README.md", "pyproject.toml"})
PRIVATE_ROOTS = frozenset({".git", ".agents", ".codex", ".aws", ".venv", ".pytest_cache", "__pycache__", ".env", "secrets", "build", "dist"})
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


def public_mounts(root: Path) -> list[str]:
    if any(ch in str(root) for ch in (",", "\n", "\r", "\x00")):
        raise OperationError("Repository path cannot be represented safely in Docker mounts")
    arguments = []
    discovered = {entry.name: entry for entry in root.iterdir()}
    unknown = sorted(discovered.keys() - PUBLIC_ROOTS - PRIVATE_ROOTS)
    if unknown:
        raise OperationError(f"Unclassified repository roots: {', '.join(unknown)}")
    for name in sorted(PUBLIC_ROOTS & discovered.keys()):
        entry = discovered[name]
        for directory, directories, files in os.walk(entry, followlinks=False):
            for child in [Path(directory), *(Path(directory) / part for part in directories + files)]:
                mode = child.lstat().st_mode
                if not (stat.S_ISREG(mode) or stat.S_ISDIR(mode)):
                    raise OperationError(f"Refusing link or non-file source: {child}")
                if (stat.S_ISDIR(mode) and mode & 0o005 != 0o005) or (
                    stat.S_ISREG(mode) and mode & 0o004 != 0o004
                ):
                    raise OperationError(f"Checker cannot read non-public source: {child}")
        mode = entry.lstat().st_mode
        if not (stat.S_ISREG(mode) or stat.S_ISDIR(mode)):
            raise OperationError(f"Refusing link or non-file source: {entry}")
        if (stat.S_ISDIR(mode) and mode & 0o005 != 0o005) or (
            stat.S_ISREG(mode) and mode & 0o004 != 0o004
        ):
            raise OperationError(f"Checker cannot read non-public source: {entry}")
        arguments.extend(["--mount", f"type=bind,source={entry},target=/workspace/{name},readonly"])
    for name in ("Makefile", "tools", "tests"):
        if name not in discovered:
            raise OperationError(f"Required repository input missing: {name}")
    return arguments


def checker_arguments(root: Path, token: str, owner: str, image_id: str) -> list[str]:
    return [
        "create", "--pull=never",
        "--label", f"{OPERATION_LABEL}={token}",
        "--label", f"{IDENTITY_LABEL}={owner}",
        "--network=none", "--read-only", "--cap-drop=ALL",
        "--security-opt=no-new-privileges:true", "--user=65532:65532",
        "--cpus=2", "--memory=512m", "--memory-swap=512m", "--pids-limit=64",
        "--ulimit=nofile=256:256", "--log-driver=none",
        "--tmpfs=/tmp:rw,nosuid,nodev,noexec,size=67108864",
        "--workdir=/workspace", "--env=HOME=/tmp", "--env=PYTHONDONTWRITEBYTECODE=1",
        "--env=HELLO_WORLD_IN_CHECKER=1", "--platform=linux/amd64",
        *public_mounts(root),
        "--entrypoint=python3", image_id,
        "-I", "-B", "/workspace/tools/repository/checks.py",
    ]


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
            raise OperationError("Repository checks require a rootless Docker engine")
        if description.get("CgroupDriver") != "systemd" or description.get("CgroupVersion") != "2":
            raise OperationError("Rootless engine must enforce resource limits through systemd cgroup v2")
        if description.get("OSType") != "linux":
            raise OperationError("Repository checks require a Linux Docker engine")
        if description.get("Architecture") not in ("x86_64", "amd64"):
            raise OperationError("Pinned checker image requires a linux/amd64 Docker engine")
        identity = description.get("ID")
        if not isinstance(identity, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9:._-]{0,127}", identity):
            raise OperationError("Docker daemon ID is missing or invalid")
        return identity

    def image(self) -> str:
        result = self.call("image", "inspect", "--format", "{{.Id}}", IMAGE)
        if result.status != 0:
            raise OperationError(
                "Pinned checker image is not available on the selected engine. "
                "Run: make repository/image/acquire DOCKER_CONTEXT=<context>"
            )
        identity = checked_result(result, "Checker image inspection")
        if identity != IMAGE_ID:
            raise OperationError(f"Pinned checker image configuration differs: {identity!r}")
        return identity


def verify_container_posture(docker: Docker, identifier: str, root: Path) -> None:
    """Require the engine's created container to retain the requested grants."""
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
        raise OperationError("Docker returned malformed checker confinement data") from error
    if not isinstance(host, dict) or not isinstance(config, dict) or not isinstance(mounts, list):
        raise OperationError("Docker returned malformed checker confinement types")
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
    expected_mounts = {
        (str(root / name), f"/workspace/{name}")
        for name in PUBLIC_ROOTS if (root / name).exists()
    }
    actual_mounts = set()
    for mount in mounts:
        if not isinstance(mount, dict) or mount.get("Type") != "bind" or mount.get("RW") is not False:
            failures.append("source mount type or writable permission")
            continue
        actual_mounts.add((mount.get("Source"), mount.get("Destination")))
    if actual_mounts != expected_mounts:
        failures.append("source mount inventory")
    if failures:
        raise OperationError(f"Docker did not apply required checker confinement: {', '.join(failures)}")


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


def operate(action: str, environ: dict[str, str], executor: Executor | None = None, root: Path = ROOT) -> int:
    if action not in {"check", "acquire"}:
        raise OperationError(f"Unknown repository operation: {action}")
    if os.geteuid() == 0 or os.getegid() == 0:
        raise OperationError("Repository operations must be invoked by a non-root user")
    context_name = required_context(environ)
    docker = Docker(executor or NativeExecutor(), environ)
    endpoint = docker.context(context_name)
    failure: str | None = None
    result_status = 0
    check_output = ""
    recovery: Path | None = None
    with tempfile.TemporaryDirectory(prefix="hello-world-docker-client-") as client_directory:
        docker.config = Path(client_directory)
        Path(client_directory, "config.json").write_text("{}\n", encoding="utf-8")
        docker.engine()
        if action == "acquire":
            output = checked_result(
                docker.call("pull", "--platform=linux/amd64", IMAGE, timeout=WORK_TIMEOUT),
                "Pinned image acquisition",
            )
            docker.image()
            print(f"Acquired checker image {IMAGE_ID} on Docker context {context_name}.")
            return 0
        image_id = docker.image()
        public_mounts(root)
        # Do not even begin a create if a public source is unsafe or missing.
        token = secrets.token_hex(16)
        owner = secrets.token_hex(16)
        operation = Path(tempfile.mkdtemp(prefix="hello-world-check-"))
        recovery = operation
        create_confirmed = False
        write_receipt(operation, context_name, endpoint, token, owner)
        try:
            arguments = checker_arguments(root, token, owner, image_id)
            arguments[1:1] = ["--cidfile", str(operation / "container.cid")]
            created = checked_result(docker.call(*arguments), "Checker container creation")
            receipt = operation / "container.cid"
            if not receipt.is_file() or receipt.is_symlink():
                raise OperationError("Docker did not write the private container identity receipt")
            identifier = receipt.read_text(encoding="ascii").strip()
            if not CONTAINER_ID.fullmatch(identifier) or created != identifier:
                raise OperationError("Docker container creation and CID receipt disagree")
            observed = owned_containers(docker, token, owner, image_id)
            if observed != [identifier]:
                raise OperationError("Docker created an unexpected container identity")
            create_confirmed = True
            verify_container_posture(docker, identifier, root)
            result = docker.call("start", "--attach", identifier, timeout=WORK_TIMEOUT)
            check_output = result.output
            if result.truncated:
                failure = f"Checker output exceeded {MAX_OUTPUT} bytes; tail retained"
            # Docker's attachment status is not an authoritative workload exit.
            finished = checked_result(
                docker.call("container", "inspect", "--format", "{{json .State}}", identifier),
                "Checker completion inspection",
            )
            try:
                state = json.loads(finished)
                exited = isinstance(state, dict) and state.get("Running") is False
                exitcode = state["ExitCode"] if exited else None
            except (ValueError, KeyError, TypeError):
                exitcode = None
            if type(exitcode) is not int or not 0 <= exitcode <= 255:
                raise OperationError("Checker stopped without a verified final exit status")
            if result.status != 0:
                failure = f"Docker attachment failed (exit {result.status})"
            if exitcode != 0:
                failure = f"Repository checks failed (container exit {exitcode})"
        except (OperationError, TimeoutError, OSError, KeyboardInterrupt, Cancelled) as error:
            failure = str(error) or error.__class__.__name__
        finally:
            # A failed or timed-out create can still complete on the daemon.
            # Quiesce the local client, then inspect engine state without
            # permitting a second terminal signal to cancel cleanup.
            with protected_cleanup():
                try:
                    cleanup(docker, token, owner, image_id)
                except (OperationError, TimeoutError, OSError) as error:
                    result_status = 1
                    print(
                        f"CLEANUP UNCONFIRMED: {error}. Retained recovery record: {operation}/recovery.txt",
                        file=sys.stderr,
                    )
                else:
                    if not create_confirmed:
                        result_status = 1
                        print(
                            "Create was not confirmed; retain the receipt even if discovery currently finds nothing. "
                            f"Recovery: {operation}/recovery.txt",
                            file=sys.stderr,
                        )
                    else:
                        try:
                            for child in operation.iterdir():
                                child.unlink()
                            operation.rmdir()
                            recovery = None
                        except OSError as error:
                            result_status = 1
                            print(f"Cannot remove operation record {operation}: {error}", file=sys.stderr)
        if check_output:
            print(check_output, end="" if check_output.endswith("\n") else "\n")
        if failure:
            print(f"REPOSITORY CHECK FAILURE: {failure}", file=sys.stderr)
            result_status = 1
        if recovery is not None:
            print(f"Retained operation: {recovery}", file=sys.stderr)
    return result_status


def main(argv: list[str] | None = None) -> int:
    actions = sys.argv[1:] if argv is None else argv
    if len(actions) != 1:
        print("Usage: operation.py check|acquire", file=sys.stderr)
        return 2
    def cancel(signum: int, _frame: object) -> None:
        raise Cancelled(f"Caller requested cancellation ({signal.Signals(signum).name})")

    signals = (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)
    previous = {number: signal.signal(number, cancel) for number in signals}
    try:
        return operate(actions[0], dict(os.environ))
    except (OperationError, OSError, TimeoutError, KeyboardInterrupt) as error:
        print(f"REPOSITORY OPERATION FAILURE: {error}", file=sys.stderr)
        return 2
    finally:
        for number, handler in previous.items():
            signal.signal(number, handler)


if __name__ == "__main__":
    raise SystemExit(main())
