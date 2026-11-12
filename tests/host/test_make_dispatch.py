"""Actual GNU Make help, dry-run and namespaced delegation qualification.

This suite runs only through make repository/make/check on a host with GNU Make.
It is not a dependency of the pinned stdlib-only Docker repository checker.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from infrastructure.commands import application


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


class MakeHostContracts(unittest.TestCase):
    def setUp(self):
        self.fixture = tempfile.TemporaryDirectory(prefix="make-host-contract-")
        self.addCleanup(self.fixture.cleanup)
        self.root = Path(self.fixture.name)
        # All three tests exercise the real dispatcher in a temporary checkout.
        # project.mk is deliberately staged only by tests that need it.
        shutil.copyfile(REPOSITORY_ROOT / "Makefile", self.root / "Makefile")
        source = REPOSITORY_ROOT / "infrastructure" / "commands" / "application.py"
        destination = self.root / "infrastructure" / "commands"
        destination.mkdir(parents=True)
        (destination.parent / "__init__.py").write_text("")
        (destination / "__init__.py").write_text("")
        shutil.copyfile(source, destination / "application.py")
        self.make_environment = {
            name: value
            for (name, value) in os.environ.items()
            if name not in application.INHERITED_MAKE_STATE
        }
        self.make_environment["PYTHONDONTWRITEBYTECODE"] = "1"

    def test_help_and_dry_run_do_not_parse_application_makefile(self):
        (self.root / "project.mk").write_text(
            "PROBE := $(shell touch PARSED_PROJECT_MAKE)\n"
            "app/tests/run: ## Application tests.\n\t@echo actually-ran\n"
        )
        for arguments in (["help"], ["-n", "app/tests/run"]):
            with self.subTest(arguments=arguments):
                result = subprocess.run(
                    ["make", "--no-print-directory", *arguments],
                    cwd=self.root,
                    env=self.make_environment,
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertFalse((self.root / "PARSED_PROJECT_MAKE").exists())
                self.assertNotIn("actually-ran", result.stdout)
                if arguments == ["help"]:
                    self.assertIn("app/tests/run", result.stdout)

    def test_root_delegation_is_non_overriding_and_performs_real_execution(self):
        (self.root / "project.mk").write_text(
            ".PHONY: app/tests/run repository/check\n"
            "app/tests/run: ## Run application tests.\n"
            "\t@printf '%s\\n' 'APP EXECUTED'\n"
            "repository/check:\n"
            "\t@printf '%s\\n' 'REPLACED COMMON CHECK'\n"
        )

        result = subprocess.run(
            ["make", "--no-print-directory", "app/tests/run",
             "HELLO_WORLD_APP_TARGET=app/different/run"],
            cwd=self.root,
            env=self.make_environment,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("APP EXECUTED", result.stdout)
        self.assertNotIn("REPLACED COMMON CHECK", result.stdout)

        common_dry_run = subprocess.run(
            ["make", "--no-print-directory", "-n", "repository/check"],
            cwd=self.root,
            env=self.make_environment,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(common_dry_run.returncode, 0, common_dry_run.stderr)
        self.assertIn("infrastructure.commands.repository check", common_dry_run.stdout)
        self.assertNotIn("REPLACED COMMON CHECK", common_dry_run.stdout)

    def test_repository_commands_do_not_write_bytecode_on_host(self):
        """Both repository targets must leave checkout Python source untouched."""
        source = REPOSITORY_ROOT / "infrastructure"
        destination = self.root / "infrastructure"

        shutil.copyfile(
            source / "commands" / "repository.py",
            destination / "commands" / "repository.py",
        )
        shutil.copytree(
            source / "docker",
            destination / "docker",
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
        )

        # Make the test sensitive to a missing -B even if the caller has
        # PYTHONDONTWRITEBYTECODE set in their shell.
        environment = self.make_environment.copy()
        environment.pop("PYTHONDONTWRITEBYTECODE", None)
        environment.pop("PYTHONPYCACHEPREFIX", None)

        for target in ("repository/check", "repository/image/acquire"):
            with self.subTest(target=target):
                result = subprocess.run(
                    ["make", "--no-print-directory", target],
                    cwd=self.root,
                    env=environment,
                    capture_output=True,
                    text=True,
                    check=False,
                )
                # Missing Docker context stops the host controller before any
                # daemon operation, but it imports its real Python modules.
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("Specify DOCKER_CONTEXT", result.stderr)
                self.assertEqual(list(destination.rglob("__pycache__")), [])
                self.assertEqual(list(destination.rglob("*.pyc")), [])

    def test_missing_application_makefile_has_actionable_failure(self):
        result = subprocess.run(
            ["make", "--no-print-directory", "app/tests/run"],
            cwd=self.root,
            env=self.make_environment,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Missing regular project.mk", result.stderr)


if __name__ == "__main__":
    unittest.main()
