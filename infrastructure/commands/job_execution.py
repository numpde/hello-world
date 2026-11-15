"""One shared application finite-job dispatcher and outcome reporter."""

from __future__ import annotations

import sys

from infrastructure.docker import client, finite_job


def run(
    docker: client.DockerClient,
    context: str,
    job: finite_job.Job,
    *,
    temporary_mount: finite_job.TemporaryJobDirectory | None = None,
) -> None:
    """Execute the declared grants; report work and cleanup separately.

    Applications specify images, commands, mounts and timeouts. Only the
    common finite-job owner may establish retirement and recovery evidence.
    """
    outcome = (
        temporary_mount.run(docker, context, job)
        if temporary_mount is not None
        else finite_job.run_container(docker, context, job)
    )
    if outcome.output:
        print(outcome.output, end="" if outcome.output.endswith("\n") else "\n")
    if outcome.failure:
        print(f"APPLICATION JOB FAILURE: {outcome.failure}", file=sys.stderr)
    for diagnostic in outcome.diagnostics:
        print(diagnostic, file=sys.stderr)
    if outcome.status != 0:
        raise client.OperationError(
            "Application finite job failed; inspect preceding output and recovery evidence"
        )
