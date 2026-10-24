import csv
import math
import re
from pathlib import Path

import abfe.monitoring.progress as monitoring_progress


def inspect_analysis(observation, directory):
    analysis = observation["analysis"]
    if not directory.exists():
        return
    files = sorted(directory.glob("*.csv")) + sorted(directory.glob("*.pdf"))
    for path in files:
        monitoring_progress.evidence(observation, path, "analysis")
    if not files:
        return
    analysis["state"] = "legacy_unverified"
    monitoring_progress.issue(
        observation,
        "analysis_provenance",
        "Analysis files exist without a validated input snapshot/cutoff; convergence remains unverified.",
    )
    convergence = directory / "ABFE_convergence.csv"
    if convergence.is_file():
        try:
            with convergence.open(newline="") as stream:
                halves = [
                    row
                    for row in csv.DictReader(stream)
                    if float(row["data_fraction"]) == 0.5
                ]
            if len(halves) != 1:
                raise ValueError("Expected exactly one data_fraction=0.5 row")
            forward, backward = (
                float(halves[0]["Forward"]),
                float(halves[0]["Backward"]),
            )
            if not all(math.isfinite(value) for value in (forward, backward)):
                raise ValueError("Non-finite convergence estimate")
            difference = abs(forward - backward)
            if not math.isfinite(difference):
                raise ValueError("Non-finite convergence difference")
            analysis["reported_half_difference_kcal_mol"] = difference
        except (ValueError, KeyError, TypeError, csv.Error) as exc:
            analysis["state"] = "invalid"
            monitoring_progress.issue(observation, "invalid_analysis", str(exc))
    latest_source = max(
        (
            item["mtime_ns"]
            for item in observation["evidence"]
            if item["role"] == "production_data"
        ),
        default=0,
    )
    if files and latest_source > min(path.stat().st_mtime_ns for path in files):
        analysis["state"] = (
            "possibly_stale" if analysis["state"] != "invalid" else "invalid"
        )
        monitoring_progress.issue(
            observation,
            "newer_source_files",
            "Production files have newer timestamps than analysis; timestamps alone cannot establish analysis freshness.",
        )


