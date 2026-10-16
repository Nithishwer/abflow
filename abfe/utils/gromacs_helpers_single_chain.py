#!/usr/bin/env python3
import os
import shutil
import sys
import subprocess
import glob
import importlib.resources
from importlib.resources import files
from importlib.abc import Traversable
from pathlib import Path


from abfe.utils.smirnoff_param_func import smirnoff_parameterize
from abfe.utils.bespokefit_param_func import bespokefit_parameterize
from abfe.utils.extract_membrane import extract_membrane

def addgro2gro(source_file, destination_file):
    """
    Function to add two gro files:
    1. Copies all lines from source file except the first two and the last one.
    2. Inserts these lines into the destination file just before the last line.
    3. updates the second line of the destination file with the total number of lines after the insertion.
    4. Box dimensions are used from source
    """
    try:
        # Read the specific lines from the source file
        with open(source_file, 'r') as source:
            source_contents = source.readlines()
            if len(source_contents) < 3:
                raise ValueError(f"Source file '{source_file}' must have at least 3 lines")

            source_contents = source_contents[2:]
            #print(source_contents)

        # Read the contents of the destination file
        with open(destination_file, 'r') as destination:
            destination_contents = destination.readlines()
            if len(destination_contents) < 2:
                raise ValueError(f"Destination file '{destination_file}' must have at least 2 lines")

        # Count the number of lines added
        num_lines_added = len(source_contents)

        # Insert the specific lines from the source file before the last line of the destination file
        modified_contents = destination_contents[:-1] + source_contents 

        # Calculate the new value for the second line
        second_line = int(destination_contents[1].strip()) + num_lines_added -1

        # Update the second line with the new value
        modified_contents[1] = str(second_line) + '\n'

        # Write the modified contents back to the destination file
        with open(destination_file, 'w') as destination:
            destination.writelines(modified_contents)

        print(f"Specific lines from '{source_file}' copied to '{destination_file}' before the last line successfully.")
        print(f"Number of lines added: {num_lines_added}")
        print(f"Second line updated with new value: {second_line}")

    except FileNotFoundError as e:
        print(f"Error: File '{e.filename}' not found.")
    except Exception as e:
        print(f"Error: {e}")

def get_popc_number(file_path):
    try:
        # Open the file and read its contents
        with open(file_path, 'r') as file:
            for line in file:
                # Check if the line starts with "POPC"
                if line.startswith("POPC"):
                    # Split the line by spaces and extract the number
                    popc_number = int(line.split()[1])
                    return popc_number

        # If no line starting with "POPC" is found, return None
        return None

    except FileNotFoundError:
        print(f"Error: File '{file_path}' not found.")
        return None
    except Exception as e:
        print(f"Error: {e}")
        return None

def add_POPC_to_topol(filename,popc_count):
    try:
        # Content to be added to the file
        content_to_add = 'POPC         '+str(popc_count)+'\n'

        # Open the file in append mode and write the content
        with open(filename, 'a') as file:
            file.write(content_to_add)

        print(f"Content added to '{filename}' successfully.")

    except FileNotFoundError:
        print(f"Error: File '{filename}' not found.")
    except Exception as e:
        print(f"Error: {e}")

    
def copy_complex2membrane():
    """
    Adds membrane.gro to protein.gro
    """
    protein_file = 'protein.gro'
    membrane_file = 'membrane.gro'
    destination_file = 'complex_membrane.gro'
    # Line to copy the protein.gro file into a new file called complex_membrane.gro
    shutil.copy(protein_file,destination_file)
    

    try:
        # Use addgro2gro to add the contents of the complex file to the membrane file
        addgro2gro(membrane_file,destination_file)
        print(f"Content from '{membrane_file}' copied to '{destination_file}' successfully.")

    except FileNotFoundError:
        print(f"Error: One of the files '{membrane_file}' or '{destination_file}' not found.")
    except Exception as e:
        print(f"Error: {e}")


