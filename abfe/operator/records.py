import fcntl
import json
import sqlite3
import uuid
from contextlib import closing, contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from pathlib import Path

import abfe.execution.files as files
import abfe.registry.read as registry_read
import abfe.registry.values as registry_values

OPERATIONS = {
    "plan",
    "stage",
    "submit",
    "monitor",
    "retrieve",
    "map-source",
    "analyse",
    "decide",
    "continue",
    "extend",
}


CURRENT = ContextVar("abflow_operator", default=None)


def tables(db):
    db.execute(
        "CREATE TABLE IF NOT EXISTS operator_scopes (id TEXT PRIMARY KEY, record TEXT NOT NULL)"
    )
    db.execute("""CREATE TABLE IF NOT EXISTS operator_events (
        revision INTEGER PRIMARY KEY, scope_id TEXT NOT NULL, run_id TEXT NOT NULL, record TEXT NOT NULL)""")


def events(db):
    if not db.execute(
        "SELECT 1 FROM sqlite_master WHERE name='operator_events'"
    ).fetchone():
        return []
    return [
        json.loads(r[0])
        for r in db.execute("SELECT record FROM operator_events ORDER BY revision")
    ]


def journal(registry, record, state, **evidence):
    record = dict(record, at=registry_values.now(), state=state, **evidence)
    with closing(sqlite3.connect(registry)) as db, db:
        tables(db)
        db.execute(
            "INSERT INTO operator_events(scope_id,run_id,record) VALUES (?,?,?)",
            (record["scope_id"], record["run_id"], registry_values.encode(record)),
        )
    return record


def incident(registry, run_id, code, message):
    at = datetime.now(timezone.utc).isoformat()
    with closing(sqlite3.connect(registry)) as db, db:
        db.row_factory = sqlite3.Row
        previous = db.execute(
            "SELECT * FROM incidents WHERE run_id=? AND code=?", (run_id, code)
        ).fetchone()
        if previous and previous["status"] == "retired":
            tables(db)
            archived = dict(
                id=uuid.uuid4().hex,
                scope_id="",
                run_id=run_id,
                at=at,
                state="recorded",
                operation="incident-reopened",
                incident_code=code,
                previous_incident=dict(previous),
            )
            db.execute(
                "INSERT INTO operator_events(scope_id,run_id,record) VALUES (?,?,?)",
                ("", run_id, registry_values.encode(archived)),
            )
        db.execute(
            """INSERT INTO incidents(run_id,code,message,first_seen,last_seen) VALUES (?,?,?,?,?)
            ON CONFLICT(run_id,code) DO UPDATE SET message=excluded.message,last_seen=excluded.last_seen,status='open' """,
            (run_id, code, message, at, at),
        )


@contextmanager
def lock(registry):
    with Path(str(registry) + ".operator.lock").open("a") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield


def load(registry, scope_id):
    with closing(registry_read.readonly(registry)) as db:
        row = db.execute(
            "SELECT record FROM operator_scopes WHERE id=?", (scope_id,)
        ).fetchone()
    if row is None:
        raise ValueError("Unknown operator scope")
    scope = json.loads(row[0])
    if files.digest(scope) != scope_id:
        raise ValueError("Operator scope fingerprint differs")
    if scope.get("kind") == "next-step":
        return scope
    path = Path(registry).resolve().parent / "operator-scopes" / scope_id / "scope.json"
    if json.loads(path.read_text()) != scope:
        raise ValueError("Saved operator scope changed")
    return scope


def entry_for(scope, run_id):
    entry = next((e for e in scope["runs"] if e["run_id"] == run_id), None)
    if entry is None:
        raise ValueError("Run is outside the saved scope")
    return entry


def intent_id():
    return (CURRENT.get() or {}).get("id")


def summarize(result):
    if "analysis_id" in result:
        return result
    if result.get("kind") == "binding":
        return dict(analysis_id=result["id"], state=result["state"])
    if "action_id" in result:
        return {k: result[k] for k in ("action_id", "state", "error") if k in result}
    if "fingerprint" in result and "config" in result:
        return dict(
            action_id=result["id"], state="planned", fingerprint=result["fingerprint"]
        )
    if "decision" in result:
        return dict(
            analysis_id=result["id"], state=result["state"], decision=result["decision"]
        )
    return result
