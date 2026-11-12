"""Exercise the boundary between common Make and an application Makefile."""

from __future__ import annotations

import contextlib
import io
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from infrastructure.commands import application


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


class ApplicationDispatchContracts(unittest.TestCase):
    def setUp(self):
        self.fixture = tempfile.TemporaryDirectory(prefix="application-dispatch-")
        self.addCleanup(self.fixture.cleanup)
        self.root = Path(self.fixture.name)

    def test_invalid_goal_never_reaches_application_make(self):
        (self.root / "project.mk").write_text("app/tests/run:\n\t@echo ok\n")
        for goal in ("", "repository/check", "app/../check", "app/test;echo bad", "app/a//b"):
            with self.subTest(goal=goal):
                with patch.object(application.subprocess, "run") as delegate:
                    with contextlib.redirect_stderr(io.StringIO()):
                        self.assertEqual(application.run_application(goal, root=self.root), 2)
                    delegate.assert_not_called()

    def test_missing_or_linked_project_makefile_fails_before_delegation(self):
        with patch.object(application.subprocess, "run") as delegate:
            with contextlib.redirect_stderr(io.StringIO()) as errors:
                self.assertEqual(application.run_application("app/tests/run", root=self.root), 2)
            self.assertIn("project.mk", errors.getvalue())
            delegate.assert_not_called()

            (self.root / "project.mk").symlink_to(self.root / "nonexistent.mk")
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(application.run_application("app/tests/run", root=self.root), 2)
            delegate.assert_not_called()

    def test_delegate_uses_literal_target_and_not_inherited_make_parser_state(self):
        (self.root / "project.mk").write_text("app/tests/run:\n\t@echo ok\n")
        inherited = {
            "MAKEFLAGS": "n", "GNUMAKEFLAGS": "r", "MFLAGS": "-k",
            "MAKEOVERRIDES": "a=b", "MAKEFILES": "/unexpected/Makefile",
            "MAKELEVEL": "3", "HELLO_WORLD_APP_TARGET": "app/other",
            "APP_API_BASE": "https://api.example.org",
        }
        with patch.object(
            application.subprocess, "run",
            return_value=subprocess.CompletedProcess(args=[], returncode=0),
        ) as delegated:
            self.assertEqual(
                application.run_application("app/tests/run", self.root, inherited), 0
            )
        (arguments, options) = delegated.call_args
        self.assertEqual(
            arguments[0], ["make", "--no-print-directory", "-f", "project.mk", "app/tests/run"]
        )
        self.assertEqual(options["cwd"], self.root)
        self.assertEqual(options["env"]["APP_API_BASE"], "https://api.example.org")
        for key in application.INHERITED_MAKE_STATE:
            self.assertNotIn(key, options["env"])

    def test_child_failure_and_interruption_status_survive(self):
        (self.root / "project.mk").write_text("app/tests/run:\n\t@echo ok\n")
        for (child_status, expected_status) in ((7, 7), (-2, 130)):
            with self.subTest(child_status=child_status):
                with patch.object(
                    application.subprocess, "run",
                    return_value=subprocess.CompletedProcess(args=[], returncode=child_status),
                ), contextlib.redirect_stderr(io.StringIO()) as errors:
                    self.assertEqual(application.run_application("app/tests/run", self.root), expected_status)
                self.assertIn("Next: make help", errors.getvalue())

    def test_help_and_dry_run_do_not_parse_application_makefile(self):
        shutil.copyfile(REPOSITORY_ROOT / "Makefile", self.root / "Makefile")
        (self.root / "project.mk").write_text(
            "PROBE := $(shell touch PARSED_PROJECT_MAKE)\n"
            "app/tests/run: ## Application tests.\n\t@echo actually-ran\n"
        )
        for arguments in (["help"], ["-n", "app/tests/run"]):
            with self.subTest(arguments=arguments):
                result = subprocess.run(
                    ["make", "--no-print-directory", *arguments],
                    cwd=self.root,
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
        shutil.copyfile(REPOSITORY_ROOT / "Makefile", self.root / "Makefile")
        source = REPOSITORY_ROOT / "infrastructure" / "commands" / "application.py"
        destination = self.root / "infrastructure" / "commands"
        destination.mkdir(parents=True)
        (destination.parent / "__init__.py").write_text("")
        (destination / "__init__.py").write_text("")
        shutil.copyfile(source, destination / "application.py")
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
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
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
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(common_dry_run.returncode, 0, common_dry_run.stderr)
        self.assertIn("infrastructure.commands.repository check", common_dry_run.stdout)
        self.assertNotIn("REPLACED COMMON CHECK", common_dry_run.stdout)


if __name__ == "__main__":
    unittest.main()
