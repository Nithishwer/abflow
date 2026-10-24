import importlib.resources as pkg_resources
import logging
from pathlib import Path

from abfe.utils.data.mdps import (
    super_mdp_files_pertuball,
    super_mdp_files_pertuball_smaller_dumps,
    super_mdp_files_soluble,
)


def create_directories_with_MDP_pertuball(leg: str, pml_index: str, wiai_index: str):
    """
    Create FEP simulation directories and fill them with modified .mdp files
    from the packaged super_mdp_files_pertuball directory.

    Parameters:
        leg (str): Perturbation leg ('coul', 'rest', 'vdw').
        pml_index (str): Index group for protein/membrane/ligand.
        wiai_index (str): Index group for water/ions/alchemical ion.
    """
    leg_windows = {"coul": (11, 12), "rest": (12, 0), "vdw": (21, 23)}

    windows, state_id_start = leg_windows.get(leg, (0, 0))
    state_id = state_id_start
    stages = ["enmin", "npt_b", "npt_pr", "nvt", "prod"]

    for i in range(windows):
        i_str = f"{i:02d}"
        dir_base = Path(f"{leg}.{i_str}")
        dir_base.mkdir(exist_ok=True)

        for stage in stages:
            stage_dir = dir_base / stage
            stage_dir.mkdir(exist_ok=True)

            template_filename = f"{leg}.{stage}.super.mdp"
            output_filename = stage_dir / f"{leg}.{stage}.{i_str}.mdp"

            with (
                pkg_resources.files(super_mdp_files_pertuball)
                .joinpath(template_filename)
                .open("r") as f
            ):
                template_content = f.read()

            modified_content = (
                template_content.replace("Protein_LIG_POPC", pml_index)
                .replace("Water_and_ions_B_CL", wiai_index)
                .replace("<state>", str(state_id))
            )

            with open(output_filename, "w") as f_out:
                f_out.write(modified_content)

            logging.info(f"Written: {output_filename}")

        state_id += 1


def create_directories_with_MDP_pertuball_smaller_dumps(
    leg: str, pml_index: str, wiai_index: str
):
    """
    Create FEP simulation directories using MDPs with smaller dump frequencies,
    sourced from packaged `super_mdp_files_pertuball_smaller_dumps`.

    Parameters:
        leg (str): 'coul', 'rest', or 'vdw'.
        pml_index (str): Index group name for protein/membrane/ligand.
        wiai_index (str): Index group name for water/ions/alchemical ion.
    """
    leg_windows = {"coul": (11, 12), "rest": (12, 0), "vdw": (21, 23)}

    windows, state_id_start = leg_windows.get(leg, (0, 0))
    state_id = state_id_start
    stages = ["enmin", "npt_b", "npt_pr", "nvt", "prod"]

    for i in range(windows):
        i_str = f"{i:02d}"
        dir_base = Path(f"{leg}.{i_str}")
        dir_base.mkdir(exist_ok=True)

        for stage in stages:
            stage_dir = dir_base / stage
            stage_dir.mkdir(exist_ok=True)

            template_filename = f"{leg}.{stage}.super.mdp"
            output_filename = stage_dir / f"{leg}.{stage}.{i_str}.mdp"

            with (
                pkg_resources.files(super_mdp_files_pertuball_smaller_dumps)
                .joinpath(template_filename)
                .open("r") as f
            ):
                template_content = f.read()

            modified_content = (
                template_content.replace("Protein_LIG_POPC", pml_index)
                .replace("Water_and_ions_B_CL", wiai_index)
                .replace("<state>", str(state_id))
            )

            with open(output_filename, "w") as f_out:
                f_out.write(modified_content)

            logging.info(f"Written: {output_filename}")

        state_id += 1


def create_directories_with_soluble_MDP(leg: str, pml_index: str, wiai_index: str):
    """
    Create FEP simulation directories for soluble systems using MDP templates
    from the `super_mdp_files_soluble` package data.

    Parameters:
        leg (str): Perturbation leg ('coul', 'rest', 'vdw').
        pml_index (str): Index group name for protein/ligand.
        wiai_index (str): Index group name for water/ions/alchemical ion.
    """
    leg_windows = {"coul": 11, "rest": 12, "vdw": 21}

    windows = leg_windows.get(leg, 0)
    stages = ["enmin", "npt_b", "npt_pr", "nvt", "prod"]

    for i in range(windows):
        i_str = f"{i:02d}"
        dir_base = Path(f"{leg}.{i_str}")
        dir_base.mkdir(exist_ok=True)

        for stage in stages:
            stage_dir = dir_base / stage
            stage_dir.mkdir(exist_ok=True)

            template_filename = f"{leg}.{stage}.super.mdp"
            output_filename = stage_dir / f"{leg}.{stage}.{i_str}.mdp"

            with (
                pkg_resources.files(super_mdp_files_soluble)
                .joinpath(template_filename)
                .open("r") as f
            ):
                template_content = f.read()

            modified_content = (
                template_content.replace("Protein_LIG_POPC", pml_index)
                .replace("Water_and_ions_B_CL", wiai_index)
                .replace("<state>", i_str)
            )

            with open(output_filename, "w") as f_out:
                f_out.write(modified_content)

            logging.info(f"Wrote: {output_filename}")
