"""Behavior contracts for the engine boundary and ownership lifecycle."""

from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tools.container import finite_job
from tools.repository import checker


IDENTIFIER = "a" * 64
ENDPOINT = "unix:///tmp/rootless-docker.sock"
ENGINE = {
    "ID": "rootless-test-engine", "SecurityOptions": ["name=rootless"],
    "CgroupDriver": "systemd", "CgroupVersion": "2", "OSType": "linux",
    "Architecture": "x86_64",
}


class FakeExecutor:
    def __init__(self):
        self.calls = []
        self.present = False
        self.labels = {}
        self.exit_status = 0
        self.failed_start = False
        self.malformed_ownership = False
        self.failed_removal = False
        self.creation_timeout = False
        self.work_timeout = False
        self.cancel_start = False
        self.bad_exit_state = False
        self.bad_confinement = False
        self.mounts = []
        self.source_output = "Ran 17 tests in 0.12s\n\nOK\n"
        self.engine = ENGINE.copy()
        self.endpoint = ENDPOINT
        self.image_present = True

    def run(self, argv, env, timeout):
        self.calls.append((tuple(argv), env.copy(), timeout))
        args = argv[1:] if argv[:2] == ["docker", "context"] else argv[5:]
        if argv[:3] == ["docker", "context", "inspect"]:
            return finite_job.Result(0, self.endpoint + "\n")
        if args[:2] == ["info", "--format"]:
            return finite_job.Result(0, json.dumps(self.engine) + "\n")
        if args[:2] == ["image", "inspect"]:
            return finite_job.Result(0, checker.IMAGE_ID + "\n") if self.image_present else finite_job.Result(1, "No such image")
        if args[:1] == ["pull"]:
            return finite_job.Result(0, "Downloaded pinned digest\n")
        if args[:1] == ["create"]:
            if self.creation_timeout:
                raise TimeoutError("create timed out")
            cidfile = Path(args[args.index("--cidfile") + 1])
            self.labels = dict(
                item.split("=", 1) for index, item in enumerate(args)
                if index > 0 and args[index - 1] == "--label"
            )
            cidfile.write_text(IDENTIFIER + "\n", encoding="ascii")
            self.present = True
            self.mounts = [
                {
                    "Type": "bind", "RW": False,
                    "Source": part.split("source=", 1)[1].split(",target=", 1)[0],
                    "Destination": part.split("target=", 1)[1].split(",readonly", 1)[0],
                }
                for part in args if part.startswith("type=bind,")
            ]
            return finite_job.Result(0, IDENTIFIER + "\n")
        if args[:1] == ["ps"]:
            return finite_job.Result(0, IDENTIFIER + "\n" if self.present else "")
        if args[:3] == ["container", "inspect", "--format"]:
            if args[3] == "{{json .State}}":
                state = {"Running": False, "ExitCode": self.exit_status}
                if self.bad_exit_state:
                    state = {"Running": True, "ExitCode": 0}
                return finite_job.Result(0, json.dumps(state) + "\n")
            labels = self.labels.copy()
            if self.malformed_ownership:
                labels[finite_job.IDENTITY_LABEL] = "not-our-operation"
            host = {
                "NetworkMode": "none", "ReadonlyRootfs": True, "Privileged": False,
                "NanoCpus": 2_000_000_000, "Memory": 536_870_912,
                "MemorySwap": 536_870_912, "PidsLimit": 64,
                "CapDrop": ["ALL"], "SecurityOpt": ["no-new-privileges:true"],
                "LogConfig": {"Type": "none"},
                "Tmpfs": {"/tmp": "rw,nosuid,nodev,noexec,size=67108864"},
            }
            if self.bad_confinement:
                host["NetworkMode"] = "bridge"
            return finite_job.Result(0, json.dumps({
                "Id": IDENTIFIER, "Image": checker.IMAGE_ID,
                "Config": {"Labels": labels, "User": "65532:65532"},
                "HostConfig": host, "Mounts": self.mounts,
            }))
        if args[:1] == ["start"]:
            if self.cancel_start:
                raise finite_job.Cancelled("caller interrupted work")
            if self.work_timeout:
                raise TimeoutError("attachment timed out")
            if self.failed_start:
                return finite_job.Result(1, "Docker client failed to attach")
            return finite_job.Result(0, self.source_output)
        if args[:1] == ["rm"]:
            if self.failed_removal:
                return finite_job.Result(1, "rm rejected")
            self.present = False
            return finite_job.Result(0, IDENTIFIER + "\n")
        raise AssertionError(f"Unexpected Docker call: {argv}")


