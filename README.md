# ABFlow

Deterministic membrane-protein ABFE preparation, execution and analysis, with a
local status console and an agent operating explicitly approved batches.
Current scientific and cluster policies are linked from [CONTEXT.md](CONTEXT.md).

## Installation

Use the existing `abflow_v2` environment, or reproduce it with
`conda env create -f abfe_environment.yml`. From a checkout with its submodule:

```bash
conda activate abflow_v2
git submodule update --init --recursive
pip install --no-deps ./src/MDRestraintsGenerator
pip install --no-deps .
```

Preparation requires the reviewed GROMACS/parameter/restraint inputs. Cluster jobs
run plain Slurm/GROMACS scripts; analysis runs locally in `abflow_v2`.

## Campaign workflow

The default registry is the existing campaign database at
`/biggin/b211/reub0138/Documents/ChatGPT/Membrane-protein-FEP/.abflow/campaign.sqlite`.
Use `--registry /absolute/campaign.sqlite` to operate another campaign. A missing
registry is an error; operating commands never initialize a replacement.

```bash
abflow refresh --remote
abflow next-step --dry-run
abflow next-step
```

`next-step --dry-run` and `next-step` use the same selection code and the latest
recorded refresh. The dry run only prints action types/counts and the numbers of
ready, complete, waiting and blocked runs. It does not update SQLite, register
incidents, publish reports, create a plan or script on disk, or execute an action.
It does not run refresh; use `refresh --remote` to update campaign observations.
Recorded scheduler observations do not expire with age. The CLI and console use
the latest recorded evidence until refreshed; missing or failed checks still block.
Both modes retain read-only checks of the relevant source files and receipts;
selecting a download also inspects the remote outputs without transferring them.

Plain `next-step` registers the selected batch and executes one immediate operation
per ready run. It does not advance a run through multiple phases in one invocation.
Shared preparations and solvents appear once. Execution rechecks the saved evidence
and current jobs before each mutation, records intent/outcome and publishes the
console. A later invocation selects again: a preview does not reserve its inputs or
freeze future execution. Changed or unavailable evidence blocks affected work.

The next-step operation names are `prepare-input`, `plan-submission`,
`upload-jobs`, `submit-jobs`, `download-jobs`, `analyse-dhdl`,
and `calculate-affinity`. These names apply to previews and named saved-operation
commands. Analysis and affinity calculations run through `next-step`; preparation
also has a standalone `prepare` command.

`download-jobs` downloads and verifies outputs, then automatically registers the
downloaded directory as the run's current source. It completes only after both
steps succeed. Interrupted operations remain blocked for explicit review;
changed files or mapping failures also pause affected work.
Older verified downloads awaiting registration use the same operation without
repeating the transfer. Analysis uses the registered source and records its own
results; it does not change source paths afterward.

Production selection reads DHDL tails and small MDP files, checks the exact common
cutoff and records file metadata in memory. Actual analysis retains full input/header
validation and SHA256 provenance. Execution/transfer snapshots retain content checks.
Converged validated simulations are left alone; compatible binding compilation can
still be selected. An analysis proposing extension does not execute that extension
in the same round. Review the next preview before invoking `next-step` again.

Existing results can be adopted once using the user-approved timestamp rule:
both `ABFE_summary.csv` and `ABFE_convergence.csv` must be strictly newer than
every required production DHDL. Tables, required windows, exact cutoff and native
inputs are checked. Shared processing settings and script identity come from a
named validated reference analysis, with explicit user confirmation that the same
scripts/settings were used. The receipt records this acceptance basis, the run's
own input/output hashes and duration, and copies of its result CSVs. Original
historical records remain immutable. This accepts timestamp freshness; it does
not reconstruct the original execution environment or detect changes that preserve
timestamps before adoption. Missing, equal/older or invalid results are not adopted.

Historical import and verification remain internal Python operations; there is no
standalone analysis command. Adoption adds no next-step operation and never runs
an estimator. The resulting receipt uses the existing freshness, convergence,
decision and binding-compatibility checks. Original CSVs
and unrelated incidents are preserved; verification does not resolve incidents,
resume interrupted work, submit jobs or establish solvent compatibility. Future
changes to source or result files invalidate the receipt. Current valid receipts
are reused and repeated verification does not create duplicate records.

