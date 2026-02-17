import shutil
from pathlib import Path
import pytest
import warnings
warnings.filterwarnings("ignore", category=DeprecationWarning, module="Bio.Application")


from abfe.setup_abfe_simulations import ABFESetup
from abfe.utils.ligand_only_setup import LigandOnlySetup
from abfe.tests import datafiles


@pytest.mark.slow
def test_setup_abfe_simulation_creates_expected_files(tmp_path):
    """
    Integration test: run the complex-leg ABFE setup and the ligand-only setup
    for the A2A_4g ligand.  Verify that key output directories and files exist.
    """
    # Temporary base directory for this test
    base_path = tmp_path / "abfe_test"

    # Name of the ligand we want to test
    ligand_name = "A2A_4g"

    # Copy the A2A_4g ABFE input directory into our temporary base path
    # The path is defined in datafiles.ABFE_DEFLORIAN
    src = (datafiles.ABFE_DEFLORIAN / ligand_name).resolve()
    dest = base_path / ligand_name
    shutil.copytree(src, dest)

    # Run complex-leg ABFE setup with one replicate
    abfe_setup = ABFESetup(
        base_path=base_path,
        ligands=[ligand_name],
        num_replicates=1,
        archer_nodes=2,
    )
    abfe_setup.setup()

    # Run ligand-only setup for replicate 1 (vanilla index 1)
    ligonly_abfe_dir = base_path / ligand_name / "abfe_van1_hrex_r1"
    ligand_setup = LigandOnlySetup(ligand_name=ligand_name, abfe_dir=ligonly_abfe_dir)
    ligand_setup.setup()

    # ---------------------------------------------------------------------
    # Assertions: verify that the ABFE folder and key outputs exist.
    # We expect the setup to create abfe_van1_hrex_r1 in the ligand directory.
    assert ligonly_abfe_dir.exists(), "ABFE directory was not created."

    # The complex structure should have been copied into the complex subfolder.
    complex_dir = ligonly_abfe_dir / "complex"
    assert complex_dir.exists(), "Complex directory missing in ABFE setup."

    # The Boresch restraints directory should have been created in the ABFE directory.
    boresch_dir = ligonly_abfe_dir / "boresch"
    assert boresch_dir.exists(), "Boresch directory missing in ABFE setup."
    assert any(boresch_dir.iterdir()), "Boresch directory is empty."


    # At least one job script (e.g. job_lig_archer.sh) should have been generated in the ligand subfolder.
    ligand_dir = ligonly_abfe_dir / "ligand"
    job_scripts = list(ligand_dir.glob("job*"))
    assert job_scripts, "No job scripts were created in the ligand ABFE directory."
    # ---------------------------------------------------------------------
