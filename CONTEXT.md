# H.A.W.T.O.W.N context

Display name: **H.A.W.T.O.W.N**. Header tagline:
**Hyperspecific ABFE Workflow That Only Works for Nithish**.
The registry's campaign name remains scientific inventory metadata.

## Design decisions

Use **Ayu Dark** base colors, **JetBrains Mono** headings/table headers, and **Inter** body
text. Fonts and SIL Open Font Licenses are bundled in `abfe/console_assets` and
embedded in reports: no remote font requests. Assets came from Google Fonts;
license sources are its [Inter](https://github.com/google/fonts/tree/main/ofl/inter)
and [JetBrains Mono](https://github.com/google/fonts/tree/main/ofl/jetbrainsmono) directories.

Use the user's **Electric Neon Burst** accents, with the latest preference to avoid
Sky Aqua and Electric Sapphire. Replace them with Aquamarine and a contrasting
Gold accent; use Aquamarine for interactive/header accents too:

| Accent | Hex | Dataset |
|---|---|---|
| Aquamarine | `#0fffdb` | Deflorian OX2 set 1 |
| Gold | `#ffd166` | Piersanti A2A |
| Violet Ray | `#9500ff` | Yuan DOR |
| Hot Fuchsia | `#ff0059` | Heffernan TA1 agonist |
| Dark Orange | `#ff8c00` | Thoma CXCR4 |
| Chartreuse | `#b4e600` | Sigma 2 |
| Aquamarine | `#0fffdb` | Connected / OK accents |

Ayu Dark background `#0b0e14`, panels `#0d1017`, raised surfaces `#141821`,
inputs `#10141c`, borders `#1b1f29`, hover `#161a24`, and primary text
`#bfbdb6`. Secondary text uses `#8a9199` for readable small labels. Base colors
follow the [official Ayu Dark theme](https://github.com/ayu-theme/vscode-ayu/blob/master/ayu-dark.json). Keep saturated purple as markers and
edges rather than body text. Dataset bullets and Dataset-cell left edges share
the same stable mapping. Stage cells have a separate colored edge and text label.

## Table and status semantics

- **Last updated** is the inventory timestamp; SSH checks have separate timestamps.
- **Complexes tracked** lists datasets as colored bullets, with registered complexes
  and planned repeat counts. Registration is not completion. Sigma 2 is unstarted.
- Separate **Simulation type**, **Leg**, **Repeat**: vanilla membrane records show
  `Vanilla / Complex / 1, 2, 3`; complex ABFE records show `Alchemical / Complex /
  1, 2, 3`; the one shared solvent record shows `Alchemical / Solvent / 1`.
  Keep persistent run IDs/dependencies intact; do not duplicate solvent calculations.
- Column-header buttons toggle ascending/descending sorting with visible arrows and keyboard access.
  Durations/repeats sort numerically, text/IDs naturally, and unknown/N/A values stay last
  in both directions. Filtering and Inspect details remain attached to their rows.
  Details is an action column and has no sort control.
- Vanilla convergence is **N/A**; legacy alchemical convergence stays unverified.
- **Job ID** and **Location** require fresh observed work-directory matches, not
  job-name guesses or intended routing. The workstation is **absolut**, shown only
  for validated completion with verified local retrieval. Unknown stays Unknown.
- **Remarks**: **Attention required** for open incidents; **Waiting** for preparation
  or queued work; **OK** for observed activity or validated local completion;
  **Unknown** when evidence is insufficient. Open incidents take precedence.
  Inspect retains incident details and transfer evidence.

## Standard stages

| Stage | Meaning | Accent |
|---|---|---|
| Not started | No preparation evidence | Gold |
| Inputs ready | Reference inputs present | Aquamarine |
| Prepared | Setup, MDPs or window directories observed | Violet |
| Queued | Fresh matched pending/configuring job | Gold |
| Equilibrating | Explicit equilibration-stage evidence | Orange |
| Production observed | Endpoint/log evidence; not a claim of live execution | Fuchsia |
| Analysis on file | Analysis artifacts observed, possibly unverified/stale/invalid | Aquamarine |
| Converged | Explicit validated analysis and convergence record | Chartreuse |
| Complete | Validated completion and verified local retrieval | Aquamarine |
| Unknown | Insufficient evidence or observation error | Neutral |

Converged/Complete are display contracts for future validated workflow records;
inventory file existence does not create those states.

## Connection checks

`python -m abfe.campaign --registry /absolute/registry.sqlite check-connections`
runs bounded, noninteractive SSH to `archer`, `ranvier`, `soma` and reads the
current user's `squeue` entries. It stores health/job snapshots in registry
metadata without changing source simulations or run observation history.
Reports remain static; regenerate after checking. Health/job evidence expires
after five minutes, including in an already-open browser report.

A green check means SSH succeeded, not validated software, available resources,
simulation completion, or convergence. Scheduler failure does not mean SSH failed.
Job association requires exact work-directory matches under documented roots,
including window subdirectories. Slot-based submissions/alternate layouts without
explicit mappings remain unassigned. Source scientific/workflow changes,
submissions/transfers and incident resolution remain outside this console milestone.

## Console views

Top-right buttons switch between **Runs**, **Clusters**, **Datasets** and **Compounds**.
Runs retains filtering, column sorting and Inspect. Datasets includes all six datasets
and counts stages separately for vanilla repeats, complex legs and shared solvent legs.
Compounds has 62 dataset–compound rows, the same stage breakdown, validated alchemical
convergence out of four legs and runs needing attention. View runs links filter the
Runs view to the selected dataset or compound. Sigma 2 has no invented compounds.
Legacy analyses and production endpoints never become convergence/completion through aggregation.

`check-connections` additionally reads `scontrol show nodes -d -o`; CPU/GPU allocations
are scheduler reservations, not utilisation. Capacity has its own timestamp and becomes
stale after five minutes. Unavailable includes drained, powered-down, reserved/planned
nodes and GPUs blocked by exhausted effective CPUs. Pool totals deduplicate node names;
Ranvier routine totals include g-ran, gpu4-biggin and gpuL40S-biggin, with short and Khalid
pools listed separately and excluded from those totals. Soma includes only gpu3-A40;
ARCHER includes standard CPU nodes, not hardware threads. Capacity is not a guaranteed
start or account/quota entitlement. Missing/invalid data shows unavailable, not zero usage.
These are bounded read-only queries; submission and conditional-pool approval rules are unchanged.

## Scheduled cluster history

The Clusters view now uses a one-third personal snapshot / two-thirds history layout.
The two introductory paragraphs were removed at the user's request; capacity semantics
remain documented here. ARCHER's primary view is the user's paid standard-QoS jobs:
16 running and 64 submitted (running plus pending), verified against the official
[ARCHER2 scheduler documentation](https://docs.archer2.ac.uk/user-guide/scheduler/#quality-of-service-qos)
and live `sacctmgr` on 16 October 2026. Lowpriority is separately 16/16; it is not added
to the paid limit. Ranvier/Soma job ceilings stay unknown rather than inventing limits.

`sample-clusters --publish-dir /absolute/local/report-directory` performs the same
bounded read-only probes and records at most one observation per cluster per five-minute
UTC bucket in the additive `cluster_samples` SQLite table. The record contains actual
probe timestamps, own expanded job-array entries/QoS, live ARCHER limits, GPU node counts
and sanitized job/user/node/GPU assignments from `scontrol show jobs -d -o` on GPU clusters.
No other users' work directories or commands are persisted. Overlapping pools stay
deduplicated; missing ownership stays unattributed, and failures/missed buckets remain
gaps. Plot bars represent observations, not interpolated GPU device load. The UI shows
the last 24 hours; the registry retains older observations. Initial history starts with
real samples only, without fabricated backfill.

The command uses a local lock, skips repeat SSH within a sampled bucket, and atomically
publishes campaign.html and preview.html after checking destinations. Failed collections
still publish their error state and return nonzero. The localhost preview reloads every
five minutes unless typing or inspecting a run; view, filters and sort are preserved.
Standalone exported files do not auto-refresh.

A thread heartbeat **record-h-a-w-t-o-w-n-cluster-usage** is active every five minutes,
running this deterministic command against the existing workspace registry. Routine
successful samples stay quiet. Collection requires the desktop/local host and SSH access
to remain available; missed times are not backfilled. No submission/cancellation,
scientific actions or automatic production repairs are authorized by this monitor.
