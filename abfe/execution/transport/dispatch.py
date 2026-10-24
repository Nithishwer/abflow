import re
import shlex

import abfe.execution.transport.files as execution_transport_files
import abfe.execution.transport.limits as execution_transport_limits
import abfe.execution.transport.observation as execution_transport_observation
import abfe.execution.transport.queue as execution_transport_queue
import abfe.execution.transport.ssh as execution_transport_ssh
import abfe.registry.values as registry_values


def remote(plan, operation):
    if operation == "reserve":
        if execution_transport_queue.writers(
            plan, execution_transport_queue.jobs(plan)
        ):
            raise ValueError("Job is writing the destination")
        capacity = execution_transport_limits.quota(plan)
        fingerprint = shlex.quote(plan["fingerprint"])
        execution_transport_ssh.execute(
            plan,
            "\n".join(
                [
                    "umask 077",
                    'if [ ! -d "$root" ]; then',
                    'mkdir -p "${root%/*}"',
                    'mkdir "$root"',
                    f'printf "%s\\n" {fingerprint} > "$root/owner"',
                    'mkdir "$root/data" "$root/status"',
                    "fi",
                    f'test "$(cat "$root/owner")" = {fingerprint}',
                    'test ! -e "$root/submission.intent"',
                ]
            ),
        )
        actual = execution_transport_files.snapshot(plan, "data")["files"]
        if any(
            k not in plan["source_snapshot"]["files"]
            or plan["source_snapshot"]["files"][k] != v
            for k, v in actual.items()
        ):
            raise ValueError("Transfer conflict or changed input snapshot")
        return dict(state="reserved", capacity=capacity)
    execution_transport_files.checks(plan, data=operation in ("verify", "submit"))
    if operation == "verify":
        return dict(state="staged", source_snapshot=plan["source_snapshot"])
    if operation == "submit":
        queue = execution_transport_queue.jobs(plan)
        if execution_transport_queue.writers(plan, queue):
            raise ValueError("Destination is already active")
        limits = execution_transport_limits.admission(plan, queue)
        capacity = execution_transport_limits.quota(plan)
        execution_transport_ssh.command(
            plan,
            [
                "sbatch",
                "--test-only",
                "--chdir=" + plan["remote"],
                plan["remote"] + "/job.sh",
            ],
        )
        output = execution_transport_ssh.execute(
            plan,
            "\n".join(
                [
                    'cd "$root"',
                    "exec 9>submission.lock",
                    "flock -n 9",
                    "if [ -f submission.id ]; then cat submission.id; exit 0; fi",
                    "test ! -e submission.intent",
                    'printf "%s\\n" '
                    + shlex.quote(plan["fingerprint"])
                    + " > submission.intent",
                    'id=$(sbatch --parsable --chdir="$root" "$root/job.sh")',
                    'printf "%s\\n" "$id" > submission.id.tmp',
                    "mv submission.id.tmp submission.id",
                    'printf "%s\\n" "$id"',
                ]
            ),
        )
        match = re.fullmatch(r"(\d+)(?:;[A-Za-z0-9_.-]+)?\s*", output)
        if not match:
            raise ValueError("Uncertain sbatch response: " + output)
        return dict(
            state="submitted",
            job_id=match[1],
            admission=limits,
            capacity=capacity,
            at=registry_values.now(),
        )
    if operation not in ("monitor", "retrieve"):
        raise ValueError("Unknown remote operation")
    result = execution_transport_observation.observe(plan)
    if operation == "retrieve":
        if result["jobs"] or result["state"] not in (
            "complete",
            "checkpointed",
            "failed",
        ):
            raise ValueError(
                "Retrieval requires stopped jobs and application/checkpoint evidence"
            )
        result["snapshot"] = execution_transport_files.snapshot(plan)
    return result
