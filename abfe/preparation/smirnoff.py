#!/usr/bin/env python3

import argparse

import parmed as pmd
from openmmforcefields.generators import SMIRNOFFTemplateGenerator

# Imports from dependencies
try:
    import openmm
except ImportError:
    from simtk import openmm

# Imports from the toolkit
from openff.toolkit.topology import Molecule


def smirnoff_parameterize(ligand_path, forcefield="openff-2.0.0.offxml"):

    ligand_name = ligand_path.split(".")[0]

    # Load a molecule from a PDB file
    ligand = Molecule.from_file(ligand_path)

    # Molecule stores both the co-ordinates of the atoms and their bond graph
    ligand_positions = ligand.conformers[0]
    ligand_topology = ligand.to_topology()

    # Load protein and water force field parameters
    omm_forcefield = openmm.app.ForceField("amber99sb.xml", "tip3p.xml")
    # Teach OpenMM about the ligand molecule and the Sage force field
    smirnoff = SMIRNOFFTemplateGenerator(forcefield=str(forcefield), molecules=[ligand])
    omm_forcefield.registerTemplateGenerator(smirnoff.generator)

    modeller = openmm.app.Modeller(ligand_topology.to_openmm(), ligand_positions)

    # Retrieve the OpenMM Topology, which stores the atoms and connectivity
    topology = modeller.getTopology()

    # Get the initial positions
    positions = modeller.getPositions()

    # ParmEd's GROMACS exporter can't handle constraints from openmm, so we need a variant for export without them
    export_system = omm_forcefield.createSystem(
        modeller.topology,
        #    nonbondedMethod=openmm.app.PME,
        constraints=None,
        rigidWater=False,
    )

    # Combine the topology, system and positions into a ParmEd Structure
    pmd_complex_struct = pmd.openmm.load_topology(topology, export_system, positions)

    # Export GROMACS files.
    pmd_complex_struct.save(ligand_name + ".top", overwrite=True)
    pmd_complex_struct.save(ligand_name + ".gro", overwrite=True)

    with open(ligand_name + ".top") as f:
        data = f.readlines()

        atomtypes = data[
            data.index("[ atomtypes ]\n") : data.index("[ moleculetype ]\n") - 1
        ]
        itp = data[data.index("[ moleculetype ]\n") : data.index("[ system ]\n") - 1]
    itp_mod = []
    for line in itp:
        to_append = line.lower()  # .replace('unk','UNK')
        itp_mod.append(to_append)
    atomtypes_mod = []
    for line in atomtypes:
        to_append = line.lower().replace(" a ", " A ")
        atomtypes_mod.append(to_append)

    # write files
    itp_filename = ligand_name + ".itp"
    atomtypes_filename = ligand_name + "_atomtypes.itp"
    with open(itp_filename, "w") as f:
        f.writelines(itp_mod)
    with open(atomtypes_filename, "w") as f:
        f.writelines(atomtypes_mod)

    # Convert gro file to lowercase
    ligand_gro = ligand_name + ".gro"

    with open(ligand_gro) as f:
        data = f.readlines()
        new_gro = []
        for line in data:
            to_append = line.lower()
            new_gro.append(to_append)

    # write files
    new_gro_file = ligand_gro.split(".")[0] + "_mod.gro"
    with open(new_gro_file, "w") as f:
        f.writelines(new_gro)


def main():
    parser = argparse.ArgumentParser(
        description="Smirnoff_param.py v2a to parameterize a sdf molecule"
    )
    parser.add_argument(
        "-f", "--file", help="Path to the Ligand structure file (sdf)", required=True
    )
    args = parser.parse_args()
    input_file_path = args.file
    print("Input file:", input_file_path)
    smirnoff_parameterize(input_file_path)


if __name__ == "__main__":
    main()
