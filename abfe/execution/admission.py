from contextlib import closing

import abfe.analysis.records as analysis_records
import abfe.execution.journal as execution_journal
import abfe.registry.read as registry_read
from abfe.monitoring.jobs import matching_jobs


def unblocked(registry, run_id, action_id=None):
    metadata, rows, row = analysis_records.selected(registry, run_id)
    if any(i["status"] == "open" for i in row["incidents"]):
        raise ValueError("Open incidents require explicit resolution")
    with closing(registry_read.readonly(registry)) as db:
        last = {}
        for record in execution_journal.records(db):
            if record["run_id"] == run_id:
                last[record["action_id"]] = record
    active = [
        r
        for key, r in last.items()
        if key != action_id
        and r["state"] not in ("planned", "retrieved")
        and not (r["state"] == "resolved" and r.get("resolution"))
    ]
    if active or matching_jobs(row, metadata):
        raise ValueError(
            "An earlier execution or scheduler match must be reconciled/retrieved first"
        )
    return metadata, rows, row
