"""Verify that unittest discovers every immediate test directory.

Discovery must use the command's actual selection options. A missing
package marker cannot hide a directory from the expected set.
"""

from __future__ import annotations

import unittest
from pathlib import Path

from infrastructure.commands import repository


ROOT = Path(__file__).resolve().parents[2]


class DiscoveryContracts(unittest.TestCase):
    """Compare filesystem test directories with unittest's loaded modules."""

    def test_discovery_covers_test_directories(self):
        """Compare all immediate test directories with modules loaded by the actual discovery settings."""
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
            """Walk unittest's nested suites to collect the discovered test cases."""
            for candidate in suite:
                if isinstance(candidate, unittest.TestSuite):
                    yield from cases(candidate)
                else:
                    yield candidate

        # Every immediate directory under tests/ is expected to be discovered.
        # A missing package marker must fail this check, not hide the directory.
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
