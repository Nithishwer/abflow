# abfe/tests/datafiles.py

from importlib.resources import files

# Base location for the vanilla input data used by test_setup_vanilla.py
BASE_DATA = files("abfe.tests.test_data.vanilla_input.system_setup")

CHARMM = BASE_DATA / "charmm" / "charmm_gmx"
PROTEIN = BASE_DATA / "protein_prep" / "protein_param"
SDF_DIR = BASE_DATA / "ligands_aligned2charmm"
CRYSTAL_WATER = BASE_DATA / "protein_prep" / "crystal_water_param" / "crystal_waters_aligned2charmm.gro"

# -------------------------------------------------------------------------
# New section for ABFE input data

ABFE_DEFLORIAN = files("abfe.tests.test_data.abfe_input.deflorian")
# Example usage: (ABFE_DEFLORIAN / "A2A_4g").resolve() gives the A2A_4g input



# -------------------------------------------------------------------------
# ABFE analysis input data for test_analysis_abfe.py

ABFE_ANALYSIS_INPUT = files("abfe.tests.test_data.abfe_analysis_input.deflorian")
