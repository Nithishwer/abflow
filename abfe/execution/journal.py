import json
import sqlite3
from contextlib import closing

import abfe.registry.read as registry_read
import abfe.registry.values as registry_values


def incident(registry, run_id, code, message):
    from abfe.operator.records import incident as record_incident

    record_incident(registry, run_id, code, message)


def save(registry, plan, state, **evidence):
    from abfe.operator.records import intent_id

    if intent_id():
        evidence["operator_intent"] = intent_id()
    record = dict(
        action_id=plan["id"],
        run_id=plan["run_id"],
        at=registry_values.now(),
        state=state,
        plan=plan,
        **evidence,
    )
    with closing(sqlite3.connect(registry)) as db, db:
        db.execute("""CREATE TABLE IF NOT EXISTS execution_events (
            revision INTEGER PRIMARY KEY, action_id TEXT NOT NULL, run_id TEXT NOT NULL, record TEXT NOT NULL)""")
        db.execute(
            "INSERT INTO execution_events(action_id,run_id,record) VALUES (?,?,?)",
            (plan["id"], plan["run_id"], registry_values.encode(record)),
        )
    return record


def records(db):
    if not db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='execution_events'"
    ).fetchone():
        return []
    return [
        json.loads(r[0])
        for r in db.execute("SELECT record FROM execution_events ORDER BY revision")
    ]


def load(registry, action_id):
    with closing(registry_read.readonly(registry)) as db:
        result = [r for r in records(db) if r["action_id"] == action_id]
    if not result:
        raise ValueError("Unknown execution action")
    return result[-1]
