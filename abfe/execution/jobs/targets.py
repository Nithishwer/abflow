from pathlib import Path

import abfe.execution.files as execution_files


def _target(plan, stage):
    if stage == "enmin":
        return 0
    if stage == "prod" and plan["config"]["mode"] == "extend":
        return plan["decision"]["next_target_ns"] * 1000
    values = [
        execution_files.duration(
            Path(plan["config"]["source"])
            / plan["config"]["relative_run"]
            / w
            / stage
            / ("{}.{}.{}.mdp".format(w.split(".")[0], stage, w.split(".")[1]))
        )
        for w in plan["windows"]
    ]
    if len(set(values)) != 1:
        raise ValueError("Window stage durations differ; review the execution profile")
    return values[0]
