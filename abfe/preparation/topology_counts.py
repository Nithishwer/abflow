from collections import Counter

import MDAnalysis as mda


def addtoppar2top(filename="topol.top"):
    with open(filename, "r") as f:
        lines = f.readlines()
    lines = [
        l.replace('#include "posre.itp"', '#include "toppar/posre.itp"') for l in lines
    ]
    with open(filename, "w") as f:
        f.writelines(lines)


def rebuild_molecule_counts_from_gro(gro_file: str) -> str:
    """
    Parse a .gro file and build a new [ molecules ] block by counting residues.
    Water names (SOL, TP3, WAT, HOH) are all treated as solvent.
    Residue segids are used to group membrane lipids.
    """
    u = mda.Universe(gro_file)
    mol_counts = Counter()

    for res in u.residues:
        name = res.resname.strip()
        if name in {"SOL", "TP3", "WAT", "HOH"}:
            mol_counts[name] += 1
        elif res.segid.strip():
            mol_counts[res.segid.strip()] += 1
        elif res.resname == "unk":
            mol_counts["UNK"] += 1
        elif res.resid == 1:
            mol_counts["Protein"] += 1

    output = "[ molecules ]\n; Compound        #mols\n"
    for mol, count in mol_counts.items():
        output += f"{mol:<15}{count}\n"
    return output


def update_topol_top_with_molecules_block(gro_file: str, top_file: str = "topol.top"):
    """
    Replace the [ molecules ] section of top_file with counts derived from gro_file.
    """
    mol_block = rebuild_molecule_counts_from_gro(gro_file)
    with open(top_file, "r") as f:
        lines = f.readlines()

    with open(top_file, "w") as f:
        inside_mol_block = False
        for line in lines:
            stripped = line.strip()
            # When we encounter the start of the old [ molecules ] block,
            # write the rebuilt block instead.
            if stripped.startswith("[ molecules ]"):
                inside_mol_block = True
                f.write(mol_block)
                continue
            # Skip lines until we leave the old [ molecules ] block.
            if inside_mol_block:
                if stripped.startswith("[") and not stripped.startswith(
                    "[ molecules ]"
                ):
                    inside_mol_block = False
                    f.write(line)
                # Otherwise continue skipping.
                continue
            f.write(line)
