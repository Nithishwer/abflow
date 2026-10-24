"""Select campaign actions once; save them only for execution."""

import sqlite3
import sys
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

import abfe.analysis.fingerprints as fingerprints
import abfe.execution.files as files
import abfe.execution.journal as execution_journal
import abfe.execution.transport.dispatch as transport
import abfe.operator.next_evidence as evidence
import abfe.operator.next_select as selector
import abfe.operator.permissions as permissions
import abfe.operator.records as records
import abfe.registry.values as values


def code_identity():
    package = Path(__file__).parents[1]
    sources = {
        str(path.relative_to(package)): files.sha(path)
        for folder in (
            "operator",
            "commands",
            "execution",
            "monitoring",
            "preparation",
            "analysis",
        )
        for path in sorted((package / folder).rglob("*.py"))
    }
    sources["mdp.py"] = files.sha(package / "mdp.py")
    return files.digest(sources)


def command(registry, scope_id, entry):
    executable = Path(sys.executable).resolve().parent / "abflow"
    prefix = (
        [str(executable)]
        if executable.is_file()
        else [str(Path(sys.executable).resolve()), "-m", "abfe.commands.main"]
    )
    return entry.get("executable", prefix) + [
        "--registry",
        str(Path(registry).resolve()),
        "next-step",
        entry["operation"],
        "--plan-id",
        scope_id,
        "--run-id",
        entry["run_id"],
    ]


def freeze(registry, entry, row, reviewed, limits, rows):
    operation = selector.INTERNAL.get(entry["operation"], entry["operation"])
    entry["operations"] = [operation] if operation else []
    entry["inputs"] = (
        [fingerprints.fingerprint(path) for path in row["spec"]["input_files"]]
        if operation
        else []
    )
    entry["analysis_id"] = (row.get("analysis_record") or {}).get("id")
    entry["execution_inventory"] = files.digest(
        [
            (item["action_id"], item["state"], item["plan"]["fingerprint"])
            for item in row.get("executions", [])
        ]
    )
    if not operation:
        return
    argv = command(registry, "", entry)
    entry["executable"] = argv[: argv.index("--registry")]
    if entry.get("config", {}).get("mode") in ("continue", "extend"):
        entry["operations"].append(entry["config"]["mode"])
    workload = entry.get("config", {}).get("workload")
    entry["limits"] = entry.get("limits") or reviewed.get("limits", {}).get(
        workload, limits
    )
    if operation in ("plan", "submit"):
        cluster = entry["config"]["cluster"]
        if (
            type(entry["limits"].get(cluster)) is not int
            or entry["limits"][cluster] < 1
        ):
            raise ValueError(
                "No recorded positive submitted-job ceiling for this execution profile"
            )
    if operation == "prepare":
        entry["affected_evidence"] = {
            sibling["spec"]["id"]: dict(
                spec=sibling["spec"],
                analysis_id=(sibling.get("analysis_record") or {}).get("id"),
                stage=(sibling.get("observation") or {}).get("stage"),
            )
            for sibling in rows
            if sibling["spec"]["id"] in entry["affected_runs"]
        }
    if operation in ("stage", "submit", "retrieve"):
        current = execution_journal.load(registry, entry["action_id"])
        if operation == "stage":
            if (
                files.snapshot(current["plan"]["config"]["source"])
                != current["plan"]["source_snapshot"]
            ):
                raise ValueError(
                    "Prepared upload source differs from the execution plan"
                )
        if operation == "retrieve":
            if current["state"] == "retrieved":
                permissions.verified_snapshot(current)
                entry["transfer_digest"] = files.digest(current["transfer"])
            else:
                entry["remote_snapshot"] = transport.remote(
                    current["plan"], "retrieve"
                )["snapshot"]


def propose(registry):
    registry = Path(registry).resolve()
    metadata, rows, coverage = evidence.collect(registry)
    metadata = dict(
        metadata,
        registry=str(registry),
        registry_parent=str(registry.parent),
        coverage=coverage,
    )
    inputs, limits, history = evidence.stored_inputs(registry, metadata)
    entries, assigned = [], set()
    for row in sorted(rows, key=lambda item: item["spec"]["id"]):
        run_id = row["spec"]["id"]
        if run_id in assigned:
            continue
        reviewed = inputs.get(run_id, {})
        try:
            entry = selector.select(row, metadata, reviewed, history, rows)
            freeze(registry, entry, row, reviewed, limits, rows)
        except (OSError, ValueError, KeyError, IndexError, ArithmeticError) as exc:
            entry = dict(
                run_id=run_id,
                spec=row["spec"],
                operation=None,
                operations=[],
                inputs=[],
                state="blocked",
                reason=str(exc),
                affected_runs=[run_id],
            )
            if isinstance(exc, selector.InvalidEvidence):
                entry["incident"] = dict(code="next-step-evidence", message=str(exc))
        entries.append(entry)
        if entry["operation"] == "prepare-input":
            assigned.update(entry["affected_runs"])
    # Every run remains visible even when one shared preparation owns its command.
    indexed = {entry["run_id"] for entry in entries}
    for row in rows:
        run_id = row["spec"]["id"]
        if run_id not in indexed:
            owner = next(entry for entry in entries if run_id in entry["affected_runs"])
            entries.append(
                dict(
                    run_id=run_id,
                    spec=row["spec"],
                    operation=None,
                    operations=[],
                    inputs=[],
                    state="waiting",
                    reason="Shared preparation command: " + owner["run_id"],
                    affected_runs=[run_id],
                )
            )
    entries.sort(key=lambda entry: entry["run_id"])
    return dict(
        kind="next-step",
        schema_version=1,
        authorization=None,
        registry=str(registry),
        created_at=datetime.now(timezone.utc).isoformat(),
        code_identity=code_identity(),
        coverage=coverage,
        max_submitted=limits,
        runs=entries,
    )


def summarize(scope):
    entries = scope["runs"]
    return dict(
        counts={
            state: sum(entry["state"] == state for entry in entries)
            for state in ("ready", "complete", "waiting", "blocked")
        },
        actions={
            operation: sum(entry["operation"] == operation for entry in entries)
            for operation in selector.OPERATIONS
            if any(entry["operation"] == operation for entry in entries)
        },
    )


def save(registry, scope):
    """Register the selected actions immediately before execution."""
    scope_id = files.digest(scope)
    with records.lock(registry):
        with (
            closing(
                sqlite3.connect(
                    Path(registry).resolve().as_uri() + "?mode=rw", uri=True
                )
            ) as db,
            db,
        ):
            records.tables(db)
            db.execute(
                "INSERT INTO operator_scopes VALUES (?, ?)",
                (scope_id, values.encode(scope)),
            )
        for entry in scope["runs"]:
            if entry.get("incident"):
                records.incident(registry, entry["run_id"], **entry["incident"])
    return scope_id
