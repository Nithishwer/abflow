import abfe.execution.jobs.targets as execution_jobs_targets


def _hrex(plan):
    c = plan["config"]
    lines = [
        "directories=()",
        "for window in " + " ".join(plan["windows"]) + "; do",
        '    cd "$RUN/$window/prod"',
        "    phase=${window%.*}; number=${window##*.}",
    ]
    if c["mode"] == "initial":
        lines += [
            "    test ! -e topol.tpr",
            '    "$GMX" grompp -f "${phase}.prod.${number}.mdp" -c ../npt_pr/confout.gro -r ../npt_pr/confout.gro -t ../npt_pr/state.cpt -p "../../complex_${phase}.top" -n "../../index_${phase}.ndx" -o topol.tpr'
            + (
                " -maxwarn " + str(c["maxwarn"]["prod"])
                if c.get("maxwarn", {}).get("prod")
                else ""
            ),
            '    sha256sum topol.tpr "${phase}.prod.${number}.mdp" "../../complex_${phase}.top" "../../index_${phase}.ndx" > abflow-state.inputs.sha256',
        ]
    else:
        lines += [
            "    sha256sum -c abflow-state.inputs.sha256 abflow-state.outputs.sha256"
        ]
    lines += [
        '    directories+=("$PWD")',
        "done",
        'cd "$RUN"',
        "budget",
        'srun --cpu-bind=cores "$GMX" mdrun -multidir "${directories[@]}" -replex 200 -hrex -plumed ../../plumed.dat -dlb no -maxh "$MAXH"'
        + (" -cpi state.cpt -append" if c["mode"] != "initial" else ""),
        "result=complete",
        "first_time=",
        'for directory in "${directories[@]}"; do',
        '    cd "$directory"',
        "    checkpoint state.cpt",
        '    if [ -n "$first_time" ]; then',
        '        awk -v t="$TIME_PS" -v first="$first_time" \'BEGIN {d=t-first; exit !(d<0.00001 && d> -0.00001)}\'',
        "    else first_time=$TIME_PS; fi",
        '    printf "%s\\n" "$TIME_PS" > abflow-state.time',
        "    test -s dhdl.xvg; test -s md.log",
        "    sha256sum topol.tpr confout.gro state.cpt abflow-state.time dhdl.xvg md.log > abflow-state.outputs.sha256",
        f"    if awk -v t=\"$TIME_PS\" 'BEGIN {{exit !(t+0.00001 >= {execution_jobs_targets._target(plan, 'prod')})}}'; then touch abflow-state.done;",
        "    else result=checkpointed; fi",
        "done",
        'finish "$result"',
    ]
    return lines
