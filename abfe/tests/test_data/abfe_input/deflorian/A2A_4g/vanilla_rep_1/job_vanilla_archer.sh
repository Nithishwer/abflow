#!/bin/bash
#SBATCH --job-name=4g
#SBATCH --nodes=2
#SBATCH --output=out.%x.%A.%a.out
#SBATCH --tasks-per-node=128
#SBATCH --cpus-per-task=1
#SBATCH --time=24:00:00
#SBATCH --account=e280-biggin 
#SBATCH --partition=standard
#SBATCH --qos=standard


# Load the relevant GROMACS module
# Load the relevant GROMACS module
module load gromacs/2022.4

# to be careful we set the number of OpenMP threads before calling gromacs
export OMP_NUM_THREADS=${SLURM_CPUS_PER_TASK}
gmx grompp -f minim.mdp -c system_solv_ions_water_removed.gro -p topol_water_removed.top -o em.tpr -r system_solv_ions_water_removed.gro -maxwarn 2
srun --cpu-bind=cores gmx_mpi mdrun -v -deffnm em -ntmpi 1

echo 'PENNYWORTH LOG:  NVT'
gmx grompp -f nvt.mdp -c em.gro -r em.gro -p topol_water_removed.top -n index.ndx -o nvt.tpr
srun --cpu-bind=cores gmx_mpi mdrun -deffnm nvt -v -stepout 1000 -s nvt.tpr -cpi nvt.cpt -ntmpi 1


echo 'PENNYWORTH LOG:  NPT_B'
gmx grompp -f npt.mdp -c nvt.gro -r nvt.gro -t nvt.cpt -p topol_water_removed.top -n index.ndx -o npt.tpr
srun --cpu-bind=cores gmx_mpi mdrun -deffnm npt -v -stepout 1000 -s npt.tpr -cpi npt.cpt -ntmpi 1

echo 'PENNYWORTH LOG: PROD'
gmx grompp -f prod.mdp -c npt.gro -r npt.gro -t npt.cpt -p topol_water_removed.top -n index.ndx -o prod.tpr
srun --cpu-bind=cores gmx_mpi mdrun -deffnm prod -v -stepout 1000 -s prod.tpr -cpi prod.cpt -ntmpi 1

