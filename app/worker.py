"""Contained uv operations; never installed into the OpenAPI bridge wheel."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path


PROJECT = Path("/tmp/project")


def run(*arguments: str, cwd: Path | None = None, env: dict[str, str] | None = None) -> None:
    subprocess.run(list(arguments), cwd=cwd, env=env, check=True)


def project_inputs(include_lock: bool) -> None:
    PROJECT.mkdir()
    shutil.copyfile("/input/pyproject.toml", PROJECT / "pyproject.toml")
    if include_lock:
        shutil.copyfile("/input/uv.lock", PROJECT / "uv.lock")
    elif Path("/input/uv.lock").is_file():
        shutil.copyfile("/input/uv.lock", PROJECT / "uv.lock")


def locked_dependencies() -> None:
    project_inputs(include_lock=True)
    os.environ["UV_CACHE_DIR"] = "/cache"
    os.environ["UV_PROJECT_ENVIRONMENT"] = "/environment/.venv"
    run("uv", "sync", "--locked", "--no-install-project", "--no-python-downloads", cwd=PROJECT)
    run("uv", "pip", "check", "--python", "/environment/.venv/bin/python")


def installed_wheel() -> None:
    wheels = list(Path("/dist").glob("*.whl"))
    if len(wheels) != 1:
        raise ValueError("Package qualification needs exactly one built wheel")

    with zipfile.ZipFile(wheels[0]) as archive:
        members = archive.namelist()
    if "openapi_mcp_bridge/__main__.py" not in members:
        raise ValueError("Built wheel does not contain the executable bridge")
    if any(
        name.startswith("tests/") or name.startswith("openapi_mcp_bridge/data/")
        or name.startswith("openapi_mcp_bridge/fixtures/") or name.endswith("/demo_api.py")
        for name in members
    ):
        raise ValueError("Built wheel contains a demonstration fixture")

    os.environ["UV_CACHE_DIR"] = "/tmp/uv-cache"
    destination = "/output/venv"
    run("uv", "venv", "--no-python-downloads", destination)
    python = destination + "/bin/python"
    run("uv", "pip", "install", "--offline", "--no-deps", "--python", python, str(wheels[0]))

    dependencies = "/deps/.venv/lib/python3.12/site-packages"
    env = dict(os.environ, PYTHONPATH="/testroot:" + dependencies)
    installed_file = subprocess.check_output(
        [python, "-B", "-c", "import openapi_mcp_bridge; print(openapi_mcp_bridge.__file__)"],
        env=env, text=True,
    ).strip()
    if not Path(installed_file).is_relative_to(destination):
        raise ValueError("Package was not imported from the newly installed wheel")

    run(python, "-B", "-m", "unittest", "discover",
        "-s", "/testroot/tests/application", "-t", "/testroot",
        "-p", "test_*.py", "-v", env=env)


def main() -> int:
    if len(sys.argv) != 2 or sys.argv[1] not in {"lock", "dependencies", "installed"}:
        print("Usage: worker.py lock|dependencies|installed", file=sys.stderr)
        return 2
    action = sys.argv[1]
    if action == "lock":
        project_inputs(include_lock=False)
        os.environ["UV_CACHE_DIR"] = "/cache"
        run("uv", "lock", "--no-python-downloads", cwd=PROJECT)
        shutil.copyfile(PROJECT / "uv.lock", "/output/uv.lock")
    elif action == "dependencies":
        locked_dependencies()
    else:
        installed_wheel()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
