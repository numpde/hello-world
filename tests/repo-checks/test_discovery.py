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
        discovery_options = dict(zip(
            repository.UNITTEST_DISCOVERY_ARGS[::2],
            repository.UNITTEST_DISCOVERY_ARGS[1::2],
            strict=True,
        ))
        test_root = ROOT / discovery_options["-s"]
        test_loader = unittest.TestLoader()
        discovered_suite = test_loader.discover(
            start_dir=str(test_root),
            top_level_dir=str(ROOT / discovery_options["-t"]),
            pattern=discovery_options["-p"],
        )

        def discovered_cases(suite):
            """Walk unittest's nested suites to collect the discovered test cases."""
            for candidate in suite:
                if isinstance(candidate, unittest.TestSuite):
                    yield from discovered_cases(candidate)
                else:
                    yield candidate

        # Every immediate directory under tests/ must be discovered.
        # A missing package marker must fail this check, not hide the directory.
        expected_directories = {
            directory.name
            for directory in test_root.iterdir()
            if directory.is_dir()
        }
        discovered_directories = {case.id().split(".")[1] for case in discovered_cases(discovered_suite)}

        self.assertFalse(test_loader.errors)
        self.assertEqual(discovered_directories, expected_directories)


if __name__ == "__main__":
    unittest.main()
