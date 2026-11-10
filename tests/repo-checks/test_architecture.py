"""Verify the deliberately one-way infrastructure dependency graph.

Source ownership follows notes/001 and 003. These restrictions protect
interfaces between separate responsibilities, not arbitrary coding style.
"""

from __future__ import annotations

import ast
import importlib.util
import unittest
from pathlib import Path

from infrastructure.commands import repository


ROOT = Path(__file__).resolve().parents[2]
RESTRICTIONS = {
    "infrastructure/docker/client.py": ("infrastructure.commands", "infrastructure.testing", "tests"),
    "infrastructure/docker/finite_job.py": ("infrastructure.commands", "infrastructure.testing", "tests"),
    "infrastructure/testing/unittest_runner.py": ("infrastructure.docker", "infrastructure.commands", "tests"),
    "infrastructure/commands/repository.py": ("infrastructure.testing", "tests"),
}


def imports(source: Path):
    package = ".".join(source.parent.relative_to(ROOT).parts)
    for node in ast.walk(ast.parse(source.read_text(encoding="utf-8"), filename=str(source))):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield alias.name
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                yield importlib.util.resolve_name("." * node.level + (node.module or ""), package)
            elif node.module:
                yield node.module


class ArchitectureContracts(unittest.TestCase):
    def test_implementation_boundaries(self):
        for relative_path, forbidden in RESTRICTIONS.items():
            source = ROOT / relative_path
            with self.subTest(source=relative_path):
                self.assertTrue(source.is_file())
                for imported in imports(source):
                    for prefix in forbidden:
                        self.assertFalse(
                            imported == prefix or imported.startswith(prefix + "."),
                            f"{relative_path} imports forbidden dependency {imported}",
                        )

    def test_discovery_covers_test_directories(self):
        options = dict(zip(
            repository.UNITTEST_DISCOVERY_ARGS[::2],
            repository.UNITTEST_DISCOVERY_ARGS[1::2],
            strict=True,
        ))
        tests_root = ROOT / options["-s"]
        loader = unittest.TestLoader()
        suite = loader.discover(
            start_dir=str(tests_root),
            top_level_dir=str(ROOT / options["-t"]),
            pattern=options["-p"],
        )

        def cases(suite):
            for candidate in suite:
                if isinstance(candidate, unittest.TestSuite):
                    yield from cases(candidate)
                else:
                    yield candidate

        expected = {
            directory.name
            for directory in tests_root.iterdir()
            if directory.is_dir()
        }
        discovered = {case.id().split(".")[1] for case in cases(suite)}

        self.assertFalse(loader.errors)
        self.assertEqual(discovered, expected)

if __name__ == "__main__":
    unittest.main()
