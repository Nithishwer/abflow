# abfe/tests/test_01_scaffold.py
import shutil
from pathlib import Path
import pytest

from abfe.setup_vanilla_simulations import SimulationSetup
from abfe.tests import datafiles


def test_scaffold_creates_expected_layout(tmp_path, monkeypatch):
    """Fast: only directory + initial file copies (no heavy setup)."""

    # Arrange: temp working dirs and inputs
    base_path = tmp_path / "test_setup"
    charmm_folder = base_path / "input_charmm"
    protein_folder = base_path / "input_protein"
    sdf_folder = base_path / "input_ligands"
    crystal_water = base_path / "crystal_waters.gro"

    shutil.copytree(datafiles.CHARMM.resolve(), charmm_folder)
    shutil.copytree(datafiles.PROTEIN.resolve(), protein_folder)
    shutil.copytree(datafiles.SDF_DIR.resolve(), sdf_folder)
    shutil.copy(datafiles.CRYSTAL_WATER.resolve(), crystal_water)

    # Stub out the heavy runner to a no-op so we verify only the scaffold work
    monkeypatch.setattr(
        SimulationSetup,
        "run_vanilla_setup",
        lambda self, vp, cn: None,
    )

    setup = SimulationSetup(
        base_path=base_path,
        charmm_folder=charmm_folder,
        protein_folder=protein_folder,
        sdf_folder=sdf_folder,
        crystal_water_gro=crystal_water,
        solvate_water_count=10286,
        crystal_water_count=136,
        num_nodes=2,
    )

    # Act
    setup.setup_all(repeats=1)

    # Assert
    ligand = next(p.stem for p in sdf_folder.glob("*.sdf"))
    outdir = base_path / f"complex_{ligand}" / "vanilla_rep_1"
    assert outdir.exists(), "vanilla_rep_1 not created."

    assert (outdir / f"{ligand}.sdf").exists(), "Ligand SDF not copied."
    assert (outdir / "charmm_gmx" / "step5_input.pdb").exists(), "CHARMM dir not copied as charmm_gmx."
