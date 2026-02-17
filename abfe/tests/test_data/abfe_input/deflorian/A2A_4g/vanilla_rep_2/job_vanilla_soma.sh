#!/bin/bash
#SBATCH --nodes=1
## The use of ntasks-per-socket instead of ntasks-per-node is *very* important
#SBATCH --ntasks-per-socket=1
#SBATCH --cpus-per-task=8
#SBATCH --gres=gpu:1
#SBATCH --gres-flags=enforce-binding
#SBATCH --time=24:00:00
#SBATCH -J 4g
#SBATCH -p gpu-biggin,gpu4-biggin
#SBATCH --output=output_%x_%A_%a.out   # Output file name (includes array ID)

# Load the relevant GROMACS module
# Load the relevant GROMACS module
module add apps/gromacs/gcc-10.3/2021.4

# to be careful we set the number of OpenMP threads before calling gromacs
export OMP_NUM_THREADS=${SLURM_CPUS_PER_TASK}

gmx grompp -f minim.mdp -c system_solv_ions_water_removed.gro -p topol_water_removed.top -o em.tpr -r system_solv_ions_water_removed.gro -maxwarn 2
gmx mdrun -v -deffnm em -ntmpi 1

echo 'PENNYWORTH LOG:  NVT'
gmx grompp -f nvt.mdp -c em.gro -r em.gro -p topol_water_removed.top -n index.ndx -o nvt.tpr
gmx mdrun -deffnm nvt -v -stepout 1000 -s nvt.tpr -cpi nvt.cpt -ntmpi 1


echo 'PENNYWORTH LOG:  NPT_B'
gmx grompp -f npt.mdp -c nvt.gro -r nvt.gro -t nvt.cpt -p topol_water_removed.top -n index.ndx -o npt.tpr
gmx mdrun -deffnm npt -v -stepout 1000 -s npt.tpr -cpi npt.cpt -ntmpi 1

echo 'PENNYWORTH LOG: PROD'
gmx grompp -f prod.mdp -c npt.gro -r npt.gro -t npt.cpt -p topol_water_removed.top -n index.ndx -o prod.tpr
gmx mdrun -deffnm prod -v -stepout 1000 -s prod.tpr -cpi prod.cpt -ntmpi 1
