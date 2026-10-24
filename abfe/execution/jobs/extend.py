def _extension(plan):
    target = plan["decision"]["next_target_ns"] * 1000
    suffix = plan["id"]
    return [
        "# Prepare every window before installing any extended TPR.",
        "(",
        "flock -x 9",
        'if [ ! -f "$BUNDLE/extended" ]; then',
        'test ! -e "$BUNDLE/extension.started"',
        'touch "$BUNDLE/extension.started"',
        'cd "$RUN"',
        "for window in " + " ".join(plan["windows"]) + "; do",
        '    cd "$RUN/$window/prod"',
        "    phase=${window%.*}; number=${window##*.}",
        "    sha256sum -c abflow-state.inputs.sha256 abflow-state.outputs.sha256",
        f'    "$GMX" convert-tpr -s topol.tpr -until {target} -o extended.tpr',
        '    "$GMX" dump -s extended.tpr -om extended.mdp',
        f'    awk -F "[=;]" \'{{gsub(/[[:space:]]/,"",$1)}} $1=="dt" {{dt=$2}} $1=="nsteps" {{n=$2}} END {{exit !(dt*n=={target})}}\' extended.mdp',
        "done",
        "for window in " + " ".join(plan["windows"]) + "; do",
        '    cd "$RUN/$window/prod"',
        "    phase=${window%.*}; number=${window##*.}",
        '    mdp="${phase}.prod.${number}.mdp"',
        f"    cp -p topol.tpr topol.tpr.before-{suffix}",
        f'    cp -p "$mdp" "$mdp.before-{suffix}"',
        f"    cp -p abflow-state.inputs.sha256 abflow-state.inputs.sha256.before-{suffix}",
        f"    cp -p abflow-state.outputs.sha256 abflow-state.outputs.sha256.before-{suffix}",
        "    mv extended.tpr topol.tpr",
        f'    awk -F "[=;]" \'$1 ~ /^[[:space:]]*dt[[:space:]]*$/ {{dt=$2}} {{line[NR]=$0}} END {{for(i=1;i<=NR;i++) if(line[i] ~ /^[[:space:]]*nsteps[[:space:]]*=/) print "nsteps = " {target}/dt; else print line[i]}}\' "$mdp" > extended-input.mdp',
        '    mv extended-input.mdp "$mdp"',
        f"    if [ -f abflow-state.done ]; then mv abflow-state.done abflow-state.done.before-{suffix}; fi",
        '    topology="../../'
        + ("lig" if plan["spec"]["leg"] == "solvent" else "complex")
        + '_${phase}.top"',
        '    index="../../'
        + ("index.ndx" if plan["spec"]["leg"] == "solvent" else "index_${phase}.ndx")
        + '"',
        '    sha256sum topol.tpr "$mdp" "$topology" "$index" > abflow-state.inputs.sha256',
        "    sha256sum topol.tpr confout.gro state.cpt abflow-state.time > abflow-state.outputs.sha256",
        "done",
        'touch "$BUNDLE/extended"',
        "fi",
        ') 9>"$BUNDLE/extension.lock"',
    ]
