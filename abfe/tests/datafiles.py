# abfe/tests/datafiles.py

from importlib.resources import files

BASE_DATA = files("abfe.tests.test_data.vanilla_input.system_setup")

CHARMM = BASE_DATA / "charmm" / "charmm_gmx"
PROTEIN = BASE_DATA / "protein_prep" / "protein_param"
SDF_DIR = BASE_DATA / "ligands_aligned2charmm"
CRYSTAL_WATER = (
    BASE_DATA
    / "protein_prep"
    / "crystal_water_param"
    / "crystal_waters_aligned2charmm.gro"
)
