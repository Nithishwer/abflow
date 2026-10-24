import shutil

import pytest

import abfe.preparation.vanilla as preparation_vanilla
from abfe.tests import datafiles


@pytest.mark.slow
def test_run_vanilla_setup_outputs_correct_files(tmp_path):
    """Integration test: run setup on 1 ligand with 1 repeat and check output"""

    # Setup temp input working directory
    base_path = tmp_path / "test_setup"
    charmm_folder = base_path / "input_charmm"
    protein_folder = base_path / "input_protein"
    sdf_folder = base_path / "input_ligands"
    crystal_water = base_path / "crystal_waters.gro"

    # Copy input files to working location (convert from Traversable to real path)
    shutil.copytree(datafiles.CHARMM.resolve(), charmm_folder)
    shutil.copytree(datafiles.PROTEIN.resolve(), protein_folder)
    shutil.copytree(datafiles.SDF_DIR.resolve(), sdf_folder)
    shutil.copy(datafiles.CRYSTAL_WATER.resolve(), crystal_water)

    setup = preparation_vanilla.SimulationSetup(
        base_path=base_path,
        charmm_folder=charmm_folder,
        protein_folder=protein_folder,
        sdf_folder=sdf_folder,
        crystal_water_gro=crystal_water,
        solvate_water_count=10286,
        crystal_water_count=136,
        num_nodes=2,
    )

    setup.setup_all(repeats=1)

    # Validate output folder
    ligand = next(p.stem for p in sdf_folder.glob("*.sdf"))
    outdir = base_path / f"complex_{ligand}" / "vanilla_rep_1"
    assert outdir.exists(), "Output folder not created."

    # Validate some expected files
    expected_files = [
        outdir / "topol.top",
        outdir / "solv_fix_crystal_water.gro",
        outdir / "system_solv_ions.gro",
        outdir / "index.ndx",
        outdir / f"{ligand}.sdf",
    ]
    for f in expected_files:
        assert f.exists(), f"{f.name} not created."

    # Optionally: check CHARMM folder is copied
    assert (outdir / "charmm_gmx" / "step5_input.pdb").exists()
