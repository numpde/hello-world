"""Runner selection is explicit, bounded, and independent of Docker."""

from __future__ import annotations

import unittest

from infrastructure.docker.client import OperationError
from infrastructure.commands import runner_selection


class RunnerSelectionContracts(unittest.TestCase):
    def test_default_and_explicit_serial_do_not_request_parallel_workers(self):
        for environment in ({}, {"HELLO_WORLD_TEST_RUNNER": "serial"}):
            with self.subTest(environment=environment):
                option = runner_selection.select(environment, "check")
                self.assertEqual((option.name, option.workers), ("serial", None))

    def test_parallel_requires_explicit_mode_and_bounds_worker_count(self):
        option = runner_selection.select({"HELLO_WORLD_TEST_RUNNER": "parallel"}, "check")
        self.assertEqual((option.name, option.workers), ("parallel", 2))
        for workers in ("1", "2", "4"):
            with self.subTest(workers=workers):
                option = runner_selection.select({
                    "HELLO_WORLD_TEST_RUNNER": "parallel",
                    "HELLO_WORLD_TEST_WORKERS": workers,
                }, "check")
                self.assertEqual(option.workers, int(workers))

    def test_bad_modes_and_worker_counts_fail_without_fallback(self):
        candidates = (
            {"HELLO_WORLD_TEST_RUNNER": ""},
            {"HELLO_WORLD_TEST_RUNNER": "PARALLEL"},
            {"HELLO_WORLD_TEST_RUNNER": "other"},
            {"HELLO_WORLD_TEST_WORKERS": "2"},
            {"HELLO_WORLD_TEST_RUNNER": "serial", "HELLO_WORLD_TEST_WORKERS": "2"},
            *({
                "HELLO_WORLD_TEST_RUNNER": "parallel",
                "HELLO_WORLD_TEST_WORKERS": workers,
            } for workers in ("", "0", "5", "-1", "1.5", " 2", "02", "unlimited")),
        )
        for environment in candidates:
            with self.subTest(environment=environment), self.assertRaises(OperationError):
                runner_selection.select(environment, "check")

    def test_serial_job_is_unchanged_and_parallel_job_has_one_adapter(self):
        from infrastructure.commands import repository
        option = runner_selection.select({"HELLO_WORLD_TEST_RUNNER": "parallel"}, "check")
        command = repository.parallel_test_command(option.workers)
        self.assertEqual(command.executable, "python3")
        self.assertIn("/workspace/infrastructure/testing/parallel_runner.py", command.arguments)
        self.assertEqual(command.arguments[command.arguments.index("-j") + 1], "2")
        self.assertEqual(command.arguments.count("-s"), 2)
        self.assertEqual(repository.UNITTEST_COMMAND.executable, "python3")
        self.assertIn("/workspace/infrastructure/testing/unittest_runner.py",
                      repository.UNITTEST_COMMAND.arguments)

    def test_parallel_image_is_not_accepted_without_a_matching_source_label(self):
        import json
        from types import SimpleNamespace
        from unittest.mock import Mock

        from infrastructure.commands import repository
        from infrastructure.docker.client import Result

        tag, fingerprint = repository.parallel_image_definition()
        self.assertIn("localhost/hello-world/parallel-checker:", tag)
        self.assertEqual(len(fingerprint), 64)
        docker = Mock()
        docker.call.return_value = Result(0, json.dumps({
            "Id": "sha256:" + "1" * 64,
            "Config": {"Labels": {repository.PARALLEL_IMAGE_LABEL: "bad-label"}},
        }))
        with self.assertRaises(OperationError):
            repository.parallel_image(docker, "generic")
        docker.call.assert_called_once()
        docker.call.return_value = Result(0, json.dumps({
            "Id": "sha256:" + "1" * 64,
            "Config": {"Labels": {repository.PARALLEL_IMAGE_LABEL: fingerprint}},
        }))
        self.assertEqual(
            repository.parallel_image(docker, "generic"), "sha256:" + "1" * 64,
        )

    def test_explicit_parallel_acquisition_uses_pinned_base_and_networked_build(self):
        from types import SimpleNamespace
        from unittest.mock import Mock, patch
        from infrastructure.commands import repository
        from infrastructure.docker.client import Result

        docker = Mock()
        docker.acquire.return_value = repository.CHECKER_IMAGE_ID
        docker.call.return_value = Result(0, "build succeeded")
        with patch.object(repository, "parallel_image", return_value="sha256:" + "b" * 64):
            image = repository.acquire_parallel_image(docker, "generic")
        self.assertEqual(image, "sha256:" + "b" * 64)
        docker.acquire.assert_called_once_with(
            repository.CHECKER_IMAGE_REFERENCE, repository.CHECKER_IMAGE_ID,
        )
        build = docker.call.call_args
        args = build.args
        self.assertEqual(args[0], "build")
        self.assertIn("--pull=false", args)
        self.assertIn("--platform=linux/amd64", args)
        self.assertIn("--network=default", args)
        self.assertTrue(any(arg.startswith("PYTHON_IMAGE=") and
                            repository.CHECKER_IMAGE_REFERENCE in arg for arg in args))
        self.assertFalse(any(arg == "--network=none" for arg in args))

    def test_parallel_check_invokes_only_prepared_parallel_image_and_job(self):
        import contextlib
        import tempfile
        from pathlib import Path
        from unittest.mock import Mock, patch

        from infrastructure.commands import repository
        from infrastructure.docker import finite_job

        docker = Mock()
        image_id = "sha256:" + "c" * 64
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "Makefile").write_text("help:\n\t@echo fixture\n")
            (root / "infrastructure").mkdir()
            (root / "tests").mkdir()
            with (
                patch.object(repository.docker_client, "docker_client",
                             return_value=contextlib.nullcontext(docker)),
                patch.object(repository, "parallel_image", return_value=image_id) as admitted,
                patch.object(finite_job, "run_container",
                             return_value=finite_job.Outcome(0, "", None, (), None)) as executed,
            ):
                status = repository.run_repository_command(
                    "check", {
                        "HELLO_WORLD_DOCKER_CONTEXT": "generic",
                        "HELLO_WORLD_TEST_RUNNER": "parallel",
                        "HELLO_WORLD_TEST_WORKERS": "3",
                    }, root=root,
                )
        self.assertEqual(status, 0)
        admitted.assert_called_once_with(docker, "generic")
        docker.image.assert_not_called()
        docker.acquire.assert_not_called()
        executed.assert_called_once()
        job = executed.call_args.args[2]
        self.assertEqual(job.image_id, image_id)
        self.assertEqual(job.network, "none")
        self.assertEqual(job.writable_mounts, ())
        self.assertEqual(job.command.arguments[job.command.arguments.index("-j") + 1], "3")
        self.assertIn("/workspace/infrastructure/testing/parallel_runner.py",
                      job.command.arguments)

    def test_missing_parallel_image_does_not_start_or_download_any_job(self):
        import contextlib
        from unittest.mock import Mock, patch

        from infrastructure.commands import repository
        from infrastructure.docker import finite_job

        docker = Mock()
        with (
            patch.object(repository.docker_client, "docker_client",
                         return_value=contextlib.nullcontext(docker)),
            patch.object(repository, "parallel_image",
                         side_effect=OperationError("parallel checker absent")),
            patch.object(finite_job, "run_container") as run,
            self.assertRaises(OperationError),
        ):
            repository.run_repository_command(
                "check", {
                    "HELLO_WORLD_DOCKER_CONTEXT": "generic",
                    "HELLO_WORLD_TEST_RUNNER": "parallel",
                },
            )
        docker.acquire.assert_not_called()
        docker.call.assert_not_called()
        run.assert_not_called()

    def test_application_operation_check_does_not_implicitly_use_parallel(self):
        self.assertEqual(runner_selection.select({}, "application-check").name, "serial")
        with self.assertRaises(OperationError):
            runner_selection.select({"HELLO_WORLD_TEST_RUNNER": "parallel"}, "application-check")


if __name__ == "__main__":
    unittest.main()
