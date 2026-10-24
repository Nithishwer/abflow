import os
from contextlib import contextmanager
from pathlib import Path


@contextmanager
def working_directory(path: Path):
    """
    Temporarily change the working directory inside a `with` block.
    Restores the original working directory upon exit.
    """
    prev_cwd = Path.cwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(prev_cwd)
