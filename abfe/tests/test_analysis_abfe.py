
import os
import shutil
from pathlib import Path
import pytest

from abfe.analyse_abfe import ABFEAnalyzer
from abfe.tests import datafiles


@pytest.mark.slow
def test_abfe_analysis_pipeline(tmp_path):
    """
    Integration test: Run ABFEAnalyzer on prepared analysis input
    and verify that analysis folders, symlinks, and output files are created.
    """
    # 1. Setup test directory with input files
    ligand_name = "A2A_4j"
    input_dir = datafiles.ABFE_ANALYSIS_INPUT / "A2A_4j"
    working_dir = tmp_path / "abfe_analysis_test" / ligand_name
    shutil.copytree(input_dir, working_dir)

    # 2. Run the ABFE analysis pipeline
    analyzer = ABFEAnalyzer(
        project_root=str(working_dir),
        abfe_replicates=1,
        temperature=298.0
    )
    analyzer.run()

    # 3. Check that the expected analysis output structure is present
    ref_path = working_dir / "abfe_van1_hrex_r1"

    analysis_dir = ref_path / "analysis"
    ligand_output = analysis_dir / "ligand"
    complex_output = analysis_dir / "complex"
    boresch_file = ref_path / "boresch" / "resgen_output.txt"

    # ---- Top-level checks
    assert ref_path.exists(), "Reference replicate folder not created."
    assert ligand_output.exists(), "Ligand analysis output missing."
    assert complex_output.exists(), "Complex analysis output missing."
    assert boresch_file.exists(), "Boresch resgen_output.txt missing."

    # ---- Output file checks
    assert (ligand_output / "ABFE_summary.csv").exists(), "Ligand ABFE_summary.csv missing."
    assert (ligand_output / "ABFE_convergence.csv").exists(), "Ligand ABFE_convergence.csv missing."
    assert (complex_output / "ABFE_summary.csv").exists(), "Complex ABFE_summary.csv missing."
    assert (complex_output / "ABFE_convergence.csv").exists(), "Complex ABFE_convergence.csv missing."
    assert (analysis_dir / "abfe_results.csv").exists(), "Final abfe_results.csv missing."

    # ---- Symlink check
    other_replicates = [f for f in working_dir.glob("abfe_van*_hrex_r*") if f.name != "abfe_van1_hrex_r1"]
    for rep in other_replicates:
        symlink = rep / "analysis" / "ligand"
        assert symlink.exists(), f"Ligand symlink not created for {rep.name}"
        assert symlink.is_symlink(), f"Ligand analysis path is not a symlink for {rep.name}"

    print("✅ All ABFE analysis outputs and symlinks verified successfully.")
