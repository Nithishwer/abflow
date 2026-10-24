import json
import sqlite3
from contextlib import closing
from pathlib import Path

import abfe.analysis.fingerprints as analysis_fingerprints
import abfe.analysis.values as analysis_values
import abfe.execution.files as files
import abfe.execution.inputs as execution_inputs
import abfe.execution.journal as execution_journal
import abfe.execution.plan as execution_plan
import abfe.execution.profile as execution_profile
import abfe.operator.records as operator_records
import abfe.operator.select as operator_select
import abfe.operator.view as operator_view
import abfe.registry.manifest as registry_manifest
import abfe.registry.read as registry_read
import abfe.registry.values as registry_values


def create_plan(registry, source):
    raw = json.loads(Path(source).read_text())
    if (
        set(raw) != {"schema_version", "authorization", "max_submitted", "runs"}
        or raw["schema_version"] != 1
    ):
        raise ValueError(
            "Expected version 1 operator scope with authorization, max_submitted and runs"
        )
    if not isinstance(raw["authorization"], str) or not raw["authorization"].strip():
        raise ValueError("Record the explicit user authorization reference")
    limits = raw["max_submitted"]
    if not isinstance(limits, dict) or any(
        c not in ("archer", "ranvier", "soma") or type(n) is not int or n < 1
        for c, n in limits.items()
    ):
        raise ValueError("Use positive per-cluster submitted-job ceilings")
    if not isinstance(raw["runs"], list) or not raw["runs"]:
        raise ValueError("Select explicit run IDs")
    metadata, rows = registry_read.read_registry(registry)
    registry_manifest.check_destination(registry, metadata, [r["spec"] for r in rows])
    indexed = {r["spec"]["id"]: r for r in rows}
    entries = []
    for item in raw["runs"]:
        if set(item) - {
            "run_id",
            "operations",
            "manifest",
            "action_id",
            "retrieval_output",
            "cutoff_ns",
        }:
            raise ValueError("Unknown operator run field")
        run_id = item["run_id"]
        if run_id not in indexed or any(e["run_id"] == run_id for e in entries):
            raise ValueError("Unknown or duplicate run ID: " + run_id)
        operations = item["operations"]
        if (
            not isinstance(operations, list)
            or not operations
            or set(operations) - operator_records.OPERATIONS
        ):
            raise ValueError("Select explicit permitted operations")
        if "submit" in operations and "monitor" not in operations:
            raise ValueError(
                "Submission permission requires monitoring for reconciliation"
            )
        if item.get("manifest") and item.get("action_id"):
            raise ValueError(
                "Choose a reviewed manifest or an existing action, not both"
            )
        entry = dict(
            item,
            spec=indexed[run_id]["spec"],
            inputs=[
                analysis_fingerprints.fingerprint(p)
                for p in indexed[run_id]["spec"]["input_files"]
            ],
        )
        if item.get("manifest"):
            path = Path(item["manifest"])
            if not path.is_absolute():
                raise ValueError("Use an absolute execution manifest path")
            config = json.loads(path.read_text())
            execution_profile.config_check(config, entry["spec"])
            source_root, _, _ = execution_inputs.source_check(config, entry["spec"])
            entry.update(
                config=config,
                manifest_identity=analysis_fingerprints.fingerprint(path),
                source_snapshot=files.snapshot(source_root),
            )
        elif item.get("action_id"):
            action = execution_journal.load(registry, item["action_id"])
            execution_plan.recheck(action["plan"])
            if action["run_id"] != run_id:
                raise ValueError("Execution belongs to another run")
            entry.update(
                config=action["plan"]["config"],
                action_fingerprint=action["plan"]["fingerprint"],
            )
        if (
            set(operations)
            & {
                "plan",
                "stage",
                "submit",
                "monitor",
                "retrieve",
                "map-source",
                "continue",
                "extend",
            }
            and "config" not in entry
        ):
            raise ValueError(
                "Execution operations require a manifest or action identity"
            )
        if "config" in entry:
            if "submit" in operations and entry["config"]["cluster"] not in limits:
                raise ValueError(
                    "Provide a submitted-job ceiling for the selected cluster"
                )
            mode = entry["config"]["mode"]
            if (
                mode in ("continue", "extend")
                and set(operations) & {"plan", "stage", "submit"}
                and mode not in operations
            ):
                raise ValueError("Explicitly permit " + mode)
        if "retrieve" in operations:
            output = Path(item.get("retrieval_output", ""))
            if not output.is_absolute():
                raise ValueError(
                    "Retrieval needs an explicit absolute output directory"
                )
            registry_manifest.check_destination(
                output, metadata, [r["spec"] for r in rows]
            )
        if "analyse" in operations:
            if (
                "cutoff_ns" not in entry
                or analysis_values.finite(entry["cutoff_ns"]) <= 0
            ):
                raise ValueError("Analysis requires a reviewed positive cutoff_ns")
        entries.append(entry)
    scope = dict(raw, runs=entries)
    scope_id = files.digest(scope)
    output = Path(registry).resolve().parent / "operator-scopes" / scope_id
    registry_manifest.check_destination(output, metadata, [r["spec"] for r in rows])
    with operator_records.lock(registry):
        with closing(sqlite3.connect(registry)) as db, db:
            operator_records.tables(db)
            if not db.execute(
                "SELECT 1 FROM operator_scopes WHERE id=?", (scope_id,)
            ).fetchone():
                output.mkdir(parents=True, exist_ok=True)
                files.write(output / "scope.json", scope)
                db.execute(
                    "INSERT INTO operator_scopes VALUES (?,?)",
                    (scope_id, registry_values.encode(scope)),
                )
    operator_view.publish(registry)
    return operator_select.overview(registry, scope_id)