def copy_lig_gro_2_protein(lig_file, protein_file):
    try:
        # Read the specific lines from the source file
        with open(lig_file, 'r') as lig:
            lig_contents = lig.readlines()[2:-1]

        # Read the contents of the protein file
        with open(protein_file, 'r') as protein:
            protein_contents = protein.readlines()

        # Count the number of lines added
        num_lines_added = len(lig_contents)

        # Insert the specific lines from the source file before the last line of the destination file
        modified_contents = protein_contents[:-1] + lig_contents + [protein_contents[-1]]

        # Calculate the new value for the second line
        second_line = int(protein_contents[1].strip()) + num_lines_added

        # Update the second line with the new value
        modified_contents[1] = str(second_line) + '\n'

        # Write the modified contents back to the destination file
        with open(protein_file, 'w') as protein:
            protein.writelines(modified_contents)

        print(f"Specific lines from '{lig_file}' copied to '{protein_file}' before the last line successfully.")
        print(f"Number of lines added: {num_lines_added}")
        print(f"Second line updated with new value: {second_line}")

    except FileNotFoundError:
        print(f"Error: One of the files '{lig_file}' or '{protein_file}' not found.")
    except Exception as e:
        print(f"Error: {e}")

def add_ligposres_to_topol(ligand_file):
    """
    Adds the ligand position restraint file to the topol.top file
    """
    try:
        ligand_name = os.path.splitext(ligand_file)[0]
        # Content to be added to the file
        content_to_add = '''
#ifdef POSRES
#include "'''+ligand_name+'''_posre.itp"
#endif
'''

        # Open the file in append mode and write the content
        file_path = 'toppar/'+ligand_name+'.itp'
        with open(file_path, 'r+') as file:
            file_content = file.read()

            # Check if the content to add is already present
            if content_to_add.strip() in file_content:
                print("Ligand posres already present, not doing anything.")
                return

            # If not present, append the content
            file.write(content_to_add)

        print(f"Content added to '{file_path}' successfully.")

    except FileNotFoundError:
        print(f"Error: File '{file_path}' not found.")
    except Exception as e:
        print(f"Error: {e}")

def add_toppar2protein_include(file_path):
    """
    Adds the 'toppar' to protein #include in the 'topol.top' file
    """
    try:
        # Open the file for reading
        with open(file_path, 'r') as file:
            file_content = file.read()

        # Check if 'toppar' is already present
        if '#include "toppar/topol_Protein_chain_' in file_content:
            print("Nothing added, 'toppar' already present.")
            return

        # Replace the string
        modified_content = file_content.replace('#include "topol_Protein_chain_', '#include "toppar/topol_Protein_chain_')

        # Write the modified content back to the file
        with open(file_path, 'w') as file:
            file.write(modified_content)

        print(f"File '{file_path}' modified successfully.")

    except FileNotFoundError:
        print(f"Error: File '{file_path}' not found.")
    except Exception as e:
        print(f"Error: {e}")

def add_line_after_pattern(file_path, pattern, line_to_add):
    try:
        # Read the file
        with open(file_path, 'r') as file:
            lines = file.readlines()

        # Find the index of the line containing the pattern
        pattern_index = -1
        for i, line in enumerate(lines):
            if pattern in line:
                pattern_index = i
                break

        if pattern_index != -1:
            # Check if the line to add is already present after the pattern
            if line_to_add + '\n' in lines[pattern_index + 1:]:
                print(f"'{line_to_add}' already present after pattern, skipping.")
            else:
                # Insert the line after the line containing the pattern
                lines.insert(pattern_index + 1, line_to_add + '\n')

                # Write the modified content back to the file
                with open(file_path, 'w') as file:
                    file.writelines(lines)

                print(f" '{line_to_add}' Line added to '{file_path}' successfully.")
        else:
            print(f"Pattern '{pattern}' not found in '{file_path}'.")

    except FileNotFoundError:
        print(f"Error: File '{file_path}' not found.")
    except Exception as e:
        print(f"Error: {e}")

