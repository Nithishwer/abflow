from pathlib import Path

import abfe.execution.journal as execution_journal


def attach(db, rows):
    latest = {}
    for record in execution_journal.records(db):
        latest.setdefault(record["run_id"], {})[record["action_id"]] = record
    for row in rows:
        row["executions"] = list(latest.get(row["spec"]["id"], {}).values())


def summary(record):
    plan = record["plan"]
    observation = record.get("observation") or {}
    transfer = record.get("transfer") or {}
    return dict(
        action_id=plan["id"],
        state=record["state"],
        cluster=plan["config"]["cluster"],
        workload=plan["config"]["workload"],
        mode=plan["config"]["mode"],
        job_id=(record.get("submission") or {}).get("job_id"),
        observed_at=observation.get("at"),
        jobs=observation.get("jobs", []),
        application_state=observation.get("state"),
        error=record.get("error"),
        remote=plan["remote"],
        plan=str(Path(plan["output"]) / "plan.json"),
        fingerprint=plan["fingerprint"],
        transfer_state=transfer.get("state"),
        retrieved_to=transfer.get("output"),
        analysis_id=(plan.get("decision") or {}).get("analysis_id"),
    )
