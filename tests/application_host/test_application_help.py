"""OpenAPI bridge-specific Make discovery remains passive."""

from __future__ import annotations

import os
import subprocess
import unittest
from pathlib import Path

from infrastructure.commands.application import INHERITED_MAKE_STATE


ROOT = Path(__file__).resolve().parents[2]


class ApplicationHelpContracts(unittest.TestCase):
    def test_example_help_is_read_only_and_lists_its_native_operations(self):
        environment = {
            name: value
            for name, value in os.environ.items()
            if name not in INHERITED_MAKE_STATE
        }
        result = subprocess.run(
            ["make", "--no-print-directory", "-f", "project.mk", "help"],
            cwd=ROOT,
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        for target in (
            "app/toolchain/acquire", "app/dependencies/lock/update",
            "app/dependencies/acquire", "app/tests/run", "app/package/check",
            "app/demo/run",
        ):
            self.assertIn(target, result.stdout)
        self.assertNotIn("uv: command not found", result.stderr)


if __name__ == "__main__":
    unittest.main()
