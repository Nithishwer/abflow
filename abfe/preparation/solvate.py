import subprocess
from importlib.resources import files


def solvate_system(water_count=0, solvent_box="spc216.gro"):
    """
    Solvates the system and deletes water molecules in the membrane
    """
    # Execute gmx solvate command
    try:
        subprocess.run(
            [
                "gmx",
                "solvate",
                "-cp",
                "complex_membrane.gro",
                "-cs",
                str(solvent_box),
                "-p",
                "topol.top",
                "-o",
                "solv.gro",
            ],
            check=True,
        )
    except subprocess.CalledProcessError:
        raise Exception("Error: gmx solvate command failed.")

    # Execute water_deletor.pl command
    perl_script_path = files("abfe.utils") / "water_deletor.pl"
    perl_script_path = str(perl_script_path)

    try:
        subprocess.run(
            [
                "perl",
                perl_script_path,
                "-in",
                "solv.gro",
                "-out",
                "solv_fix.gro",
                "-ref",
                "O33",
                "-middle",
                "C116",
                "-nwater",
                "3",
            ],
            check=True,
        )
    except subprocess.CalledProcessError:
        raise Exception("Error: water_deletor.pl command failed.")

    update_topology_file(water_count)


def solvate_soluble_system():
    """
    Solvates the soluble system
    """
    # Execute gmx solvate command
    try:
        # Run gmx editconf -f complex.gro -o newbox.gro -bt dodecahedron -d 1.0
        subprocess.run(
            [
                "gmx",
                "editconf",
                "-f",
                "protein.gro",
                "-o",
                "newbox.gro",
                "-bt",
                "dodecahedron",
                "-d",
                "1.0",
            ],
            check=True,
        )
        subprocess.run(
            [
                "gmx",
                "solvate",
                "-cp",
                "newbox.gro",
                "-cs",
                "spc216.gro",
                "-p",
                "topol.top",
                "-o",
                "solv.gro",
            ],
            check=True,
        )
    except subprocess.CalledProcessError:
        raise Exception("Error: gmx solvate command failed.")


def update_topology_file(new_water_count: int = 0):
    """
    Set the water molecule count in topol.top to new_water_count.
    Recognises SOL, TP3, WAT and HOH as solvent names.
    """
    if new_water_count == 0:
        new_water_count = input("Enter the new water count: ")

    with open("topol.top", "r") as file:
        lines = file.readlines()

    solvent_keys = ("SOL", "TP3", "WAT", "HOH")
    for i, line in enumerate(lines):
        tokens = line.split()
        if tokens and tokens[0] in solvent_keys:
            lines[i] = f"{tokens[0]:<10}{new_water_count}\n"
            break

    with open("topol.top", "w") as file:
        file.writelines(lines)


def addions(input_file="solv_fix.gro", water_group="18", salt_molar=None):
    """
    Executes gromacs commands to add ions to the system
    """
    # Execute gmx grompp command
    try:
        subprocess.run(
            [
                "gmx",
                "grompp",
                "-f",
                "ions.mdp",
                "-c",
                input_file,
                "-r",
                input_file,
                "-p",
                "topol.top",
                "-o",
                "ions.tpr",
            ],
            check=True,
        )
    except subprocess.CalledProcessError:
        raise Exception("Error: gmx grompp command failed.")

    # Execute gmx genion command
    try:
        process_input = water_group.encode()
        command = [
            "gmx",
            "genion",
            "-s",
            "ions.tpr",
            "-o",
            "system_solv_ions.gro",
            "-p",
            "topol.top",
            "-pname",
            "NA",
            "-nname",
            "CL",
            "-neutral",
        ]
        if salt_molar is not None:
            command += ["-conc", str(salt_molar)]
        subprocess.run(command, input=process_input, check=True)
    except subprocess.CalledProcessError:
        raise Exception("Error: gmx genion command failed.")
