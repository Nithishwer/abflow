# abfe/tests/test_02_membrane_extract.py
import shutil
from pathlib import Path

from abfe.tests import datafiles
from abfe.utils.gromacs_helpers_single_chain import extract_membrane_from_charmm


def test_extract_membrane_from_charmm_creates_membrane_gro(tmp_path):
    """Fast: run the real extractor on provided CHARMM box."""
    # Arrange
    charmm_src = datafiles.CHARMM.resolve()
    work = tmp_path / "work"
    work.mkdir()
    shutil.copytree(charmm_src, work / "charmm_gmx")

    charmm_gro = work / "charmm_gmx" / "step5_input.gro"
    membrane_out = work / "membrane.gro"

    # Act
    extract_membrane_from_charmm(str(charmm_gro), str(membrane_out))

    # Assert
    assert membrane_out.exists(), "membrane.gro not created"
    # lightweight content sanity check
    text = membrane_out.read_text(errors="ignore")
    assert len(text) > 0, "membrane.gro is empty"
    # Optional: look for lipid residue hints (POPC etc.) if present in your test data
    # assert "POPC" in text or "LIPID" in text
