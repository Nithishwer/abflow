from contextlib import closing
from pathlib import Path

import abfe.execution.dispatch as execution_dispatch
import abfe.execution.journal as execution_journal
import abfe.monitoring.inventory as monitoring_inventory
import abfe.monitoring.scheduler as monitoring_scheduler
import abfe.registry.manifest as registry_manifest
import abfe.registry.read as registry_read


def refresh(registry, publish_dir=None, remote=False, *, publish=True):
    """Refresh campaign evidence once; submission and repair are never performed."""
    from abfe.console.publish import publish_reports

    destination = Path(publish_dir) if publish_dir else Path(registry).parent
    metadata, rows = registry_read.read_registry(registry)
    registry_manifest.check_destination(
        destination, metadata, [row["spec"] for row in rows]
    )
    errors = []
    monitored = []
    if remote:
        connections = monitoring_scheduler.check_connections(registry)
        for alias, connection in connections.items():
            if (
                monitoring_scheduler.connection_state(connection) != "connected"
                or connection["scheduler"]["state"] != "observed"
            ):
                errors.append(
                    {
                        "cluster": alias,
                        "reason": "Own-job queue unavailable; job state unknown",
                    }
                )
        with closing(registry_read.readonly(registry)) as db:
            latest = {
                record["action_id"]: record for record in execution_journal.records(db)
            }
        for action_id, record in latest.items():
            state = record["state"]
            uncertain = state == "paused" and record.get("operation") == "submit"
            if (
                state not in ("submitted", "submission_uncertain", "active", "unknown")
                and not uncertain
            ):
                continue
            alias = record["plan"]["config"]["cluster"]
            connection = connections[alias]
            if (
                monitoring_scheduler.connection_state(connection) != "connected"
                or connection["scheduler"]["state"] != "observed"
            ):
                continue
            try:
                result = execution_dispatch.action(registry, action_id, "monitor")
                monitored.append({"action_id": action_id, "state": result["state"]})
                if result["state"] in ("failed", "unknown", "paused"):
                    errors.append(
                        {"action_id": action_id, "reason": "Execution needs review"}
                    )
            except (OSError, ValueError) as exc:
                errors.append({"action_id": action_id, "reason": str(exc)})
    count = monitoring_inventory.scan(registry)
    if publish:
        publish_reports(registry, destination)
    return {
        "state": "incomplete" if errors else "refreshed",
        "runs": count,
        "remote": remote,
        "monitored": monitored,
        "errors": errors,
    }