def inspect_run(spec):
    observation = {
        "stage": "not_prepared",
        "production_ns": None,
        "production_max_ns": None,
        "target_ns": None,
        "sampled_windows": 0,
        "expected_windows": sum(spec["windows"].values()),
        "scheduler": {"state": "unknown", "cluster": None, "job_id": None},
        "transfer": {"state": "unknown"},
        "analysis": {
            "state": "absent",
            "analysed_ns": None,
            "convergence": "unverified",
        },
        "windows": [],
        "evidence": [],
        "issues": [],
        "next_action": "Inspect inventory; prepare a reviewed batch plan for execution",
    }
    for item in spec["input_files"]:
        monitoring_progress.evidence(observation, Path(item), "input")
        if not Path(item).is_file():
            monitoring_progress.issue(
                observation,
                "missing_input:" + item,
                f"Declared input is missing: {item}",
            )
    if observation["evidence"]:
        observation["stage"] = "inputs_present"
    location = Path(spec["path"]) if spec["path"] else None
    if not location or not location.exists():
        if location and spec["leg"] != "vanilla":
            inspect_analysis(
                observation,
                location.parent
                / "analysis"
                / ("ligand" if spec["leg"] == "solvent" else "complex"),
            )
            if observation["analysis"]["state"] != "absent":
                observation["stage"] = "analysis_files_only"
        return observation
    if not location.is_dir():
        monitoring_progress.issue(
            observation, "invalid_source", f"Expected a directory: {location}"
        )
        return observation
    observation["stage"] = "directory_present"
    if spec["leg"] == "vanilla":
        log = location / "prod.log"
        mdp = location / "prod.mdp"
        for name in (
            "topol.top",
            "index.ndx",
            "system_solv_ions.gro",
            "prod.cpt",
            "prod.gro",
            "prod.xtc",
            "prod.log",
            "prod.mdp",
        ):
            monitoring_progress.evidence(
                observation,
                location / name,
                "production_log" if name == "prod.log" else "preparation",
            )
        if mdp.is_file():
            observation["target_ns"] = monitoring_progress.target_duration(mdp)
            observation["stage"] = "setup_present"
        if log.is_file():
            matches = re.findall(
                r"Step\s+Time\s*\n\s*\d+\s+([\d.eE+-]+)",
                monitoring_progress.tail(log, 262144),
            )
            if matches:
                value = float(matches[-1]) / 1000
                if not math.isfinite(value) or value < 0:
                    raise ValueError("Invalid vanilla production time")
                observation.update(
                    production_ns=value,
                    production_max_ns=value,
                    stage="production_observed",
                )
        return observation
    expected = [
        f"{phase}.{index:02d}"
        for phase, count in spec["windows"].items()
        for index in range(count)
    ]
    observed = sorted(
        path.name
        for path in location.iterdir()
        if path.is_dir() and re.fullmatch(r"[a-z]+\.\d+", path.name)
    )
    if observed:
        observation["stage"] = "windows_present"
        if set(observed) != set(expected):
            monitoring_progress.issue(
                observation,
                "window_inventory",
                f"Missing windows: {sorted(set(expected) - set(observed))}; unexpected: {sorted(set(observed) - set(expected))}",
            )
    times, targets = [], []
    for name in expected:
        prod = location / name / "prod"
        dhdl = prod / "dhdl.xvg"
        phase, index = name.split(".")
        mdp = prod / f"{phase}.prod.{index}.mdp"
        window = {
            "name": name,
            "production_ns": None,
            "target_ns": None,
            "checkpoint_present": (prod / "state.cpt").is_file(),
        }
        for path, role in (
            (dhdl, "production_data"),
            (mdp, "production_input"),
            (prod / "md.log", "production_log"),
            (prod / "state.cpt", "checkpoint"),
            (prod / "topol.tpr", "production_input"),
        ):
            monitoring_progress.evidence(observation, path, role)
        try:
            if dhdl.is_file():
                window["production_ns"] = monitoring_progress.production_end(dhdl)
                times.append(window["production_ns"])
            if mdp.is_file():
                window["target_ns"] = monitoring_progress.target_duration(mdp)
                targets.append(window["target_ns"])
        except (OSError, ValueError, KeyError) as exc:
            monitoring_progress.issue(
                observation, f"invalid_window:{name}", f"{name}: {exc}"
            )
        observation["windows"].append(window)
    observation["sampled_windows"] = len(times)
    if times:
        observation.update(stage="production_observed", production_max_ns=max(times))
        if len(times) == len(expected):
            observation["production_ns"] = min(times)
        else:
            monitoring_progress.issue(
                observation,
                "incomplete_production_data",
                f"Production times observed for {len(times)}/{len(expected)} expected windows; common duration is unknown.",
            )
    if len(targets) == len(expected) and len(set(targets)) == 1:
        observation["target_ns"] = targets[0]
    elif targets:
        monitoring_progress.issue(
            observation,
            "inconsistent_targets",
            "Production MDP targets are missing, invalid, or inconsistent across windows.",
        )
    analysis_directory = (
        location.parent
        / "analysis"
        / ("ligand" if spec["leg"] == "solvent" else "complex")
    )
    inspect_analysis(observation, analysis_directory)
    for path in (
        location.parent / "analysis/abfe_results.csv",
        location.parent / "boresch/resgen_output.txt",
    ):
        monitoring_progress.evidence(
            observation,
            path,
            "binding_result" if path.suffix == ".csv" else "restraints",
        )
    if spec["leg"] == "solvent":
        monitoring_progress.issue(
            observation,
            "solvent_compatibility",
            "Existing solvent calculation is inventoried; compatibility for reuse across complex repeats has not been verified.",
        )
    return observation