def add_unk_to_topol(filename):
    try:
        # Content to be added to the file
        content_to_add = 'unk         1\n'

        # Open the file in append mode and write the content
        with open(filename, 'a') as file:
            file.write(content_to_add)

        print(f"Moleculetype list of '{filename}' updates successfully.")

    except FileNotFoundError:
        print(f"Error: File '{filename}' not found.")
    except Exception as e:
        print(f"Error: {e}")


def param_lig(file_name, xml_file=None, sage_offxml="openff-2.0.0.offxml"):
    """
    Creates a directory structure and smirnoff parameterizes the ligand
    """

    # Set the mode variable based on whether xml_file is provided
    mode = 'bespokefit' if xml_file else 'smirnoff'

    # Create a folder with the name of the mode
    os.makedirs(mode, exist_ok=True)  
    os.chdir(mode)

    # Copying the ligand files
    parent_dir = os.path.abspath(os.path.join(os.getcwd(), '..'))
    source_file_path = os.path.join(parent_dir, file_name)
    destination_file_path = os.path.join(os.getcwd(), file_name)
    try:
        # Copy the file from the parent directory to the mode directory
        shutil.copy(source_file_path, destination_file_path)
        print(f"File '{file_name}' copied successfully to '{mode}' folder.")
    except FileNotFoundError:
        raise FileNotFoundError(f"Error: File '{file_name}' not found in the parent directory.")

    if mode == 'smirnoff':
        smirnoff_parameterize(file_name, forcefield=sage_offxml)
    else:
        bespokefit_parameterize(file_name, xml_file)
    
    os.chdir('../')    

