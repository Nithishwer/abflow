#!/bin/sh
#$ -q all.q@@ditriaconta,all.q@@tetraconta,all.q@@icosaoctad,all.q@@icosatetra
#$ -j y
#$ -o /cluster/home/anandn/logs
#$ -e /cluster/home/anandn/logs
#$ -cwd
#$ -V
#$ -N "4g"
#$ -l h=!hpcnode83
#$ -l gpun=1
#$ -now no
#$ -t 1-44  # array
#$ -tc 5   # max running concurrently
#$ -pe samenode 8


module load gridengine
module load cuda/10.2
export CUDA_VISIBLE_DEVICES=$(echo ${SGE_HGR_gpun} | tr ' ' ',')
echo "CUDA_VISIBLE_DEVICES = $CUDA_VISIBLE_DEVICES"

# Load the relevant GROMACS module
# Load the relevant GROMACS module

module add apps/gromacs/gcc-10.3/2021.4

# to be careful we set the number of OpenMP threads before calling gromacs
# export OMP_NUM_THREADS=${SLURM_CPUS_PER_TASK}

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
