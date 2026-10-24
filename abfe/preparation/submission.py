import logging
import sys
from importlib.abc import Traversable


def create_simulations_list():
    """Create a simulations_list.txt file with simulation directories."""
    leg_windows = {"coul": 11, "rest": 12, "vdw": 21}

    with open("simulations_list.txt", "w") as file:
        for leg, windows in leg_windows.items():
            for i in range(windows):
                file.write(f"{leg}.{i:02d}\n")


def gen_hrex_submission_script(
    template_path: Traversable,
    job_name,
    archer_nodes,
    simulation_list_string,
    new_script_name="job_complex_archer.sh",
    qos="standard",
):
    """Generate an Archer submission script from a packaged template."""
    try:
        # Calculate Archer layout
        tasks_per_node = 44 // archer_nodes
        cpus_per_task = 128 // tasks_per_node

        if 44 % archer_nodes != 0:
            logging.warning(
                f"Number of nodes ({archer_nodes}) does not divide 44 CPUs evenly."
            )
        if 128 % tasks_per_node != 0:
            logging.warning(
                f"Tasks per node ({tasks_per_node}) does not divide 128 CPUs per node evenly."
            )

        # Read the template content
        with template_path.open("r") as f:
            filedata = f.read()

        # Replace placeholders
        replacements = {
            "$JOBNAME": job_name,
            "$NO_OF_NODES": str(archer_nodes),
            "$TASKS_PER_NODE": str(tasks_per_node),
            "$CPUS_PER_TASK": str(cpus_per_task),
            "$WINDOW_LIST_STR": simulation_list_string.replace("/$STEP/", ""),
            "$MULTIDIR_LIST_STR_ENMIN": simulation_list_string.replace(
                "$STEP", "enmin"
            ),
            "$MULTIDIR_LIST_STR_NVT": simulation_list_string.replace("$STEP", "nvt"),
            "$MULTIDIR_LIST_STR_NPT_B": simulation_list_string.replace(
                "$STEP", "npt_b"
            ),
            "$MULTIDIR_LIST_STR_NPT_PR": simulation_list_string.replace(
                "$STEP", "npt_pr"
            ),
            "$MULTIDIR_LIST_STR_PROD": simulation_list_string.replace("$STEP", "prod"),
            "$QOS": qos,
        }

        for key, value in replacements.items():
            filedata = filedata.replace(key, value)

        # Write to output script file
        with open(new_script_name, "w") as f_out:
            f_out.write(filedata)

        logging.info(f"Submission script '{new_script_name}' generated successfully.")

    except Exception as e:
        logging.error(f"Error generating Archer submission script: {e}")
        sys.exit(1)
