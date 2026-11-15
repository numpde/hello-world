"""Host stdlib controller for the Node proxy example; jobs remain in Docker."""

from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import sys
from pathlib import Path

from infrastructure.commands import job_execution
from infrastructure.docker import client, finite_job


ROOT = Path(__file__).resolve().parents[1]
NODE_IMAGE = (
    "docker.io/library/node:24.22.0-bookworm-slim@"
    "sha256:a5f4366e834695d5f363b920c104953a535d3a9864d1bdf74566a0ebe01daee5"
)
ACTIONS = (
    "image/acquire", "image/check", "dependencies/check",
    "tests/run", "package/build", "package/check",
)


def state_directory() -> Path:
    checkout = hashlib.sha256(os.fsencode(ROOT.resolve())).hexdigest()[:16]
    return Path.home() / ".cache" / "hello-world" / "https-egress-proxy" / checkout


def run_job(
    docker: client.DockerClient,
    context: str,
    image: str,
    command: finite_job.Command,
    inputs: tuple[finite_job.ReadOnlyMount, ...] = (),
    outputs: tuple[finite_job.WritableMount, ...] = (),
    *,
    owner: finite_job.TemporaryJobDirectory | None = None,
    timeout: int = 180,
) -> None:
    job = finite_job.Job(
        image_id=image,
        command=command,
        mounts=inputs,
        writable_mounts=outputs,
        network="none",
        workdir="/",
        timeout=timeout,
        environment=("HOME=/tmp", "npm_config_cache=/tmp/npm-cache"),
    )
    job_execution.run(docker, context, job, temporary_mount=owner)


def stage_package(destination: Path) -> None:
    destination.mkdir()
    for filename in ("package.json", "package-lock.json"):
        shutil.copyfile(ROOT / filename, destination / filename)
    shutil.copytree(ROOT / "src", destination / "src")


def action(docker: client.DockerClient, context: str, name: str) -> None:
    if name == "image/acquire":
        identity = docker.acquire(NODE_IMAGE, None)
        print(f"Acquired pinned Node image: {identity}")
        return
    image = docker.image(
        NODE_IMAGE,
        None,
        f"make app/image/acquire DOCKER_CONTEXT={context}",
    )
    if name == "image/check":
        run_job(docker, context, image, finite_job.Command("node", ("--version",)))
        return
    if name == "tests/run":
        run_job(
            docker, context, image,
            finite_job.Command("node", ("--test", "/tests/application/proxy.test.mjs")),
            inputs=(
                finite_job.read_only_mount(ROOT / "src", "/src"),
                finite_job.read_only_mount(ROOT / "tests/application", "/tests/application"),
            ),
        )
        return

    state = state_directory()
    state.mkdir(parents=True, exist_ok=True)
    if name == "dependencies/check":
        with finite_job.temporary_job_directory(prefix="node-lock-", parent=state) as owned:
            for filename in ("package.json", "package-lock.json"):
                copied = owned.path / filename
                shutil.copyfile(ROOT / filename, copied)
                copied.chmod(0o644)
            # The checker runs as unprivileged 65532, unlike jobs with an
            # explicitly writable mount. These are public npm manifests.
            owned.path.chmod(0o755)
            run_job(
                docker, context, image,
                finite_job.Command("node", ("/worker/worker.mjs", "dependencies")),
                inputs=(
                    finite_job.read_only_mount(owned.path, "/stage"),
                    finite_job.read_only_mount(ROOT / "app/worker.mjs", "/worker/worker.mjs"),
                ),
                owner=owned,
            )
        return
    if name == "package/build":
        with finite_job.temporary_job_directory(prefix="node-pack-", parent=state) as owned:
            stage_package(owned.path / "project")
            output = owned.path / "out"
            output.mkdir()
            run_job(
                docker, context, image,
                finite_job.Command("npm", (
                    "pack", "--offline", "--ignore-scripts", "--pack-destination", "/output", "/stage/project",
                )),
                inputs=(finite_job.read_only_mount(owned.path, "/stage"),),
                outputs=(finite_job.WritableMount(output, "/output"),),
                owner=owned,
            )
            archives = list(output.glob("*.tgz"))
            if len(archives) != 1:
                raise client.OperationError("Expected exactly one packed npm artifact")
            target = state / "dist"
            target.mkdir(exist_ok=True)
            for previous in target.iterdir():
                if previous.is_file():
                    previous.unlink()
            shutil.copy2(archives[0], target / archives[0].name)
            print(f"Built npm package: {target / archives[0].name}")
        return
    if name == "package/check":
        dist = state / "dist"
        if not dist.is_dir() or len(list(dist.glob("*.tgz"))) != 1:
            raise client.OperationError(
                f"Missing one npm package; next: make app/package/build DOCKER_CONTEXT={context}"
            )
        with finite_job.temporary_job_directory(prefix="node-installed-", parent=state) as owned:
            run_job(
                docker, context, image,
                finite_job.Command("node", ("/worker/worker.mjs", "installed")),
                inputs=(
                    finite_job.read_only_mount(ROOT / "app/worker.mjs", "/worker/worker.mjs"),
                    finite_job.read_only_mount(ROOT / "tests/application", "/tests/application"),
                    finite_job.read_only_mount(dist, "/dist"),
                ),
                outputs=(finite_job.WritableMount(owned.path, "/output"),),
                owner=owned,
            )
        print("Installed npm package passed tests without application source mounts")
        return
    raise client.OperationError(f"Unsupported application action: {name}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("action", choices=ACTIONS)
    arguments = parser.parse_args()
    try:
        environment = dict(os.environ)
        context = client.required_context(environment)
        with client.docker_client(environment) as docker:
            action(docker, context, arguments.action)
    except (client.OperationError, OSError, TimeoutError) as error:
        print(f"APPLICATION OPERATION FAILURE: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
