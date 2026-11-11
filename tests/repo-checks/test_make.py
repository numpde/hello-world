"""Check Make's advertised commands without starting Docker.

Help must be derived from maintained targets, and repository/check must
dispatch to the confined container operation rather than a host test runner.
"""

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


class MakeContracts(unittest.TestCase):
    """Protect the public command list and the boundary between Make and Python."""
    def test_public_targets_have_one_source_of_help(self):
        """Every public phony target needs one help annotation; discovery must remain read-only."""
        makefile_text = (ROOT / "Makefile").read_text(encoding="utf-8")
        declared_targets = set(re.search(r"^\.PHONY: (.+)$", makefile_text, re.MULTILINE).group(1).split())
        documented_targets = set(re.findall(r"^([a-z][a-z0-9/-]*): ## .+$", makefile_text, re.MULTILINE))
        self.assertEqual(declared_targets, documented_targets)
        self.assertIn(".DEFAULT_GOAL := help", makefile_text)
        self.assertNotIn("$(shell", makefile_text)
        self.assertIn("$(value DOCKER_CONTEXT)", makefile_text)
        # AWK field references must survive Make's dollar expansion.
        self.assertIn('$$1, $$2', makefile_text)

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
