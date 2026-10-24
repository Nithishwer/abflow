import subprocess

import abfe.preparation.ligand_topology as preparation_ligand_topology


def remove_oldprotposre(filename):
    """
    Removes the old protein position restraint file from the topology filesL
    Removes everything after: '; Include Position restraint file'
    """
    with open(filename, "r") as file:
        lines = file.readlines()

    # Find the line with the specified text
    for i, line in enumerate(lines):
        if line.strip() == "; Include Position restraint file":
            break
    else:  # If the line was not found, raise an error
        raise ValueError(
            "Line '; Include Position restraint file' not found in the file."
        )

    # Remove everything after the found line
    lines = lines[: i + 1]

    # Write the modified lines back to the file
    with open(filename, "w") as file:
        file.writelines(lines)


def addproteinposres(file_path, protein_chains):
    line_to_add = """; Include Position restraint file
#ifdef POSRES
#include "toppar/posres_bb.itp"
#include "toppar/posres_sc.itp"
#endif"""
    for protein_chain in protein_chains:
        pattern = f'#include "toppar/topol_Protein_chain_{protein_chain}.itp"'
        preparation_ligand_topology.add_line_after_pattern(
            file_path, pattern, line_to_add
        )
        remove_oldprotposre(f"toppar/topol_Protein_chain_{protein_chain}.itp")


def gnrt_protposres(gro_file):
    """
    Generates position restraint files for protein backbone and side-chain atoms
    """
    # Command 1: Generate index file
    # command = f"echo 'splitch 1\nq\n' | gmx make_ndx -f {gro_file} -o chain_index.ndx"
    result = subprocess.run(command, shell=True, check=True)

    # Command 2: Extract chain 1 from membrane.gro
    result = subprocess.run(
        f"echo 17 | gmx editconf -f {gro_file} -n chain_index.ndx -o chain1.gro",
        shell=True,
        check=True,
    )

    # Command 3: Generate position restraint file for side-chain atoms
    result = subprocess.run(
        f"echo 8 | gmx genrestr -f chain1.gro -o toppar/posres_sc.itp",
        shell=True,
        check=True,
    )

    # Command 4: Generate position restraint file for backbone atoms
    result = subprocess.run(
        f"echo 4 | gmx genrestr -f chain1.gro -o toppar/posres_bb.itp",
        shell=True,
        check=True,
    )
