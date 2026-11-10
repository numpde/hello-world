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
        makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
        declared_targets = set(re.search(r"^\.PHONY: (.+)$", makefile, re.MULTILINE).group(1).split())
        documented_targets = set(re.findall(r"^([a-z][a-z0-9/-]*): ## .+$", makefile, re.MULTILINE))
        self.assertEqual(declared_targets, documented_targets)
        self.assertIn(".DEFAULT_GOAL := help", makefile)
        self.assertNotIn("$(shell", makefile)
        self.assertIn("$(value DOCKER_CONTEXT)", makefile)

    def test_repository_checks_have_only_one_container_entry_point(self):
        """Keep Make as a dispatcher so tests cannot accidentally acquire a host fallback."""
        makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
        recipe = re.search(r"^repository/check:.*\n(\t.*\n)+", makefile, re.MULTILINE)
        self.assertIsNotNone(recipe)
        self.assertIn("infrastructure.commands.repository check", recipe.group())
        self.assertNotIn("unittest", recipe.group())
        self.assertNotIn("docker run", recipe.group())


if __name__ == "__main__":
    unittest.main()
