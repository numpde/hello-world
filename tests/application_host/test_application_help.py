"""The Node example's Make discovery must remain passive on the host."""

from __future__ import annotations

import os
import subprocess
import unittest
from pathlib import Path

from infrastructure.commands.application import INHERITED_MAKE_STATE


ROOT = Path(__file__).resolve().parents[2]


class NodeMakeContracts(unittest.TestCase):
    def test_help_does_not_invoke_node_or_docker(self):
        environment = {
            name: value for name, value in os.environ.items()
            if name not in INHERITED_MAKE_STATE
        }
        result = subprocess.run(
            ["make", "--no-print-directory", "-f", "project.mk", "help"],
            cwd=ROOT, env=environment, capture_output=True, text=True, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        for target in (
            "app/image/acquire", "app/image/check", "app/dependencies/check",
            "app/tests/run", "app/package/build", "app/package/check",
        ):
            self.assertIn(target, result.stdout)
        self.assertNotIn("node: command not found", result.stderr)
        self.assertNotIn("docker: command not found", result.stderr)


if __name__ == "__main__":
    unittest.main()
