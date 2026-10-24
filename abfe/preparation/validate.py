import math
import re
from decimal import Decimal
from pathlib import Path

import abfe.analysis.policy as analysis_policy
import abfe.preparation.manifest as preparation_manifest


def validate_mdp(path, duration_ps, temperature):
    mdp = analysis_policy.settings(path)
    if Decimal(mdp["dt"]) * Decimal(mdp["nsteps"]) != duration_ps:
        raise ValueError(f"Unexpected production target in {path}")
    temperatures = [float(value) for value in mdp["ref-t"].split()]
    if not temperatures or any(value != temperature for value in temperatures):
        raise ValueError(f"Temperature mismatch in {path}")
    return mdp


def validate_outputs(plan):
    output, ligand = Path(plan["output"]), plan["ligand"]
    temp = float(plan["config"]["protocol"]["temperature_K"])
    if plan["stage"] == "vanilla":
        for r in (1, 2, 3):
            path = output / ligand / f"vanilla_rep_{r}"
            for name in ("system_solv_ions.gro", "topol.top", "index.ndx"):
                if not (path / name).is_file() or not (path / name).stat().st_size:
                    raise ValueError(
                        f"Missing/empty prepared vanilla output: {path / name}"
                    )
            validate_mdp(path / "prod.mdp", 100000, temp)
        return
    expected = {f"abfe_van{r}_hrex_r1" for r in (1, 2, 3)}
    if {p.name for p in (output / ligand).iterdir() if p.is_dir()} != expected:
        raise ValueError("Expected exactly three complex preparation directories")
    if list((output / ligand).glob("*/ligand")) != [Path(plan["solvent"])]:
        raise ValueError("Expected exactly one shared solvent preparation")
    for r in (1, 2, 3):
        root = output / ligand / f"abfe_van{r}_hrex_r1"
        for name in (
            "ClosestRestraintFrame.gro",
            "BoreschRestraint.top",
            "resgen_output.txt",
        ):
            if (
                not (root / "boresch" / name).is_file()
                or not (root / "boresch" / name).stat().st_size
            ):
                raise ValueError(
                    f"Missing/empty restraint output: {root / 'boresch' / name}"
                )
        corrections = re.findall(
            r"^dG_off:\s*([^,\s]+)",
            (root / "boresch/resgen_output.txt").read_text(),
            re.M,
        )
        if len(corrections) != 1 or not math.isfinite(float(corrections[0])):
            raise ValueError("Missing/invalid MDRestraintsGenerator correction")
        validate_windows(root / "complex", preparation_manifest.COMPLEX_WINDOWS, temp)
    validate_windows(Path(plan["solvent"]), preparation_manifest.SOLVENT_WINDOWS, temp)


def validate_windows(root, windows, temperature, duration_ps=20000):
    actual = {
        p.name
        for p in root.iterdir()
        if p.is_dir() and re.fullmatch(r"(rest|coul|vdw)\.\d+", p.name)
    }
    expected = {
        f"{phase}.{i:02d}" for phase, count in windows.items() for i in range(count)
    }
    if actual != expected:
        raise ValueError(f"Unexpected/missing windows: {root}")
    state, reference = 0, None
    for phase, count in windows.items():
        for i in range(count):
            for stage in preparation_manifest.STAGES:
                path = (
                    root / f"{phase}.{i:02d}" / stage / f"{phase}.{stage}.{i:02d}.mdp"
                )
                mdp = analysis_policy.settings(path)
                if int(mdp["init-lambda-state"]) != state:
                    raise ValueError(f"Wrong lambda state in {path}")
                vectors = {
                    key: value.split()
                    for key, value in mdp.items()
                    if key.endswith("-lambdas")
                }
                if not vectors or any(
                    len(v) != sum(windows.values()) for v in vectors.values()
                ):
                    raise ValueError(f"Wrong lambda map length in {path}")
                if reference is None:
                    reference = vectors
                if vectors != reference:
                    raise ValueError(f"Inconsistent lambda maps in {path}")
                if stage == "prod":
                    validate_mdp(path, duration_ps, temperature)
            state += 1
