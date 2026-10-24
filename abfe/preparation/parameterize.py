import os
import shutil
import subprocess
from importlib.resources import files

import abfe.preparation.bespokefit as preparation_bespokefit
import abfe.preparation.smirnoff as preparation_smirnoff


def param_lig(file_name, xml_file=None, sage_offxml="openff-2.0.0.offxml"):
    """
    Creates a directory structure and smirnoff parameterizes the ligand
    """

    # Set the mode variable based on whether xml_file is provided
    mode = "bespokefit" if xml_file else "smirnoff"

    # Create a folder with the name of the mode
    os.makedirs(mode, exist_ok=True)
    os.chdir(mode)

    # Copying the ligand files
    parent_dir = os.path.abspath(os.path.join(os.getcwd(), ".."))
    source_file_path = os.path.join(parent_dir, file_name)
    destination_file_path = os.path.join(os.getcwd(), file_name)
    try:
        # Copy the file from the parent directory to the mode directory
        shutil.copy(source_file_path, destination_file_path)
        print(f"File '{file_name}' copied successfully to '{mode}' folder.")
    except FileNotFoundError:
        raise FileNotFoundError(
            f"Error: File '{file_name}' not found in the parent directory."
        )

    if mode == "smirnoff":
        preparation_smirnoff.smirnoff_parameterize(file_name, forcefield=sage_offxml)
    else:
        preparation_bespokefit.bespokefit_parameterize(file_name, xml_file)

    os.chdir("../")


def param_protein(pdb_file):
    # Copy the forcefields folder into the current directory
    script_dir = str(files("abfe.utils"))
    forcefields_src = os.path.join(
        script_dir, "data", "forcefield_dir", "amber99sb-star-ildn-mut.ff"
    )
    forcefields_dest = os.path.join(os.getcwd(), "amber99sb-star-ildn-mut.ff")

    if not os.path.exists(forcefields_dest):
        shutil.copytree(forcefields_src, forcefields_dest)
        print("Forcefields folder copied successfully.")
    # Run the grep command to filter out non-protein atoms and save it as protein.pdb
    pdb_protein = "protein.pdb"
    subprocess.run(["grep", "-v", "^HETATM", pdb_file], stdout=open(pdb_protein, "w"))

    # Run the pdb2gmx command and save output as protein.gro
    pdb2gmx_command = [
        "gmx",
        "pdb2gmx",
        "-f",
        pdb_protein,
        "-o",
        os.path.splitext(pdb_protein)[0] + ".gro",
        "-water",
        "tip3",
        "-ignh",
    ]
    # subprocess.run(pdb2gmx_command)
    # Run the pdb2gmx command and automatically input '1' when prompted
    process = subprocess.Popen(
        pdb2gmx_command,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    # Send '1\n' to the stdin (to select the first force field when prompted)
    stdout, stderr = process.communicate(input="1\n")

    # Print output and error messages if needed
    print(stdout)
    print(stderr)


def param_protein_w_protonation(pdb_file):
    # Copy the forcefields folder into the current directory
    script_dir = str(files("abfe.utils"))
    forcefields_src = os.path.join(
        script_dir, "data", "forcefield_dir", "amber99sb-star-ildn-mut.ff"
    )
    forcefields_dest = os.path.join(os.getcwd(), "amber99sb-star-ildn-mut.ff")

    if not os.path.exists(forcefields_dest):
        shutil.copytree(forcefields_src, forcefields_dest)
        print("Forcefields folder copied successfully.")

    # Run the grep command to filter out non-protein atoms and save it as protein.pdb
    pdb_protein = "protein.pdb"
    subprocess.run(["grep", "-v", "^HETATM", pdb_file], stdout=open(pdb_protein, "w"))

    # Run the pdb2gmx command and save output as protein.gro
    pdb2gmx_command = [
        "gmx",
        "pdb2gmx",
        "-f",
        pdb_protein,
        "-o",
        os.path.splitext(pdb_protein)[0] + ".gro",
        "-water",
        "tip3",
        "-ignh",
        "-lys",
        "-arg",
        "-asp",
        "-glu",
        "-his",
    ]
    subprocess.run(pdb2gmx_command)
