import re
from pathlib import Path

import abfe.execution.transport.ssh as execution_transport_ssh


def jobs(plan):
    result = []
    for line in execution_transport_ssh.command(
        plan, ["squeue", "--me", "--array", "--noheader", "--format=%i|%T|%Z|%q|%j"]
    ).splitlines():
        if not line.strip():
            continue
        job, state, work, qos, name = line.strip().split("|")
        if not re.fullmatch(r"[0-9][0-9_+.-]*", job) or not work.startswith("/"):
            raise ValueError("Unrecognized scheduler output")
        result.append(dict(job_id=job, state=state, work_dir=work, qos=qos, name=name))
    return result


def writers(plan, queue):
    root = Path(plan["remote"])
    return [
        j
        for j in queue
        if Path(j["work_dir"]) == root or root in Path(j["work_dir"]).parents
    ]
