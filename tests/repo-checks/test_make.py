"""Check Make's advertised commands without starting Docker.

Help must be derived from maintained targets, and repository/check must
dispatch to the confined container operation rather than a host test runner.
"""

import re
import stat
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


class MakeContracts(unittest.TestCase):
    """Protect the public command list and the boundary between Make and Python."""
    def test_public_targets_have_one_source_of_help(self):
        """Every public phony target needs one help annotation; discovery must remain read-only."""
        makefile_text = (ROOT / "Makefile").read_text(encoding="utf-8")
        phony_lines = re.findall(r"^\.PHONY: (.+)$", makefile_text, re.MULTILINE)
        declared_targets = {
            name
            for line in phony_lines
            for name in line.split()
            if not name.startswith("__")
        }
        self.assertIn("__app_dispatch_force", " ".join(phony_lines))
        documented_targets = set(re.findall(r"^([a-z][a-z0-9/-]*): ## .+$", makefile_text, re.MULTILINE))
        self.assertEqual(declared_targets, documented_targets)
        self.assertIn(".DEFAULT_GOAL := help", makefile_text)
        self.assertNotIn("$(shell", makefile_text)
        self.assertIn("$(value DOCKER_CONTEXT)", makefile_text)
        # AWK field references must survive Make's dollar expansion.
        self.assertIn('$$1, $$2', makefile_text)
        self.assertNotIn("include project.mk", makefile_text)
        self.assertIn("app/%: override export HELLO_WORLD_APP_TARGET = $@", makefile_text)
        self.assertIn("app/%: __app_dispatch_force", makefile_text)

    def test_host_python_wrapper_is_the_only_make_interpreter(self):
        """Host Make recipes must not bypass the maintained Python policy."""
        wrapper = ROOT / "infrastructure" / "host-python"
        self.assertTrue(wrapper.is_file())
        self.assertTrue(wrapper.stat().st_mode & stat.S_IXUSR)

        # This checks host launch recipes. Python commands selected *inside*
        # Docker are covered by their own finite-job contracts.
        direct_python = re.compile(r"(?<![\\w./-])(?:python3(?:\\.\\d+)?|python)(?=\\s|$)")
        self.assertIsNotNone(direct_python.search("@python3 -m module"))
        self.assertIsNone(direct_python.search("@./infrastructure/host-python -m module"))

        for name in ("Makefile", "project.mk"):
            makefile = ROOT / name
            if not makefile.exists():
                continue
            for line_number, line in enumerate(makefile.read_text(encoding="utf-8").splitlines(), 1):
                if line.startswith("\t") and direct_python.search(line):
                    self.fail(f"{name}:{line_number}: call host Python through infrastructure/host-python")

    def test_repository_checks_have_only_one_container_entry_point(self):
        """Keep Make as a dispatcher so tests cannot accidentally acquire a host fallback."""
        makefile_text = (ROOT / "Makefile").read_text(encoding="utf-8")
        check_recipe = re.search(r"^repository/check:.*\n(\t.*\n)+", makefile_text, re.MULTILINE)
        self.assertIsNotNone(check_recipe)
        self.assertIn("infrastructure.commands.repository check", check_recipe.group())
        self.assertNotIn("unittest", check_recipe.group())
        self.assertNotIn("docker run", check_recipe.group())


if __name__ == "__main__":
    unittest.main()
