import os
import sqlite3
import sys
import uuid
from contextlib import closing, contextmanager, redirect_stderr, redirect_stdout

import abfe.registry.values as registry_values


@contextmanager
def preparation_log(path):
    # Capture child-process output as well as Python diagnostics, restoring both on failure.
    sys.stdout.flush()
    sys.stderr.flush()
    saved = [os.dup(1), os.dup(2)]
    try:
        with path.open("x") as stream:
            os.dup2(stream.fileno(), 1)
            os.dup2(stream.fileno(), 2)
            with redirect_stdout(stream), redirect_stderr(stream):
                yield
    finally:
        for fd, original in zip((1, 2), saved):
            os.dup2(original, fd)
            os.close(original)


def save_record(registry, record):
    with closing(sqlite3.connect(registry)) as db, db:
        db.execute(
            "CREATE TABLE IF NOT EXISTS preparations (id TEXT PRIMARY KEY, compound TEXT NOT NULL, record TEXT NOT NULL)"
        )
        db.execute(
            "INSERT INTO preparations VALUES (?,?,?)",
            (str(uuid.uuid4()), record["compound"], registry_values.encode(record)),
        )
