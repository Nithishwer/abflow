import shutil


def addgro2gro(source_file, destination_file):
    """
    Function to add two gro files:
    1. Copies all lines from source file except the first two and the last one.
    2. Inserts these lines into the destination file just before the last line.
    3. updates the second line of the destination file with the total number of lines after the insertion.
    4. Box dimensions are used from source
    """
    # Read the specific lines from the source file
    with open(source_file, "r") as source:
        source_contents = source.readlines()
        if len(source_contents) < 3:
            raise ValueError(f"Source file '{source_file}' must have at least 3 lines")

        source_contents = source_contents[2:]
        # print(source_contents)

    # Read the contents of the destination file
    with open(destination_file, "r") as destination:
        destination_contents = destination.readlines()
        if len(destination_contents) < 2:
            raise ValueError(
                f"Destination file '{destination_file}' must have at least 2 lines"
            )

    # Count the number of lines added
    num_lines_added = len(source_contents)

    # Insert the specific lines from the source file before the last line of the destination file
    modified_contents = destination_contents[:-1] + source_contents

    # Calculate the new value for the second line
    second_line = int(destination_contents[1].strip()) + num_lines_added - 1

    # Update the second line with the new value
    modified_contents[1] = str(second_line) + "\n"

    # Write the modified contents back to the destination file
    with open(destination_file, "w") as destination:
        destination.writelines(modified_contents)

    print(
        f"Specific lines from '{source_file}' copied to '{destination_file}' before the last line successfully."
    )
    print(f"Number of lines added: {num_lines_added}")
    print(f"Second line updated with new value: {second_line}")


def copy_complex2membrane():
    """
    Adds membrane.gro to protein.gro
    """
    protein_file = "protein.gro"
    membrane_file = "membrane.gro"
    destination_file = "complex_membrane.gro"
    # Line to copy the protein.gro file into a new file called complex_membrane.gro
    shutil.copy(protein_file, destination_file)

    # Use addgro2gro to add the contents of the complex file to the membrane file
    addgro2gro(membrane_file, destination_file)
    print(
        f"Content from '{membrane_file}' copied to '{destination_file}' successfully."
    )


def copy_lig_gro_2_protein(lig_file, protein_file):
    # Read the specific lines from the source file
    with open(lig_file, "r") as lig:
        lig_contents = lig.readlines()[2:-1]

    # Read the contents of the protein file
    with open(protein_file, "r") as protein:
        protein_contents = protein.readlines()

    # Count the number of lines added
    num_lines_added = len(lig_contents)

    # Insert the specific lines from the source file before the last line of the destination file
    modified_contents = protein_contents[:-1] + lig_contents + [protein_contents[-1]]

    # Calculate the new value for the second line
    second_line = int(protein_contents[1].strip()) + num_lines_added

    # Update the second line with the new value
    modified_contents[1] = str(second_line) + "\n"

    # Write the modified contents back to the destination file
    with open(protein_file, "w") as protein:
        protein.writelines(modified_contents)

    print(
        f"Specific lines from '{lig_file}' copied to '{protein_file}' before the last line successfully."
    )
    print(f"Number of lines added: {num_lines_added}")
    print(f"Second line updated with new value: {second_line}")
