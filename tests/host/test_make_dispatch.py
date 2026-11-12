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
        # Exercise the real command dispatcher even for missing-project tests.
        # Do not stage project.mk: each test controls its own presence.
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

    def test_real_example_help_is_read_only_and_lists_commands(self):
        project = REPOSITORY_ROOT / "project.mk"
        if not project.is_file():
            self.skipTest("main has no application Makefile")
        result = subprocess.run(
            ["make", "--no-print-directory", "-f", "project.mk", "help"],
            cwd=REPOSITORY_ROOT,
            env=self.make_environment,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        for target in (
            "app/toolchain/acquire", "app/dependencies/lock/update",
            "app/dependencies/acquire", "app/tests/run", "app/package/check",
        ):
            self.assertIn(target, result.stdout)
        self.assertNotIn("uv: command not found", result.stderr)

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
