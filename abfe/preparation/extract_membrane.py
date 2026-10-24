#!/usr/bin/env python3

"""
Filters lines in a text file based on specified substrings and counts the number of deleted lines.

Parameters:
- input_file (str): The path to the input text file.
- output_file (str): The path to the output text file.

Also prints old and updated atom counts
"""


# Set of su
def filter_and_count_lines(input_file, output_file):
    substrings_to_keep = {"PA", "PC", "OL"}
    lines_deleted = 0

    with open(input_file, "r") as infile, open(output_file, "w") as outfile:
        total_lines = len(infile.readlines())
        infile.seek(0)  # Reset the file cursor to the beginning
        print("Total_lines:", total_lines)
        for line_num, line in enumerate(infile, start=1):
            if line_num <= 2 or line_num > (total_lines - 1):
                # Keep the first two and last two lines
                # print('Writing this to membrane.gro')
                # print(line_num)
                # print(line)
                if line_num == 2:
                    atom_count = int(line)
                    print("Existing Atom count:", atom_count)
                outfile.write(line)
            elif any(substring in line for substring in substrings_to_keep):
                # Write out the POPC lines
                outfile.write(line)
            else:
                lines_deleted += 1

    print(f"Number of atoms deleted: {lines_deleted}")
    print(
        "The updated atom count:",
        atom_count,
        " - ",
        lines_deleted,
        " = ",
        atom_count - lines_deleted,
    )
    return atom_count - lines_deleted


def replace_second_line(file_path, new_value):
    # Read the contents of the file
    with open(file_path, "r") as file:
        lines = file.readlines()

    # Replace the second line with the new value
    lines[1] = str(new_value) + "\n"

    # Write the modified contents back to the file
    with open(file_path, "w") as file:
        file.writelines(lines)

    print(f"Second line of '{file_path}' replaced successfully with '{new_value}'.")


def extract_membrane(input_file, output_file):
    new_atom_count = filter_and_count_lines(input_file, output_file)
    replace_second_line(output_file, new_atom_count)


if __name__ == "__main__":
    input_filename = "step5_input.gro"
    output_filename = "membrane.gro"
    extract_membrane(input_filename, output_filename)
