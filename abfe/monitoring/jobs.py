"""Match fresh scheduler evidence to registered and managed run locations."""

from datetime import datetime
from pathlib import Path, PurePosixPath

import abfe.monitoring.scheduler as monitoring_scheduler


def matching_jobs(row, metadata, at=None):
    spec, observed = row["spec"], row["observation"] or {}
    matches = []
    if spec["path"] and Path(spec["path"]).is_relative_to(spec["source_root"]):
        relative = Path(spec["path"]).relative_to(spec["source_root"])
        for alias, root in monitoring_scheduler.CLUSTER_ROOTS.items():
            connection = metadata.get("connections", {}).get(alias, {})
            if (
                monitoring_scheduler.connection_state(connection, at=at) != "connected"
                or connection.get("scheduler", {}).get("state") != "observed"
            ):
                continue
            expected = PurePosixPath(root) / relative.as_posix()
            for job in connection["scheduler"]["jobs"]:
                directory = PurePosixPath(job["work_dir"])
                if directory == expected or expected in directory.parents:
                    matches.append(dict(job, cluster=alias))
    for execution in row.get("executions", []):
        observation = execution.get("observation") or {}
        alias = execution["plan"]["config"]["cluster"]
        connection = metadata.get("connections", {}).get(alias, {})
        live = (
            monitoring_scheduler.connection_state(connection, at=at) == "connected"
            and connection.get("scheduler", {}).get("state") == "observed"
        )
        monitored = (
            execution["state"] == "active"
            and monitoring_scheduler.connection_state(
                {"state": "connected", "checked_at": observation.get("at")}, at=at
            )
            == "connected"
        )
        # Use the newest fresh evidence; a newer empty queue must retire an older match.
        if live and (
            not monitored
            or datetime.fromisoformat(connection["checked_at"].replace("Z", "+00:00"))
            > datetime.fromisoformat(observation["at"].replace("Z", "+00:00"))
        ):
            expected = PurePosixPath(execution["plan"]["remote"])
            for job in connection["scheduler"]["jobs"]:
                directory = PurePosixPath(job["work_dir"])
                if directory == expected or expected in directory.parents:
                    matches.append(
                        dict(job, cluster=alias, observed_at=connection["checked_at"])
                    )
        elif monitored:
            matches.extend(
                dict(job, cluster=alias, observed_at=observation["at"])
                for job in observation.get("jobs", [])
            )
    # A local file or an absent queue match does not establish where a calculation is running.
    scheduler = observed.get("scheduler", {})
    if (
        not matches
        and monitoring_scheduler.connection_state(
            {"state": "connected", "checked_at": scheduler.get("observed_at")}, at=at
        )
        == "connected"
    ):
        if (
            scheduler.get("job_id")
            and scheduler.get("cluster") in monitoring_scheduler.CLUSTER_ROOTS
        ):
            matches.append(
                dict(
                    scheduler,
                    job_id=str(scheduler["job_id"]),
                    state=scheduler.get("state", "unknown"),
                )
            )
    return matches
