"""Read the latest refresh and reviewed operating inputs without registering changes."""

import json
from contextlib import closing

import abfe.execution.journal as execution_journal
import abfe.operator.records as records
import abfe.registry.read as registry_read
import abfe.registry.values as values


def stored_inputs(registry, metadata):
    inputs = {
        key: dict(value) for key, value in metadata.get("next_step_inputs", {}).items()
    }
    limits = {}
    with closing(registry_read.readonly(registry)) as db:
        if db.execute(
            "SELECT 1 FROM sqlite_master WHERE name='operator_scopes'"
        ).fetchone():
            for (raw,) in db.execute(
                "SELECT record FROM operator_scopes ORDER BY rowid"
            ):
                scope = json.loads(raw)
                if scope.get("kind") == "next-step":
                    continue
                for entry in scope["runs"]:
                    target = inputs.setdefault(entry["run_id"], {})
                    if entry.get("config"):
                        key = entry["config"]["workload"]
                        target.setdefault("profiles", {})[key] = entry["config"]
                        target.setdefault("limits", {})[key] = scope["max_submitted"]
                limits.update(scope["max_submitted"])
        history = records.events(db)
        executions = execution_journal.records(db)
        if db.execute(
            "SELECT 1 FROM sqlite_master WHERE name='preparations'"
        ).fetchone():
            for (raw,) in db.execute("SELECT record FROM preparations ORDER BY rowid"):
                preparation = json.loads(raw)
                plan = preparation.get("plan")
                if not plan:
                    continue
                manifest = plan["sources"][0]["path"]
                for run_id in plan["run_ids"]:
                    inputs.setdefault(run_id, {}).setdefault(
                        "preparation",
                        dict(
                            compound=plan["compound"],
                            manifest=manifest,
                            output=plan["output"],
                            stage=plan["stage"],
                        ),
                    )
    # Saved execution settings are evidence, not new execution permission.
    for event in executions:
        config = event["plan"]["config"]
        target = inputs.setdefault(event["run_id"], {})
        target.setdefault("profiles", {}).setdefault(config["workload"], config)
    return inputs, limits, history


def collect(registry):
    metadata, rows = registry_read.read_registry(registry)
    return (
        dict(metadata, selection_at=values.now()),
        rows,
        dict(
            scanned_at=metadata.get("scanned_at"),
            connections=metadata.get("connections", {}),
        ),
    )
