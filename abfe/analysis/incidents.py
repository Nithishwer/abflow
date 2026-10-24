import sqlite3
from contextlib import closing

import abfe.analysis.values as analysis_values


def incident(registry, run_id, code, message):
    at = analysis_values.timestamp()
    with closing(sqlite3.connect(registry)) as db, db:
        db.execute(
            """INSERT INTO incidents(run_id,code,message,first_seen,last_seen) VALUES (?,?,?,?,?)
            ON CONFLICT(run_id,code) DO UPDATE SET message=excluded.message,last_seen=excluded.last_seen""",
            (run_id, code, message, at, at),
        )
