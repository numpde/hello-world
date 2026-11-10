"""Exercise the repository command and Docker lifecycle without a daemon.

The fake client supplies controlled Docker replies, including failed starts,
ambiguous creates, invalid confinement and failed removal. These tests check
command construction and failure reporting; they do not prove real engine
behavior or effective operating-system isolation.
"""

from __future__ import annotations

import contextlib
import io
from concurrent.futures import ThreadPoolExecutor
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from infrastructure.docker import client as docker_client
from infrastructure.docker import finite_job
from infrastructure.commands import repository


FAKE_CONTAINER_ID = "a" * 64
FAKE_ENGINE_ENDPOINT = "unix:///tmp/rootless-docker.sock"
FAKE_ENGINE_INFORMATION = {
    "ID": "rootless-test-engine", "SecurityOptions": ["name=rootless"],
    "CgroupDriver": "systemd", "CgroupVersion": "2", "OSType": "linux",
    "Architecture": "x86_64",
}


class FakeExecutor:
    """Model the Docker CLI responses and resource state used by these tests.
    
    Every call is recorded. Flags can alter one observed outcome while keeping
    the rest of the fake engine consistent for the lifecycle under test."""
    def __init__(self):
        """Start with a successful fake engine and disabled fault injections."""
        self.docker_calls = []
        self.container_present = False
        self.container_labels = {}
        self.workload_exit_status = 0
        self.failed_start = False
        self.attachment_truncated = False
        self.malformed_ownership = False
        self.failed_removal = False
        self.creation_timeout = False
        self.work_timeout = False
        self.cancel_start = False
        self.bad_exit_state = False
        self.host_configuration_overrides = {}
        self.container_mounts = []
        self.observed_additional_mounts = []
        self.workload_output = "Ran 17 tests in 0.12s\n\nOK\n"
        self.engine_information = FAKE_ENGINE_INFORMATION.copy()
        self.engine_endpoint = FAKE_ENGINE_ENDPOINT
        self.checker_image_present = True

    def run(self, argv, env, timeout):
        """Return deterministic replies for the Docker commands used by the adapter.
        
        Creating a container writes the supplied CID file; list and inspection
        queries report fake daemon state independently of client success."""
        self.docker_calls.append((tuple(argv), env.copy(), timeout))
        docker_arguments = argv[1:] if argv[:2] == ["docker", "context"] else argv[5:]
        if argv[:3] == ["docker", "context", "inspect"]:
            return docker_client.Result(0, self.engine_endpoint + "\n")
        if docker_arguments[:2] == ["info", "--format"]:
            return docker_client.Result(0, json.dumps(self.engine_information) + "\n")
        if docker_arguments[:2] == ["image", "inspect"]:
            return docker_client.Result(0, repository.CHECKER_IMAGE_ID + "\n") if self.checker_image_present else docker_client.Result(1, "No such image")
        if docker_arguments[:1] == ["pull"]:
            return docker_client.Result(0, "Downloaded pinned digest\n")
        if docker_arguments[:1] == ["create"]:
            if self.creation_timeout:
                raise TimeoutError("create timed out")
            container_id_receipt = Path(docker_arguments[docker_arguments.index("--cidfile") + 1])
            self.container_labels = dict(
                label_declaration.split("=", 1) for argument_position, label_declaration in enumerate(docker_arguments)
                if argument_position > 0 and docker_arguments[argument_position - 1] == "--label"
            )
            container_id_receipt.write_text(FAKE_CONTAINER_ID + "\n", encoding="ascii")
            self.container_present = True
            self.container_mounts = [
                {
                    "Type": "bind", "RW": False,
                    "Source": mount_declaration.split("source=", 1)[1].split(",target=", 1)[0],
                    "Destination": mount_declaration.split("target=", 1)[1].split(",readonly", 1)[0],
                }
                for mount_declaration in docker_arguments if mount_declaration.startswith("type=bind,")
            ]
            return docker_client.Result(0, FAKE_CONTAINER_ID + "\n")
        if docker_arguments[:1] == ["ps"]:
            return docker_client.Result(0, FAKE_CONTAINER_ID + "\n" if self.container_present else "")
        if docker_arguments[:3] == ["container", "inspect", "--format"]:
            if docker_arguments[3] == "{{json .State}}":
                container_state = {"Running": False, "ExitCode": self.workload_exit_status}
                if self.bad_exit_state:
                    container_state = {"Running": True, "ExitCode": 0}
                return docker_client.Result(0, json.dumps(container_state) + "\n")
            container_labels = self.container_labels.copy()
            if self.malformed_ownership:
                container_labels[finite_job.IDENTITY_LABEL] = "not-our-operation"
            host_configuration = {
                "NetworkMode": "none", "ReadonlyRootfs": True, "Privileged": False,
                "NanoCpus": 2_000_000_000, "Memory": 536_870_912,
                "MemorySwap": 536_870_912, "PidsLimit": 64,
                "CapDrop": ["ALL"], "CapAdd": None,
                "SecurityOpt": ["no-new-privileges:true"],
                "LogConfig": {"Type": "none"},
                "Ulimits": [{"Name": "nofile", "Soft": 256, "Hard": 256}],
                "Tmpfs": {"/tmp": "rw,nosuid,nodev,noexec,size=67108864"},
            }
            host_configuration.update(self.host_configuration_overrides)
            return docker_client.Result(0, json.dumps({
                "Id": FAKE_CONTAINER_ID, "Image": repository.CHECKER_IMAGE_ID,
                "Config": {"Labels": container_labels, "User": "65532:65532"},
                "HostConfig": host_configuration,
                "Mounts": [*self.container_mounts, *self.observed_additional_mounts],
            }))
        if docker_arguments[:1] == ["start"]:
            if self.cancel_start:
                raise docker_client.Cancelled("caller interrupted work")
            if self.work_timeout:
                raise TimeoutError("attachment timed out")
            if self.failed_start:
                return docker_client.Result(1, "Docker client failed to attach", self.attachment_truncated)
            return docker_client.Result(0, self.workload_output, self.attachment_truncated)
        if docker_arguments[:1] == ["rm"]:
            if self.failed_removal:
                return docker_client.Result(1, "rm rejected")
            self.container_present = False
            return docker_client.Result(0, FAKE_CONTAINER_ID + "\n")
        raise AssertionError(f"Unexpected Docker call: {argv}")


