# ABFE_Package

## Install from Source

1. **Clone the repository**

```bash
git clone https://github.com/Nithishwer/abflow.git
cd your-repo
```

2. **Create and activate the conda environment**

```bash
conda env create -f abfe_environment.yml
conda activate abflow
```

3. **Install `MDRestraintsGenerator`**

```bash
mkdir src
cd src
git clone https://github.com/Nithishwer/MDRestraintsGenerator.git
cd MDRestraintsGenerator
pip install --no-deps .
```

4. **Install this repository**

```bash
cd ../../
pip install .
```

## HPC-specific dependencies

In addition to the Python environment (`abfe_environment.yml`), this workflow requires the following modules to be loaded on the HPC environment before running:

```bash
module load GROMACS/2023
```

MDRestraintsGenerator package needs to be installed with no-dependencies flag to avoid clashing with scipy module: 

```
cd ~/Documents/abflow/src  # or wherever you keep your editable sources
git clone https://github.com/Nithishwer/MDRestraintsGenerator.git
cd MDRestraintsGenerator
pip install --no-deps -e .
```

## Usage

The `SimulationSetup` class prepares full membrane-protein-ligand systems for ABFE simulations (one folder per ligand and replicate). This includes merging protein, membrane, ligand, and crystal waters, solvating the box, and generating GROMACS input files.

### Example: Running ABFE Setup for All Ligands

```python
from abfe.setup_vanilla_simulations import SimulationSetup

# Define your paths (replace with your actual directories)
base_path = "/ABS/PATH/TO/WORKDIR"  # e.g. /home/user/abfe_project
charmm_folder = "/ABS/PATH/TO/CHARMM_GMX"  # contains step5_input.pdb and membrane files
protein_folder = "/ABS/PATH/TO/PROTEIN_PARAM"  # contains topol.top, forcefield.itp, posres.itp, etc.
sdf_folder = "/ABS/PATH/TO/LIGAND_SDFS"  # contains *.sdf files, pre-aligned to protein binding site
crystal_water_gro = "/ABS/PATH/TO/CRYSTAL_WATERS.gro"  # aligned and parameterized crystal waters

# Optional settings
solvate_water_count = 10286
crystal_water_count = 136
num_nodes = 2

# Instantiate the setup class
setup = SimulationSetup(
    base_path,
    charmm_folder,
    protein_folder,
    sdf_folder,
    crystal_water_gro,
    solvate_water_count,
    crystal_water_count,
    num_nodes
)

# prepare every ligand in the SDF folder
setup.setup_all()

# or, for a single ligand, single run:
# setup.setup_simulation("cmp_123")

# all ligands, 5 repeats
# setup.setup_all(repeats=5)

# single ligand, custom repeats (three replicas for only cmp_42
# for replica in range(1, 4):
#    setup.setup_simulation("cmp_42", replicate=replica)
```

## ABFE Setup

The `ABFESetup` class automates the creation of simulation-ready folders for Absolute Binding Free Energy (ABFE) calculations, one per replicate. The workflow supports both **complex-leg** (protein-ligand) and **ligand-only** simulations.

> **Only edit the two lines under "Define base path and ligand names"** — the rest will work as-is.

### Example: Full ABFE Setup Workflow

```python
from abfe.setup_abfe_simulations import ABFESetup
from abfe.utils.ligand_only_setup import LigandOnlySetup
from pathlib import Path
import logging

# --- Define base path and ligand names (EDIT THIS ONLY) ---
base_path = "/ABS/PATH/TO/WORKDIR"
ligands = ["Ligand1", "Ligand2"]  # Each should be a subfolder under base_path

# --- Optional settings ---
num_replicates = 3
archer_nodes = 22  # Adjust based on your cluster requirements

# --- Logging (optional but recommended) ---
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)

# --- Step 1: Set up complex-leg ABFE folders ---
abfe_setup = ABFESetup(
    base_path=base_path,
    ligands=ligands,
    num_replicates=num_replicates,
    archer_nodes=archer_nodes
)
abfe_setup.setup()

# --- Step 2: Set up ligand-only simulations for replicate 1 of each ligand ---
for ligand in ligands:
    lig_dir = Path(base_path) / ligand
    ligonly_abfe_dir = lig_dir / "abfe_van1_hrex_r1"

    try:
        logging.info(f"Starting ligand-only setup in: {ligonly_abfe_dir}")
        setup = LigandOnlySetup(ligand_name=ligand, abfe_dir=ligonly_abfe_dir)
        setup.setup()
        logging.info(f"Completed ligand-only setup for {ligand} rep 1")
    except Exception as e:
        logging.error(f"Ligand-only setup failed for {ligand} rep 1: {e}")
```



## ABFE Analysis

The `ABFEAnalyzer` class performs automated analysis of complex-leg and ligand-only ABFE simulations.

- Computes ΔG for complex and ligand legs across multiple replicates  
- Ensures ligand-only analysis is done **once per system**  
- Symlinks ligand results across replicates for consistency  
- Compiles final ΔG results for each ligand

> **Only edit the `base_path` and `ligand_folders` list — everything else works out of the box.**

### Example: Analyzing ABFE Results Across Ligands

```python
from abfe.analyse_abfe import ABFEAnalyzer

# --- Define your system path and ligands ---
base_path = "/ABS/PATH/TO/WORKDIR"  # Folder containing all ligand folders
ligand_folders = ["Ligand1", "Ligand2"]  # Replace with your actual ligand folder names
n_replicates = 3  # Number of ABFE replicates per ligand

# --- Run analysis for each ligand ---
for ligand in ligand_folders:
    ligand_path = f"{base_path}/{ligand}"
    print(f"Running ABFE analysis for {ligand_path}")

    analyzer = ABFEAnalyzer(
        project_root=ligand_path,
        abfe_replicates=n_replicates,
        temperature=298.0  # Optional: change if your simulation was at a different T
    )
    analyzer.run()

