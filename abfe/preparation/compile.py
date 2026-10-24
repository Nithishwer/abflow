import subprocess


def compile_input(root, mdp, topology, structure, index, suffix="vanilla"):
    output = root / f"preparation-{suffix}.tpr"
    subprocess.run(
        [
            "gmx",
            "grompp",
            "-f",
            str(root / mdp),
            "-c",
            str(root / structure),
            "-r",
            str(root / structure),
            "-p",
            str(root / topology),
            "-n",
            str(root / index),
            "-o",
            str(output),
        ],
        cwd=root,
        check=True,
    )
    if not output.is_file() or not output.stat().st_size:
        raise ValueError(f"GROMACS did not validate the prepared inputs: {root}")