class RepositoryContainerContracts(unittest.TestCase):
    """Test command-level behavior with an untrusted environment and fake engine.
    
    Failure scenarios establish what must be reported, cleaned up or retained;
    they are not live security qualification."""
    def setUp(self):
        """Create a minimal checkout and poison inherited Docker and proxy settings.
        
        Patch only host identity and socket inspection, leaving command dispatch
        observable through the fake executor."""
        self.directory = tempfile.TemporaryDirectory(prefix="checker-fixture-")
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        (self.root / "Makefile").write_text("help:\n\t@echo ok\n")
        (self.root / "infrastructure").mkdir()
        (self.root / "tests").mkdir()
        (self.root / "infrastructure" / "dummy.py").write_text("x = 1\n")
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
        self.uid_patch = patch.object(docker_client.os, "geteuid", return_value=1000)
        self.uid_patch.start()
        self.addCleanup(self.uid_patch.stop)
        self.gid_patch = patch.object(docker_client.os, "getegid", return_value=1000)
        self.gid_patch.start()
        self.addCleanup(self.gid_patch.stop)
        self.socket_patch = patch.object(docker_client, "verify_socket")
        self.socket_patch.start()
        self.addCleanup(self.socket_patch.stop)

    def execute(self, action="check"):
        """Run the repository command and capture its public output and diagnostics."""
        captured_stdout = io.StringIO()
        captured_stderr = io.StringIO()

        with contextlib.redirect_stdout(captured_stdout), contextlib.redirect_stderr(captured_stderr):
            operation_status = repository.run_repository_command(
                action,
                self.environment,
                self.executor,
                self.root,
            )

        return operation_status, captured_stdout.getvalue(), captured_stderr.getvalue()

    def test_success_contained_and_cleanup_verified(self):
        """A successful job must still prove confinement, sanitize Docker inputs and retire its container."""
        status, output, errors = self.execute()
        self.assertEqual(status, 0, errors)
        self.assertIn("17 tests", output)
        self.assertFalse(self.executor.container_present)
        arguments = [call[0] for call in self.executor.docker_calls]
        create = next(argv for argv in arguments if "create" in argv)
        for required in (
            "--pull=never", "--network=none", "--read-only", "--cap-drop=ALL",
            "--security-opt=no-new-privileges:true", "--user=65532:65532",
            "--pids-limit=64", "--memory=512m", "--memory-swap=512m",
            "--cpus=2", "--ulimit=nofile=256:256",
            "--tmpfs=/tmp:rw,nosuid,nodev,noexec,size=67108864",
            "--log-driver=none", "--platform=linux/amd64",
            repository.CHECKER_IMAGE_ID,
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
                            for _, env, _ in self.executor.docker_calls))
        self.assertTrue(all(argv[0] == "docker" for argv in arguments))
        self.assertNotIn("Retained", errors)

    def test_failed_tests_fail_and_are_removed(self):
        """A nonzero container exit is the authoritative failure even when attachment succeeds."""
        self.executor.workload_exit_status = 1
        self.executor.workload_output = "Ran 2 tests\nFAIL: test_example\n"
        status, output, errors = self.execute()
        self.assertEqual(status, 1)
        self.assertIn("FAIL: test_example", output)
        self.assertIn("container exit 1", errors)
        self.assertFalse(self.executor.container_present)

    def test_client_attach_failure_is_not_silently_a_success(self):
        """Losing the Docker attachment must not turn a failed operation into success."""
        self.executor.failed_start = True
        status, _, errors = self.execute()
        self.assertEqual(status, 1)
        self.assertIn("attachment failed", errors)
        self.assertFalse(self.executor.container_present)

    def test_simultaneous_failures_preserve_secondary_evidence(self):
        """The final exit is primary, while truncated output and attachment failure remain visible."""
        self.executor.failed_start = True
        self.executor.attachment_truncated = True
        self.executor.workload_exit_status = 9

        (operation_status, _, diagnostics) = self.execute()

        self.assertEqual(operation_status, 1)
        self.assertIn("container exit 9", diagnostics)
        self.assertIn("attachment failed", diagnostics)
        self.assertIn("tail retained", diagnostics)
        self.assertFalse(self.executor.container_present)

    def test_secondary_workload_failures_precede_cleanup_diagnostics(self):
        """Report workload evidence before later cleanup uncertainty."""
        self.executor.failed_start = True
        self.executor.attachment_truncated = True
        self.executor.workload_exit_status = 9
        self.executor.failed_removal = True

        (operation_status, _, errors) = self.execute()

        self.assertEqual(operation_status, 1)
        self.assertLess(errors.index("tail retained"), errors.index("CLEANUP UNCONFIRMED"))
        self.assertLess(errors.index("attachment failed"), errors.index("CLEANUP UNCONFIRMED"))
        self.assertTrue(self.executor.container_present)

    def test_timeout_on_running_container_still_retired(self):
        """A client timeout does not stop the daemon's container; cleanup must inspect and remove it."""
        self.executor.work_timeout = True
        status, _, errors = self.execute()
        self.assertEqual(status, 1)
        self.assertIn("attachment timed out", errors)
        self.assertFalse(self.executor.container_present)

    def test_work_deadline_during_discovery_still_retires_container(self):
        """Work expiry after create cannot consume the separate cleanup budget."""
        def observed_work_time() -> float:
            create_was_sent = any("create" in argv for (argv, _, _) in self.executor.docker_calls)
            return finite_job.WORK_TIMEOUT + 1.0 if create_was_sent else 0.0

        with patch.object(finite_job.time, "monotonic", side_effect=observed_work_time):
            (operation_status, _, errors) = self.execute()

        self.assertEqual(operation_status, 1)
        self.assertIn("Docker work budget exhausted", errors)
        self.assertFalse(self.executor.container_present)
        self.assertTrue(any("create" in argv for (argv, _, _) in self.executor.docker_calls))

        recovery_line = next(line for line in errors.splitlines() if "Recovery: " in line)
        recovery_receipt = Path(recovery_line.split("Recovery: ", 1)[1])
        self.assertTrue(recovery_receipt.is_file())
        self.addCleanup(shutil.rmtree, recovery_receipt.parent)

    def test_expired_work_budget_never_requests_create(self):
        """A timeout before Docker create leaves no daemon resources or recovery claim."""
        with patch.object(finite_job, "WORK_TIMEOUT", 0):
            (operation_status, _, errors) = self.execute()

        self.assertEqual(operation_status, 1)
        self.assertIn("Docker work budget exhausted", errors)
        self.assertNotIn("Retained operation", errors)
        self.assertFalse(any("create" in argv for (argv, _, _) in self.executor.docker_calls))

    def test_receipt_write_failure_does_not_claim_recovery(self):
        """No daemon mutation has occurred if the receipt cannot be written."""
        with patch.object(finite_job, "write_receipt", side_effect=OSError("receipt storage unavailable")):
            (operation_status, _, errors) = self.execute()

        self.assertEqual(operation_status, 1)
        self.assertIn("receipt storage unavailable", errors)
        self.assertNotIn("Retained operation", errors)
        self.assertFalse(any("create" in argv for (argv, _, _) in self.executor.docker_calls))
        self.assertFalse(any("ps" in argv for (argv, _, _) in self.executor.docker_calls))

    def test_engine_that_ignores_confinement_cannot_start_checker(self):
        """A created container with broader network access must be rejected before it starts."""
        self.executor.host_configuration_overrides = {"NetworkMode": "bridge"}
        status, _, errors = self.execute()
        self.assertEqual(status, 1)
        self.assertIn("NetworkMode", errors)
        self.assertFalse(self.executor.container_present)
        self.assertFalse(any("start" in args for args, _, _ in self.executor.docker_calls))

    def test_capabilities_nofile_and_tmpfs_must_match(self):
        """Contradictory limit entries and broader temporary storage must prevent startup."""
        expected_nofile = {"Name": "nofile", "Soft": 256, "Hard": 256}
        rejected_host_configuration = (
            {"CapAdd": ["SYS_ADMIN"]},
            {"Ulimits": None},
            {"Ulimits": [{"Name": "nofile", "Soft": 4096, "Hard": 4096}]},
            {"Ulimits": [{"Name": "nofile", "Soft": 256, "Hard": 4096}]},
            {"Ulimits": [expected_nofile, {"Name": "nofile", "Soft": 4096, "Hard": 4096}]},
            {"Tmpfs": {"/tmp": "rw,nosuid,nodev,noexec,size=67108864,exec"}},
            {"Tmpfs": {"/tmp": "rw,nosuid,nodev,noexec,size=67108864,size=1073741824"}},
        )

        for host_configuration_overrides in rejected_host_configuration:
            with self.subTest(host_configuration_overrides=host_configuration_overrides):
                self.executor.host_configuration_overrides = host_configuration_overrides

                (operation_status, _, errors) = self.execute()

                self.assertEqual(operation_status, 1)
                self.assertIn("Docker did not apply required confinement", errors)
                self.assertFalse(self.executor.container_present)
                self.assertFalse(
                    any("start" in arguments for (arguments, _, _) in self.executor.docker_calls)
                )

    def test_equivalent_inspection_representations_are_accepted(self):
        """Option order and unrelated resource limits do not weaken confinement."""
        self.executor.host_configuration_overrides = {
            "Ulimits": [
                {"Name": "nofile", "Soft": 256, "Hard": 256},
                {"Name": "nproc", "Soft": 64, "Hard": 64},
            ],
            "Tmpfs": {"/tmp": "noexec,nodev,nosuid,size=67108864,rw"},
        }

        (operation_status, _, errors) = self.execute()

        self.assertEqual(operation_status, 0, errors)
        self.assertFalse(self.executor.container_present)

    def test_expected_tmpfs_mount_can_appear_in_inspection(self):
        """Docker may report the admitted /tmp tmpfs alongside the read-only binds."""
        self.executor.observed_additional_mounts = [
            {"Type": "tmpfs", "Source": "", "Destination": "/tmp", "RW": True},
        ]

        (operation_status, _, errors) = self.execute()

        self.assertEqual(operation_status, 0, errors)
        self.assertFalse(self.executor.container_present)

    def test_unrequested_tmpfs_storage_is_rejected(self):
        """An additional tmpfs grant cannot be hidden in either inspect field."""
        extra_tmpfs_mount = {"Type": "tmpfs", "Source": "", "Destination": "/other", "RW": True}
        for (tmpfs_config, additional_mounts) in (
            ({"/tmp": "rw,nosuid,nodev,noexec,size=67108864", "/other": "rw"}, []),
            ({"/tmp": "rw,nosuid,nodev,noexec,size=67108864"}, [extra_tmpfs_mount]),
        ):
            with self.subTest(tmpfs_config=tmpfs_config, additional_mounts=additional_mounts):
                self.executor.host_configuration_overrides = {"Tmpfs": tmpfs_config}
                self.executor.observed_additional_mounts = additional_mounts

                (operation_status, _, errors) = self.execute()

                self.assertEqual(operation_status, 1)
                self.assertIn("Docker did not apply required confinement", errors)
                self.assertFalse(any("start" in args for (args, _, _) in self.executor.docker_calls))

    def test_public_root_file_permissions_are_checked(self):
        """Do not mount source files that the unprivileged container cannot read."""
        source = self.root / "Makefile"
        source.chmod(0o600)
        self.addCleanup(lambda: source.chmod(0o644))
        with self.assertRaisesRegex(docker_client.OperationError, "non-public"):
            self.execute()
        self.assertFalse(self.executor.container_present)

    def test_worker_thread_can_retire_its_container(self):
        """A worker can complete cleanup without attempting to install signal handlers."""
        with ThreadPoolExecutor(max_workers=1) as worker_pool:
            (operation_status, _, errors) = worker_pool.submit(self.execute).result()

        self.assertEqual(operation_status, 0, errors)
        self.assertFalse(self.executor.container_present)

    def test_cancellation_removes_owned_container(self):
        """A caller interruption still leaves the operation responsible for its container."""
        self.executor.cancel_start = True
        status, _, errors = self.execute()
        self.assertEqual(status, 1)
        self.assertIn("caller interrupted", errors)
        self.assertFalse(self.executor.container_present)

    def test_running_state_cannot_be_mistaken_for_exit_success(self):
        """A successful attachment reply is not evidence that the workload has exited."""
        self.executor.bad_exit_state = True
        status, _, errors = self.execute()
        self.assertEqual(status, 1)
        self.assertIn("without a verified final exit status", errors)
        self.assertFalse(self.executor.container_present)

    def test_uncertain_creation_keeps_recovery_even_when_ps_is_empty(self):
        """A timed-out create may complete later; an empty inspection cannot justify discarding recovery data."""
        self.executor.creation_timeout = True
        status, _, errors = self.execute()
        self.assertEqual(status, 1)
        self.assertIn("Create was not confirmed", errors)
        recovery = next(line.split("Recovery: ", 1)[1] for line in errors.splitlines()
                        if "Recovery: " in line)
        self.assertTrue(Path(recovery).is_file())
        recovery_text = Path(recovery).read_text()
        self.assertIn(f"Image ID: {repository.CHECKER_IMAGE_ID}", recovery_text)
        self.assertIn("docker --context rootless ps -a", recovery_text)
        self.assertIn(f"--filter label={finite_job.OPERATION_LABEL}=", recovery_text)
        self.assertIn("container.cid", recovery_text)
        self.assertNotIn("<context>", recovery_text)
        self.addCleanup(shutil.rmtree, Path(recovery).parent)

    def test_unconfirmed_cleanup_retains_receipt(self):
        """Keep ownership evidence when Docker refuses to remove the container."""
        self.executor.failed_removal = True
        status, _, errors = self.execute()
        self.assertEqual(status, 1)
        self.assertTrue(self.executor.container_present)
        self.assertIn("CLEANUP UNCONFIRMED", errors)
        path = Path(errors.split("Retained recovery record: ", 1)[1].splitlines()[0])
        self.assertTrue(path.is_file())
        self.addCleanup(shutil.rmtree, path.parent)

    def test_ownership_mismatch_prohibits_deletion(self):
        """A search-label match must never authorize deleting an unrelated container."""
        self.executor.malformed_ownership = True
        status, _, errors = self.execute()
        self.assertEqual(status, 1)
        self.assertTrue(self.executor.container_present)
        self.assertIn("Refusing to remove", errors)
        path = Path(errors.split("Retained recovery record: ", 1)[1].splitlines()[0])
        self.addCleanup(shutil.rmtree, path.parent)

    def test_parallel_test_command_is_an_adapter_choice(self):
        """Switching to unittest-parallel must change only the job command, not its container grants."""
        replacement = finite_job.Command(
            executable="unittest-parallel",
            arguments=(*repository.UNITTEST_DISCOVERY_ARGS, "--level=module", "-j", "2"),
        )
        job = repository.repository_test_job(self.root, repository.CHECKER_IMAGE_ID, replacement)
        create = finite_job.container_arguments(job, "test-token", "test-owner")
        self.assertIn("--entrypoint=unittest-parallel", create)
        self.assertEqual(tuple(create[-len(replacement.arguments):]), replacement.arguments)
        self.assertNotIn("/workspace/infrastructure/testing/unittest_runner.py", create)

    def test_generic_job_does_not_depend_on_repository_checks(self):
        """A finite Docker job can execute an unrelated command without importing test selection."""
        job = finite_job.Job(
            image_id=repository.CHECKER_IMAGE_ID,
            command=finite_job.Command("/bin/echo", ("hello",)),
            mounts=(),
        )
        create = finite_job.container_arguments(job, "test-token", "test-owner")
        self.assertEqual(create[-2:], [repository.CHECKER_IMAGE_ID, "hello"])
        self.assertIn("--entrypoint=/bin/echo", create)

    def test_networked_acquisition_is_separate(self):
        """Image acquisition may pull; offline repository checking must not."""
        status, output, errors = self.execute("acquire")
        self.assertEqual(status, 0, errors)
        self.assertIn("Acquired checker image", output)
        calls = [argv for argv, _, _ in self.executor.docker_calls]
        self.assertEqual(sum("pull" in argv for argv in calls), 1)
        self.assertFalse(any("create" in argv for argv in calls))

    def test_missing_pinned_image_has_explicit_remedy(self):
        """An absent image requires an explicit acquisition command, not an implicit download."""
        self.executor.checker_image_present = False
        with self.assertRaisesRegex(docker_client.OperationError, "repository/image/acquire"):
            self.execute()
        self.assertFalse(self.executor.container_present)

    def test_reject_nonrootless_and_missing_cgroup_enforcement(self):
        """Do not run when the selected daemon cannot meet the declared security and resource requirements."""
        for changed in ({"SecurityOptions": []}, {"CgroupVersion": "1"}, {"Architecture": "arm64"}):
            with self.subTest(changed=changed):
                self.executor.engine_information = FAKE_ENGINE_INFORMATION | changed
                with self.assertRaises(docker_client.OperationError):
                    self.execute()
                self.assertFalse(self.executor.container_present)

    def test_explicit_selection_not_ambient_default(self):
        """Malformed or missing selection must fail before any Docker call, despite ambient settings."""
        for input_name in (None, "", "../../engine", "bad context", "$(shell touch /tmp/nope)"):
            with self.subTest(input_name=input_name):
                self.environment["HELLO_WORLD_DOCKER_CONTEXT"] = input_name
                with self.assertRaises(docker_client.OperationError):
                    self.execute()
        self.assertFalse(self.executor.docker_calls)

    def test_remote_endpoint_rejected(self):
        """A Docker context pointing at a remote daemon must not be admitted as local."""
        self.executor.engine_endpoint = "tcp://not-allowed:2376"
        with self.assertRaisesRegex(docker_client.OperationError, "local Unix socket"):
            self.execute()

    def test_source_symlink_and_unknown_root_rejected_before_create(self):
        """Unclassified files or symlinks must fail before any container obtains source access."""
        extra = self.root / "config.env"
        extra.write_text("SECRET=hello")
        with self.assertRaisesRegex(docker_client.OperationError, "Unclassified"):
            self.execute()
        extra.unlink()
        (self.root / "infrastructure" / "link").symlink_to("/etc/passwd")
        with self.assertRaisesRegex(docker_client.OperationError, "Refusing link"):
            self.execute()
        self.assertFalse(self.executor.container_present)

    def test_private_roots_are_never_mounted(self):
        """Git metadata and secrets remain invisible even when they exist beside public files."""
        (self.root / ".git").mkdir()
        (self.root / ".git" / "config").write_text("private")
        (self.root / "secrets").mkdir()
        (self.root / "secrets" / "key").write_text("private")
        mounts = repository.public_mounts(self.root)
        self.assertEqual(len(mounts), 3)
        self.assertFalse(any("secrets" in str(value.source) or "/.git" in str(value.source) for value in mounts))

    def test_mount_paths_are_admitted_independently(self):
        """Source and destination paths must each satisfy Docker's mount grammar."""
        valid_mount = finite_job.ReadOnlyMount(Path("/tmp/source"), "/workspace/source")
        valid_job = finite_job.Job(
            image_id=repository.CHECKER_IMAGE_ID,
            command=finite_job.Command("python3"),
            mounts=(valid_mount,),
        )
        arguments = finite_job.container_arguments(valid_job, "operation", "owner")
        self.assertIn("type=bind,source=/tmp/source,target=/workspace/source,readonly", arguments)

        for invalid_mount in (
            finite_job.ReadOnlyMount(Path("relative"), "/workspace/source"),
            finite_job.ReadOnlyMount(Path("/tmp/source"), "relative"),
            finite_job.ReadOnlyMount(Path("/tmp,source"), "/workspace/source"),
            finite_job.ReadOnlyMount(Path("/tmp/source"), "/workspace,source"),
            finite_job.ReadOnlyMount(Path("/tmp/source"), "/workspace/line\nbreak"),
        ):
            with self.subTest(invalid_mount=invalid_mount):
                invalid_job = finite_job.Job(
                    image_id=repository.CHECKER_IMAGE_ID,
                    command=finite_job.Command("python3"),
                    mounts=(invalid_mount,),
                )
                with self.assertRaises(docker_client.OperationError):
                    finite_job.container_arguments(invalid_job, "operation", "owner")


    def test_cleanup_refuses_subsecond_remaining_budget(self):
        """Cleanup may not round an insufficient time allowance up to one second."""
        unused_docker = Mock()
        observed_times = iter((0.0, finite_job.CLEANUP_TIMEOUT - 0.5))

        with patch.object(
            finite_job.time,
            "monotonic",
            side_effect=lambda: next(observed_times, finite_job.CLEANUP_TIMEOUT - 0.5),
        ):
            with self.assertRaisesRegex(TimeoutError, "cleanup budget exhausted"):
                finite_job.cleanup(unused_docker, "operation", "owner", repository.CHECKER_IMAGE_ID)

        unused_docker.call.assert_not_called()

    def test_full_ids_only_and_no_collision(self):
        """Cleanup requires unique full IDs; shortened or duplicated IDs are not safe deletion targets."""
        with self.assertRaisesRegex(docker_client.OperationError, "malformed"):
            finite_job.container_ids("abc\n")
        with self.assertRaisesRegex(docker_client.OperationError, "duplicate"):
            finite_job.container_ids(FAKE_CONTAINER_ID + "\n" + FAKE_CONTAINER_ID + "\n")


    def test_partial_signal_handler_installation_restores_previous_handlers(self):
        """Failure while installing SIGTERM must restore an earlier SIGINT handler."""
        observed_signal_calls = []

        def simulate_signal_installation(signal_number, replacement_handler):
            observed_signal_calls.append((signal_number, replacement_handler))
            if len(observed_signal_calls) == 2:
                raise RuntimeError("SIGTERM installation failed")
            return repository.signal.SIG_DFL

        with patch.object(repository.signal, "signal", side_effect=simulate_signal_installation):
            with self.assertRaisesRegex(RuntimeError, "SIGTERM installation failed"):
                repository.main(["check"])

        self.assertEqual(
            [signal_number for (signal_number, _) in observed_signal_calls],
            [repository.signal.SIGINT, repository.signal.SIGTERM, repository.signal.SIGINT],
        )
        self.assertIs(observed_signal_calls[-1][1], repository.signal.SIG_DFL)


