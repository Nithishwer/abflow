# ABFE_Package
Code for setting up and running replica ABFE simulations
Got it — here’s a clean README section with just your **install from source** steps:

---

## Install from Source

1. **Clone the repository**

```bash
git clone --recurse-submodules https://github.com/Nithishwer/abflow.git
cd abflow
```

2. **Create and activate the conda environment**

```bash
conda env create -f abfe_environment.yml
conda activate abflow_v2
```

3. **Install `MDRestraintsGenerator`**

```bash
git submodule update --init --recursive
pip install --no-deps ./src/MDRestraintsGenerator
```

4. **Install this repository**

```bash
pip install .
```

## HPC-specific dependencies

In addition to the Python environment (`abfe_environment.yml`), this workflow requires the following modules to be loaded on the HPC environment before running:

```bash
module load GROMACS/2023
```

MDRestraintsGenerator is pinned by the Git submodule. Install it with `--no-deps` to retain the environment's SciPy version. For an editable installation, run this from the ABFlow repository root:

```bash
git submodule update --init --recursive
pip install --no-deps -e ./src/MDRestraintsGenerator
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

Use `abflow_v2` and fill in `examples/preparation.json` with one registered compound's
three equilibration repeats, a CHARMM-GUI membrane input directory, reviewed
parameters and explicit water/ion settings.

```bash
python -m abfe.campaign --registry /absolute/campaign.sqlite prepare DATASET/LIGAND \
  --manifest /absolute/preparation.json --stage vanilla --output /absolute/staging/vanilla --plan
```

Omit `--plan` to generate staged inputs. After the three vanilla trajectories reach
100 ns, use `--stage alchemical --output /absolute/staging/alchemical` with the same
manifest. This generates three complex legs and one shared solvent leg, without
submitting jobs or adopting existing runs. Identical reruns verify/reuse outputs;
failures preserve staging and create incidents. Inspect shows preparation status.

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

# --- 🔧 Define your system path and ligands ---
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
```


## H.A.W.T.O.W.N console

Track runs, inspect incidents and view cluster history. Keep the registry and
reports outside simulation directories.

```bash
REGISTRY="$HOME/.local/state/abflow/campaign.sqlite"
python -m abfe.campaign --registry "$REGISTRY" init \
  --manifest examples/membrane-campaign.json --source-root /absolute/path/to/orexin
python -m abfe.campaign --registry "$REGISTRY" scan
python -m abfe.campaign --registry "$REGISTRY" status
python -m abfe.campaign --registry "$REGISTRY" report --output /tmp/hawtown.html
```

Open the report in a browser. Refresh cluster observations with `check-connections`,
or collect history and refresh reports with:

```bash
python -m abfe.campaign --registry "$REGISTRY" sample-clusters --publish-dir /absolute/console-directory
```

## Local analysis

Run on **absolut** using stable local DHDL files in place. Outputs are saved under
`<registry-directory>/analyses/<analysis-id>/`.

```bash
conda activate abflow_v2
RUN="deflorian-set1/OX2_48845/complex/3"
python -m abfe.campaign --registry "$REGISTRY" analyse "$RUN" --cutoff-ns 16 --plan
python -m abfe.campaign --registry "$REGISTRY" analyse "$RUN" --cutoff-ns 16
python -m abfe.campaign --registry "$REGISTRY" decide "$RUN"
python -m abfe.campaign --registry "$REGISTRY" import-analysis piersanti/A2A_14/complex/1 --plan
python -m abfe.campaign --registry "$REGISTRY" bind "$RUN" --compatibility /absolute/review.json
```

Both legs use a 16 ns minimum, a 10 ps discard and an absolute MBAR half-difference
<0.5 kcal/mol. Proposed targets use `min(analysed_cutoff_ns + 5, 50)`.
See [CONTEXT.md](CONTEXT.md) for provenance and solvent compatibility review details.

Run the focused tests, including the scientific integration check:

```bash
ABFLOW_SCIENTIFIC_TEST=1 python -m unittest \
  abfe.tests.test_analysis abfe.tests.test_campaign \
  abfe.tests.test_cluster_capacity abfe.tests.test_cluster_monitor
```
