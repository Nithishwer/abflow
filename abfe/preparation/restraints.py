import logging
import os
import subprocess
import sys
from pathlib import Path

import MDRestraintsGenerator


def check_boresch(vanilla_folder):  # Accepts str or Path
    """Check that vanilla folder and required files exist."""
    vanilla_folder = Path(vanilla_folder)  # Ensure it's a Path object

    tpr_file = vanilla_folder / "prod.tpr"
    xtc_file = vanilla_folder / "prod.xtc"

    if not vanilla_folder.exists():
        raise FileNotFoundError(f"Missing folder: {vanilla_folder}")
    if not tpr_file.exists() or not xtc_file.exists():
        raise FileNotFoundError(f"Missing prod.tpr or prod.xtc in {vanilla_folder}")


def generate_boresch_restraints(
    vanilla_folder_name: str,
    abfe_path: Path,
    resname: str = "unk",
    input_gro: str = "npt.gro",
    gmx_exec: str = "gmx",
    restraint_script=None,
    topology_path=None,
):
    vanilla = Path(vanilla_folder_name).resolve()
    boresch = abfe_path / "boresch"
    boresch.mkdir(parents=True, exist_ok=True)

    cwd_orig = Path.cwd()
    os.chdir(boresch)

    # 1) build the tpr
    logging.info("Creating GROMACS tpr file…")
    subprocess.run(
        [
            gmx_exec,
            "grompp",
            "-f",
            str(vanilla / "prod.mdp"),
            "-c",
            str(vanilla / input_gro),
            "-r",
            str(vanilla / input_gro),
            "-p",
            str(topology_path or vanilla / "topol_water_removed.top"),
            "-n",
            str(vanilla / "index.ndx"),
            "-o",
            "prod.tpr",
        ],
        check=True,
    )

    # 2) locate BoreschRestraintGMX.py in the cloned repo
    #    go up two levels from the module's __file__ to get the project root
    module_path = Path(MDRestraintsGenerator.__file__)
    project_root = module_path.parents[1]
    script_path = (
        Path(restraint_script)
        if restraint_script
        else project_root / "scripts" / "BoreschRestraintGMX.py"
    )
    if not script_path.exists():
        raise FileNotFoundError(
            f"Cannot find BoreschRestraintGMX.py at {script_path!s}"
        )

    # 3) run it
    logging.info("Running BoreschRestraintGMX.py…")
    bo_cmd = [
        sys.executable,
        str(script_path),
        "--top",
        "prod.tpr",
        "--traj",
        str(vanilla / "prod.xtc"),
        "--host_selection",
        "protein and name CA",
        "--ligand_selection",
        f"resname {resname}",
    ]
    with open("resgen_output.txt", "w") as out, open("resgen_error.txt", "w") as err:
        subprocess.run(bo_cmd, check=True, stdout=out, stderr=err)

    os.chdir(cwd_orig)
    logging.info(f"Boresch restraints generated and dumped into {boresch}")
