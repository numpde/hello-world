"""Host-side application commands: Docker orchestrates, no host uv or pip."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path

from infrastructure.commands import repository
from infrastructure.docker import client, finite_job


ROOT = Path(__file__).resolve().parents[1]
DOCKERFILE = ROOT / "containers/python-uv/Dockerfile"
UV_IMAGE = (
    "ghcr.io/astral-sh/uv:0.11.16@"
    "sha256:440fd6477af86a2f1b38080c539f1672cd22acb1b1a47e321dba5158ab08864d"
)
TOOLCHAIN_LABEL = "org.numpde.hello-world.app-toolchain-sha256"
ACTIONS = (
    "toolchain/acquire", "toolchain/check",
    "dependencies/lock/update", "dependencies/acquire", "dependencies/check",
    "tests/run", "package/build", "package/check",
)


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def image_definition() -> tuple[str, str]:
    fingerprint = digest(
        DOCKERFILE.read_bytes()
        + repository.CHECKER_IMAGE_REFERENCE.encode("utf-8")
        + UV_IMAGE.encode("utf-8")
    )
    return ("localhost/hello-world/openapi-mcp-toolchain:" + fingerprint[:20], fingerprint)


def state_directory() -> Path:
    checkout = digest(os.fsencode(ROOT.resolve()))[:16]
    return Path.home() / ".cache" / "hello-world" / "openapi-mcp-bridge" / checkout


def require_lock() -> Path:
    lock = ROOT / "uv.lock"
    if lock.is_symlink() or not lock.is_file():
        raise client.OperationError(
            "Missing regular uv.lock. Next: make app/dependencies/lock/update DOCKER_CONTEXT=<context>"
        )
    return lock


def toolchain_image(docker: client.DockerClient) -> str:
    tag, expected_hash = image_definition()
    result = docker.call("image", "inspect", "--format", "{{json .}}", tag)
    if result.status:
        raise client.OperationError(
            f"Pinned application toolchain is absent ({result.output.strip()[-250:]}). "
            "Next: make app/toolchain/acquire DOCKER_CONTEXT=<context>"
        )
    try:
        observed = json.loads(client.checked_result(result, "Toolchain image inspection"))
        identity = observed["Id"]
        labels = observed["Config"]["Labels"]
    except (KeyError, TypeError, ValueError) as error:
        raise client.OperationError("Docker returned malformed toolchain image metadata") from error
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", identity) or labels.get(TOOLCHAIN_LABEL) != expected_hash:
        raise client.OperationError("Toolchain image identity does not match the reviewed source pins")
    return identity


def acquire_toolchain(docker: client.DockerClient) -> str:
    docker.acquire(repository.CHECKER_IMAGE_REFERENCE, repository.CHECKER_IMAGE_ID)
    docker.acquire(UV_IMAGE, None)

    tag, definition_hash = image_definition()
    with tempfile.TemporaryDirectory(prefix="hello-world-toolchain-build-") as directory:
        identity_file = Path(directory) / "image-id"
        client.checked_result(docker.call(
            "build", "--pull=false", "--network=none", "--platform=linux/amd64",
            "--file", str(DOCKERFILE),
            "--tag", tag,
            "--label", f"{TOOLCHAIN_LABEL}={definition_hash}",
            "--build-arg", f"PYTHON_IMAGE={repository.CHECKER_IMAGE_REFERENCE}",
            "--build-arg", f"UV_IMAGE={UV_IMAGE}",
            "--iidfile", str(identity_file), str(DOCKERFILE.parent),
            timeout=300,
        ), "Offline application toolchain build")
        built_id = identity_file.read_text(encoding="ascii").strip()
        if toolchain_image(docker) != built_id:
            raise client.OperationError("Built toolchain identity differs from the recorded Docker image")
    return built_id


def run_job(
    docker: client.DockerClient,
    context: str,
    image: str,
    command: finite_job.Command,
    inputs: tuple[finite_job.ReadOnlyMount, ...] = (),
    outputs: tuple[finite_job.WritableMount, ...] = (),
    *,
    network: bool = False,
    timeout: int = 180,
    environment: tuple[str, ...] = (),
) -> None:
    result = finite_job.run_container(docker, context, finite_job.Job(
        image_id=image, command=command, mounts=inputs,
        writable_mounts=outputs, network="bridge" if network else "none",
        timeout=timeout, environment=environment, workdir="/",
    ))
    if result.output:
        print(result.output, end="" if result.output.endswith("\n") else "\n")
    if result.failure:
        print(result.failure, file=sys.stderr)
    for diagnostic in result.diagnostics:
        print(diagnostic, file=sys.stderr)
    if result.status:
        raise client.OperationError("Application container did not complete successfully")


def read(path: Path, target: str) -> finite_job.ReadOnlyMount:
    if path.is_symlink() or not (path.is_file() or path.is_dir()):
        raise client.OperationError(f"Missing regular application input: {path}")
    return finite_job.ReadOnlyMount(path, target)


def write(path: Path, target: str) -> finite_job.WritableMount:
    path.mkdir(parents=True, exist_ok=True)
    return finite_job.WritableMount(path, target)


def worker(action: str) -> finite_job.Command:
    return finite_job.Command("python", ("-B", "/worker/worker.py", action))


def uv_environment() -> tuple[str, ...]:
    return ("UV_NO_MANAGED_PYTHON=1", "UV_NO_PROGRESS=1", "HOME=/tmp")


def input_metadata(lock: bool) -> tuple[finite_job.ReadOnlyMount, ...]:
    sources = [read(ROOT / "pyproject.toml", "/input/pyproject.toml")]
    if lock:
        sources.append(read(require_lock(), "/input/uv.lock"))
    elif (ROOT / "uv.lock").exists():
        sources.append(read(require_lock(), "/input/uv.lock"))
    return tuple(sources)


def prepared_identity(image: str) -> dict[str, str]:
    return {
        "toolchain": image,
        "pyproject_sha256": digest((ROOT / "pyproject.toml").read_bytes()),
        "lock_sha256": digest(require_lock().read_bytes()),
    }


def require_prepared(state: Path, image: str) -> Path:
    prepared = state / "prepared.json"
    environment = state / "environment"
    try:
        identity = json.loads(prepared.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise client.OperationError(
            "Dependencies have not been prepared. Next: make app/dependencies/acquire DOCKER_CONTEXT=<context>"
        ) from error
    if identity != prepared_identity(image):
        raise client.OperationError(
            "Prepared environment does not match uv.lock, pyproject.toml and toolchain. "
            "Next: make app/dependencies/acquire DOCKER_CONTEXT=<context>"
        )
    venv_python = environment / ".venv/bin/python"
    if not (venv_python.is_symlink() or venv_python.is_file()):
        raise client.OperationError("Prepared Python environment is missing; reacquire dependencies")
    return environment


def qualify_dependencies(docker: client.DockerClient, context: str, image: str, state: Path) -> Path:
    env_dir = require_prepared(state, image)
    run_job(docker, context, image,
            finite_job.Command("uv", ("pip", "check", "--python", "/environment/.venv/bin/python")),
            inputs=(read(env_dir, "/environment"),),
            environment=uv_environment())
    return env_dir


def action(docker: client.DockerClient, context: str, name: str) -> None:
    if name == "toolchain/acquire":
        image = acquire_toolchain(docker)
        run_job(docker, context, image, finite_job.Command("uv", ("--version",)))
        return
    image = toolchain_image(docker)
    if name == "toolchain/check":
        run_job(docker, context, image, finite_job.Command("uv", ("--version",)))
        return

    state = state_directory()
    cache = state / "uv-cache"
    if name == "dependencies/lock/update":
        if (ROOT / "uv.lock").is_symlink():
            raise client.OperationError("Refusing linked uv.lock; remove the link before updating")
        with tempfile.TemporaryDirectory(prefix="hello-world-lock-") as temporary:
            output = Path(temporary)
            run_job(docker, context, image, worker("lock"),
                    inputs=input_metadata(False) + (read(ROOT / "app/worker.py", "/worker/worker.py"),),
                    outputs=(write(cache, "/cache"), finite_job.WritableMount(output, "/output")),
                    network=True, timeout=600, environment=uv_environment())
            result_lock = output / "uv.lock"
            if not result_lock.is_file() or not result_lock.read_bytes():
                raise client.OperationError("Dependency resolver returned no uv.lock")
            # Stage on the checkout filesystem for atomic replacement.
            destination = ROOT / "uv.lock"
            with tempfile.NamedTemporaryFile(prefix=".uv-lock-", dir=ROOT, delete=False) as temporary_file:
                staged = Path(temporary_file.name)
            try:
                shutil.copyfile(result_lock, staged)
                staged.chmod(0o644)
                staged.replace(destination)
            finally:
                staged.unlink(missing_ok=True)
        print("Updated uv.lock. Next: review and commit it, then make app/dependencies/acquire.")
        return

    if name == "dependencies/acquire":
        identity = prepared_identity(image)
        (state / "prepared.json").unlink(missing_ok=True)
        run_job(docker, context, image, worker("dependencies"),
                inputs=input_metadata(True) + (read(ROOT / "app/worker.py", "/worker/worker.py"),),
                outputs=(write(cache, "/cache"), write(state / "environment", "/environment")),
                network=True, timeout=900, environment=uv_environment())
        if identity != prepared_identity(image):
            raise client.OperationError("Application inputs changed during dependency acquisition; repeat it")
        state.mkdir(parents=True, exist_ok=True)
        temporary_stamp = state / "prepared.json.tmp"
        temporary_stamp.write_text(json.dumps(identity, sort_keys=True) + "\n", encoding="utf-8")
        temporary_stamp.replace(state / "prepared.json")
        print("Prepared locked dependencies. Next: make app/dependencies/check DOCKER_CONTEXT=<context>")
        return

    env_dir = qualify_dependencies(docker, context, image, state)
    if name == "dependencies/check":
        return
    if name == "tests/run":
        run_job(docker, context, image,
                finite_job.Command("/environment/.venv/bin/python", (
                    "-B", "-m", "unittest", "discover", "-s", "/testroot/tests/application",
                    "-t", "/testroot", "-p", "test_*.py", "-v")),
                inputs=(
                    read(ROOT / "src", "/testroot/src"),
                    read(ROOT / "tests", "/testroot/tests"),
                    read(env_dir, "/environment"),
                ),
                environment=("PYTHONPATH=/testroot:/testroot/src", "PYTHONDONTWRITEBYTECODE=1", "HOME=/tmp"),
                timeout=300)
        return
    if name == "package/build":
        with tempfile.TemporaryDirectory(prefix="hello-world-package-", dir=state) as temporary:
            output = Path(temporary)
            run_job(docker, context, image,
                    finite_job.Command("uv", (
                        "build", "--offline", "--no-build-isolation",
                        "--out-dir", "/output", "/project",
                    )),
                    inputs=(
                        read(ROOT / "src", "/project/src"),
                        read(ROOT / "pyproject.toml", "/project/pyproject.toml"),
                        read(env_dir, "/environment"),
                    ),
                    outputs=(write(cache, "/cache"), finite_job.WritableMount(output, "/output")),
                    environment=uv_environment() + ("UV_CACHE_DIR=/cache", "VIRTUAL_ENV=/environment/.venv"),
                    timeout=300)
            if len(list(output.glob("*.whl"))) != 1 or len(list(output.glob("*.tar.gz"))) != 1:
                raise client.OperationError("Expected exactly one wheel and one source distribution")
            target = state / "dist"
            if target.exists():
                shutil.rmtree(target)
            target.mkdir()
            for artifact in output.iterdir():
                shutil.copy2(artifact, target / artifact.name)
            print(f"Built artifacts: {target}")
        return
    if name == "package/check":
        dist = state / "dist"
        if not dist.is_dir() or len(list(dist.glob("*.whl"))) != 1:
            raise client.OperationError(
                "Missing exactly one wheel. Next: make app/package/build DOCKER_CONTEXT=<context>"
            )
        with tempfile.TemporaryDirectory(prefix="hello-world-installed-", dir=state) as temporary:
            run_job(docker, context, image, worker("installed"),
                    inputs=(
                        read(ROOT / "app/worker.py", "/worker/worker.py"),
                        read(ROOT / "tests", "/testroot/tests"),
                        read(env_dir, "/deps"),
                        read(dist, "/dist"),
                    ),
                    outputs=(finite_job.WritableMount(Path(temporary), "/output"),),
                    environment=uv_environment(), timeout=300)
        print("Installed wheel passed application tests without mounting package sources.")
        return
    raise client.OperationError(f"Unsupported application action: {name}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("action", choices=ACTIONS)
    args = parser.parse_args(argv)
    try:
        environment = dict(os.environ)
        context = client.required_context(environment)
        with client.docker_client(environment) as docker:
            action(docker, context, args.action)
    except (client.OperationError, OSError, TimeoutError) as error:
        print(f"APPLICATION OPERATION FAILURE: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
