import fcntl
import json
import sqlite3
import uuid
from contextlib import closing
from pathlib import Path

import abfe.analysis.records as analysis_records
import abfe.execution.admission as execution_admission
import abfe.execution.files as files
import abfe.execution.journal as execution_journal
import abfe.execution.profile as execution_profile
import abfe.operator.permissions as operator_permissions
import abfe.operator.records as operator_records
import abfe.registry.manifest as registry_manifest
import abfe.registry.read as registry_read
import abfe.registry.values as registry_values


def map_source(registry, action_id):
    with (
        (Path(registry).parent / "analysis.lock").open("a") as analysis_lock,
        Path(str(registry) + ".execution.lock").open("a") as execution_lock,
    ):
        fcntl.flock(analysis_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(execution_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return _map_source(registry, action_id)


def _map_source(registry, action_id):
    current = execution_journal.load(registry, action_id)
    plan = current["plan"]
    operator_permissions.authorize(
        registry,
        current["run_id"],
        "map-source",
        config=plan["config"],
        action_id=action_id,
    )
    execution_admission.unblocked(registry, current["run_id"], action_id)
    if (
        current["state"] != "retrieved"
        or current["transfer"]["state"] != "verified_local"
    ):
        raise ValueError("Source mapping requires a verified retrieval")
    receipt = current["transfer"]
    output = operator_permissions.verified_snapshot(current)
    target = (
        output / "data" / execution_profile.relative(plan["config"]["relative_run"])
    )
    if not target.is_dir():
        raise ValueError("Missing retrieved run directory")
    key = "retrieved_source_mapping:" + action_id
    metadata, rows, row = analysis_records.selected(registry, current["run_id"])
    with closing(registry_read.readonly(registry)) as db:
        before_row = tuple(
            db.execute(
                "SELECT spec,observation,observed_at FROM runs WHERE id=?",
                (current["run_id"],),
            ).fetchone()
        )
    if json.loads(before_row[0]) != row["spec"]:
        raise ValueError("Registered source changed during reconciliation")
    context = operator_records.CURRENT.get() or {}
    record = dict(
        run_id=current["run_id"],
        action_id=action_id,
        previous_spec=row["spec"],
        previous_observation=json.loads(before_row[1]) if before_row[1] else None,
        new_path=str(target.resolve()),
        receipt_sha256=files.digest(receipt),
        scope_id=context.get("scope_id"),
        operator_intent=operator_records.intent_id(),
        at=registry_values.now(),
    )
    with closing(sqlite3.connect(registry)) as db, db:
        db.execute("BEGIN IMMEDIATE")
        stored = tuple(
            db.execute(
                "SELECT spec,observation,observed_at FROM runs WHERE id=?",
                (current["run_id"],),
            ).fetchone()
        )
        if stored != before_row:
            raise ValueError(
                "Run mapping/observation changed during source reconciliation"
            )
        previous = db.execute(
            "SELECT value FROM metadata WHERE key=?", (key,)
        ).fetchone()
        if previous:
            previous = json.loads(previous[0])
            if (
                previous["new_path"] != record["new_path"]
                or previous.get("receipt_sha256") != record["receipt_sha256"]
            ):
                raise ValueError(
                    "Existing source mapping conflicts; explicit reconciliation required"
                )
            return previous
        spec = dict(row["spec"], path=record["new_path"])
        previous_analysis = row.get("analysis_record")
        if previous_analysis:
            # An authorized new snapshot supersedes the previous analysis without repairing it.
            identity = uuid.uuid4().hex
            directory = Path(registry).resolve().parent / "analyses" / identity
            registry_manifest.check_destination(
                directory, metadata, [r["spec"] for r in rows]
            )
            directory.mkdir(parents=True)
            marker = dict(
                id=identity,
                run_id=current["run_id"],
                kind="leg",
                state="superseded",
                created_at=registry_values.now(),
                output=str(directory),
                supersedes=previous_analysis["id"],
                source_mapping=key,
                reason="Verified new retrieval mapped; fresh analysis required",
                operator_intent=operator_records.intent_id(),
                decision=dict(
                    action="analyse",
                    reason="New verified source needs fresh analysis",
                    next_target_ns=None,
                ),
            )
            files.write(directory / "record.json", marker)
            db.execute(
                "INSERT INTO analyses VALUES (?,?,?,?)",
                (
                    identity,
                    current["run_id"],
                    marker["created_at"],
                    registry_values.encode(marker),
                ),
            )
            record["superseded_analysis_id"] = previous_analysis["id"]
        db.execute(
            "INSERT INTO metadata VALUES (?,?)", (key, registry_values.encode(record))
        )
        db.execute(
            "INSERT INTO history(run_id,observed_at,observation) VALUES (?,?,?)",
            (
                current["run_id"],
                before_row[2] or registry_values.now(),
                before_row[1] or registry_values.encode({}),
            ),
        )
        db.execute(
            "UPDATE runs SET spec=?,observation=NULL,observed_at=NULL WHERE id=?",
            (registry_values.encode(spec), current["run_id"]),
        )
    return record
