import os
import shutil
from importlib.resources import files

import abfe.preparation.extract_membrane as preparation_extract_membrane
import abfe.preparation.ligand_topology as preparation_ligand_topology


def get_popc_number(file_path):
    # Open the file and read its contents
    with open(file_path, "r") as file:
        for line in file:
            # Check if the line starts with "POPC"
            if line.startswith("POPC"):
                # Split the line by spaces and extract the number
                fields = line.split()
                if len(fields) < 2:
                    raise ValueError(f"Missing POPC count in {file_path}")
                return int(fields[1])

    raise ValueError(f"Missing POPC count in {file_path}")


def add_POPC_to_topol(filename, popc_count):
    # Content to be added to the file
    content_to_add = "POPC         " + str(popc_count) + "\n"

    # Open the file in append mode and write the content
    with open(filename, "a") as file:
        file.write(content_to_add)

    print(f"Content added to '{filename}' successfully.")


def extract_membrane_from_charmm(charmm_gro_path, output_path):
    preparation_extract_membrane.extract_membrane(charmm_gro_path, output_path)


def includememtop(patterns, lines_to_add):
    for pattern, line_to_add in zip(patterns, lines_to_add):
        # Open the file for reading
        with open("topol.top", "r") as file:
            file_content = file.read()

        # Check if the pattern is present in the file content
        if pattern not in file_content:
            raise ValueError(f"Pattern '{pattern}' not found in the file.")

        # Check if the line to add is already present
        if line_to_add in file_content:
            print(
                f"Line '{line_to_add}' is already present in the file. Not doing anything."
            )
            continue

        # If the pattern is present and the line to add is not already present, add the line after the pattern
        preparation_ligand_topology.add_line_after_pattern(
            "topol.top", pattern, line_to_add
        )

    p = get_popc_number("charmm_gmx/topol.top")
    add_POPC_to_topol("topol.top", p)


def addmemtop(lig_filename, lipid_dir=None):
    """
    Adds the membrane itp files to the topol.top file
    """
    ligname = os.path.splitext(lig_filename)[0]

    # Resolve path to the forcefield_dir relative to this file (gromacs_helpers_single_chain.py)
    script_dir = str(files("abfe.utils"))
    forcefield_dir = os.path.join(script_dir, "data", "forcefield_dir")

    if lipid_dir is not None:
        forcefield_dir = str(lipid_dir)

    # Define the source and destination file paths
    source_files = [
        (os.path.join(forcefield_dir, "forcefield_popc.itp"), "toppar"),
        (os.path.join(forcefield_dir, "POPC.itp"), "toppar"),
        (os.path.join(forcefield_dir, "popc_posres.itp"), "toppar"),
    ]

    # Copy the files to the destination directory
    for source_file, destination_dir in source_files:
        shutil.copy(source_file, destination_dir)

    # Adding the membrane itp files to the topol.top file
    patterns = [ligname + '_atomtypes.itp"', ligname + '.itp"', '"toppar/POPC.itp"']
    lines_to_add = [
        '#include "toppar/forcefield_popc.itp"',
        '#include "toppar/POPC.itp"',
        """#ifdef POSRES
#include "toppar/popc_posres.itp"'
#endif
        """,
    ]
    # Adds lines after the patterns

    includememtop(patterns, lines_to_add)
