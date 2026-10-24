import json
import sqlite3
from contextlib import closing

import abfe.analysis.values as analysis_values


def tables(db):
    db.execute(
        "CREATE TABLE IF NOT EXISTS analyses (id TEXT PRIMARY KEY, run_id TEXT NOT NULL, created_at TEXT NOT NULL, record TEXT NOT NULL)"
    )
    for operation in ("UPDATE", "DELETE"):
        db.execute(
            f"CREATE TRIGGER IF NOT EXISTS immutable_analysis_{operation.lower()} BEFORE {operation} ON analyses BEGIN SELECT RAISE(ABORT, 'Analysis records are immutable'); END"
        )


def persist(registry, record, output):
    record["finished_at"] = analysis_values.timestamp()
    with (output / "record.json").open("x") as stream:
        stream.write(
            json.dumps(record, indent=2, sort_keys=True, allow_nan=False) + "\n"
        )
    with closing(sqlite3.connect(registry)) as db, db:
        tables(db)
        db.execute(
            "INSERT INTO analyses VALUES (?,?,?,?)",
            (
                record["id"],
                record["run_id"],
                record["created_at"],
                analysis_values.encode(record),
            ),
        )
        if record["state"] == "failed" or record.get("decision", {}).get("action") in (
            "stop_at_limit",
            "blocked",
        ):
            message = record.get("error") or record["decision"]["reason"]
            db.execute(
                """INSERT INTO incidents(run_id,code,message,first_seen,last_seen) VALUES (?,?,?,?,?)
                ON CONFLICT(run_id,code) DO UPDATE SET message=excluded.message,last_seen=excluded.last_seen""",
                (
                    record["run_id"],
                    "analysis:" + record["id"],
                    message,
                    record["created_at"],
                    record["created_at"],
                ),
            )


def latest(db, run_id, kind="leg"):
    if not db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='analyses'"
    ).fetchone():
        return None
    planned = None
    for row in db.execute(
        "SELECT record FROM analyses WHERE run_id=? ORDER BY rowid DESC", (run_id,)
    ):
        record = json.loads(row[0])
        if record["kind"] != kind:
            continue
        if record["state"] != "planned":
            return record
        if planned is None:
            planned = record
    return planned


def selected(registry, run_id):
    from abfe.registry.read import read_registry

    metadata_, rows = read_registry(registry)
    row = next((row for row in rows if row["spec"]["id"] == run_id), None)
    if row is None:
        raise ValueError(f"Unknown run: {run_id}")
    return metadata_, rows, row