class RepositoryContainerContracts(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="checker-fixture-")
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        (self.root / "Makefile").write_text("help:\n\t@echo ok\n")
        (self.root / "tools").mkdir()
        (self.root / "tests").mkdir()
        (self.root / "tools" / "dummy.py").write_text("x = 1\n")
        self.executor = FakeExecutor()
        self.environment = {
            "HELLO_WORLD_DOCKER_CONTEXT": "rootless",
            "PATH": os.environ["PATH"], "HOME": os.environ.get("HOME", "/tmp"),
            "DOCKER_HOST": "tcp://malicious:2375", "DOCKER_CONTEXT": "remote",
            "DOCKER_CONFIG": "/private", "DOCKER_TLS_VERIFY": "1",
            "COMPOSE_PROJECT_NAME": "another", "BUILDX_BUILDER": "hostile",
            "HTTP_PROXY": "http://untrusted", "HTTPS_PROXY": "http://untrusted",
            "PYTHONPATH": "/untrusted", "AWS_SECRET_ACCESS_KEY": "nope",
        }
        self.uid_patch = patch.object(finite_job.os, "geteuid", return_value=1000)
        self.uid_patch.start()
        self.addCleanup(self.uid_patch.stop)
        self.gid_patch = patch.object(finite_job.os, "getegid", return_value=1000)
        self.gid_patch.start()
        self.addCleanup(self.gid_patch.stop)
        self.socket_patch = patch.object(checker, "verify_socket")
        self.socket_patch.start()
        self.addCleanup(self.socket_patch.stop)

    def execute(self, action="check"):
        output, errors = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            status = checker.run_repository_command(action, self.environment, self.executor, self.root)
        return status, output.getvalue(), errors.getvalue()

    def test_success_contained_and_cleanup_verified(self):
        status, output, errors = self.execute()
        self.assertEqual(status, 0, errors)
        self.assertIn("17 tests", output)
        self.assertFalse(self.executor.present)
        arguments = [call[0] for call in self.executor.calls]
        create = next(argv for argv in arguments if "create" in argv)
        for required in (
            "--pull=never", "--network=none", "--read-only", "--cap-drop=ALL",
            "--security-opt=no-new-privileges:true", "--user=65532:65532",
            "--pids-limit=64", "--memory=512m", "--memory-swap=512m",
            "--cpus=2", "--log-driver=none", "--platform=linux/amd64",
            checker.IMAGE_ID,
        ):
            self.assertTrue(any(required in value for value in create), required)
        mounts = [create[index + 1] for index, value in enumerate(create) if value == "--mount"]
        self.assertEqual(len(mounts), 3)
        self.assertTrue(all("readonly" in value for value in mounts))
        self.assertTrue(all("/workspace/" in value for value in mounts))
        self.assertFalse(any(".git" in value or "secrets" in value for value in mounts))
        self.assertFalse(any("pull" in argv for argv in arguments))
        self.assertTrue(any("--host" in argv for argv in arguments if "context" not in argv))
        self.assertTrue(all("DOCKER_HOST" not in env and "AWS_SECRET_ACCESS_KEY" not in env
                            and "HTTP_PROXY" not in env and "DOCKER_CONFIG" not in env
                            for _, env, _ in self.executor.calls))
        self.assertTrue(all(argv[0] == "docker" for argv in arguments))
        self.assertNotIn("Retained", errors)

    def test_failed_tests_fail_and_are_removed(self):
        self.executor.exit_status = 1
        self.executor.source_output = "Ran 2 tests\nFAIL: test_example\n"
        status, output, errors = self.execute()
        self.assertEqual(status, 1)
        self.assertIn("FAIL: test_example", output)
        self.assertIn("container exit 1", errors)
        self.assertFalse(self.executor.present)

    def test_client_attach_failure_is_not_silently_a_success(self):
        self.executor.failed_start = True
        status, _, errors = self.execute()
        self.assertEqual(status, 1)
        self.assertIn("attachment failed", errors)
        self.assertFalse(self.executor.present)

    def test_timeout_on_running_container_still_retired(self):
        self.executor.work_timeout = True
        status, _, errors = self.execute()
        self.assertEqual(status, 1)
        self.assertIn("attachment timed out", errors)
        self.assertFalse(self.executor.present)

    def test_engine_that_ignores_confinement_cannot_start_checker(self):
        self.executor.bad_confinement = True
        status, _, errors = self.execute()
        self.assertEqual(status, 1)
        self.assertIn("NetworkMode", errors)
        self.assertFalse(self.executor.present)
        self.assertFalse(any("start" in args for args, _, _ in self.executor.calls))

    def test_public_root_file_permissions_are_checked(self):
        source = self.root / "Makefile"
        source.chmod(0o600)
        self.addCleanup(lambda: source.chmod(0o644))
        with self.assertRaisesRegex(finite_job.OperationError, "non-public"):
            self.execute()
        self.assertFalse(self.executor.present)

    def test_cancellation_removes_owned_container(self):
        self.executor.cancel_start = True
        status, _, errors = self.execute()
        self.assertEqual(status, 1)
        self.assertIn("caller interrupted", errors)
        self.assertFalse(self.executor.present)

    def test_running_state_cannot_be_mistaken_for_exit_success(self):
        self.executor.bad_exit_state = True
        status, _, errors = self.execute()
        self.assertEqual(status, 1)
        self.assertIn("without a verified final exit status", errors)
        self.assertFalse(self.executor.present)

    def test_uncertain_creation_keeps_recovery_even_when_ps_is_empty(self):
        self.executor.creation_timeout = True
        status, _, errors = self.execute()
        self.assertEqual(status, 1)
        self.assertIn("Create was not confirmed", errors)
        recovery = next(line.split("Recovery: ", 1)[1] for line in errors.splitlines()
                        if "Recovery: " in line)
        self.assertTrue(Path(recovery).is_file())
        self.addCleanup(lambda: __import__("shutil").rmtree(Path(recovery).parent))

    def test_unconfirmed_cleanup_retains_receipt(self):
        self.executor.failed_removal = True
        status, _, errors = self.execute()
        self.assertEqual(status, 1)
        self.assertTrue(self.executor.present)
        self.assertIn("CLEANUP UNCONFIRMED", errors)
        path = Path(errors.split("Retained recovery record: ", 1)[1].splitlines()[0])
        self.assertTrue(path.is_file())
        self.addCleanup(lambda: __import__("shutil").rmtree(path.parent))

    def test_ownership_mismatch_prohibits_deletion(self):
        self.executor.malformed_ownership = True
        status, _, errors = self.execute()
        self.assertEqual(status, 1)
        self.assertTrue(self.executor.present)
        self.assertIn("Refusing to remove", errors)
        path = Path(errors.split("Retained recovery record: ", 1)[1].splitlines()[0])
        self.addCleanup(lambda: __import__("shutil").rmtree(path.parent))

    def test_parallel_test_command_is_an_adapter_choice(self):
        replacement = finite_job.Command(
            executable="unittest-parallel",
            arguments=(*checker.UNITTEST_DISCOVERY_ARGS, "--level=module", "-j", "2"),
        )
        job = checker.repository_test_job(self.root, checker.IMAGE_ID, replacement)
        create = finite_job.container_arguments(job, "test-token", "test-owner")
        self.assertIn("--entrypoint=unittest-parallel", create)
        self.assertEqual(tuple(create[-len(replacement.arguments):]), replacement.arguments)
        self.assertNotIn("/workspace/tools/repository/unittest_runner.py", create)

    def test_generic_job_does_not_depend_on_repository_checks(self):
        job = finite_job.Job(
            image_id=checker.IMAGE_ID,
            command=finite_job.Command("/bin/echo", ("hello",)),
            mounts=(),
        )
        create = finite_job.container_arguments(job, "test-token", "test-owner")
        self.assertEqual(create[-2:], [checker.IMAGE_ID, "hello"])
        self.assertIn("--entrypoint=/bin/echo", create)

    def test_networked_acquisition_is_separate(self):
        status, output, errors = self.execute("acquire")
        self.assertEqual(status, 0, errors)
        self.assertIn("Acquired checker image", output)
        calls = [argv for argv, _, _ in self.executor.calls]
        self.assertEqual(sum("pull" in argv for argv in calls), 1)
        self.assertFalse(any("create" in argv for argv in calls))

    def test_missing_pinned_image_has_explicit_remedy(self):
        self.executor.image_present = False
        with self.assertRaisesRegex(finite_job.OperationError, "repository/image/acquire"):
            self.execute()
        self.assertFalse(self.executor.present)

    def test_reject_nonrootless_and_missing_cgroup_enforcement(self):
        for changed in ({"SecurityOptions": []}, {"CgroupVersion": "1"}, {"Architecture": "arm64"}):
            with self.subTest(changed=changed):
                self.executor.engine = ENGINE | changed
                with self.assertRaises(finite_job.OperationError):
                    self.execute()
                self.assertFalse(self.executor.present)

    def test_explicit_selection_not_ambient_default(self):
        for input_name in (None, "", "../../engine", "bad context", "$(shell touch /tmp/nope)"):
            with self.subTest(input_name=input_name):
                self.environment["HELLO_WORLD_DOCKER_CONTEXT"] = input_name
                with self.assertRaises(finite_job.OperationError):
                    self.execute()
        self.assertFalse(self.executor.calls)

    def test_remote_endpoint_rejected(self):
        self.executor.endpoint = "tcp://not-allowed:2376"
        with self.assertRaisesRegex(finite_job.OperationError, "local Unix socket"):
            self.execute()

    def test_source_symlink_and_unknown_root_rejected_before_create(self):
        extra = self.root / "config.env"
        extra.write_text("SECRET=hello")
        with self.assertRaisesRegex(finite_job.OperationError, "Unclassified"):
            self.execute()
        extra.unlink()
        (self.root / "tools" / "link").symlink_to("/etc/passwd")
        with self.assertRaisesRegex(finite_job.OperationError, "Refusing link"):
            self.execute()
        self.assertFalse(self.executor.present)

    def test_private_roots_are_never_mounted(self):
        (self.root / ".git").mkdir()
        (self.root / ".git" / "config").write_text("private")
        (self.root / "secrets").mkdir()
        (self.root / "secrets" / "key").write_text("private")
        mounts = checker.public_mounts(self.root)
        self.assertEqual(len(mounts), 3)
        self.assertFalse(any("secrets" in str(value.source) or "/.git" in str(value.source) for value in mounts))

    def test_full_ids_only_and_no_collision(self):
        with self.assertRaisesRegex(finite_job.OperationError, "malformed"):
            finite_job.container_ids("abc\n")
        with self.assertRaisesRegex(finite_job.OperationError, "duplicate"):
            finite_job.container_ids(IDENTIFIER + "\n" + IDENTIFIER + "\n")


