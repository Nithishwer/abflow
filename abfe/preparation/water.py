from pathlib import Path

import MDAnalysis as mda
from MDAnalysis.coordinates.GRO import GROWriter


def merge_gro_files(file1, file2, output_suffix="_crystal_water.gro"):
    u1 = mda.Universe(file1)
    u2 = mda.Universe(file2)
    combined = mda.Merge(u1.atoms, u2.atoms)
    combined.dimensions = u1.dimensions
    output_file = Path(file1).with_name(Path(file1).stem + output_suffix)
    with GROWriter(str(output_file), n_atoms=combined.atoms.n_atoms) as W:
        W.write(combined.atoms)
    print(f"Combined GRO file saved as {output_file}")


def add_water2topology(n: int = 0):
    """
    Increment the water molecule count in topol.top by n.
    Recognises common solvent residue names (SOL, TP3, WAT, HOH).
    """
    with open("topol.top", "r") as f:
        lines = f.readlines()

    solvent_keys = ("SOL", "TP3", "WAT", "HOH")
    for i, line in enumerate(lines):
        tokens = line.split()
        if tokens and tokens[0] in solvent_keys:
            current_water_count = int(tokens[1])
            new_count = current_water_count + n
            # Preserve the solvent name and column width
            lines[i] = f"{tokens[0]:<10}{new_count}\n"
            break

    with open("topol.top", "w") as f:
        f.writelines(lines)
