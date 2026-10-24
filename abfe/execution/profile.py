import re
from pathlib import Path

import abfe.monitoring.scheduler as monitoring_scheduler

GPU_PARTITIONS = {
    "ranvier": {"g-ran": "A40", "gpu4-biggin": "A40", "gpuL40S-biggin": "L40S"},
    "soma": {"gpu3-A40": "A40"},
}


WORKLOADS = ("vanilla", "complex-equilibration", "solvent", "complex-production")


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


def relative(value):
    if (
        not isinstance(value, str)
        or not value
        or not re.fullmatch(r"[A-Za-z0-9_./-]+", value)
    ):
        raise ValueError("relative_run must be a simple relative path")
    path = Path(value)
    if path.is_absolute() or ".." in path.parts:
        raise ValueError("relative_run must remain inside source")
    return path


def config_check(config, spec):
    if config["schema_version"] != 1 or config["run_id"] != spec["id"]:
        raise ValueError("Execution manifest version/run mismatch")
    workload, cluster, mode = config["workload"], config["cluster"], config["mode"]
    if workload not in WORKLOADS or mode not in ("initial", "continue", "extend"):
        raise ValueError("Unknown workload/mode")
    if set(spec["windows"]) - {"rest", "coul", "vdw"}:
        raise ValueError("Unsupported window phases; review the execution profile")
    expected_leg = {
        "vanilla": "vanilla",
        "complex-equilibration": "complex",
        "complex-production": "complex",
        "solvent": "solvent",
    }[workload]
    if spec["leg"] != expected_leg:
        raise ValueError("Workload does not match the registered leg")
    if (
        type(config.get("output_reserve_bytes")) is not int
        or config["output_reserve_bytes"] <= 0
    ):
        raise ValueError("Provide a positive output_reserve_bytes storage estimate")
    hours = config["walltime_hours"]
    if type(hours) not in (int, float) or not 0.5 <= hours <= 24:
        raise ValueError("Use a wall time between 0.5 and 24 hours")
    for value in [
        config["partition"],
        config.get("account") or "",
        config.get("qos") or "",
    ]:
        if value and not re.fullmatch(r"[A-Za-z0-9_-]+", value):
            raise ValueError("Unsafe scheduler profile field")
    if not config["partition"] or not config["gromacs_version"]:
        raise ValueError("Explicit partition and GROMACS version required")
    binary = Path(config["gromacs"])
    if (
        not binary.is_absolute()
        or ".." in binary.parts
        or any(c in str(binary) for c in "\n\r")
    ):
        raise ValueError("Provide an absolute GROMACS executable")
    if workload == "complex-production":
        if (
            cluster != "archer"
            or config["partition"] != "standard"
            or config["qos"] not in ("standard", "lowpriority")
            or config["account"] != "e280-biggin"
        ):
            raise ValueError("Unsupported ARCHER profile")
        if Path(monitoring_scheduler.CLUSTER_ROOTS["archer"]) not in binary.parents:
            raise ValueError(
                "ARCHER campaign executable must be under the approved work root"
            )
        if spec["windows"] != {"rest": 12, "coul": 11, "vdw": 21}:
            raise ValueError(
                "The reference ARCHER allocation supports exactly the reviewed 44-window schedule"
            )
    else:
        if cluster not in GPU_PARTITIONS or GPU_PARTITIONS[cluster].get(
            config["partition"]
        ) != config.get("gpu"):
            raise ValueError(
                "Select a routine A40/L40S partition; conditional/short queues need a scoped implementation review"
            )
        if cluster == "soma" and config.get("account") != "biggingroup":
            raise ValueError("Soma profile requires biggingroup")
    if mode == "extend" and workload not in ("solvent", "complex-production"):
        raise ValueError("Only alchemical production can extend")
    for module in config.get("modules", []):
        if not isinstance(module, str) or not re.fullmatch(
            r"[A-Za-z0-9_./+-]+", module
        ):
            raise ValueError("Unsafe module name")
    for path in config.get("library_dirs", []) + config.get("path_dirs", []):
        path = Path(path)
        if (
            not path.is_absolute()
            or ".." in path.parts
            or ":" in str(path)
            or "\n" in str(path)
        ):
            raise ValueError("Unsafe environment directory")
        if (
            cluster == "archer"
            and Path(monitoring_scheduler.CLUSTER_ROOTS["archer"]) not in path.parents
        ):
            raise ValueError(
                "ARCHER managed software must be under the approved work root"
            )
    for stage, count in config.get("maxwarn", {}).items():
        if (
            stage
            not in (
                "vanilla",
                "minim",
                "enmin",
                "nvt",
                "npt",
                "npt_b",
                "npt_pr",
                "prod",
            )
            or type(count) is not int
            or count < 0
        ):
            raise ValueError("Invalid explicit grompp warning allowance")
    if any(config.get("maxwarn", {}).values()) and not config.get("warning_review"):
        raise ValueError("Nonzero maxwarn requires a recorded warning review")
