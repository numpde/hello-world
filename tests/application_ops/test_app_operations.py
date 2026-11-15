"""Test the application command boundary using only host stdlib and fake jobs."""

from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app import operations as app
from infrastructure.docker import client, finite_job


IMAGE = "sha256:" + "a" * 64


class ApplicationOperationsContracts(unittest.TestCase):
    def setUp(self):
        fixture = tempfile.TemporaryDirectory(prefix="app-ops-")
        self.addCleanup(fixture.cleanup)
        self.root = Path(fixture.name)
        (self.root / "pyproject.toml").write_text('[project]\nname = "example"\n')
        (self.root / "uv.lock").write_text("version = 1\n")
        (self.root / "src").mkdir()
        (self.root / "tests").mkdir()
        (self.root / "app").mkdir()
        (self.root / "app/worker.py").write_text("pass\n")
        (self.root / ".idea").mkdir()
        (self.root / ".idea/private.xml").write_text("private")
        self.state = self.root.parent / (self.root.name + "-cache")
        self.state.mkdir()
        self.addCleanup(lambda: __import__("shutil").rmtree(self.state))
        self.patch_root = patch.object(app, "ROOT", self.root)
        self.patch_state = patch.object(app, "state_directory", return_value=self.state)
        self.patch_image = patch.object(app, "toolchain_image", return_value=IMAGE)
        for setting in (self.patch_root, self.patch_state, self.patch_image):
            setting.start()
            self.addCleanup(setting.stop)

    def prepared(self):
        env = self.state / "environment/.venv/bin"
        env.mkdir(parents=True)
        (env / "python").symlink_to("/usr/local/bin/python")
        (self.state / "prepared.json").write_text(json.dumps(app.prepared_identity(IMAGE)))
        return self.state / "environment"

    def test_missing_lock_is_actionable_without_host_install(self):
        (self.root / "uv.lock").unlink()
        with self.assertRaisesRegex(client.OperationError, "dependencies/lock/update"):
            app.require_lock()

    def test_offline_tests_never_mount_private_editor_or_grant_network(self):
        self.prepared()
        observed = []
        with (
            patch.object(app, "run_job", side_effect=lambda *args, **kwargs: observed.append((args, kwargs))),
            patch.object(app, "qualify_dependencies", return_value=self.state / "environment"),
        ):
            app.action(object(), "science", "tests/run")
        self.assertEqual(len(observed), 1)
        args, settings = observed[0]
        self.assertEqual(args[3].executable, "/environment/.venv/bin/python")
        self.assertFalse(settings.get("network", False))
        self.assertEqual({mount.target for mount in settings["inputs"]},
                         {"/testroot/src", "/testroot/tests", "/environment"})
        self.assertTrue((self.root / ".idea/private.xml").is_file())

    def test_dependency_acquire_exposes_only_cache_and_environment(self):
        seen = []
        with patch.object(app, "run_job", side_effect=lambda *args, **kw: seen.append(kw)):
            app.action(object(), "science", "dependencies/acquire")
        self.assertEqual(len(seen), 1)
        self.assertTrue(seen[0]["network"])
        self.assertEqual({w.target for w in seen[0]["outputs"]}, {"/cache", "/environment"})
        self.assertEqual({m.target for m in seen[0]["inputs"]},
                         {"/input/pyproject.toml", "/input/uv.lock", "/worker/worker.py"})
        self.assertNotIn("/testroot/src", {m.target for m in seen[0]["inputs"]})
        self.assertEqual(json.loads((self.state / "prepared.json").read_text()), app.prepared_identity(IMAGE))

    def test_failed_reacquisition_does_not_leave_prior_prepared_stamp(self):
        self.prepared()
        with patch.object(app, "run_job", side_effect=client.OperationError("remote index unavailable")):
            with self.assertRaisesRegex(client.OperationError, "remote index unavailable"):
                app.action(object(), "science", "dependencies/acquire")
        self.assertFalse((self.state / "prepared.json").exists())

    def test_no_preparation_is_a_precise_offline_failure(self):
        with self.assertRaisesRegex(client.OperationError, "dependencies/acquire"):
            app.require_prepared(self.state, IMAGE)

    def test_lock_update_can_generate_initial_lock_without_host_uv(self):
        (self.root / "uv.lock").unlink()
        seen = []

        def fake_work(_docker, _context, _image, _command, **settings):
            seen.append(settings)
            output = next(w.source for w in settings["outputs"] if w.target == "/output")
            (output / "uv.lock").write_text("version = 1\n")

        with patch.object(app, "run_job", side_effect=fake_work):
            app.action(object(), "science", "dependencies/lock/update")
        self.assertTrue(seen[0]["network"])
        self.assertEqual((self.root / "uv.lock").read_text(), "version = 1\n")
        self.assertNotIn("/input/uv.lock", {m.target for m in seen[0]["inputs"]})
        self.assertTrue((self.root / ".idea/private.xml").is_file())

    def test_package_build_is_offline_and_receives_only_public_sources(self):
        env_dir = self.prepared()
        seen = []

        def fake_build(_docker, _context, _image, _command, **options):
            seen.append(options)
            directory = next(m.source for m in options["outputs"] if m.target == "/output")
            (directory / "example-0.1.0-py3-none-any.whl").write_bytes(b"wheel")
            (directory / "example-0.1.0.tar.gz").write_bytes(b"source")

        with (
            patch.object(app, "qualify_dependencies", return_value=env_dir),
            patch.object(app, "run_job", side_effect=fake_build),
        ):
            app.action(object(), "science", "package/build")
        self.assertEqual(len(seen), 1)
        self.assertFalse(seen[0].get("network", False))
        self.assertEqual({m.target for m in seen[0]["inputs"]},
                         {"/project/src", "/project/pyproject.toml", "/environment"})
        self.assertTrue((self.state / "dist/example-0.1.0-py3-none-any.whl").is_file())

    def test_installed_wheel_tests_never_mount_package_source(self):
        env_dir = self.prepared()
        dist = self.state / "dist"
        dist.mkdir()
        (dist / "example-0.1.0-py3-none-any.whl").write_bytes(b"wheel")
        seen = []
        with (
            patch.object(app, "qualify_dependencies", return_value=env_dir),
            patch.object(app, "run_job", side_effect=lambda *a, **kw: seen.append(kw)),
        ):
            app.action(object(), "science", "package/check")
        self.assertEqual(len(seen), 1)
        self.assertFalse(seen[0].get("network", False))
        self.assertEqual({m.target for m in seen[0]["inputs"]},
                         {"/worker/worker.py", "/testroot/tests", "/deps", "/dist"})
        self.assertEqual([mount.target for mount in seen[0]["outputs"]], ["/output"])
        self.assertTrue((self.root / ".idea/private.xml").is_file())

    def test_all_temporary_docker_mounts_survive_unconfirmed_removal(self):
        """An application failure must not release mounted work held by Docker."""
        environment = self.prepared()
        dist = self.state / "dist"
        dist.mkdir()
        (dist / "example-0.1.0-py3-none-any.whl").write_bytes(b"wheel")
        recovery = self.state / "operation"
        recovery.mkdir()
        receipt = recovery / "recovery.txt"
        receipt.write_text("Owned container still present\n")

        for action in ("dependencies/lock/update", "package/build", "package/check"):
            with self.subTest(action=action):
                observed = []

                def removal_refused(_docker, _context, job):
                    outputs = [mount.source for mount in job.writable_mounts if mount.target == "/output"]
                    self.assertEqual(len(outputs), 1)
                    output = outputs[0]
                    self.assertTrue(output.is_dir())
                    (output / "artifact").write_text("still in use")
                    observed.append(output)
                    return finite_job.Outcome(
                        status=1,
                        output="",
                        failure="Docker refused container removal",
                        diagnostics=("CLEANUP UNCONFIRMED",),
                        recovery=recovery,
                    )

                stderr = io.StringIO()
                with (
                    patch.object(app, "qualify_dependencies", return_value=environment),
                    patch.object(finite_job, "run_container", side_effect=removal_refused) as run,
                    contextlib.redirect_stderr(stderr),
                    self.assertRaisesRegex(client.OperationError, "Application container"),
                ):
                    app.action(object(), "generic", action)

                run.assert_called_once()
                self.assertEqual(len(observed), 1)
                output = observed[0]
                self.assertTrue(output.is_dir())
                self.assertEqual((output / "artifact").read_text(), "still in use")
                self.assertIn(str(output), stderr.getvalue())
                self.assertIn(str(output), receipt.read_text())

    def test_toolchain_check_does_not_pull_or_build(self):
        commands = []
        with patch.object(app, "run_job", side_effect=lambda *args, **kwargs: commands.append(args[3])):
            app.action(object(), "science", "toolchain/check")
        self.assertEqual(commands, [finite_job.Command("uv", ("--version",))])


if __name__ == "__main__":
    unittest.main()
