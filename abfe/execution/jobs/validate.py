from pathlib import Path

import abfe.execution.files as execution_files


def check_source(config, spec, run, windows):
    source = Path(config["source"]).resolve()
    for receipt in run.rglob("abflow-*.sha256"):
        if receipt.name.startswith("abflow-gromacs-"):
            continue  # The executable is on the destination cluster, checked by the batch shell.
        for line in receipt.read_text().splitlines():
            digest, name = line.split("  ", 1)
            target = (receipt.parent / name).resolve()
            if (
                source not in target.parents
                or not target.is_file()
                or execution_files.sha(target) != digest
            ):
                raise ValueError("Changed stage/checkpoint evidence: " + str(receipt))
    if config["workload"] == "complex-production" and config["mode"] == "initial":
        for window in windows:
            path = run / window / "npt_pr"
            if (
                not (path / "abflow-state.done").is_file()
                or not (path / "state.cpt").is_file()
            ):
                raise ValueError("GPU equilibration is incomplete: " + window)
    if config["mode"] == "initial":
        paths = (
            [run]
            if spec["leg"] == "vanilla"
            else [
                run / w / s
                for w in windows
                for s in (
                    ("prod",)
                    if config["workload"] == "complex-production"
                    else ("enmin", "nvt", "npt_b", "npt_pr", "prod")
                )
            ]
        )
        for directory in paths:
            if list(directory.glob("*.cpt")) or any(
                (directory / name).exists()
                for name in (
                    ("em.tpr", "nvt.tpr", "npt.tpr", "prod.tpr")
                    if spec["leg"] == "vanilla"
                    else ("topol.tpr",)
                )
            ):
                raise ValueError(
                    "Existing simulation inputs require an explicit continuation plan"
                )
    else:
        if not list(run.rglob("abflow-*.inputs.sha256")):
            raise ValueError(
                "Continuation requires recorded stage inputs; legacy adoption is separate"
            )
        if not (run / ("abflow-gromacs-" + config["cluster"] + ".sha256")).is_file():
            raise ValueError(
                "Continuation requires the same recorded cluster executable"
            )
        for directory in (
            [run] if spec["leg"] == "vanilla" else [run / w / "prod" for w in windows]
        ):
            if config["mode"] == "extend" or config["workload"] == "complex-production":
                for name in ("state.cpt", "topol.tpr", "abflow-state.outputs.sha256"):
                    if not (directory / name).is_file():
                        raise ValueError(
                            "Missing managed checkpoint/TPR: " + str(directory / name)
                        )
