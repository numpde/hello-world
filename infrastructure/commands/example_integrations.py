"""Qualify exact main/example revisions without changing either branch.

Only locally available Git objects are used. The example owns the app; main
owns the runner and the disposable merge. No acquisition happens implicitly.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
import tomllib
from dataclasses import dataclass
from pathlib import Path

from infrastructure.commands.application import INHERITED_MAKE_STATE


ROOT = Path(__file__).resolve().parents[2]
SHA = re.compile(r"[0-9a-f]{40}\Z")
NAME = re.compile(r"[a-z0-9][a-z0-9-]{0,62}\Z")
APPLICATION_FILES = frozenset({
    ".gitignore", "project.mk", "pyproject.toml", "uv.lock",
    "package.json", "package-lock.json", "notes/005_https_egress_proxy_example.txt",
})
APPLICATION_DIRECTORIES = (
    "app/", "src/", "containers/", "tests/application/",
    "tests/application_ops/", "tests/application_host/",
)
CHECK_TARGETS = (
    "repository/make/check",
    "repository/check",
    "repository/application/make/check",
    "repository/application/check",
)


class IntegrationError(RuntimeError):
    """Example revision cannot safely or successfully qualify."""


@dataclass(frozen=True)
class Plan:
    name: str
    branch: str
    base: str
    example: str


def _git(root: Path, *arguments: str, timeout: int = 30) -> str:
    """Read or mutate only the selected local repository under explicit Git policy."""
    environment = {
        name: os.environ[name]
        for name in ("HOME", "PATH", "LANG", "LC_ALL", "TMPDIR")
        if name in os.environ
    }
    environment["GIT_TERMINAL_PROMPT"] = "0"
    try:
        completed = subprocess.run(
            ["git", "-C", str(root),
             "-c", "core.hooksPath=/dev/null",
             "-c", "commit.gpgsign=false",
             "-c", "user.name=hello-world qualification",
             "-c", "user.email=qualification@invalid", *arguments],
            env=environment, stdin=subprocess.DEVNULL,
            capture_output=True, text=True, timeout=timeout, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise IntegrationError(f"Local Git operation could not finish: {error}") from error
    if completed.returncode:
        raise IntegrationError(
            f"Local Git {arguments[0]} failed (exit {completed.returncode}): "
            f"{completed.stderr.strip()[-800:]}"
        )
    return completed.stdout


def parse_catalog(source: str) -> dict[str, str]:
    """Admit only named branches and no executable commands from catalog data."""
    try:
        parsed = tomllib.loads(source)
    except ValueError as error:
        raise IntegrationError("Cannot read the maintained example catalog") from error
    if not isinstance(parsed, dict) or set(parsed) != {"examples"}:
        raise IntegrationError("Catalog must contain only [[examples]]")
    entries = parsed["examples"]
    if not isinstance(entries, list) or not entries:
        raise IntegrationError("Catalog must declare at least one example")

    result = {}
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != {"name", "branch"}:
            raise IntegrationError("Each catalog entry needs exactly name and branch")
        name, branch = entry["name"], entry["branch"]
        if (type(name) is not str or NAME.fullmatch(name) is None
                or type(branch) is not str or branch != f"example/{name}"
                or name in result):
            raise IntegrationError("Example catalog has invalid or duplicate name/branch")
        result[name] = branch
    return result


def load_catalog(path: Path) -> dict[str, str]:
    """Parse a catalog file for source-level and unit qualification."""
    try:
        source = path.read_text(encoding="utf-8")
    except OSError as error:
        raise IntegrationError("Cannot read the maintained example catalog") from error
    return parse_catalog(source)


def admit_example_paths(paths) -> None:
    """Reject any application revision that changes main-owned paths."""
    rejected = sorted(
        path for path in paths
        if (path not in APPLICATION_FILES
            and not any(path.startswith(prefix) for prefix in APPLICATION_DIRECTORIES))
        or path.startswith("/")
        or "/../" in path
    )
    if rejected:
        raise IntegrationError(f"Example changed main-owned paths: {rejected}")


def _commit(root: Path, identifier: str) -> str:
    if SHA.fullmatch(identifier) is None:
        raise IntegrationError("Commit selection requires an exact 40-character SHA")
    observed = _git(root, "rev-parse", "--verify", f"{identifier}^{{commit}}").strip()
    if observed != identifier:
        raise IntegrationError("Selected commit identity changed")
    return identifier


def plan(
    root: Path,
    *,
    base: str,
    selected: str | None = None,
    example_head: str | None = None,
) -> tuple[Plan, ...]:
    """Resolve committed example refs and audit their application-only changes."""
    main_sha = _commit(root, base)
    # Bind the catalog to the selected main commit, never an edited checkout.
    catalog = parse_catalog(_git(root, "show", f"{main_sha}:examples/catalog.toml"))
    if selected is not None and selected not in catalog:
        raise IntegrationError(f"Unknown example {selected!r}; use a catalog name")
    if example_head is not None and selected is None:
        raise IntegrationError("EXAMPLE_HEAD requires an explicit EXAMPLE")

    selected_names = (selected,) if selected is not None else tuple(catalog)
    result = []
    for name in selected_names:
        branch = catalog[name]
        if example_head is None:
            remote_ref = f"refs/remotes/origin/{branch}"
            resolved = _git(root, "rev-parse", "--verify", f"{remote_ref}^{{commit}}").strip()
            sha = _commit(root, resolved)
        else:
            sha = _commit(root, example_head)
        ancestor = _git(root, "merge-base", main_sha, sha).strip()
        changed = _git(root, "diff", "--name-only", "-z", ancestor, sha).split("\0")
        admit_example_paths(path for path in changed if path)
        if not changed or "project.mk" not in changed:
            raise IntegrationError(f"Example {name!r} has no application project.mk in its owned diff")
        result.append(Plan(name, branch, main_sha, sha))
    return tuple(result)



def _run_checks(checkout: Path, context: str) -> None:
    """Run the same public Make targets as a normal operator.

    This is a compatibility gate, not native package acquisition or full
    installed-artifact qualification. No target here is allowed to download.
    """
    child_environment = {
        name: value for name, value in os.environ.items()
        if name not in INHERITED_MAKE_STATE
    }
    for goal in CHECK_TARGETS:
        print(f"Checking {goal} in {checkout}", flush=True)
        try:
            completed = subprocess.run(
                ["make", "--no-print-directory", "-C", str(checkout), goal,
                 f"DOCKER_CONTEXT={context}"],
                env=child_environment, stdin=subprocess.DEVNULL,
                check=False, timeout=600,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise IntegrationError(f"{goal} could not finish: {error}") from error
        if completed.returncode:
            raise IntegrationError(f"{goal} failed (exit {completed.returncode})")


def execute(
    root: Path,
    plans: tuple[Plan, ...],
    context: str,
    *,
    check=None,
) -> None:
    """Merge each exact revision on a temporary named branch.

    A successful check retires its worktree and exact qualification branch.
    Failures retain both for diagnosis and potential container recovery.
    """
    run = _run_checks if check is None else check
    failures = []
    for selection in plans:
        private = Path(tempfile.mkdtemp(prefix="hello-world-integration-"))
        checkout = private / "checkout"
        owner_branch = f"hello-world/qualification/{private.name}"
        try:
            _git(root, "worktree", "add", "-b", owner_branch, str(checkout), selection.example)
            _git(
                checkout, "merge", "--no-ff", "--no-edit",
                "-m", "Disposable main/example qualification", selection.base,
                timeout=60,
            )
            changed = _git(
                checkout, "diff", "--name-only", "-z", selection.base, "HEAD",
            ).split("\0")
            admit_example_paths(item for item in changed if item)
            print(
                f"Checking {selection.name}: main={selection.base} "
                f"example={selection.example}; combined={_git(checkout, 'rev-parse', 'HEAD^{tree}').strip()}",
                flush=True,
            )
            run(checkout, context)
            _git(root, "worktree", "remove", "--force", str(checkout), timeout=60)
            _git(root, "branch", "-D", owner_branch)
            private.rmdir()
            print(f"Compatibility checks passed: {selection.name}", flush=True)
        except IntegrationError as error:
            print(
                f"Integration not completed for {selection.name}; "
                f"retained checkout: {checkout}; qualification branch: {owner_branch}",
                file=sys.stderr,
            )
            failures.append(
                f"{selection.name}: {error} "
                f"(retained checkout {checkout}; branch {owner_branch})"
            )
        except BaseException:
            print(
                f"Integration interrupted for {selection.name}; "
                f"retained checkout: {checkout}; qualification branch: {owner_branch}",
                file=sys.stderr,
            )
            raise

    if failures:
        raise IntegrationError(
            "Independent example checks failed:\n" + "\n".join(failures)
        )


def main(argv: list[str] | None = None) -> int:
    """Select exact commits from literal Make command-line inputs."""
    from infrastructure.docker.client import OperationError, required_context

    arguments = sys.argv[1:] if argv is None else argv
    if arguments not in (["plan"], ["check"]):
        print("Use examples/plan or examples/check with BASE=<full commit SHA>", file=sys.stderr)
        return 2
    environment = dict(os.environ)
    try:
        context = required_context(environment) if arguments == ["check"] else None
        base = environment.get("HELLO_WORLD_EXAMPLE_BASE", "")
        selected = environment.get("HELLO_WORLD_EXAMPLE")
        override = environment.get("HELLO_WORLD_EXAMPLE_HEAD")
        selections = plan(ROOT, base=base, selected=selected, example_head=override)
        for item in selections:
            print(f"Planned {item.name}: main={item.base} example={item.example}")
        if context is not None:
            execute(ROOT, selections, context)
    except (IntegrationError, OperationError) as error:
        print(f"EXAMPLE INTEGRATION FAILURE: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
