"""Verify that common tests are discovered without importing optional app tests."""

from __future__ import annotations

import unittest
from pathlib import Path

from infrastructure.commands import repository


ROOT = Path(__file__).resolve().parents[2]
COMMON_TEST_DIRS = {"infrastructure", "repo-checks"}
APPLICATION_TEST_DIRS = {"application"}


class DiscoveryContracts(unittest.TestCase):
    """Keep the stdlib-only lane complete while application tests remain separate."""

    def test_discovery_covers_maintained_common_suites(self):
        """Every common directory is selected; other test directories need a known owner."""
        args = repository.UNITTEST_DISCOVERY_ARGS
        start_directories = [args[i + 1] for (i, name) in enumerate(args) if name == "-s"]
        top_level_dir = args[args.index("-t") + 1]
        pattern = args[args.index("-p") + 1]

        expected_common_dirs = {Path(directory).name for directory in start_directories}
        self.assertEqual(expected_common_dirs, COMMON_TEST_DIRS)
        self.assertEqual(len(start_directories), len(COMMON_TEST_DIRS))

        actual_directories = {
            entry.name for entry in (ROOT / "tests").iterdir()
            if entry.is_dir() and entry.name != "__pycache__"
        }
        self.assertEqual(actual_directories - APPLICATION_TEST_DIRS, COMMON_TEST_DIRS)

        loader = unittest.TestLoader()
        suites = [
            loader.discover(
                start_dir=str(ROOT / directory),
                top_level_dir=str(ROOT / top_level_dir),
                pattern=pattern,
            )
            for directory in start_directories
        ]

        def test_cases(suite):
            for entry in suite:
                if isinstance(entry, unittest.TestSuite):
                    yield from test_cases(entry)
                else:
                    yield entry

        observed_directories = {
            case.id().split(".")[1]
            for suite in suites
            for case in test_cases(suite)
        }

        self.assertFalse(loader.errors)
        self.assertEqual(observed_directories, COMMON_TEST_DIRS)


if __name__ == "__main__":
    unittest.main()
