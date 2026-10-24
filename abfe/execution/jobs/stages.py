def _stage(
    config,
    stage,
    directory,
    mdp,
    topology,
    coordinates,
    index,
    target,
    prefix=None,
    previous_checkpoint=None,
):
    """One visible grompp/mdrun block, with native checkpoint continuation."""
    tpr, cpt, gro = (
        (prefix + ".tpr", prefix + ".cpt", prefix + ".gro")
        if prefix
        else ("topol.tpr", "state.cpt", "confout.gro")
    )
    label = prefix or "state"
    marker = "abflow-" + label
    q = lambda s: (
        '"' + s + '"'
    )  # Values are internal shell paths, including the selected array window.
    compile_args = [
        '"$GMX" grompp',
        "-f " + q(mdp),
        "-c " + q(coordinates),
        "-r " + q(coordinates),
        "-p " + q(topology),
        "-n " + q(index),
        "-o " + tpr,
    ]
    if previous_checkpoint:
        compile_args += ["-t " + q(previous_checkpoint)]
    warnings = config.get("maxwarn", {}).get(stage, 0)
    if warnings:
        compile_args += ["-maxwarn " + str(warnings)]
    inputs = " ".join(map(q, (tpr, mdp, topology, index)))
    minim = stage in ("minim", "enmin")
    commands = [
        f'cd "{directory}"',
        f"if [ -f {marker}.done ]; then",
        f"    sha256sum -c {marker}.inputs.sha256 {marker}.outputs.sha256",
        "else",
        "    budget",
        f"    if [ -f {marker}.inputs.sha256 ]; then",
        f"        sha256sum -c {marker}.inputs.sha256",
        "    else",
        f"        test ! -e {tpr}",
        "        " + " ".join(compile_args),
        f"        sha256sum {inputs} > {marker}.inputs.sha256",
        "    fi",
        "    resume=()",
        f"    if [ -s {cpt} ]; then resume=(-cpi {cpt} -append); fi",
        '    "$GMX" mdrun -s '
        + tpr
        + (" -deffnm " + prefix if prefix else "")
        + ' -ntmpi 1 -maxh "$MAXH" "${resume[@]}"',
    ]
    if not minim:
        commands += [
            f"    checkpoint {cpt}",
            f'    printf "%s\\n" "$TIME_PS" > {marker}.time',
        ]
    outputs = (
        tpr + " " + gro + (" " + cpt + " " + marker + ".time" if not minim else "")
    )
    commands += [f"    sha256sum {outputs} > {marker}.outputs.sha256"]
    if not minim:
        commands += [
            f'    if ! awk -v t="$TIME_PS" -v goal="{target}" \'BEGIN {{exit !(t+0.00001 >= goal)}}\'; then',
            "        finish checkpointed",
            "    fi",
        ]
    commands += [f"    touch {marker}.done", "fi"]
    return commands
