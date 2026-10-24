import fcntl
import subprocess
from pathlib import Path

import abfe.analysis.fingerprints as analysis_fingerprints
import abfe.analysis.run as analysis_run
import abfe.execution.admission as execution_admission
import abfe.execution.files as worker
import abfe.execution.journal as execution_journal
import abfe.execution.monitor as execution_monitor
import abfe.execution.plan as execution_plan
import abfe.execution.transfer as execution_transfer
import abfe.execution.transport.dispatch as execution_transport_dispatch
import abfe.execution.transport.limits as execution_transport_limits


def action(registry, action_id, operation, apply=False, output=None):
    if operation not in ("stage", "submit", "monitor", "retrieve"):
        raise ValueError("Unknown cluster operation")
    current = execution_journal.load(registry, action_id)
    plan = current["plan"]
    try:
        execution_plan.recheck(plan)
    except (ValueError, OSError) as exc:
        execution_journal.incident(
            registry, plan["run_id"], "execution:" + plan["id"], str(exc)
        )
        raise
    if operation != "monitor" and not apply:
        return dict(
            operation=operation,
            apply=False,
            action_id=action_id,
            cluster=plan["config"]["cluster"],
            remote=plan["remote"],
            script=str(Path(plan["output"]) / "job.sh"),
            output=str(output) if output else None,
        )
    if plan.get("executor") != "slurm-shell-v1" and operation in ("stage", "submit"):
        raise ValueError(
            "Historical worker plans are preserved; create a new plain Slurm plan"
        )
    lock_path = Path(str(registry) + ".execution.lock")
    with lock_path.open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            current = execution_journal.load(registry, action_id)
            from abfe.operator.permissions import authorize
            from abfe.operator.records import CURRENT

            if (
                operation in ("stage", "submit")
                and plan.get("operator")
                and CURRENT.get() is None
            ):
                raise ValueError(
                    "Scoped executions must advance through the operator dispatcher"
                )
            authorize(
                registry,
                plan["run_id"],
                operation,
                config=plan["config"],
                action_id=action_id,
                output=output,
            )
            if operation in ("stage", "submit"):
                execution_admission.unblocked(registry, plan["run_id"], action_id)
                if worker.snapshot(plan["config"]["source"]) != plan["source_snapshot"]:
                    raise ValueError(
                        "Source snapshot changed; create/review a new plan"
                    )
                if [
                    analysis_fingerprints.fingerprint(p["path"]) for p in plan["inputs"]
                ] != plan["inputs"]:
                    raise ValueError("Declared campaign inputs changed")
                if (
                    plan["decision"]
                    and analysis_run.current_decision(registry, plan["run_id"])
                    != plan["decision"]
                ):
                    raise ValueError("Analysis decision changed since planning")
            if operation == "stage":
                if current["state"] not in ("planned", "staging", "staged", "paused"):
                    raise ValueError("Execution already dispatched; staging is closed")
                execution_journal.save(registry, plan, "staging")
                reservation = execution_transport_dispatch.remote(plan, "reserve")
                alias = plan["config"]["cluster"] + ":" + plan["remote"]
                execution_transfer.rsync(
                    plan, plan["config"]["source"].rstrip("/") + "/", alias + "/data/"
                )
                for name in ("plan.json", "job.sh"):
                    execution_transfer.rsync(
                        plan, str(Path(plan["output"]) / name), alias + "/" + name
                    )
                verified = execution_transport_dispatch.remote(plan, "verify")
                return execution_journal.save(
                    registry, plan, "staged", reservation=reservation, transfer=verified
                )
            if operation == "submit":
                if current["state"] in (
                    "submitted",
                    "submission_uncertain",
                    "active",
                    "complete",
                    "checkpointed",
                    "unknown",
                    "retrieved",
                ):
                    return execution_monitor.action_monitor(registry, plan)
                if current["state"] != "staged":
                    raise ValueError("Submit requires verified staging")
                execution_journal.save(registry, plan, "submission_uncertain")
                submission = execution_transport_dispatch.remote(plan, "submit")
                return execution_journal.save(
                    registry, plan, "submitted", submission=submission
                )
            if operation == "monitor":
                return execution_monitor.action_monitor(registry, plan)
            return execution_transfer.retrieve(registry, plan, output)
        except execution_transport_limits.AdmissionWait as exc:
            return execution_journal.save(registry, plan, "staged", waiting=str(exc))
        except (ValueError, OSError, subprocess.SubprocessError, KeyError) as exc:
            if operation == "monitor":
                return execution_journal.save(
                    registry,
                    plan,
                    "unknown",
                    error=str(exc),
                    submission=current.get("submission"),
                    observation=current.get("observation"),
                )
            execution_journal.save(
                registry, plan, "paused", operation=operation, error=str(exc)
            )
            execution_journal.incident(
                registry,
                plan["run_id"],
                "execution:" + plan["id"],
                "{} paused: {}. Evidence preserved; explicit resolution required.".format(
                    operation, exc
                ),
            )
            raise ValueError(str(exc)) from exc
