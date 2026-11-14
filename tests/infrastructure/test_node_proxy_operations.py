"""The Node example uses generic finite jobs without acquiring during checks."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app import operations as app
from infrastructure.docker import client, finite_job


IMAGE = "sha256:" + "a" * 64
SUCCESS = finite_job.Outcome(0, "", None, (), None)


class FakeDocker:
    def image(self, *_args):
        return IMAGE

    def acquire(self, *_args):
        return IMAGE


class NodeProxyOperationsContracts(unittest.TestCase):
    def setUp(self):
        fixture = tempfile.TemporaryDirectory(prefix="node-ops-contract-")
        self.addCleanup(fixture.cleanup)
        self.root = Path(fixture.name) / "checkout"
        self.root.mkdir()
        (self.root / "src").mkdir()
        (self.root / "src" / "https_egress_proxy.mjs").write_text("export const ready = true;\n")
        (self.root / "tests/application").mkdir(parents=True)
        (self.root / "tests/application/proxy.test.mjs").write_text("export {};\n")
        (self.root / "app").mkdir()
        (self.root / "app/worker.mjs").write_text("export {};\n")
        (self.root / "package.json").write_text('{"name":"example"}\n')
        (self.root / "package-lock.json").write_text('{"lockfileVersion":3}\n')
        (self.root / ".idea").mkdir()
        (self.root / ".idea" / "private").write_text("private")
        self.state = Path(fixture.name) / "cache"
        for replacement in (
            patch.object(app, "ROOT", self.root),
            patch.object(app, "state_directory", return_value=self.state),
        ):
            replacement.start()
            self.addCleanup(replacement.stop)
        self.docker = FakeDocker()

    def test_direct_tests_run_without_network_or_writable_source(self):
        observed = []
        with patch.object(finite_job, "run_container",
                          side_effect=lambda _docker, _context, job: observed.append(job) or SUCCESS):
            app.action(self.docker, "generic", "tests/run")
        self.assertEqual(len(observed), 1)
        self.assertEqual(observed[0].network, "none")
        self.assertEqual(observed[0].writable_mounts, ())
        self.assertEqual({mount.target for mount in observed[0].mounts},
                         {"/src", "/tests/application"})
        self.assertFalse(any(".idea" in str(mount.source) for mount in observed[0].mounts))

    def test_lock_check_uses_a_private_mounted_copy_and_offline_npm(self):
        observed = []

        def check_manifest_permissions(_docker, _context, job):
            staged = next(m.source for m in job.mounts if m.target == "/stage")
            self.assertTrue(staged.stat().st_mode & 0o005 == 0o005)
            for filename in ("package.json", "package-lock.json"):
                self.assertTrue((staged / filename).stat().st_mode & 0o004)
            observed.append(job)
            return SUCCESS

        with patch.object(finite_job, "run_container", side_effect=check_manifest_permissions):
            app.action(self.docker, "generic", "dependencies/check")
        self.assertEqual(len(observed), 1)
        self.assertEqual(observed[0].network, "none")
        self.assertEqual(observed[0].command.arguments[-1], "dependencies")
        self.assertEqual({mount.target for mount in observed[0].mounts},
                         {"/stage", "/worker/worker.mjs"})
        self.assertEqual(list(self.state.glob("node-lock-*")), [])

    def test_pack_stages_only_selected_application_files(self):
        stage = self.state / "project"
        self.state.mkdir()
        app.stage_package(stage)
        self.assertEqual({item.name for item in stage.iterdir()},
                         {"package.json", "package-lock.json", "src"})
        self.assertFalse((stage / ".idea").exists())

    def test_package_build_publishes_one_tarball_after_contained_work(self):
        observed = []

        def package_work(_docker, _context, job):
            observed.append(job)
            output = next(m.source for m in job.writable_mounts if m.target == "/output")
            (output / "example-0.1.0.tgz").write_bytes(b"artifact")
            return SUCCESS

        with patch.object(finite_job, "run_container", side_effect=package_work):
            app.action(self.docker, "generic", "package/build")
        self.assertEqual(len(observed), 1)
        self.assertEqual(observed[0].network, "none")
        self.assertEqual({mount.target for mount in observed[0].mounts}, {"/stage"})
        self.assertEqual({mount.target for mount in observed[0].writable_mounts}, {"/output"})
        self.assertEqual((self.state / "dist/example-0.1.0.tgz").read_bytes(), b"artifact")
        self.assertEqual(list(self.state.glob("node-pack-*")), [])

    def test_package_check_uses_only_tarball_and_tests_not_application_source(self):
        dist = self.state / "dist"
        dist.mkdir(parents=True)
        (dist / "example-0.1.0.tgz").write_bytes(b"artifact")
        observed = []
        with patch.object(finite_job, "run_container",
                          side_effect=lambda _docker, _context, job: observed.append(job) or SUCCESS):
            app.action(self.docker, "generic", "package/check")
        self.assertEqual(len(observed), 1)
        self.assertEqual(observed[0].network, "none")
        self.assertEqual({mount.target for mount in observed[0].mounts},
                         {"/worker/worker.mjs", "/tests/application", "/dist"})
        self.assertEqual({mount.target for mount in observed[0].writable_mounts}, {"/output"})
        self.assertNotIn("/src", {mount.target for mount in observed[0].mounts})
        self.assertEqual(list(self.state.glob("node-installed-*")), [])

    def test_refused_removal_preserves_mounted_package_check_output(self):
        dist = self.state / "dist"
        dist.mkdir(parents=True)
        (dist / "example-0.1.0.tgz").write_bytes(b"artifact")
        recovery = self.state / "owned-operation"
        recovery.mkdir()
        (recovery / "recovery.txt").write_text("owned container\n")
        retained = []

        def refused(_docker, _context, job):
            directory = next(m.source for m in job.writable_mounts if m.target == "/output")
            (directory / "pending").write_text("may still be mounted")
            retained.append(directory)
            return finite_job.Outcome(
                status=1, output="", failure="retirement unconfirmed",
                diagnostics=("CLEANUP UNCONFIRMED",), recovery=recovery,
            )

        with patch.object(finite_job, "run_container", side_effect=refused):
            with self.assertRaisesRegex(client.OperationError, "Contained application job failed"):
                app.action(self.docker, "generic", "package/check")
        self.assertEqual(len(retained), 1)
        self.assertEqual((retained[0] / "pending").read_text(), "may still be mounted")
        self.assertIn(str(retained[0]), (recovery / "recovery.txt").read_text())

    def test_missing_package_has_explicit_next_command(self):
        with self.assertRaisesRegex(client.OperationError, "app/package/build"):
            app.action(self.docker, "generic", "package/check")


if __name__ == "__main__":
    unittest.main()
