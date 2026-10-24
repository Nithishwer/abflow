import logging
import shutil
import subprocess
import sys
from importlib.resources import files
from pathlib import Path

from rdkit import Chem


def calculate_total_charge(sdf_file):
    """Calculate the total formal charge of a molecule from an SDF file."""
    # Load the molecule from the SDF file
    supplier = Chem.SDMolSupplier(str(sdf_file))

    # Check if there is a molecule in the SDF file
    if not supplier or len(supplier) == 0:
        raise ValueError("No molecules found in the SDF file.")

    mol = supplier[0]  # Assuming there is only one molecule in the file

    # Calculate the total charge
    total_charge = sum(atom.GetFormalCharge() for atom in mol.GetAtoms())
    logging.info(f"Ligand charge: {total_charge}")

    return total_charge


def create_fep_system(
    lig_name: str, vanilla_folder: Path, force=False, expected_charge=None
):  ## REFACTORED
    """Create a Free Energy Perturbation (FEP) system for a given ligand."""
    try:
        # Calculate the ligand charge
        lig_suffix = lig_name.split("_")[-1]
        lig_sdf = vanilla_folder / "smirnoff" / f"{lig_suffix}.sdf"

        lig_ch = calculate_total_charge(lig_sdf)
        if expected_charge is not None:
            if lig_ch != expected_charge:
                raise ValueError(
                    f"Unexpected ligand charge: {lig_ch}; expected {expected_charge}"
                )
            force = True
        if lig_ch != 0:
            logging.warning(f"Ligand {lig_name} has a non-zero charge of {lig_ch} e.")
            if not force:
                user_input = input(
                    f"Ligand {lig_name} charge = {lig_ch} e. Proceed? (y/n): "
                )
                if user_input.lower() != "y":
                    logging.info("Aborting FEP system creation by user choice.")
                    return

        # Define the command parameters
        alch_mthd = "co-annihilation" if lig_ch != 0 else "None"
        lig_resname = "unk"
        lig_itp = f"toppar/{lig_suffix}.itp"
        lig_atomtype_itp = f"toppar/{lig_suffix}_atomtypes.itp"
        script_path = (
            Path(files("abfe.utils.data.ligand_only"))
            / "add_alchemical_ion_v3_pertuball.py"
        )

        command = [
            sys.executable,
            str(script_path),
            "--lig_charge",
            str(lig_ch),
            "--alch_ion_method",
            alch_mthd,
            "--lig",
            lig_resname,
            "--lig_itp",
            lig_itp,
            "--atomtype_itp",
            lig_atomtype_itp,
        ]

        # Run the command
        subprocess.run(command, check=True)
        print("Ligand itp:", lig_itp)
        print("Ligand atomtype itp:", lig_atomtype_itp)

    except subprocess.CalledProcessError as e:
        print(f"ERROR with Alchemical Ion v3: {e}")
        sys.exit(1)
    except Exception as e:
        print(f"Unexpected error: {e}")
        sys.exit(1)


def create_index(input_file, output_file):
    """
    Create an index file using gmx make_ndx command.

    Parameters:
        input_file (str): Input structure file (.gro, .pdb, etc.).
        output_file (str): Output index file.
    """
    try:
        # Run the gmx make_ndx command
        logging.info(
            f"Creating index file: create Protein_lig_mem and water_ion_alchion groups"
        )
        command = f'echo "1|13|23|24|25\n q\n" | gmx make_ndx -f {input_file} -o {output_file}'
        subprocess.run(command, shell=True, check=True)
        logging.info(f"Index file {output_file} created successfully.")

        # Copy index.ndx to index_coul.ndx, index_vdw.ndx, and index_rest.ndx
        for suffix in ["coul", "vdw", "rest"]:
            shutil.copy(output_file, f"index_{suffix}.ndx")
        logging.info(
            "Index files copied to index_coul.ndx, index_vdw.ndx, and index_rest.ndx successfully."
        )
    except subprocess.CalledProcessError as e:
        logging.error(f"Error occurred while creating index file: {e}")
        sys.exit(1)
    except IOError as e:
        logging.error(f"Error copying index files: {e}")
        sys.exit(1)
