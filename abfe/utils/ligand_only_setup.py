import os
import shutil
import subprocess
from pathlib import Path
from rdkit import Chem
import logging
from importlib.resources import files, as_file
import importlib.resources as pkg_resources


from abfe.utils.data import forcefield_dir
from abfe.utils.data.ligand_only import lig_super_mdp_files_pertuball




class LigandOnlySetup:
    def __init__(self, ligand_name: str, abfe_dir: Path):
        self.ligand_name = ligand_name
        self.abfe_dir = abfe_dir
        self.lig_dir = abfe_dir / "ligand"
        self.toppar_dir = self.lig_dir / "toppar"

        # Access data from within the installed Python package
        self.forcefield_src = files("abfe.utils.data.forcefield_dir")
        self.template_script = files("abfe.utils.cluster.job_script_template.abfe_ligonly") / "job_abfe_lig_archer.sh"

        # Directory containing topol_ref, em.mdp, sim list, and submit.sh
        self.script_dir = files("abfe.utils.data.ligand_only")

        # Derived paths to individual files
        self.topol_template = self.script_dir / "lig_topol_ref.top"
        self.em_mdp = self.script_dir / "ligand_em.mdp"
        self.submit_script = self.script_dir / "lig_submit.sh"
        self.sim_list = self.script_dir / "lig_simulations_list.txt"

    def setup(self):
        self._make_dirs()
        self._copy_files()
        self._replace_lig_name()
        self._run_gromacs_prep()
        self._create_fep_system()
        self._cleanup()
        self._make_index()
        self._write_mdps()
        self._generate_submission_script()

    def _make_dirs(self):
        self.lig_dir.mkdir(exist_ok=True)
        self.toppar_dir.mkdir(exist_ok=True)
        os.chdir(self.lig_dir)

    def _copy_files(self):

        lig_suffix = self.ligand_name.split("_")[-1]
        base = self.abfe_dir.parent / "vanilla_rep_1"

        file_map = {
            base / "smirnoff" / f"{lig_suffix}_mod.gro": self.lig_dir / f"{self.ligand_name}_mod.gro",
            base / "toppar" / f"{lig_suffix}.itp": self.toppar_dir / f"{lig_suffix}.itp",
            base / "toppar" / f"{lig_suffix}_posre.itp": self.toppar_dir / f"{lig_suffix}_posre.itp",
            base / "toppar" / f"{lig_suffix}_atomtypes.itp": self.toppar_dir / f"{lig_suffix}_atomtypes.itp",
            self.script_dir / "lig_topol_ref.top": self.lig_dir / "lig.top",
            self.script_dir / "ligand_em.mdp": self.lig_dir / "em.mdp",
            self.script_dir / "lig_submit.sh": self.lig_dir / "submit.sh",
            self.script_dir / "lig_simulations_list.txt": self.lig_dir / "simulations_list.txt",
        }

        for src, dst in file_map.items():
            if not src.exists():
                raise FileNotFoundError(f"Required file not found: {src}")
            shutil.copyfile(src, dst)

        # ✅ Copy forcefield
        with as_file(files(forcefield_dir) / "amber99sb-star-ildn-mut.ff") as ff_src_real:
            ff_dst = self.lig_dir / "amber99sb-star-ildn-mut.ff"
            shutil.copytree(ff_src_real, ff_dst, dirs_exist_ok=True)
            logging.info(f"Copied forcefield directory from {ff_src_real} to {ff_dst}")

        # ✅ Copy MDP templates (FIXED)
        mdp_dst_dir = self.lig_dir / "super_mdp_files"
        mdp_dst_dir.mkdir(exist_ok=True)

        for template in pkg_resources.files(lig_super_mdp_files_pertuball).iterdir():
            if template.is_file():
                dst_file = mdp_dst_dir / template.name
                with template.open("rb") as src, open(dst_file, "wb") as dst:
                    shutil.copyfileobj(src, dst)

        logging.info(f"Copied {len(list(mdp_dst_dir.iterdir()))} MDP templates into {mdp_dst_dir}")



        # Copy alchemical ion script
        script_src = self.script_dir / "add_alchemical_ion_v3_pertuball.py"
        if script_src.exists():
            shutil.copyfile(script_src, self.lig_dir / "add_alchemical_ion_v3_pertuball.py")





    def _replace_lig_name(self):
        top_path = self.lig_dir / "lig.top"

        if not top_path.is_file():
            raise FileNotFoundError(f"File not found: {top_path}")

        text = top_path.read_text()

        # Replace placeholders
        text = text.replace("$LIG$", self.ligand_name)
        lig_suffix = self.ligand_name.split("_")[-1]
        text = text.replace("$SUFFIX$", lig_suffix)

        top_path.write_text(text)

        logging.info(f"Patched lig.top: $LIG$ → {self.ligand_name}, $SUFFIX$ → {lig_suffix}")



    def _run_gromacs_prep(self):
        subprocess.run([
            "gmx", "editconf",
            "-f", f"{self.ligand_name}_mod.gro",
            "-o", "ligand_only.gro",
            "-princ", "-box", "6.0", "6.0", "6.00000", "-c"
        ], input="2\n", text=True, check=True)

        subprocess.run([
            "gmx", "solvate",
            "-cp", "ligand_only.gro",
            "-cs", "spc216.gro",
            "-o", "system_solv.gro",
            "-p", "lig.top"
        ], check=True)

        subprocess.run([
            "gmx", "grompp",
            "-f", "em.mdp",
            "-c", "system_solv.gro",
            "-r", "system_solv.gro",
            "-p", "lig.top",
            "-o", "ions.tpr",
            "-maxwarn", "1"
        ], check=True)

        subprocess.run(
            "echo SOL | gmx genion -s ions.tpr -o lig.gro -p lig.top -pname NA -nname CL -neutral -conc 0.15",
            shell=True, check=True
        )

    def _calculate_total_charge(self, sdf_path):
        """Calculate the total formal charge of a molecule from an SDF file."""
        supplier = Chem.SDMolSupplier(str(sdf_path))

        if not supplier or len(supplier) == 0:
            raise ValueError(f"No molecules found in SDF file: {sdf_path}")

        mol = supplier[0]
        if mol is None:
            raise ValueError(f"Failed to parse molecule from SDF file: {sdf_path}")

        total_charge = sum(atom.GetFormalCharge() for atom in mol.GetAtoms())
        logging.info(f"Ligand {self.ligand_name} formal charge: {total_charge}")
        return total_charge


    def _create_fep_system(self, force=False):
        lig_suffix = self.ligand_name.split("_")[-1]
        sdf_path = self.abfe_dir.parent / "vanilla_rep_1" / "smirnoff" / f"{lig_suffix}.sdf"


        lig_charge = self._calculate_total_charge(sdf_path)
        if lig_charge != 0:
            logging.warning(f"Ligand {self.ligand_name} has a non-zero charge of {lig_charge} e.")
            if not force:
                user_input = input(f"Ligand charge = {lig_charge}. Proceed? (y/n): ")
                if user_input.lower() != 'y':
                    logging.info("Aborting FEP system creation by user choice.")
                    return

        alch_method = "co-annihilation" if lig_charge != 0 else "None"
        script_path = self.lig_dir / "add_alchemical_ion_v3_pertuball.py"

        command = [
            "python", str(script_path),
            "--lig_charge", str(lig_charge),
            "--alch_ion_method", alch_method,
            "--lig", "unk",
            "--lig_itp", f"toppar/{lig_suffix}.itp",
            "--atomtype_itp", f"toppar/{lig_suffix}_atomtypes.itp",
            "--top", "lig.top",
            "--gro", "lig.gro"
        ]

        subprocess.run(command, check=True)
        logging.info(f"FEP system created for ligand {self.ligand_name}")



    def _cleanup(self):
        for file in os.listdir():
            if "rest." in file:
                os.remove(file)

    def _make_index(self):
        subprocess.run("echo q | gmx make_ndx -f lig_coul.gro -o index.ndx", shell=True, check=True)



    def _write_mdps(self):
        """Create FEP simulation directories and write templated MDP files using soluble system templates."""
        leg_windows = {
            "coul": 11,
            "vdw": 21
        }
        stages = ["enmin", "npt_b", "npt_pr", "nvt", "prod"]

        # Group names typical for ligand-only simulations
        pml_index = "LIG"
        wiai_index = "SOL_ION"

        for leg, windows in leg_windows.items():
            for i in range(windows):
                i_str = f"{i:02d}"
                leg_dir = self.lig_dir / f"{leg}.{i_str}"
                leg_dir.mkdir(exist_ok=True)

                for stage in stages:
                    stage_dir = leg_dir / stage
                    stage_dir.mkdir(exist_ok=True)

                    template_filename = f"{leg}.{stage}.super.mdp"
                    output_path = stage_dir / f"{leg}.{stage}.{i_str}.mdp"

                    try:
                        with pkg_resources.files(lig_super_mdp_files_pertuball).joinpath(template_filename).open("r") as f:
                            content = f.read()

                        # Replace template placeholders
                        modified_content = (
                            content.replace("<state>", str(i))
                                .replace("Protein_LIG_POPC", pml_index)
                                .replace("Water_and_ions_B_CL", wiai_index)
                        )

                        with open(output_path, "w") as f_out:
                            f_out.write(modified_content)

                        logging.info(f"Written: {output_path}")
                    except FileNotFoundError:
                        logging.error(f"Missing MDP template: {template_filename}")


    def _generate_submission_script(self):
        print(f"DEBUG: Using job script template: {self.template_script}")
        print(f"DEBUG: Exists? {self.template_script.exists()}")
        dst = self.lig_dir / "job_lig_archer.sh"
        with open(self.template_script, "r") as file:
            filedata = file.read()
        filedata = filedata.replace("JOBNAME", f"lig{self.ligand_name}")
        filedata = filedata.replace("NO_OF_NODES", str(1))
        with open(dst, "w") as file:
            file.write(filedata)
        print(f"DEBUG: Wrote job script to {dst}")