class NativeClientContracts(unittest.TestCase):
    def test_capture_is_bounded_even_for_large_output(self):
        client = finite_job.NativeExecutor()
        command = [sys.executable, "-I", "-c", "import sys;sys.stdout.write('x'*1500000)"]
        result = client.run(command, finite_job.clean_environment(os.environ.copy()), timeout=10)
        self.assertEqual(result.status, 0)
        self.assertTrue(result.truncated)
        self.assertEqual(len(result.output), finite_job.MAX_OUTPUT)

    def test_native_timeout_does_not_leave_its_client(self):
        client = finite_job.NativeExecutor()
        command = [sys.executable, "-I", "-c", "import time;time.sleep(30)"]
        with self.assertRaises(TimeoutError):
            client.run(command, finite_job.clean_environment(os.environ.copy()), timeout=1)


class InputContracts(unittest.TestCase):
    def test_socket_addresses(self):
        self.assertEqual(str(finite_job.socket_path("unix:///run/user/1000/docker.sock")),
                         "/run/user/1000/docker.sock")
        for endpoint in ("tcp://elsewhere", "unix://relative", "unix:///a\nb", "ssh://hostname", "unix:///a?b"):
            with self.subTest(endpoint=endpoint), self.assertRaises(finite_job.OperationError):
                finite_job.socket_path(endpoint)

    def test_owned_rootless_socket_requires_actual_socket(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "not-a-socket"
            path.write_text("fake")
            with self.assertRaisesRegex(finite_job.OperationError, "user-owned"):
                finite_job.verify_socket("unix://" + str(path))

    def test_checked_result_does_not_relabel_errors(self):
        with self.assertRaisesRegex(finite_job.OperationError, "exit 33"):
            finite_job.checked_result(finite_job.Result(33, "trace was retained"), "context")


if __name__ == "__main__":
    unittest.main()