Failed actions do not prevent independent selected actions from running. The command
exits nonzero when actions fail or work remains blocked. Recorded successful
actions are not repeated. Interrupted attempts remain blocked for explicit review;
no automatic reconciliation is performed. Named saved-operation commands
remain available internally and in the console. Historical scopes, scripts and
receipts are preserved; code changes invalidate earlier frozen commands.
Incident repairs/resumption still require explicit incident authorization. No polling,
scheduling, scientific-setting changes or unbounded extensions are enabled.

Readiness uses reviewed execution/preparation records and resource ceilings
already stored for the run. Agents may record additional reviewed preparation,
execution-profile or binding-compatibility inputs in registry metadata
`next_step_inputs`; this is internal configuration, not a required user-authored
file. Missing profiles, incomplete preparation inputs, incompatible source layouts
or partial compound preparations remain blocked. Existing native execution and
analysis receipts keep their established formats.

Ask the agent to review the whole campaign. It inspects registered runs and known
local/cluster locations, reports coverage and blockers, and saves one concrete
proposal with a per-run appendix. Approve all or selected groups in chat; the agent
manages manifests, IDs and commands, executes that exact batch, and reports outcomes.
New incidents pause affected work before correction. Repair/resumption needs explicit
incident authority. Approval never enables unspecified work or recurring execution.

View campaign status in `.abflow/campaign.html`; reload the page after publication.

## Commands

The optional `--registry /absolute/campaign.sqlite` goes before the command verb.
The registry and reports must be outside simulation/input directories.

| Verb | Purpose |
|---|---|
| `init --manifest FILE [--source-root DIR]` | Register a new campaign from YAML |
| `refresh [--remote] [--publish-dir DIR]` | Scan local evidence and publish; optionally inspect own queues and active/uncertain jobs |
| `next-step --dry-run` | Show action types/counts using the same selection as execution; save and execute nothing |
| `next-step` | Select, register and execute one immediate action per ready run |
| `next-step OPERATION --plan-id ID --run-id RUN` | Execute one exact saved operation; internal/advanced interface |
| `prepare COMPOUND --manifest FILE --stage vanilla\|alchemical --output DIR [--plan]` | Protected local preparation |

Preparation and analysis write only their stated local outputs. Incident holds and
explicit repair/resumption requirements remain enforced internally.

`refresh` publishes `campaign.html` and `preview.html`
beside the registry unless `--publish-dir` is supplied. Remote refresh only inspects
jobs; it does not submit, retrieve, resume or repair them. Unavailable evidence stays
unknown and incomplete refresh exits nonzero. No periodic telemetry is provided.

## Formats and tests

Campaign manifests use YAML; see [the campaign example](examples/membrane-campaign.yaml).
For a new campaign, run `abflow --registry /absolute/new-campaign.sqlite init --manifest campaign.yaml`.
Use strings for ligand IDs and repeat-map keys (for example, `'19'` and `'1'`).
Existing JSON campaign manifests remain readable as a subset of YAML. Preparation,
GPU/ARCHER execution and batch-scope manifests retain their versioned JSON formats.
SQLite stores state/history; CSV stores scientific results; TSV
supports review appendices. Generated job scripts retain native checkpoints and
checksum receipts. Simulation, scheduler, transfer, analysis freshness and convergence
remain separate evidence.

```bash
ABFLOW_SCIENTIFIC_TEST=1 python -m unittest discover -s abfe/tests -t .
```

The scientific integration test uses synthetic data. Real-compound preparation and
GPU-to-ARCHER handoff acceptance remain outstanding. No environment replacement,
production rollout or automatic incident repair is implied by installation.

## Source layout

Each folder groups one area. Each module handles one operation or a small group of
closely related functions. Tests follow the same folders under `abfe/tests/`.

| Folder | Contents |
|---|---|
| `commands/` | Arguments, dispatch and status output |
| `registry/` | Campaign manifests, SQLite reads and shared record values |
| `monitoring/` | Scheduler queries, local inspection and evidence refresh |
| `console/` | Status presentation, datasets, run details and report publication |
| `analysis/` | Inputs, estimation, convergence, provenance, history and binding |
| `preparation/` | Plan validation, parameterization, topology, windows and preparation stages |
| `execution/` | Plans, journal, staging, submission, monitoring and retrieval |
| `execution/jobs/` | Stage, HREX and extension script generation |
| `execution/transport/` | SSH, file checks, queues, admission limits and remote dispatch |
| `operator/` | Approval checks, batch selection, steps, journals and incidents |
| `console_assets/`, `utils/data/`, `utils/cluster/` | Packaged styles/scripts and native scientific/job resources |

Folder `__init__.py` files expose existing Python entry points; implementations live
in the named modules. Scientific resources keep their existing package paths.
