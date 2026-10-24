import glob
import os
import shutil
import subprocess

import abfe.preparation.coordinates as preparation_coordinates


def add_ligposres_to_topol(ligand_file):
    """
    Adds the ligand position restraint file to the topol.top file
    """
    ligand_name = os.path.splitext(ligand_file)[0]
    # Content to be added to the file
    content_to_add = (
        '''
#ifdef POSRES
#include "'''
        + ligand_name
        + """_posre.itp"
#endif
"""
    )

    # Open the file in append mode and write the content
    file_path = "toppar/" + ligand_name + ".itp"
    with open(file_path, "r+") as file:
        file_content = file.read()

        # Check if the content to add is already present
        if content_to_add.strip() in file_content:
            print("Ligand posres already present, not doing anything.")
            return

        # If not present, append the content
        file.write(content_to_add)

    print(f"Content added to '{file_path}' successfully.")


def add_toppar2protein_include(file_path):
    """
    Adds the 'toppar' to protein #include in the 'topol.top' file
    """
    # Open the file for reading
    with open(file_path, "r") as file:
        file_content = file.read()

    # Check if 'toppar' is already present
    if '#include "toppar/topol_Protein_chain_' in file_content:
        print("Nothing added, 'toppar' already present.")
        return

    # Replace the string
    modified_content = file_content.replace(
        '#include "topol_Protein_chain_', '#include "toppar/topol_Protein_chain_'
    )

    # Write the modified content back to the file
    with open(file_path, "w") as file:
        file.write(modified_content)

    print(f"File '{file_path}' modified successfully.")


def add_line_after_pattern(file_path, pattern, line_to_add):
    # Read the file
    with open(file_path, "r") as file:
        lines = file.readlines()

    # Find the index of the line containing the pattern
    pattern_index = -1
    for i, line in enumerate(lines):
        if pattern in line:
            pattern_index = i
            break

    if pattern_index != -1:
        # Check if the line to add is already present after the pattern
        if line_to_add + "\n" in lines[pattern_index + 1 :]:
            print(f"'{line_to_add}' already present after pattern, skipping.")
        else:
            # Insert the line after the line containing the pattern
            lines.insert(pattern_index + 1, line_to_add + "\n")

            # Write the modified content back to the file
            with open(file_path, "w") as file:
                file.writelines(lines)

            print(f" '{line_to_add}' Line added to '{file_path}' successfully.")
    else:
        raise ValueError(f"Pattern '{pattern}' not found in '{file_path}'.")


def add_unk_to_topol(filename):
    # Content to be added to the file
    content_to_add = "unk         1\n"

    # Open the file in append mode and write the content
    with open(filename, "a") as file:
        file.write(content_to_add)

    print(f"Moleculetype list of '{filename}' updates successfully.")


def organize_topologies(xml_file=None):
    """
    Organizes the topology files by moving them to a directory called "toppar"
    Looks for smirnoff or bespokefit directory for itp files from xml_file
    """
    # Create a directory called "toppar" if it doesn't exist
    toppar_dir = os.path.join(os.getcwd(), "toppar")
    os.makedirs(toppar_dir, exist_ok=True)

    # Move all .itp files from the current directory to "toppar/"
    itp_files_current_dir = glob.glob("*.itp")
    if not itp_files_current_dir:
        raise FileNotFoundError("No .itp files found in the current directory.")
    for itp_file in itp_files_current_dir:
        shutil.move(itp_file, os.path.join(toppar_dir, itp_file))
    print("Moved .itp files from the current directory to 'toppar/'")

    # Move *.itp files from "smirnoff/" to "toppar/"
    mode = "bespokefit" if xml_file else "smirnoff"

    param_dir = os.path.join(os.getcwd(), mode)
    itp_files_param = glob.glob(os.path.join(param_dir, "*.itp"))
    if not itp_files_param:
        raise FileNotFoundError(f"No .itp files found in the '{mode}' directory.")
    for itp_file in itp_files_param:
        shutil.copy(itp_file, os.path.join(toppar_dir, os.path.basename(itp_file)))
    print("Copied *.itp files from 'smirnoff/' to 'toppar/'")

    # Add toppar to protein include in topology
    add_toppar2protein_include("topol.top")


def addligtop(ligand_sdf):
    """
    Adds the ligand topology files to the topol.top file
    """
    # Example usage:
    file_path = "topol.top"  # Replace with the actual file path
    ligand_name = os.path.splitext(ligand_sdf)[0]
    pattern = 'forcefield.itp"'
    line_to_add = '#include "toppar/' + ligand_name + '_atomtypes.itp"'
    add_line_after_pattern(file_path, pattern, line_to_add)
    line_to_add = '#include "toppar/' + ligand_name + '.itp"'
    pattern = "; Include water topology"
    add_line_after_pattern(file_path, pattern, line_to_add)
    add_unk_to_topol(file_path)


def addliggro(ligand_sdf, xml_file=None):
    """
    Adds the ligand gro file to the protein gro file
    """
    mode = "bespokefit/" if xml_file else "smirnoff/"
    # Example usage:
    source_file = (
        mode + os.path.splitext(ligand_sdf)[0] + "_mod.gro"
    )  # Replace with the actual path of the source file
    destination_file = (
        "protein.gro"  # Replace with the actual path of the destination file
    )
    preparation_coordinates.addgro2gro(source_file, destination_file)


def addligposres(ligand_sdf, xml_file=None):
    """
    Adds the ligand position restraint file to the topol.top file
    """
    ligand_name = os.path.splitext(ligand_sdf)[0]
    mode = "bespokefit" if xml_file else "smirnoff"
    # Define the input and output file paths
    input_gro_file = os.path.join(mode, ligand_name + "_mod.gro")
    output_itp_file = os.path.join("toppar", ligand_name + "_posre.itp")

    # Generate lig posres using gmx genrestr
    command = ["gmx", "genrestr", "-f", input_gro_file, "-o", output_itp_file]

    # Prepare input for the subprocess (1 for group selection and q to quit)
    process_input = b"1\nq\n"
    subprocess.run(command, input=process_input)

    add_ligposres_to_topol(ligand_name)
