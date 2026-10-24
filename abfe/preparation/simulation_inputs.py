import os
import shutil
from importlib.abc import Traversable


def cp_mdps(source_dir: Traversable):
    """
    Copies membrane protein .mdp files to the current directory.
    Accepts a Traversable object, e.g. from importlib.resources.files.
    """
    for f in source_dir.iterdir():
        if f.name.endswith(".mdp"):
            with f.open("rb") as src_file:
                with open(f.name, "wb") as dest_file:
                    shutil.copyfileobj(src_file, dest_file)


def create_index(gro_file):
    """
    Creates index file using gmx make_ndx command
    """
    # Execute the command
    os.system(f"echo '1|13|23|24|25\nq' | gmx make_ndx -f {gro_file} -o index.ndx")


def create_soluble_index(gro_file):
    """
    Creates index file using gmx make_ndx command
    """
    # Execute the command
    os.system(f"echo '1|13\nq' | gmx make_ndx -f {gro_file} -o index.ndx")