def param_protein(pdb_file):
    # Copy the forcefields folder into the current directory
    script_dir = os.path.dirname(os.path.abspath(__file__))
    forcefields_src = os.path.join(script_dir, "data", "forcefield_dir", "amber99sb-star-ildn-mut.ff")
    forcefields_dest = os.path.join(os.getcwd(), "amber99sb-star-ildn-mut.ff")
    
    if not os.path.exists(forcefields_dest):
        shutil.copytree(forcefields_src, forcefields_dest)
        print("Forcefields folder copied successfully.")
    # Run the grep command to filter out non-protein atoms and save it as protein.pdb
    pdb_protein = "protein.pdb"
    subprocess.run(["grep", "-v", "^HETATM", pdb_file], stdout=open(pdb_protein, "w"))

    # Run the pdb2gmx command and save output as protein.gro
    pdb2gmx_command = [
        "gmx", "pdb2gmx",
        "-f", pdb_protein,
        "-o", os.path.splitext(pdb_protein)[0] + ".gro",
        "-water", "tip3",
        "-ignh"
    ]
    #subprocess.run(pdb2gmx_command)
    # Run the pdb2gmx command and automatically input '1' when prompted
    process = subprocess.Popen(pdb2gmx_command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

    # Send '1\n' to the stdin (to select the first force field when prompted)
    stdout, stderr = process.communicate(input="1\n")

    # Print output and error messages if needed
    print(stdout)
    print(stderr)

def param_protein_w_protonation(pdb_file):
    # Copy the forcefields folder into the current directory
    script_dir = os.path.dirname(os.path.abspath(__file__))
    forcefields_src = os.path.join(script_dir, "data", "forcefield_dir", "amber99sb-star-ildn-mut.ff")
    forcefields_dest = os.path.join(os.getcwd(), "amber99sb-star-ildn-mut.ff")
    
    if not os.path.exists(forcefields_dest):
        shutil.copytree(forcefields_src, forcefields_dest)
        print("Forcefields folder copied successfully.")
        
    # Run the grep command to filter out non-protein atoms and save it as protein.pdb
    pdb_protein = "protein.pdb"
    subprocess.run(["grep", "-v", "^HETATM", pdb_file], stdout=open(pdb_protein, "w"))

    # Run the pdb2gmx command and save output as protein.gro
    pdb2gmx_command = [
        "gmx", "pdb2gmx",
        "-f", pdb_protein,
        "-o", os.path.splitext(pdb_protein)[0] + ".gro",
        "-water", "tip3",
        "-ignh","-lys","-arg","-asp","-glu","-his"
    ]
    subprocess.run(pdb2gmx_command)

def organize_topologies(xml_file=None):
    """
    Organizes the topology files by moving them to a directory called "toppar"
    Looks for smirnoff or bespokefit directory for itp files from xml_file
    """
    # Create a directory called "toppar" if it doesn't exist
    toppar_dir = os.path.join(os.getcwd(), "toppar")
    os.makedirs(toppar_dir, exist_ok=True)
    
    # Move all .itp files from the current directory to "toppar/"
    itp_files_current_dir = glob.glob("*.itp")
    if not itp_files_current_dir:
        raise FileNotFoundError("No .itp files found in the current directory.")
    for itp_file in itp_files_current_dir:
        shutil.move(itp_file, os.path.join(toppar_dir, itp_file))
    print("Moved .itp files from the current directory to 'toppar/'")
    
    # Move *.itp files from "smirnoff/" to "toppar/"
    mode = 'bespokefit' if xml_file else 'smirnoff'
    
    param_dir = os.path.join(os.getcwd(), mode)
    itp_files_param = glob.glob(os.path.join(param_dir, "*.itp"))
    if not itp_files_param:
        raise FileNotFoundError(f"No .itp files found in the '{mode}' directory.")
    for itp_file in itp_files_param:
        shutil.copy(itp_file, os.path.join(toppar_dir, os.path.basename(itp_file)))
    print("Copied *.itp files from 'smirnoff/' to 'toppar/'")

    # Add toppar to protein include in topology
    add_toppar2protein_include('topol.top')

def addligtop(ligand_sdf):
    """
    Adds the ligand topology files to the topol.top file
    """
    # Example usage:
    file_path = 'topol.top'  # Replace with the actual file path
    ligand_name = os.path.splitext(ligand_sdf)[0]
    pattern = 'forcefield.itp"'
    line_to_add = '#include "toppar/'+ ligand_name + '_atomtypes.itp"'
    add_line_after_pattern(file_path, pattern, line_to_add)
    line_to_add = '#include "toppar/' + ligand_name + '.itp"'
    pattern='; Include water topology'
    add_line_after_pattern(file_path, pattern, line_to_add)
    add_unk_to_topol(file_path)

def addliggro(ligand_sdf,xml_file=None):
    """
    Adds the ligand gro file to the protein gro file
    """
    mode = 'bespokefit/' if xml_file else 'smirnoff/'
    # Example usage:
    source_file = mode+os.path.splitext(ligand_sdf)[0]+'_mod.gro'  # Replace with the actual path of the source file
    destination_file = 'protein.gro'  # Replace with the actual path of the destination file
    addgro2gro(source_file, destination_file)

def addligposres(ligand_sdf,xml_file=None):
    """
    Adds the ligand position restraint file to the topol.top file
    """
    ligand_name = os.path.splitext(ligand_sdf)[0]
    mode = 'bespokefit' if xml_file else 'smirnoff'
    # Define the input and output file paths    
    input_gro_file = os.path.join(mode, ligand_name+"_mod.gro")
    output_itp_file = os.path.join("toppar", ligand_name + "_posre.itp")

    # Generate lig posres using gmx genrestr
    command = ["gmx", "genrestr", "-f", input_gro_file, "-o", output_itp_file]
    
    # Prepare input for the subprocess (1 for group selection and q to quit)
    process_input = b"1\nq\n"
    subprocess.run(command,input=process_input)
    
    add_ligposres_to_topol(ligand_name)

def extract_membrane_from_charmm(charmm_gro_path, output_path):
    extract_membrane(charmm_gro_path, output_path)



def includememtop(patterns, lines_to_add):
    for pattern, line_to_add in zip(patterns, lines_to_add):
        try:
            # Open the file for reading
            with open('topol.top', 'r') as file:
                file_content = file.read()

            # Check if the pattern is present in the file content
            if pattern not in file_content:
                raise ValueError(f"Pattern '{pattern}' not found in the file.")

            # Check if the line to add is already present
            if line_to_add in file_content:
                print(f"Line '{line_to_add}' is already present in the file. Not doing anything.")
                continue

            # If the pattern is present and the line to add is not already present, add the line after the pattern
            add_line_after_pattern('topol.top', pattern, line_to_add)

        except FileNotFoundError:
            print(f"Error: File 'topol.top' not found.")
        except Exception as e:
            print(f"Error: {e}")

    p = get_popc_number('charmm_gmx/topol.top')
    add_POPC_to_topol('topol.top', p)

def addmemtop(lig_filename, lipid_dir=None):
    """
    Adds the membrane itp files to the topol.top file
    """
    ligname = os.path.splitext(lig_filename)[0]

    # Resolve path to the forcefield_dir relative to this file (gromacs_helpers_single_chain.py)
    script_dir = os.path.dirname(os.path.abspath(__file__))
    forcefield_dir = os.path.join(script_dir, "data", "forcefield_dir")

    if lipid_dir is not None:
        forcefield_dir = str(lipid_dir)

    # Define the source and destination file paths
    source_files = [
        (os.path.join(forcefield_dir, "forcefield_popc.itp"), "toppar"),
        (os.path.join(forcefield_dir, "POPC.itp"), "toppar"),
        (os.path.join(forcefield_dir, "popc_posres.itp"), "toppar")
    ]

    # Copy the files to the destination directory
    for source_file, destination_dir in source_files:
        shutil.copy(source_file, destination_dir)

    # Adding the membrane itp files to the topol.top file
    patterns = [
        ligname +'_atomtypes.itp"',
        ligname +'.itp"',
        '"toppar/POPC.itp"'
    ]
    lines_to_add = [
        '#include "toppar/forcefield_popc.itp"',
        '#include "toppar/POPC.itp"',
        '''#ifdef POSRES
#include "toppar/popc_posres.itp"'
#endif
        '''
    ]
    # Adds lines after the patterns
    
    includememtop(patterns, lines_to_add)

def remove_oldprotposre(filename):
    """
    Removes the old protein position restraint file from the topology filesL
    Removes everything after: '; Include Position restraint file'
    """
    with open(filename, 'r') as file:
        lines = file.readlines()

    # Find the line with the specified text
    for i, line in enumerate(lines):
        if line.strip() == '; Include Position restraint file':
            break
    else:  # If the line was not found, raise an error
        raise ValueError("Line '; Include Position restraint file' not found in the file.")

    # Remove everything after the found line
    lines = lines[:i+1]

    # Write the modified lines back to the file
    with open(filename, 'w') as file:
        file.writelines(lines)

def addproteinposres(file_path, protein_chains):
    line_to_add = '''; Include Position restraint file
#ifdef POSRES
#include "toppar/posres_bb.itp"
#include "toppar/posres_sc.itp"
#endif'''
    for protein_chain in protein_chains:
        pattern = f'#include "toppar/topol_Protein_chain_{protein_chain}.itp"'
        add_line_after_pattern(file_path, pattern, line_to_add)
        remove_oldprotposre(f"toppar/topol_Protein_chain_{protein_chain}.itp")

def gnrt_protposres(gro_file):
    """
    Generates position restraint files for protein backbone and side-chain atoms
    """
    # Command 1: Generate index file
    #command = f"echo 'splitch 1\nq\n' | gmx make_ndx -f {gro_file} -o chain_index.ndx"
    result = subprocess.run(command, shell=True, check=True)

    # Command 2: Extract chain 1 from membrane.gro
    result = subprocess.run(f"echo 17 | gmx editconf -f {gro_file} -n chain_index.ndx -o chain1.gro", shell=True, check=True)

    # Command 3: Generate position restraint file for side-chain atoms
    result = subprocess.run(f"echo 8 | gmx genrestr -f chain1.gro -o toppar/posres_sc.itp", shell=True, check=True)

    # Command 4: Generate position restraint file for backbone atoms
    result = subprocess.run(f"echo 4 | gmx genrestr -f chain1.gro -o toppar/posres_bb.itp", shell=True, check=True)

def solvate_system(water_count=0, solvent_box="spc216.gro"):
    """
    Solvates the system and deletes water molecules in the membrane
    """
    # Execute gmx solvate command
    try:
        subprocess.run(["gmx", "solvate", "-cp", "complex_membrane.gro", "-cs", str(solvent_box), "-p", "topol.top", "-o", "solv.gro"], check=True)
    except subprocess.CalledProcessError:
        raise Exception("Error: gmx solvate command failed.")
    
    # Execute water_deletor.pl command
    perl_script_path = files("abfe.utils") / "water_deletor.pl"
    perl_script_path = str(perl_script_path)
    

    try:
        subprocess.run(["perl", perl_script_path, "-in", "solv.gro", "-out", "solv_fix.gro", "-ref", "O33", "-middle", "C116", "-nwater", "3"], check=True)
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
        subprocess.run(["gmx", "editconf", "-f", "protein.gro", "-o", "newbox.gro", "-bt", "dodecahedron", "-d", "1.0"], check=True)
        subprocess.run(["gmx", "solvate", "-cp", "newbox.gro", "-cs", "spc216.gro", "-p", "topol.top", "-o", "solv.gro"], check=True)
    except subprocess.CalledProcessError:
        raise Exception("Error: gmx solvate command failed.")

#def update_topology_file(new_water_count = 0):
#    """
#    Updates the water count in the topology file by asking the user for a new water count
#    """
#    if new_water_count == 0:
        # Prompt the user for an input number
#        new_water_count = input("Enter the new water count: ")

    # Read the content of the topology file
#    with open("topol.top", "r") as file:
#        lines = file.readlines()

    # Find and replace the line starting with 'SOL'
#    for i, line in enumerate(lines):
#        if line.startswith('SOL'):
#            lines[i] = f'SOL        {new_water_count}\n'
#            break

    # Write the modified content back to the topology file
#    with open("topol.top", "w") as file:
#        file.writelines(lines)



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
        subprocess.run(["gmx", "grompp", "-f", "ions.mdp", "-c", input_file, "-r",input_file, "-p", "topol.top", "-o", "ions.tpr"], check=True)
    except subprocess.CalledProcessError:
        raise Exception("Error: gmx grompp command failed.")

    # Execute gmx genion command
    try:
        process_input = water_group.encode()
        command = ["gmx", "genion", "-s", "ions.tpr", "-o", "system_solv_ions.gro", "-p", "topol.top", "-pname", "NA", "-nname", "CL", "-neutral"]
        if salt_molar is not None:
            command += ["-conc", str(salt_molar)]
        subprocess.run(command, input=process_input, check=True)
    except subprocess.CalledProcessError:
        raise Exception("Error: gmx genion command failed.")
    
def cp_mdps(source_dir: Traversable):
    """
    Copies membrane protein .mdp files to the current directory.
    Accepts a Traversable object, e.g. from importlib.resources.files.
    """
    for f in source_dir.iterdir():
        if f.name.endswith('.mdp'):
            with f.open('rb') as src_file:
                with open(f.name, 'wb') as dest_file:
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