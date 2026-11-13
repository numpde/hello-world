"""Explicit acquisition grants use the same owned lifecycle as default finite jobs."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from infrastructure.docker import client, finite_job


IMAGE = "sha256:" + "a" * 64


class InspectingDocker:
    def __init__(self, job, host_changes=None, mount_changes=None):
        self.job = job
        self.host_changes = host_changes or {}
        self.mount_changes = mount_changes or {}

    def call(self, *_args, **_kwargs):
        host = {
            "NetworkMode": self.job.network,
            "ReadonlyRootfs": True,
            "Privileged": False,
            "NanoCpus": 2_000_000_000,
            "Memory": 536_870_912,
            "MemorySwap": 536_870_912,
            "PidsLimit": 64,
            "CapDrop": ["ALL"], "CapAdd": None,
            "SecurityOpt": ["no-new-privileges:true"],
            "LogConfig": {"Type": "none"},
            "Ulimits": [{"Name": "nofile", "Soft": 256, "Hard": 256}],
            "Tmpfs": {"/tmp": "rw,nosuid,nodev,noexec,size=67108864"},
            "PortBindings": None,
            "PublishAllPorts": False,
        } | self.host_changes
        mounts = [
            {"Type": "bind", "Source": str(mount.source), "Destination": mount.target, "RW": False}
            for mount in self.job.mounts
        ] + [
            {"Type": "bind", "Source": str(mount.source), "Destination": mount.target, "RW": True}
            for mount in self.job.writable_mounts
        ]
        for mount in mounts:
            mount.update(self.mount_changes)
        return client.Result(0, json.dumps({
            "HostConfig": host,
            "Config": {"User": "0:0" if self.job.writable_mounts else "65532:65532"},
            "Mounts": mounts,
        }))


class AcquisitionJobContracts(unittest.TestCase):
    def test_default_is_still_networkless_read_only(self):
        job = finite_job.Job(IMAGE, finite_job.Command("python", ("-V",)), ())
        arguments = finite_job.container_arguments(job, "op", "owner")
        self.assertIn("--network=none", arguments)
        self.assertIn("--user=65532:65532", arguments)
        self.assertNotIn("--user=0:0", arguments)
        finite_job.verify_container_posture(InspectingDocker(job), "id", job)

    def test_explicit_acquisition_has_only_requested_grants(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)
            job = finite_job.Job(
                IMAGE, finite_job.Command("uv", ("lock",)),
                (finite_job.ReadOnlyMount(output, "/input"),),
                writable_mounts=(finite_job.WritableMount(output, "/output"),),
                network="bridge", timeout=600,
            )
            arguments = finite_job.container_arguments(job, "op", "owner")
            self.assertIn("--network=bridge", arguments)
            self.assertIn("--user=0:0", arguments)
            self.assertIn(f"type=bind,source={output},target=/input,readonly", arguments)
            self.assertIn(f"type=bind,source={output},target=/output", arguments)
            finite_job.verify_container_posture(InspectingDocker(job), "id", job)

            for changed in (
                {"NetworkMode": "host"},
                {"PortBindings": {"1234/tcp": [{"HostPort": "1234"}]}},
                {"CapAdd": ["SYS_ADMIN"]},
            ):
                with self.subTest(changed=changed), self.assertRaises(client.OperationError):
                    finite_job.verify_container_posture(
                        InspectingDocker(job, host_changes=changed), "id", job
                    )
            with self.assertRaisesRegex(client.OperationError, "Mount inventory"):
                finite_job.verify_container_posture(
                    InspectingDocker(job, mount_changes={"RW": False}), "id", job
                )

    def test_invalid_network_deadline_and_symlink_output_rejected_before_create(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)
            command = finite_job.Command("uv")
            for network in ("host", "container:other", "unreviewed"):
                with self.subTest(network=network), self.assertRaises(client.OperationError):
                    finite_job.container_arguments(
                        finite_job.Job(IMAGE, command, (), network=network), "op", "owner"
                    )
            for timeout in (0, 901, 1.5):
                with self.subTest(timeout=timeout), self.assertRaises(client.OperationError):
                    finite_job.container_arguments(
                        finite_job.Job(IMAGE, command, (), timeout=timeout), "op", "owner"
                    )
            (output / "link").symlink_to(output, target_is_directory=True)
            with self.assertRaises(client.OperationError):
                finite_job.container_arguments(
                    finite_job.Job(
                        IMAGE, command, (),
                        writable_mounts=(finite_job.WritableMount(output / "link", "/output"),),
                    ), "op", "owner"
                )


if __name__ == "__main__":
    unittest.main()
