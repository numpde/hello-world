"""Real Git integration and worktree-retirement checks, with no Docker."""

from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from infrastructure.commands import example_integrations as examples


class GitIntegrationContracts(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="hello-world-git-fixture-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "repo"
        self.root.mkdir()
        self.git("init", "-q", "--initial-branch=main")
        self.git("config", "user.email", "fixture@invalid")
        self.git("config", "user.name", "Test Fixture")
        (self.root / "Makefile").write_text("help:\n\t@echo main\n")
        (self.root / "examples").mkdir()
        (self.root / "examples" / "catalog.toml").write_text(
            '[[examples]]\nname = "small-app"\nbranch = "example/small-app"\n'
        )
        self.git("add", ".")
        self.git("commit", "-q", "-m", "base")
        self.base = self.git("rev-parse", "HEAD").strip()
        self.git("switch", "-q", "-c", "example/small-app")
        (self.root / "app").mkdir()
        (self.root / "app" / "worker.py").write_text("print('sample')\n")
        self.git("add", ".")
        self.git("commit", "-q", "-m", "app")
        self.example = self.git("rev-parse", "HEAD").strip()
        self.git("update-ref", "refs/remotes/origin/example/small-app", self.example)
        self.git("switch", "-q", "main")

    def git(self, *arguments):
        operation = subprocess.run(
            ["git", "-C", str(self.root), *arguments],
            capture_output=True, text=True, timeout=10, check=False,
        )
        self.assertEqual(operation.returncode, 0, operation.stderr)
        return operation.stdout

    def test_plan_uses_exact_commits_without_fetch_or_mutating_branches(self):
        plan, = examples.plan(self.root, base=self.base, selected="small-app")
        self.assertEqual((plan.base, plan.example), (self.base, self.example))
        self.assertEqual(self.git("rev-parse", "main").strip(), self.base)
        self.assertEqual(self.git("rev-parse", "example/small-app").strip(), self.example)
        self.assertEqual(len(list((self.root / "app").glob("*"))) if (self.root / "app").exists() else 0, 0)

    def test_common_owned_application_change_is_refused_before_worktree(self):
        self.git("switch", "-q", "example/small-app")
        (self.root / "Makefile").write_text("help:\n\t@echo override\n")
        self.git("add", ".")
        self.git("commit", "-q", "-m", "bad override")
        changed = self.git("rev-parse", "HEAD").strip()
        self.git("switch", "-q", "main")
        with self.assertRaisesRegex(examples.IntegrationError, "Makefile"):
            examples.plan(self.root, base=self.base, selected="small-app", example_head=changed)

    def test_disposable_merge_checks_combined_checkout_and_retires_it(self):
        plan, = examples.plan(self.root, base=self.base, selected="small-app")
        seen = []

        def check(checkout, _context):
            self.assertTrue((checkout / "app" / "worker.py").is_file())
            self.assertTrue((checkout / "examples" / "catalog.toml").is_file())
            seen.append(checkout)

        examples.execute(self.root, (plan,), "generic", check=check)
        self.assertEqual(len(seen), 1)
        self.assertFalse(seen[0].exists())
        self.assertEqual(self.git("rev-parse", "main").strip(), self.base)

    def test_failed_check_retains_integration_checkout(self):
        plan, = examples.plan(self.root, base=self.base, selected="small-app")
        seen = []

        def fail(checkout, _context):
            seen.append(checkout)
            raise examples.IntegrationError("injected qualification failure")

        with self.assertRaisesRegex(examples.IntegrationError, "injected qualification failure"):
            examples.execute(self.root, (plan,), "generic", check=fail)

        self.assertTrue(seen[0].is_dir())
        self.assertTrue((seen[0] / "app" / "worker.py").is_file())
        self.git("worktree", "remove", "--force", str(seen[0]))
        seen[0].parent.rmdir()


if __name__ == "__main__":
    unittest.main()
