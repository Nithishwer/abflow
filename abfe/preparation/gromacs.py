import logging
import subprocess


def run_gmx_command(command):  ## REFACTORED
    """Run a GROMACS (gmx) command."""
    try:
        logging.info(f"Executing: {command}")
        subprocess.run(command, shell=True, check=True)
        logging.info("Success.")
    except subprocess.CalledProcessError as e:
        logging.error(f"GROMACS command failed: {e}")
        raise RuntimeError(f"GROMACS command failed: {e}")
