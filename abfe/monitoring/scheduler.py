import re
import sqlite3
import subprocess
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime

import abfe.registry.manifest as registry_manifest
import abfe.registry.read as registry_read
import abfe.registry.values as registry_values

CLUSTER_ROOTS = {
    "archer": "/work/e280/e280-Biggin/reub0138",
    "ranvier": "/home/reub0138",
    "soma": "/home/reub0138",
}


def connection_state(record, at=None):
    if not record or not record.get("checked_at"):
        return "unknown"
    try:
        datetime.fromisoformat(record["checked_at"].replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return "unknown"
    return record.get("state", "unknown")


def probe_cluster(alias):
    if alias not in CLUSTER_ROOTS:
        raise ValueError("Only configured cluster aliases may be checked")
    command = [
        "ssh",
        "-T",
        "-o",
        "BatchMode=yes",
        "-o",
        "ConnectTimeout=8",
        "-o",
        "ServerAliveInterval=5",
        "-o",
        "ServerAliveCountMax=1",
        alias,
    ]
    result = {
        "state": "unreachable",
        "checked_at": registry_values.now(),
        "hostname": None,
        "scheduler": {"state": "unknown", "jobs": []},
    }
    try:
        host = subprocess.run(
            command + ["hostname"],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        if host.returncode:
            result["error"] = host.stderr.strip()[-500:] or "SSH command failed"
            return result
        result.update(state="connected", hostname=host.stdout.strip())
        queue = subprocess.run(
            command
            + ["squeue --me --array --noheader --format='%i|%T|%Z|%q|%a|%N|%u'"],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        if queue.returncode:
            result["scheduler"] = {
                "state": "unavailable",
                "jobs": [],
                "error": queue.stderr.strip()[-500:],
            }
        jobs = []
        for line in queue.stdout.splitlines() if queue.returncode == 0 else []:
            if not line.strip():
                continue
            job_id, state, directory, qos, account, nodes, user = (
                part.strip() for part in line.split("|")
            )
            if not re.fullmatch(
                r"[0-9][0-9_\[\],%+\-]*", job_id
            ) or not directory.startswith("/"):
                raise ValueError("Unrecognized scheduler output")
            jobs.append(
                {
                    "job_id": job_id,
                    "state": state,
                    "work_dir": directory,
                    "qos": qos,
                    "account": account,
                    "nodes": nodes,
                    "user": user,
                }
            )
        if queue.returncode == 0:
            result["scheduler"] = {"state": "observed", "jobs": jobs}
    except (OSError, subprocess.TimeoutExpired, ValueError) as exc:
        if result["state"] == "connected":
            result["scheduler"] = {
                "state": "unavailable",
                "jobs": [],
                "error": str(exc),
            }
        else:
            result["error"] = str(exc)
    finally:
        result["checked_at"] = registry_values.now()
    return result


def check_connections(registry):
    """Refresh only the current user's queues; never collect cluster telemetry."""
    metadata, rows = registry_read.read_registry(registry)
    registry_manifest.check_destination(
        registry, metadata, [row["spec"] for row in rows]
    )
    with ThreadPoolExecutor(max_workers=3) as pool:
        connections = dict(zip(CLUSTER_ROOTS, pool.map(probe_cluster, CLUSTER_ROOTS)))
    with closing(sqlite3.connect(registry)) as db, db:
        db.execute(
            "INSERT INTO metadata(key,value) VALUES ('connections',?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (registry_values.encode(connections),),
        )
    return connections
