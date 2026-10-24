import sqlite3
import uuid
from contextlib import closing

import abfe.analysis.fingerprints as analysis_fingerprints
import abfe.execution.journal as execution_journal
import abfe.operator.records as operator_records
import abfe.operator.view as operator_view
import abfe.registry.values as registry_values


def incident_event(
    registry,
    run_id,
    code,
    operation,
    note,
    authorization=None,
    evidence=(),
    allow_resume=False,
):
    if operation not in ("acknowledge", "resolve", "resume") or not note.strip():
        raise ValueError("Select an incident operation and provide a note")
    if operation != "acknowledge" and (
        not authorization or not authorization.strip() or not evidence
    ):
        raise ValueError(
            "Resolution/resumption needs explicit authorization and evidence files"
        )
    if operation == "acknowledge" and allow_resume:
        raise ValueError("Acknowledgement cannot authorize resumption")
    with operator_records.lock(registry):
        current = None
        if (allow_resume or operation == "resume") and code.startswith("execution:"):
            current = execution_journal.load(registry, code.split(":", 1)[1])
            if current["run_id"] != run_id:
                raise ValueError("Incident/execution identity mismatch")
        with closing(sqlite3.connect(registry)) as db, db:
            db.row_factory = sqlite3.Row
            operator_records.tables(db)
            row = db.execute(
                "SELECT * FROM incidents WHERE run_id=? AND code=?", (run_id, code)
            ).fetchone()
            if row is None or row["status"] != "open":
                raise ValueError("Select an open incident")
            previous = [
                e
                for e in operator_records.events(db)
                if e["run_id"] == run_id and e.get("incident_code") == code
            ]
            if operation == "resume":
                resolved = next(
                    (e for e in reversed(previous) if e["operation"] == "resolve"), None
                )
                if (
                    not resolved
                    or resolved["incident"]["last_seen"] != row["last_seen"]
                ):
                    raise ValueError(
                        "Resolve the current incident before authorizing resume"
                    )
                for identity in resolved["evidence"]:
                    if analysis_fingerprints.fingerprint(identity["path"]) != identity:
                        raise ValueError("Resolution evidence changed")
            record = dict(
                id=uuid.uuid4().hex,
                scope_id="",
                run_id=run_id,
                at=registry_values.now(),
                state="recorded",
                operation=operation,
                incident_code=code,
                incident=dict(row),
                note=note,
                authorization=authorization,
                evidence=[analysis_fingerprints.fingerprint(p) for p in evidence],
                allow_resume=operation == "resume" or allow_resume,
            )
            db.execute(
                "INSERT INTO operator_events(scope_id,run_id,record) VALUES (?,?,?)",
                ("", run_id, registry_values.encode(record)),
            )
            if record["allow_resume"]:
                db.execute(
                    "UPDATE incidents SET status='retired' WHERE run_id=? AND code=?",
                    (run_id, code),
                )
                if current:
                    updated = dict(current, at=registry_values.now(), resolution=record)
                    db.execute(
                        "INSERT INTO execution_events(action_id,run_id,record) VALUES (?,?,?)",
                        (current["action_id"], run_id, registry_values.encode(updated)),
                    )
    operator_view.publish(registry)
    return record
