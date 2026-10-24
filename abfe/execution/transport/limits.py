import re

import abfe.execution.transport.ssh as execution_transport_ssh
import abfe.monitoring.scheduler as monitoring_scheduler


class AdmissionWait(ValueError):
    """Known capacity limits defer dispatch without an execution failure."""


def quota(plan):
    root = monitoring_scheduler.CLUSTER_ROOTS[plan["config"]["cluster"]]
    fields = (
        execution_transport_ssh.command(plan, ["df", "-Pk", root])
        .splitlines()[-1]
        .split()
    )
    available = int(fields[3]) * 1024
    if plan["config"]["cluster"] == "archer":
        project = execution_transport_ssh.command(
            plan, ["lfs", "project", "-d", root]
        ).split()[0]
        if not project.isdigit():
            raise ValueError("Missing ARCHER project quota identity")
        text = execution_transport_ssh.command(
            plan, ["lfs", "quota", "-p", project, root]
        )
        match = re.search(r"^\s*(?:/\S+\s+)?(\d+)\*?\s+(\d+)\s+(\d+)\s+\S+", text, re.M)
        if not match or int(match[3]) <= 0:
            raise ValueError("Unrecognized ARCHER project quota: " + text)
        available = min(available, (int(match[3]) - int(match[1])) * 1024)
    required = (
        plan["source_snapshot"]["bytes"] * 2 + plan["config"]["output_reserve_bytes"]
    )
    if available < required:
        raise ValueError("Insufficient verified staging space")
    return dict(available_bytes=available, staging_required_bytes=required)


def admission(plan, queue):
    c = plan["config"]
    from abfe.operator.records import CURRENT

    context = CURRENT.get()
    if context:
        limit = context["max_submitted"][c["cluster"]]
        incoming = (
            len(plan["windows"])
            if c["workload"] in ("solvent", "complex-equilibration")
            else 1
        )
        if len(queue) + incoming > limit:
            raise AdmissionWait("Operator submitted-job ceiling reached")
    text = execution_transport_ssh.command(
        plan, ["scontrol", "show", "partition", c["partition"], "-o"]
    )
    if "State=UP" not in text or "PartitionName=" + c["partition"] not in text:
        raise ValueError("Selected partition is not available")
    limit = re.search(r"MaxTime=(\S+)", text)
    if not limit:
        raise ValueError("Missing partition wall-time limit")
    if limit[1] not in ("UNLIMITED", "INFINITE"):
        days, clock = limit[1].split("-") if "-" in limit[1] else ("0", limit[1])
        seconds = int(days) * 86400 + sum(
            int(x) * y for x, y in zip(clock.split(":"), (3600, 60, 1))
        )
        if seconds < c["walltime_hours"] * 3600:
            raise ValueError("Requested wall time exceeds partition limit")
    result = dict(
        submitted=len(queue), running=sum(j["state"] == "RUNNING" for j in queue)
    )
    if c["cluster"] == "archer":
        fields = (
            execution_transport_ssh.command(
                plan,
                [
                    "sacctmgr",
                    "-n",
                    "-P",
                    "show",
                    "qos",
                    c["qos"],
                    "format=Name,MaxJobsPU,MaxSubmitPU",
                ],
            )
            .strip()
            .splitlines()[0]
            .split("|")
        )
        if fields[0] != c["qos"] or not fields[1].isdigit() or not fields[2].isdigit():
            raise ValueError("Missing numeric ARCHER QoS ceilings")
        if sum(j["qos"] == c["qos"] for j in queue) >= int(fields[2]):
            raise AdmissionWait("ARCHER submitted-job ceiling reached")
        result.update(maximum_running=int(fields[1]), maximum_submitted=int(fields[2]))
    elif not re.search(
        r"gpu:" + c["gpu"] + r":\d+",
        execution_transport_ssh.command(
            plan, ["sinfo", "-h", "-p", c["partition"], "-o", "%G"]
        ),
    ):
        raise ValueError("Selected partition no longer advertises the requested GPU")
    return result
