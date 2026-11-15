"""Catalog and example-owned path policy are application-independent."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from infrastructure.commands import example_integrations as examples


class ExampleCatalogContracts(unittest.TestCase):
    def test_two_named_examples_with_distinct_branches(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "catalog.toml"
            path.write_text(
                '[[examples]]\nname = "openapi-mcp-bridge"\n'
                'branch = "example/openapi-mcp-bridge"\n'
                '[[examples]]\nname = "https-egress-proxy"\n'
                'branch = "example/https-egress-proxy"\n'
            )
            catalog = examples.load_catalog(path)
            self.assertEqual(
                catalog,
                {
                    "openapi-mcp-bridge": "example/openapi-mcp-bridge",
                    "https-egress-proxy": "example/https-egress-proxy",
                },
            )

    def test_catalog_rejects_duplicate_names_and_unrecognized_keys(self):
        invalid = (
            '[[examples]]\nname = "dup"\nbranch = "example/dup"\n'
            '[[examples]]\nname = "dup"\nbranch = "example/other"\n',
            '[[examples]]\nname = "x"\nbranch = "example/x"\n'
            'command = "shell"\n',
            '[[examples]]\nname = "x"\nbranch = "../../host"\n',
            'examples = []\n',
        )
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "catalog.toml"
            for source in invalid:
                with self.subTest(source=source):
                    path.write_text(source)
                    with self.assertRaises(examples.IntegrationError):
                        examples.load_catalog(path)

    def test_nested_make_checks_discard_inherited_parser_state(self):
        """A parent make invocation cannot silently affect nested check parsing."""
        import os
        from types import SimpleNamespace
        from unittest.mock import patch

        inherited = {
            "MAKEFLAGS": "-n",
            "MAKEFILES": "/tmp/untrusted.mk",
            "GNUMAKEFLAGS": "--dry-run",
            "MAKEOVERRIDES": "other=value",
        }
        with (
            patch.dict(os.environ, inherited),
            patch.object(examples.subprocess, "run",
                         return_value=SimpleNamespace(returncode=0)) as invoked,
        ):
            examples._run_checks(Path("/tmp/disposable"), "generic")

        self.assertEqual(invoked.call_count, len(examples.CHECK_TARGETS))
        for call in invoked.call_args_list:
            environment = call.kwargs["env"]
            self.assertFalse(any(name in environment for name in inherited))
            self.assertTrue(any(argument == "DOCKER_CONTEXT=generic" for argument in call.args[0]))
            self.assertIn("-C", call.args[0])

    def test_application_paths_admitted_but_common_owned_paths_rejected(self):
        examples.admit_example_paths((
            "app/operations.py",
            "src/module.py",
            "tests/application_ops/test_app.py",
            "tests/application_host/test_help.py",
            "tests/application/test_contract.py",
            "containers/toolchain/Dockerfile",
            "project.mk",
            "package-lock.json",
            "uv.lock",
            "notes/005_https_egress_proxy_example.txt",
        ))
        for path in (
            "Makefile", "infrastructure/docker/finite_job.py",
            "tests/infrastructure/test_application_jobs.py",
            "tests/host/test_make_dispatch.py",
            "tests/repo-checks/test_make.py",
            "README.md", ".github/workflows/ci.yml",
        ):
            with self.subTest(path=path), self.assertRaises(examples.IntegrationError):
                examples.admit_example_paths((path,))


if __name__ == "__main__":
    unittest.main()
