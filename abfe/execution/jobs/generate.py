import re
import shlex
from pathlib import Path

import abfe.execution.files as execution_files
import abfe.execution.jobs.extend as execution_jobs_extend
import abfe.execution.jobs.hrex as execution_jobs_hrex
import abfe.execution.jobs.stages as execution_jobs_stages
import abfe.execution.jobs.targets as execution_jobs_targets


def script(plan):
    c, remote = plan["config"], plan["remote"]
    name = re.sub(r"[^A-Za-z0-9_-]", "-", plan["run_id"])[:128]
    lines = [
        "#!/bin/bash -l" if c.get("modules") else "#!/bin/bash",
        "#SBATCH --job-name=" + name,
        "#SBATCH --partition=" + c["partition"],
        "#SBATCH --time=" + str(round(c["walltime_hours"] * 60)),
        "#SBATCH --no-requeue",
        "#SBATCH --output=" + remote + "/slurm-%A_%a.out",
        "#SBATCH --nodes=" + ("22" if c["cluster"] == "archer" else "1"),
    ]
    if c.get("account"):
        lines += ["#SBATCH --account=" + c["account"]]
    if c["cluster"] == "archer":
        lines += [
            "#SBATCH --ntasks-per-node=2",
            "#SBATCH --cpus-per-task=64",
            "#SBATCH --qos=" + c["qos"],
        ]
    else:
        lines += [
            "#SBATCH --ntasks-per-socket=1",
            "#SBATCH --cpus-per-task=8",
            "#SBATCH --gres=gpu:" + c["gpu"] + ":1",
            "#SBATCH --gres-flags=enforce-binding",
        ]
        if c["workload"] in ("complex-equilibration", "solvent"):
            count = c.get("array_concurrency")
            if type(count) is not int or not 1 <= count <= len(plan["windows"]):
                raise ValueError(
                    "Provide an explicit array_concurrency within the window count"
                )
            lines += [f"#SBATCH --array=0-{len(plan['windows']) - 1}%{count}"]
    lines += [
        "set -euo pipefail",
        "umask 077",
        "BUNDLE=" + shlex.quote(remote),
        'STATUS="$BUNDLE/status/${SLURM_ARRAY_TASK_ID:-0}.state"',
        'mkdir -p "$BUNDLE/status"',
        'test ! -e "$STATUS"',
        'trap \'rc=$?; if ((rc)); then printf "failed %s\\n" "$rc" > "$STATUS"; fi\' EXIT',
        'finish() { printf "%s\\n" "$1" > "$STATUS"; set +e; exit 0; }',
        'printf "running\\n" > "$STATUS"',
        'export OMP_NUM_THREADS="$SLURM_CPUS_PER_TASK"',
        'export SRUN_CPUS_PER_TASK="$SLURM_CPUS_PER_TASK"',
        "unset GMX_MAXBACKUP",
    ]
    lines += ["module load " + shlex.quote(module) for module in c.get("modules", [])]
    for variable, key in [("PATH", "path_dirs"), ("LD_LIBRARY_PATH", "library_dirs")]:
        if c.get(key):
            lines += [
                "export "
                + variable
                + "="
                + shlex.quote(":".join(c[key]))
                + ':"${'
                + variable
                + ':-}"'
            ]
    run = remote + "/data/" + c["relative_run"]
    lines += [
        "RUN=" + shlex.quote(run),
        "GMX=" + shlex.quote(c["gromacs"]),
        'cd "$RUN"',
        'VERSION="$BUNDLE/gromacs-version-${SLURM_ARRAY_TASK_ID:-0}.txt"',
        '"$GMX" --version > "$VERSION"',
        'test "$(awk \'/GROMACS version:/ {print $3}\' "$VERSION")" = '
        + shlex.quote(c["gromacs_version"]),
    ]
    binary_receipt = "abflow-gromacs-" + c["cluster"] + ".sha256"
    lines += [
        "(",
        "flock -x 9",
        f"if [ -f {binary_receipt} ]; then sha256sum -c {binary_receipt};",
        f'else sha256sum "$(readlink -f "$GMX")" > {binary_receipt}; fi',
        ") 9>abflow-software.lock",
        "budget() {",
        f"    local left=$(({round(c['walltime_hours'] * 3600)} - SECONDS - 900))",
        "    ((left > 0)) || finish checkpointed",
        "    MAXH=$(awk -v s=\"$left\" 'BEGIN {print s/3600}')",
        "}",
        "checkpoint() {",
        '    "$GMX" dump -cp "$1" > "$BUNDLE/checkpoint-${SLURM_ARRAY_TASK_ID:-0}.txt"',
        '    TIME_PS=$(awk \'$1=="t" && $2=="=" {print $3; n++} END {if(n!=1) exit 1}\' "$BUNDLE/checkpoint-${SLURM_ARRAY_TASK_ID:-0}.txt")',
        "    awk -v t=\"$TIME_PS\" 'BEGIN {exit !(t ~ /^[0-9.eE+-]+$/ && t+0>=0)}'",
        "}",
        "",
    ]
    if c["mode"] == "extend":
        lines += execution_jobs_extend._extension(plan)
    if c["workload"] == "vanilla":
        previous = "system_solv_ions.gro"
        for stage, out in [
            ("minim", "em"),
            ("nvt", "nvt"),
            ("npt", "npt"),
            ("prod", "prod"),
        ]:
            target = (
                execution_files.duration(
                    Path(c["source"]) / c["relative_run"] / (stage + ".mdp")
                )
                if stage != "minim"
                else 0
            )
            # Match the supplied vanilla script's checkpoint velocity handoff.
            checkpoint = {"npt": "nvt.cpt", "prod": "npt.cpt"}.get(stage)
            lines += (
                ["# " + stage.upper()]
                + execution_jobs_stages._stage(
                    c,
                    stage,
                    "$RUN",
                    stage + ".mdp",
                    "topol.top",
                    previous,
                    "index.ndx",
                    target,
                    out,
                    checkpoint,
                )
                + [""]
            )
            previous = out + ".gro"
    elif c["workload"] == "complex-production":
        lines += execution_jobs_hrex._hrex(plan)
    else:
        lines += [
            "windows=(" + " ".join(plan["windows"]) + ")",
            "window=${windows[$SLURM_ARRAY_TASK_ID]}",
            "phase=${window%.*}",
            "number=${window##*.}",
        ]
        solvent = c["workload"] == "solvent"
        prefix = "lig" if solvent else "complex"
        coordinates = "$RUN/" + prefix + "_${phase}.gro"
        for stage in ["enmin", "nvt", "npt_b", "npt_pr"] + (
            ["prod"] if solvent else []
        ):
            target = execution_jobs_targets._target(plan, stage)
            lines += (
                ["# " + stage.upper()]
                + execution_jobs_stages._stage(
                    c,
                    stage,
                    "$RUN/$window/" + stage,
                    "${phase}." + stage + ".${number}.mdp",
                    "$RUN/" + prefix + "_${phase}.top",
                    coordinates,
                    "$RUN/" + ("index.ndx" if solvent else "index_${phase}.ndx"),
                    target,
                )
                + [""]
            )
            coordinates = "$RUN/$window/" + stage + "/confout.gro"
            if solvent:
                lines += [
                    "if [ ! -f abflow-centered.gro ]; then",
                    '    printf "0\\n" | "$GMX" trjconv -f confout.gro -s topol.tpr -pbc nojump -o abflow-nojump.gro',
                    '    printf "1\\n0\\n" | "$GMX" trjconv -f abflow-nojump.gro -s topol.tpr -center -pbc mol -o abflow-centered.gro',
                    "fi",
                ]
                coordinates = "$RUN/$window/" + stage + "/abflow-centered.gro"
    return "\n".join(lines + ["finish complete", ""])
