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

The `ABFESetup` class automates the creation of simulation-ready folders for Absolute Binding Free Energy (ABFE) calculations, one per replicate. The workflow supports both **complex-leg** (protein-ligand) and **ligand-only** simulations.

> **Only edit the two lines under "Define base path and ligand names"** — the rest will work as-is.

### Example: Full ABFE Setup Workflow

```python
from abfe.setup_abfe_simulations import ABFESetup
from abfe.utils.ligand_only_setup import LigandOnlySetup
from pathlib import Path
import logging

# --- 🔧 Define base path and ligand names (EDIT THIS ONLY) ---
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



## Campaign registry and read-only console

The campaign inventory runs with Python 3.10+ and the standard library. It does
not import the scientific stack, invoke GROMACS, connect to clusters, or submit
jobs. Use `python -m abfe.campaign` from this checkout, or `abflow-campaign` after
installing the updated package.

The supplied manifest selects Deflorian set 1 and Piersanti **v2**, Yuan's two
preparation groups, TA1 **Activate**, and CXCR4. It registers 62 ligands, each with
three vanilla records, three complex records and one shared solvent record
(434 records). Sigma 2 appears in the dataset overview with an unknown ligand
count and no invented runs. Earlier dataset versions are not merged into v2.

Keep the registry and reports **outside the simulation source tree**. For example:

```bash
REGISTRY="$HOME/.local/state/abflow/campaign.sqlite"
python -m abfe.campaign --registry "$REGISTRY" init \
  --manifest examples/membrane-campaign.json \
  --source-root /absolute/path/to/orexin
python -m abfe.campaign --registry "$REGISTRY" scan
python -m abfe.campaign --registry "$REGISTRY" status
python -m abfe.campaign --registry "$REGISTRY" status --dataset piersanti --issues-only
python -m abfe.campaign --registry "$REGISTRY" show piersanti/A2A_14/complex/1
python -m abfe.campaign --registry "$REGISTRY" report --output /tmp/abflow-campaign.html
```

Open the HTML file in a browser for a filterable table. Each **Inspect** control
shows provenance, dependencies, open incidents, file/log/plot links, and observation
history. File links are intended for opening the report locally; browsers can
block `file:` links if the report is served over HTTP. `status --json` exports the
same registry state for other displays. `show` includes complete historical
observations and per-window evidence. Reports are static snapshots; scan again
and generate a report at a new path to refresh. Existing registries and reports
are never silently overwritten.

### What the inventory establishes

- All expected alchemical windows are checked, using the manifest's explicit
  schedule. Common observed production time is shown only when every expected
  window has a readable endpoint with explicit ps units. These endpoints do not
  validate the whole trajectory or constitute an analysis cutoff.
- Vanilla progress is read from production logs. Input MDP targets, checkpoints,
  and result files are evidence, not proof of successful completion.
- Historical analysis is labelled **legacy_unverified**, **possibly_stale**, or
  **invalid**. An existing CSV never supplies a verified analysed duration or a
  scientific convergence decision. The reported absolute half-difference, when
  readable, is evidence only. Analysis/extension policy execution comes later.
- Embedding paths must be supplied explicitly; the inventory never infers
  independent embeddings from repeat names or copied CHARMM folders.
- Cluster/job and transfer states remain **unknown** until monitoring is added.
  There are no executable submission, repair, or extension actions in this milestone.
- Changed observations append history; identical refreshes do not duplicate it.
  Incidents remain open even if a later scan stops seeing the symptom. This
  milestone provides inspection, not incident acknowledgement/resolution.

### Manifest mappings

The JSON manifest has `schema_version: 1`, a `name`, an absolute `source_root`
(overridable with `--source-root`), and `datasets`. Each dataset supplies `id`,
`label`, `expected_ligands`, `ligands`, and explicit `complex_windows` and
`solvent_windows` counts. Empty, unselected datasets use an empty ligand list
and `expected_ligands: null`.

A dataset's optional `directory` contains ligand directories. Optional
`input_directory` and `ligand_prefix` map reference SDF names. Ligand entries
can be simple IDs or objects with `id`, `directory`, `inputs`, `vanilla_paths`,
`complex_paths`, `embeddings`, and `solvent_path`. Repeat path/embedding mappings
use keys `"1"`, `"2"`, `"3"`; paths are relative to `source_root` or absolute.
Without overrides, existing paths are inventoried as `vanilla_rep_N`,
`abfe_vanN_hrex_r1/complex`, and one `abfe_van1_hrex_r1/ligand`. A ligand with no
run directory records only its supplied inputs. Extra legacy ABFE folders are
flagged and preserved, not adopted as additional planned repeats.

`init` stores the expanded mapping and manifest hash. It deliberately has no
implicit configuration update: review mappings before creating a registry. A
missing primary solvent directory does not authorize reuse from an older
protocol or alternate calculation. The three complex records explicitly depend
on the one solvent record; chemical/protocol compatibility remains unverified.

Run the focused tests without MD/GPU dependencies:

```bash
python -m unittest discover -s abfe/tests -p test_campaign.py -v
```


### H.A.W.T.O.W.N

The local console uses Ayu Dark base colors with Electric Neon Burst accents, bundled JetBrains
Mono headings and Inter body text. See [CONTEXT.md](CONTEXT.md) for the design,
dataset/stage colors and display semantics. Reports embed fonts and licenses,
so viewing them makes no remote font requests.

To refresh SSH indicators and read your own Slurm queues:

```bash
python -m abfe.campaign --registry "$REGISTRY" check-connections
python -m abfe.campaign --registry "$REGISTRY" report --output /tmp/hawton-console.html
```

Probes are bounded and noninteractive. SSH health is separate from scheduler
observations. Job IDs and locations require fresh run-work-directory matches;
slot-based jobs or alternate layouts stay unassigned until mapped. Evidence
expires after five minutes. `absolut` means validated completion and verified
local retrieval, rather than merely local files.

The table separates simulation type, leg and repeat, with N/A convergence for
vanilla MD. Remarks use Attention required, Waiting, OK and Unknown; open
incidents take precedence. Inspect retains transfer evidence and incident
details. `status` uses the same stage/remarks/job/location presentation.

The H.A.W.T.O.W.N console has Runs, Clusters, Datasets and Compounds views.
`check-connections` collects SSH/own-queue and node-capacity snapshots; regenerate the
report afterwards. Capacity is allocated/available/unavailable scheduler resources,
not GPU utilisation or a start guarantee. Dataset/compound stage counts retain the
independent complex repeats and one shared solvent leg; legacy analyses stay unverified.

For periodic cluster history, schedule the deterministic sampler (one observation per
five-minute UTC bucket, bounded read-only SSH):

```bash
python -m abfe.campaign --registry /absolute/campaign.sqlite sample-clusters --publish-dir /absolute/console-directory
```

This appends real snapshots to SQLite and atomically updates `campaign.html` and
`preview.html`. ARCHER shows standard-QoS running/submission ceilings. GPU clusters
also retain node ownership and plot allocated GPUs by user. Collection errors publish
an error snapshot and return nonzero; missing intervals remain gaps. No job actions run.
