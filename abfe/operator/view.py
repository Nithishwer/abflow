import json
import shlex
from pathlib import Path

import abfe.operator.records as operator_records
import abfe.operator.select as operator_select


def attach(db, metadata, rows):
    metadata.pop("next_step_coverage", None)
    history = operator_records.events(db)
    for row in rows:
        row.pop("next_step", None)
        row.pop("operator", None)
        row["operator_history"] = [
            e for e in history if e["run_id"] == row["spec"]["id"]
        ]
    if not db.execute(
        "SELECT 1 FROM sqlite_master WHERE name='operator_scopes'"
    ).fetchone():
        return
    indexed = {row["spec"]["id"]: row for row in rows}
    for scope_id, raw in db.execute(
        "SELECT id,record FROM operator_scopes ORDER BY rowid DESC"
    ):
        scope = json.loads(raw)
        for entry in scope["runs"]:
            row = indexed.get(entry["run_id"])
            if row is None or scope.get("kind") == "next-step" and "next_step" in row:
                continue
            per_run = [e for e in row["operator_history"] if e["scope_id"] == scope_id]
            if scope.get("kind") == "next-step":
                from abfe.operator.next_plan import command

                event = per_run[-1] if per_run else {}
                if "next_step_coverage" not in metadata:
                    metadata["next_step_coverage"] = {
                        cluster: {
                            key: value
                            for key, value in item.items()
                            if key != "variants_and_native_evidence"
                        }
                        for cluster, item in scope["coverage"]
                        .get("locations", {})
                        .items()
                    }
                proposed = dict(
                    plan_id=scope_id,
                    current_state=entry["state"],
                    proposed_operation=entry["operation"],
                    reason=entry["reason"],
                    parameters={
                        key: entry[key]
                        for key in (
                            "config",
                            "cutoff_ns",
                            "limits",
                            "retrieval_output",
                            "compatibility",
                            "decision",
                            "preparation",
                        )
                        if key in entry
                    },
                    affected_runs=entry["affected_runs"],
                    command=shlex.join(command(scope["registry"], scope_id, entry))
                    if entry["operation"]
                    else None,
                    outcome=event.get("state", "proposed"),
                    last_operation=event.get("operation"),
                    result=event.get("result"),
                    error=event.get("error") or event.get("reason"),
                )
                row["next_step"] = proposed
                continue
            row.setdefault("operator", []).insert(
                0,
                dict(
                    scope_id=scope_id,
                    authorization=scope["authorization"],
                    **operator_select.next_action(
                        scope_id, entry, row, per_run, metadata
                    ),
                ),
            )


def publish(registry):
    from abfe.console.publish import publish_reports

    publish_reports(registry, Path(registry).resolve().parent)
