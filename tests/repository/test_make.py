"""Static checks of Make's public command contract; execution is Docker-only."""

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


class MakeContracts(unittest.TestCase):
    def test_public_targets_have_one_source_of_help(self):
        makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
        declared = set(re.search(r"^\.PHONY: (.+)$", makefile, re.MULTILINE).group(1).split())
        annotated = set(re.findall(r"^([a-z][a-z0-9/-]*): ## .+$", makefile, re.MULTILINE))
        self.assertEqual(declared, annotated)
        self.assertEqual(declared, {"help", "repository/check", "repository/image/acquire"})
        self.assertIn(".DEFAULT_GOAL := help", makefile)
        self.assertNotIn("$(shell", makefile)
        self.assertIn("$(value DOCKER_CONTEXT)", makefile)

    def test_repository_checks_have_only_one_container_entry_point(self):
        makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
        recipe = re.search(r"^repository/check:.*\n(\t.*\n)+", makefile, re.MULTILINE)
        self.assertIsNotNone(recipe)
        self.assertIn("tools/repository/operation.py check", recipe.group())
        self.assertNotIn("unittest", recipe.group())
        self.assertNotIn("docker run", recipe.group())


if __name__ == "__main__":
    unittest.main()