class NativeClientContracts(unittest.TestCase):
    """Test the local process wrapper directly, without invoking Docker."""
    def test_capture_is_bounded_even_for_large_output(self):
        """The native client must drain long output without retaining unbounded memory."""
        client = docker_client.NativeExecutor()
        command = [sys.executable, "-I", "-c", "import sys;sys.stdout.write('x'*1500000)"]
        result = client.run(command, docker_client.clean_environment(os.environ.copy()), timeout=10)
        self.assertEqual(result.status, 0)
        self.assertTrue(result.truncated)
        self.assertEqual(len(result.output), docker_client.MAX_OUTPUT)

    def test_native_timeout_does_not_leave_its_client(self):
        """The local subprocess is terminated at its deadline, independently of any daemon work."""
        client = docker_client.NativeExecutor()
        command = [sys.executable, "-I", "-c", "import time;time.sleep(30)"]
        with self.assertRaises(TimeoutError):
            client.run(command, docker_client.clean_environment(os.environ.copy()), timeout=1)


class InputContracts(unittest.TestCase):
    """Test admission of endpoint addresses and propagation of client errors."""
    def test_socket_addresses(self):
        """Reject remote, relative and malformed endpoints before connecting to Docker."""
        self.assertEqual(str(docker_client.socket_path("unix:///run/user/1000/docker.sock")),
                         "/run/user/1000/docker.sock")
        for endpoint in ("tcp://elsewhere", "unix://relative", "unix:///a\nb", "ssh://hostname", "unix:///a?b"):
            with self.subTest(endpoint=endpoint), self.assertRaises(docker_client.OperationError):
                docker_client.socket_path(endpoint)

    def test_owned_rootless_socket_requires_actual_socket(self):
        """A plain user-owned file is not enough to establish a local Docker endpoint."""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "not-a-socket"
            path.write_text("fake")
            with self.assertRaisesRegex(docker_client.OperationError, "user-owned"):
                docker_client.verify_socket("unix://" + str(path))

    def test_checked_result_does_not_relabel_errors(self):
        """Retain the original Docker exit status and diagnostic text for callers."""
        with self.assertRaisesRegex(docker_client.OperationError, "exit 33"):
            docker_client.checked_result(docker_client.Result(33, "trace was retained"), "context")


if __name__ == "__main__":
    unittest.main()
