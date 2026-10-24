import importlib.resources as pkg_resources
import logging
import os
import shutil
import sys
from pathlib import Path

from abfe.utils.data import forcefield_dir


def check_line(filename, text):
    """Check if a specific text is present in a file."""
    if not os.path.isfile(filename):
        logging.error(f"File '{filename}' not found.")
        return False

    with open(filename, "r") as file:
        for line in file:
            if text in line:
                return True
    return False


def add_boresch_to_topol(source_file, destination_file):
    """Add Boresch restraints from source_file to destination_file if not already present."""
    if check_line(destination_file, "; restraints"):
        logging.info("The 'topol.top' file already contains the line '; restraints'.")
    else:
        logging.info(
            "The 'topol.top' file does not contain the line '; restraints'. Adding..."
        )
        try:
            with open(source_file, "r") as source:
                source_contents = source.read()
            with open(destination_file, "a") as destination:
                destination.write("\n; restraints\n")
                destination.write(source_contents)
            logging.info("Boresch restraints added successfully.")
        except IOError as e:
            logging.error(f"Error reading or writing files: {e}")
            sys.exit(1)


def copy_complex(
    vanilla_folder_name: str,
    abfe_path: Path,
    copy_ff: bool = True,
    forcefield_path=None,
    topology_path=None,
):
    vanilla_folder = Path(vanilla_folder_name).resolve()
    complex_dir = abfe_path / "complex"
    complex_dir.mkdir(parents=True, exist_ok=True)

    shutil.copy(
        abfe_path / "boresch" / "ClosestRestraintFrame.gro", complex_dir / "complex.gro"
    )
    shutil.copy(
        abfe_path / "boresch" / "BoreschRestraint.top",
        complex_dir / "BoreschRestraint.top",
    )
    shutil.copy(
        topology_path or vanilla_folder / "topol_water_removed.top",
        complex_dir / "complex.top",
    )
    shutil.copytree(
        vanilla_folder / "toppar", complex_dir / "toppar", dirs_exist_ok=True
    )

    if copy_ff:
        ff_src = (
            Path(forcefield_path)
            if forcefield_path
            else pkg_resources.files(forcefield_dir) / "amber99sb-star-ildn-mut.ff"
        )
        ff_dst = complex_dir / ff_src.name
        shutil.copytree(ff_src, ff_dst, dirs_exist_ok=True)

    add_boresch_to_topol(
        complex_dir / "BoreschRestraint.top", complex_dir / "complex.top"
    )
    logging.info(f"Copied all required files into {complex_dir}")
