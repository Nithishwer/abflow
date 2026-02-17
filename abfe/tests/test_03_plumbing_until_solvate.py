# abfe/tests/test_03_plumbing_until_solvate.py
import shutil
from pathlib import Path

from abfe.setup_vanilla_simulations import SimulationSetup
from abfe.tests import datafiles


def test_run_setup_through_solvation_fast(tmp_path, monkeypatch):
    """
    Medium-fast: run run_vanilla_setup with light stubs so we pass:
    param_lig -> copy protein -> organize/add ligand files -> extract membrane ->
    copy_complex2membrane -> addmemtop -> solvate_system -> cp_mdps
    """

    # --- Arrange inputs
    base_path = tmp_path / "test_setup"
    charmm_folder = base_path / "input_charmm"
    protein_folder = base_path / "input_protein"
    sdf_folder = base_path / "input_ligands"
    crystal_water = base_path / "crystal_waters.gro"

    shutil.copytree(datafiles.CHARMM.resolve(), charmm_folder)
    shutil.copytree(datafiles.PROTEIN.resolve(), protein_folder)
    shutil.copytree(datafiles.SDF_DIR.resolve(), sdf_folder)
    shutil.copy(datafiles.CRYSTAL_WATER.resolve(), crystal_water)

    # Ensure there's at least one SDF so setup_all() always iterates.
    if not any(sdf_folder.glob("*.sdf")):
        (sdf_folder / "DUMMY.sdf").write_text("\n")

    # --- Patch functions as imported INTO the target module
    target_mod = "abfe.setup_vanilla_simulations"

    # No-op parametrization (avoid openff/bespokefit)
    monkeypatch.setattr(f"{target_mod}.param_lig", lambda sdf: Path("smirnoff").mkdir(exist_ok=True))

    # Provide a minimal topology so downstream steps that read/append to topol.top succeed
    monkeypatch.setattr(
        SimulationSetup,
        "copy_parameterized_protein",
        lambda self: Path("topol.top").write_text(
            "; minimal topol for test\n[ system ]\nTest\n\n[ molecules ]\n; name  number\n"
        ),
    )

    # Lightweight ligand topology steps
    monkeypatch.setattr(f"{target_mod}.organize_topologies", lambda: None)
    monkeypatch.setattr(f"{target_mod}.addligtop", lambda sdf: None)
    monkeypatch.setattr(f"{target_mod}.addliggro", lambda sdf: None)
    monkeypatch.setattr(f"{target_mod}.addligposres", lambda sdf: None)

    # FAKE the membrane extraction here to avoid MDAnalysis StopIteration on some fixtures
    def _fake_extract_membrane_from_charmm(charmm_gro, membrane_out):
        Path(membrane_out).write_text("membrane\n")
    monkeypatch.setattr(f"{target_mod}.extract_membrane_from_charmm", _fake_extract_membrane_from_charmm)

    # Fake the combine step
    def _fake_copy_complex2membrane():
        Path("complex_membrane.gro").write_text("combined\n")
    monkeypatch.setattr(f"{target_mod}.copy_complex2membrane", _fake_copy_complex2membrane)

    # addmemtop can be a no-op
    monkeypatch.setattr(f"{target_mod}.addmemtop", lambda sdf: None)

    # Fake solvation: create solv.gro and solv_fix.gro
    def _fake_solvate(waters: int):
        Path("solv.gro").write_text("solv\n")
        Path("solv_fix.gro").write_text("solv_fix\n")
    monkeypatch.setattr(f"{target_mod}.solvate_system", _fake_solvate)

    # Fake mdp copy so a file exists
    def _fake_cp_mdps(_):
        (Path.cwd() / "mdps").mkdir(exist_ok=True)
        (Path("mdps") / "minim.mdp").write_text("; mdp")
    monkeypatch.setattr(f"{target_mod}.cp_mdps", _fake_cp_mdps)

    monkeypatch.setattr(
    f"{target_mod}.merge_gro_files",
    lambda file1, file2: Path("solv_fix_crystal_water.gro").write_text("merged\n"),
    )
    monkeypatch.setattr(f"{target_mod}.update_topology_file", lambda *_: None)
    monkeypatch.setattr(f"{target_mod}.add_water2topology", lambda *_: None)

    # pretend the ions step succeeded and produced the expected output file
    monkeypatch.setattr(
        f"{target_mod}.addions",
        lambda *_: Path("system_solv_ions.gro").write_text("ions\n"),
    )

    # pretend index creation succeeded
    monkeypatch.setattr(
        f"{target_mod}.create_index",
        lambda *_: Path("index.ndx").write_text("[ System ]\n1-10\n"),
    )

    # (optional but safe) avoid touching real cluster templates
    monkeypatch.setattr(
        f"{target_mod}.copy_jobscripts_vanilla",
        lambda templates, name, nodes: Path("submit.sh").write_text("#!/bin/bash\ntrue\n"),
    )

    setup = SimulationSetup(
        base_path=base_path,
        charmm_folder=charmm_folder,
        protein_folder=protein_folder,
        sdf_folder=sdf_folder,
        crystal_water_gro=crystal_water,
        solvate_water_count=10286,
        crystal_water_count=136,
        num_nodes=1,
    )

    # --- Act
    setup.setup_all(repeats=1)

    # --- Assert
    complex_dirs = [p for p in base_path.iterdir() if p.is_dir() and p.name.startswith("complex_")]
    assert complex_dirs, "No complex_* output folder was created."
    complex_root = complex_dirs[0]
    outdir = complex_root / "vanilla_rep_1"

    assert outdir.exists(), f"Output folder not created: {outdir}"
    assert (outdir / "membrane.gro").exists(), "membrane.gro missing"
    assert (outdir / "complex_membrane.gro").exists(), "combined complex/membrane GRO missing"
    assert (outdir / "solv_fix.gro").exists(), "solv_fix.gro missing"
    assert (outdir / "mdps" / "minim.mdp").exists(), "MDP not copied"
    assert (outdir / "topol.top").exists(), "topol.top missing"
