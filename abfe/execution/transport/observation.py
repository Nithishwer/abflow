import json
import re

import abfe.execution.transport.files as execution_transport_files
import abfe.execution.transport.queue as execution_transport_queue
import abfe.execution.transport.ssh as execution_transport_ssh
import abfe.registry.values as registry_values


def observe(plan):
    execution_transport_files.checks(plan)
    active = execution_transport_queue.writers(
        plan, execution_transport_queue.jobs(plan)
    )
    raw = execution_transport_ssh.execute(
        plan, 'if [ -f "$root/submission.id" ]; then cat "$root/submission.id"; fi'
    ).strip()
    submission = dict(job_id=raw.split(";")[0], state="submitted") if raw else None
    if not raw and plan.get("worker_sha256"):
        raw = execution_transport_ssh.execute(
            plan,
            'if [ -f "$root/submission.json" ]; then cat "$root/submission.json"; fi',
        ).strip()
        submission = json.loads(raw) if raw else None
    text = execution_transport_ssh.execute(
        plan,
        'for f in "$root"/status/*.state "$root"/status/*.json; do [ -f "$f" ] || continue; printf "%s\\0" "${f##*/}"; cat "$f"; printf "\\0"; done',
    )
    values = text.split("\0")
    application = []
    for i in range(0, len(values) - 1, 2):
        name, value = values[i : i + 2]
        application.append(
            json.loads(value)
            if name.endswith(".json")
            else dict(
                task=name.split(".")[0], state=value.split()[0], detail=value.strip()
            )
        )
    expected = (
        len(plan["windows"])
        if plan["config"]["workload"] in ("solvent", "complex-equilibration")
        else 1
    )
    state = "active" if active else "unknown"
    if (
        not active
        and len(application) == expected
        and all(r["state"] in ("complete", "checkpointed") for r in application)
    ):
        # Completion markers alone are insufficient: retain and check actual outputs/checkpoints.
        evidence = execution_transport_ssh.execute(
            plan,
            'find "$root/data" -name "abflow-*.outputs.sha256" -type f -print0 | while IFS= read -r -d "" f; do (cd "${f%/*}"; sha256sum -c "${f##*/}"); done',
        )
        if not evidence.strip():
            raise ValueError("Missing application/checkpoint evidence")
        state = (
            "complete"
            if all(r["state"] == "complete" for r in application)
            else "checkpointed"
        )
    if any(r["state"] == "failed" for r in application):
        state = "failed"
    scheduler = dict(state="unknown")
    if not active and submission and submission.get("job_id"):
        job = submission["job_id"]
        if not re.fullmatch(r"\d+", job):
            raise ValueError("Unrecognized submission job ID")
        output = execution_transport_ssh.execute(
            plan, "scontrol show job " + job + " -o 2>&1 || true"
        )
        scheduler = dict(state="observed", report=output)
        if re.search(
            r"JobState=(FAILED|CANCELLED|TIMEOUT|NODE_FAIL|OUT_OF_MEMORY|PREEMPTED)",
            output,
        ):
            state = "failed"
        elif plan["config"]["cluster"] != "ranvier" and "Invalid job id" in output:
            output = execution_transport_ssh.command(
                plan,
                ["sacct", "-n", "-P", "-j", job, "--format=JobIDRaw,State,ExitCode"],
            )
            scheduler["accounting"] = output
            if re.search(
                r"\|(FAILED|CANCELLED|TIMEOUT|NODE_FAIL|OUT_OF_MEMORY|PREEMPTED)",
                output,
            ):
                state = "failed"
    logs = (
        execution_transport_ssh.execute(
            plan,
            'for f in "$root"/slurm-*.out; do [ ! -f "$f" ] || tail -c 4000 "$f"; done',
        )
        if state in ("failed", "unknown")
        else ""
    )
    return dict(
        state=state,
        jobs=active,
        application=application,
        submission=submission,
        scheduler=scheduler,
        logs=logs,
        at=registry_values.now(),
    )
