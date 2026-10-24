import shlex
import subprocess
from pathlib import Path

import abfe.monitoring.scheduler as monitoring_scheduler

SSH = [
    "ssh",
    "-T",
    "-o",
    "BatchMode=yes",
    "-o",
    "ConnectTimeout=15",
    "-o",
    "ServerAliveInterval=15",
    "-o",
    "ServerAliveCountMax=2",
]


def execute(plan, body):
    path, allowed = (
        Path(plan["remote"]),
        Path(monitoring_scheduler.CLUSTER_ROOTS[plan["config"]["cluster"]]),
    )
    if allowed not in path.parents or ".." in path.parts:
        raise ValueError("Remote path outside approved root")
    guard = [
        "set -euo pipefail",
        "root=" + shlex.quote(str(path)),
        'case "$(realpath -m "$root")" in "$(realpath '
        + shlex.quote(str(allowed))
        + ')"/*) ;; *) exit 1;; esac',
    ]
    guard += [
        "test ! -L " + shlex.quote(str(p))
        for p in [path] + list(path.parents)
        if p != allowed and allowed in p.parents
    ]
    result = subprocess.run(
        SSH + [plan["config"]["cluster"], "bash -s"],
        input="\n".join(guard) + "\n" + body + "\n",
        text=True,
        capture_output=True,
        timeout=300,
    )
    with (Path(plan["output"]) / "ssh.log").open("a") as log:
        log.write(body + "\n" + result.stdout + result.stderr + "\n")
    if result.returncode:
        raise ValueError(
            "Remote command failed:\n" + result.stdout[-4000:] + result.stderr[-4000:]
        )
    return result.stdout


def command(plan, args):
    return execute(plan, shlex.join([str(a) for a in args]))
