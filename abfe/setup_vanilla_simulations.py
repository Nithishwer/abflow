import os
import shutil
import re
from pathlib import Path
import MDAnalysis as mda
from importlib.resources import files
from MDAnalysis.coordinates.GRO import GROWriter

from abfe.utils.utils import (
    merge_gro_files, add_water2topology, addtoppar2top,
    copy_jobscripts_vanilla, working_directory
)
from abfe.utils.extract_membrane import extract_membrane
from abfe.utils.smirnoff_param_func import smirnoff_parameterize
from abfe.utils.gromacs_helpers_single_chain import (
    param_lig, organize_topologies, addligtop, addliggro,
    addligposres, extract_membrane_from_charmm,
    copy_complex2membrane, addmemtop, solvate_system,
    cp_mdps, addions, create_index, update_topology_file)

mdp_dir = files('abfe.utils.data.mdps') / 'mdps_vanilla_memprot'
submission_script_templates = files(
    "abfe.utils.cluster.job_script_template.equilibration"
)




class SimulationSetup:
    def __init__(self, base_path, charmm_folder, protein_folder, sdf_folder,
                 crystal_water_gro, solvate_water_count=10286, crystal_water_count=136, num_nodes=2, *, forcefield_path=None,
                 sage_offxml="openff-2.0.0.offxml", lipid_dir=None,
                 solvent_box="spc216.gro", salt_molar=None, write_job_scripts=True):
        self.base_path = Path(base_path)
        self.charmm_folder = Path(charmm_folder)
        self.protein_folder = Path(protein_folder)
        self.sdf_folder = Path(sdf_folder)
        self.crystal_water_gro = Path(crystal_water_gro)
        self.solvate_water_count = solvate_water_count
        self.crystal_water_count = crystal_water_count
        self.num_nodes = num_nodes
        self.forcefield_path = forcefield_path
        self.sage_offxml = sage_offxml
        self.lipid_dir = lipid_dir
        self.solvent_box = solvent_box
        self.salt_molar = salt_molar
        self.write_job_scripts = write_job_scripts

    def setup_simulation(self, compound_name, replicate: int = 1, *, output_path=None):
        complex_folder = self.base_path / f"complex_{compound_name}"   ## Refactor for different compound names
        
        # name each repeat folder “vanilla_rep_N”
        vanilla_name = f"vanilla_rep_{replicate}"
        vanilla_path = Path(output_path).resolve() if output_path else complex_folder / vanilla_name
        vanilla_path.mkdir(parents=True)

        sdf_file = self.sdf_folder / f"{compound_name}.sdf"
        shutil.copy(sdf_file, vanilla_path)

#        dest_charmm = vanilla_path / self.charmm_folder.name
#        shutil.copytree(self.charmm_folder, dest_charmm)

        dest_charmm = vanilla_path / "charmm_gmx"
        shutil.copytree(self.charmm_folder, dest_charmm)

        print(f"Setup complete in {vanilla_path}")
        self.run_vanilla_setup(vanilla_path, compound_name)

    def run_vanilla_setup(self, vanilla_path: Path, compound_name: str) -> None:
        """
        Perform all steps needed to prepare a vanilla membrane–protein simulation.
        Works within a temporary working directory using a context manager.
        """
        # use a context manager to avoid leaking the working directory
        with working_directory(vanilla_path):
            # ligand file name relative to current working directory
            sdf_filename = f"{compound_name}.sdf"

            # parameterise the ligand (creates smirnoff/bespokefit folder)
            param_lig(sdf_filename, sage_offxml=self.sage_offxml)

            # copy protein parameter files into the working directory
            self.copy_parameterized_protein()
            if self.forcefield_path is not None:
                ff = Path(self.forcefield_path)
                if not Path(ff.name).exists():
                    shutil.copytree(ff, ff.name)

            # organise ligand/protein .itp files into a toppar directory
            organize_topologies()
            addligtop(sdf_filename)
            addliggro(sdf_filename)
            addligposres(sdf_filename)

            # extract membrane from the CHARMM box
#            charmm_gro = self.charmm_folder / "step5_input.gro"
            charmm_gro = vanilla_path / "charmm_gmx" / "step5_input.gro"
            membrane_out = vanilla_path / "membrane.gro"
            extract_membrane_from_charmm(str(charmm_gro), str(membrane_out))


            # combine protein and membrane into a single gro
            copy_complex2membrane()

            # include membrane .itp files and POPC count in the topology
            addmemtop(sdf_filename, lipid_dir=self.lipid_dir)

            # solvate the system (generates solv.gro and solv_fix.gro)
            solvate_system(self.solvate_water_count, solvent_box=self.solvent_box)

            # copy mdp files into the working directory
            cp_mdps(mdp_dir)

            # merge the crystal waters with solv_fix.gro -> solv_fix_crystal_water.gro
            merge_gro_files("solv_fix.gro", self.crystal_water_gro.resolve())

            # rebuild the [ molecules ] block based on solv_fix_crystal_water.gro
            #update_topol_top_with_molecules_block("solv_fix_crystal_water.gro")

            # Manually update water counts in topol.top:
            #  - first set the water count to the number of waters remaining after deletion (self.solvate_water_count)
            #  - then add the crystal water molecules (self.crystal_water_count)
            update_topology_file(self.solvate_water_count)
            add_water2topology(self.crystal_water_count)           

            # now add ions; gmx grompp will find matching numbers
            addions("solv_fix_crystal_water.gro", water_group="SOL", salt_molar=self.salt_molar)

            # create an index file for later MD runs
            create_index("system_solv_ions.gro")

            # adjust #include lines for toppar/posre.itp if needed
            addtoppar2top("topol.top")

            # write job scripts for submission
            if self.write_job_scripts:
                copy_jobscripts_vanilla(submission_script_templates, compound_name, self.num_nodes)



    def copy_parameterized_protein(self):
        for item in self.protein_folder.iterdir():
            if item.is_file():
                shutil.copy(item, item.name)
            elif item.is_dir():
                shutil.copytree(item, item.name)

    def setup_all(self, repeats: int = 1):
        """
        Loop over every ligand in `sdf_folder`, running `repeats` independent setups.

        Args:
            repeats: how many vanilla_rep_N folders per ligand to produce.
        """
        compound_list = [p.stem for p in self.sdf_folder.glob("*.sdf")]
        for compound in compound_list:
            for rep in range(1, repeats + 1):
                print(f"Setting up {compound}, replica {rep}")
                self.setup_simulation(compound, replicate=rep)
