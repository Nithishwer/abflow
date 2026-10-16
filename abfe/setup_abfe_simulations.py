import logging
from pathlib import Path
from importlib.resources import files

from abfe.utils.abfe_helpers_hrex import (
    generate_boresch_restraints, copy_complex, create_fep_system, create_index,
    create_directories_with_MDP_pertuball_smaller_dumps, create_simulations_list,
    gen_hrex_submission_script,
)
from abfe.utils.utils import working_directory


class ABFESetup:
    """Prepare one complex leg per vanilla repeat, preserving historical folders."""

    def __init__(self, base_path, ligands, num_replicates, archer_nodes=22, *,
                 vanilla_paths=None, forcefield_path=None, write_job_scripts=True,
                 expected_charge=None, restraint_script=None, topology_paths=None):
        self.base_path = Path(base_path).resolve()
        self.ligands = ligands
        self.num_replicates = num_replicates
        self.archer_nodes = archer_nodes
        self.vanilla_paths = vanilla_paths
        self.forcefield_path = forcefield_path
        self.write_job_scripts = write_job_scripts
        self.expected_charge = expected_charge
        self.restraint_script = restraint_script
        self.topology_paths = topology_paths

    def setup(self):
        if self.num_replicates < 1:
            raise ValueError("num_replicates must be positive")
        for ligand in self.ligands:
            ligand_dir = self.base_path / ligand
            for repeat in range(1, self.num_replicates + 1):
                vanilla = (Path(self.vanilla_paths[repeat]).resolve() if self.vanilla_paths
                           else ligand_dir / f"vanilla_rep_{repeat}")
                if not vanilla.is_dir():
                    raise FileNotFoundError(f"Missing vanilla repeat {repeat}: {vanilla}")
                if self.topology_paths:
                    self._setup_abfe_folder(ligand_dir, f"van{repeat}_hrex_r1", vanilla,
                                            topology_path=self.topology_paths[repeat])
                else:
                    self._setup_abfe_folder(ligand_dir, f"van{repeat}_hrex_r1", vanilla)

    def _setup_abfe_folder(self, ligand_dir, suffix, vanilla_folder_name, topology_path=None):
        abfe_folder = ligand_dir / f"abfe_{suffix}"
        if abfe_folder.exists():
            logging.warning("%s already exists; preserving it", abfe_folder)
            return
        complex_path = abfe_folder / "complex"
        complex_path.mkdir(parents=True)
        vanilla = (ligand_dir / vanilla_folder_name).resolve()
        with working_directory(complex_path):
            generate_boresch_restraints(vanilla_folder_name=vanilla, abfe_path=abfe_folder,
                                        restraint_script=self.restraint_script, topology_path=topology_path)
            copy_complex(vanilla_folder_name=vanilla, abfe_path=abfe_folder,
                         forcefield_path=self.forcefield_path, topology_path=topology_path)
            create_fep_system(ligand_dir.name, vanilla, expected_charge=self.expected_charge)
            create_index("complex_coul.gro", "index.ndx")
            for phase in ('rest', 'coul', 'vdw'):
                create_directories_with_MDP_pertuball_smaller_dumps(
                    phase, "Protein_unk_PA_PC_OL", "Water_and_ions")
            create_simulations_list()
            if self.write_job_scripts:
                simulation_list = ' '.join(
                    f"{phase}.{i:02d}/$STEP/" for phase, count in
                    (('rest', 12), ('coul', 11), ('vdw', 21)) for i in range(count))
                templates = files("abfe.utils.cluster.job_script_template.abfe")
                for template, destination in (
                    ("job_complex_archer_hrex_template.sh", "job_complex_archer.sh"),
                    ("job_complex_archer_hrex_cont_template.sh", "job_complex_archer_contd.sh")):
                    gen_hrex_submission_script(
                        template_path=templates / template, new_script_name=destination,
                        job_name=ligand_dir.name, archer_nodes=self.archer_nodes,
                        simulation_list_string=simulation_list)
            (complex_path / "plumed.dat").touch()


def main():
    import argparse
    parser = argparse.ArgumentParser(description=ABFESetup.__doc__)
    parser.add_argument('--base_path', required=True)
    parser.add_argument('--ligands', nargs='+', required=True)
    parser.add_argument('--num_replicates', type=int, default=3,
                        help='Total complex repeats per ligand, one per vanilla repeat')
    parser.add_argument('--archer_nodes', type=int, default=22)
    args = parser.parse_args()
    ABFESetup(args.base_path, args.ligands, args.num_replicates, args.archer_nodes).setup()


if __name__ == "__main__":
    main()
