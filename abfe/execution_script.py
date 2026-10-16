"""Render ordinary Slurm/GROMACS scripts. All planning happens on the workstation."""
import math
from pathlib import Path
import re
import shlex
from .execution_files import sha, duration


def check_source(config, spec, run, windows):
    source = Path(config['source']).resolve()
    for receipt in run.rglob('abflow-*.sha256'):
        if receipt.name.startswith('abflow-gromacs-'):
            continue  # The executable is on the destination cluster, checked by the batch shell.
        for line in receipt.read_text().splitlines():
            digest, name = line.split('  ', 1)
            target = (receipt.parent / name).resolve()
            if source not in target.parents or not target.is_file() or sha(target) != digest:
                raise ValueError('Changed stage/checkpoint evidence: ' + str(receipt))
    if config['workload'] == 'complex-production' and config['mode'] == 'initial':
        for window in windows:
            path = run / window / 'npt_pr'
            if not (path / 'abflow-state.done').is_file() or not (path / 'state.cpt').is_file():
                raise ValueError('GPU equilibration is incomplete: ' + window)
    if config['mode'] == 'initial':
        paths = [run] if spec['leg'] == 'vanilla' else [run / w / s for w in windows for s in
                (('prod',) if config['workload'] == 'complex-production' else ('enmin', 'nvt', 'npt_b', 'npt_pr', 'prod'))]
        for directory in paths:
            if list(directory.glob('*.cpt')) or any((directory / name).exists() for name in
                    (('em.tpr', 'nvt.tpr', 'npt.tpr', 'prod.tpr') if spec['leg'] == 'vanilla' else ('topol.tpr',))):
                raise ValueError('Existing simulation inputs require an explicit continuation plan')
    else:
        if not list(run.rglob('abflow-*.inputs.sha256')):
            raise ValueError('Continuation requires recorded stage inputs; legacy adoption is separate')
        if not (run / ('abflow-gromacs-' + config['cluster'] + '.sha256')).is_file():
            raise ValueError('Continuation requires the same recorded cluster executable')
        for directory in [run] if spec['leg'] == 'vanilla' else [run / w / 'prod' for w in windows]:
            if config['mode'] == 'extend' or config['workload'] == 'complex-production':
                for name in ('state.cpt', 'topol.tpr', 'abflow-state.outputs.sha256'):
                    if not (directory / name).is_file():
                        raise ValueError('Missing managed checkpoint/TPR: ' + str(directory / name))


def _stage(config, stage, directory, mdp, topology, coordinates, index, target, prefix=None, previous_checkpoint=None):
    """One visible grompp/mdrun block, with native checkpoint continuation."""
    tpr, cpt, gro = ((prefix + '.tpr', prefix + '.cpt', prefix + '.gro') if prefix else
                     ('topol.tpr', 'state.cpt', 'confout.gro'))
    label = prefix or 'state'
    marker = 'abflow-' + label
    q = lambda s: '"' + s + '"'  # Values are internal shell paths, including the selected array window.
    compile_args = ['"$GMX" grompp', '-f '+q(mdp), '-c '+q(coordinates), '-r '+q(coordinates),
                    '-p '+q(topology), '-n '+q(index), '-o '+tpr]
    if previous_checkpoint:
        compile_args += ['-t '+q(previous_checkpoint)]
    warnings = config.get('maxwarn', {}).get(stage, 0)
    if warnings:
        compile_args += ['-maxwarn '+str(warnings)]
    inputs = ' '.join(map(q, (tpr, mdp, topology, index)))
    minim = stage in ('minim', 'enmin')
    commands = [f'cd "{directory}"', f'if [ -f {marker}.done ]; then',
                f'    sha256sum -c {marker}.inputs.sha256 {marker}.outputs.sha256', 'else',
                '    budget', f'    if [ -f {marker}.inputs.sha256 ]; then',
                f'        sha256sum -c {marker}.inputs.sha256', '    else', f'        test ! -e {tpr}',
                '        '+' '.join(compile_args), f'        sha256sum {inputs} > {marker}.inputs.sha256', '    fi',
                '    resume=()', f'    if [ -s {cpt} ]; then resume=(-cpi {cpt} -append); fi',
                '    "$GMX" mdrun -s '+tpr+(' -deffnm '+prefix if prefix else '')+
                ' -ntmpi 1 -maxh "$MAXH" "${resume[@]}"']
    if not minim:
        commands += [f'    checkpoint {cpt}', f'    printf "%s\\n" "$TIME_PS" > {marker}.time']
    outputs = tpr+' '+gro+(' '+cpt+' '+marker+'.time' if not minim else '')
    commands += [f'    sha256sum {outputs} > {marker}.outputs.sha256']
    if not minim:
        commands += [f'    if ! awk -v t="$TIME_PS" -v goal="{target}" \'BEGIN {{exit !(t+0.00001 >= goal)}}\'; then',
                     '        finish checkpointed', '    fi']
    commands += [f'    touch {marker}.done', 'fi']
    return commands


def script(plan):
    c, remote = plan['config'], plan['remote']
    name = re.sub(r'[^A-Za-z0-9_-]', '-', plan['run_id'])[:128]
    lines = ['#!/bin/bash -l' if c.get('modules') else '#!/bin/bash', '#SBATCH --job-name='+name,
             '#SBATCH --partition='+c['partition'], '#SBATCH --time='+str(round(c['walltime_hours']*60)), '#SBATCH --no-requeue',
             '#SBATCH --output='+remote+'/slurm-%A_%a.out', '#SBATCH --nodes='+('22' if c['cluster']=='archer' else '1')]
    if c.get('account'):
        lines += ['#SBATCH --account='+c['account']]
    if c['cluster']=='archer':
        lines += ['#SBATCH --ntasks-per-node=2', '#SBATCH --cpus-per-task=64', '#SBATCH --qos='+c['qos']]
    else:
        lines += ['#SBATCH --ntasks-per-socket=1', '#SBATCH --cpus-per-task=8',
                  '#SBATCH --gres=gpu:'+c['gpu']+':1', '#SBATCH --gres-flags=enforce-binding']
        if c['workload'] in ('complex-equilibration', 'solvent'):
            count = c.get('array_concurrency')
            if type(count) is not int or not 1 <= count <= len(plan['windows']):
                raise ValueError('Provide an explicit array_concurrency within the window count')
            lines += [f'#SBATCH --array=0-{len(plan["windows"])-1}%{count}']
    lines += ['set -euo pipefail', 'umask 077', 'BUNDLE='+shlex.quote(remote),
              'STATUS="$BUNDLE/status/${SLURM_ARRAY_TASK_ID:-0}.state"', 'mkdir -p "$BUNDLE/status"', 'test ! -e "$STATUS"',
              'trap \'rc=$?; if ((rc)); then printf "failed %s\\n" "$rc" > "$STATUS"; fi\' EXIT',
              'finish() { printf "%s\\n" "$1" > "$STATUS"; exit 0; }',
              'printf "running\\n" > "$STATUS"', 'export OMP_NUM_THREADS="$SLURM_CPUS_PER_TASK"',
              'export SRUN_CPUS_PER_TASK="$SLURM_CPUS_PER_TASK"', 'unset GMX_MAXBACKUP']
    lines += ['module load '+shlex.quote(module) for module in c.get('modules', [])]
    for variable,key in [('PATH','path_dirs'),('LD_LIBRARY_PATH','library_dirs')]:
        if c.get(key):
            lines += ['export '+variable+'='+shlex.quote(':'.join(c[key]))+':"${'+variable+':-}"']
    run = remote+'/data/'+c['relative_run']
    lines += ['RUN='+shlex.quote(run), 'GMX='+shlex.quote(c['gromacs']), 'cd "$RUN"',
              'VERSION="$BUNDLE/gromacs-version-${SLURM_ARRAY_TASK_ID:-0}.txt"',
              '"$GMX" --version > "$VERSION"',
              'test "$(awk \'/GROMACS version:/ {print $3}\' "$VERSION")" = '+shlex.quote(c['gromacs_version'])]
    binary_receipt = 'abflow-gromacs-'+c['cluster']+'.sha256'
    lines += ['(', 'flock -x 9', f'if [ -f {binary_receipt} ]; then sha256sum -c {binary_receipt};',
              f'else sha256sum "$(readlink -f "$GMX")" > {binary_receipt}; fi',
              ') 9>abflow-software.lock',
              'budget() {', f'    local left=$(({round(c["walltime_hours"]*3600)} - SECONDS - 900))',
              '    ((left > 0)) || finish checkpointed', '    MAXH=$(awk -v s="$left" \'BEGIN {print s/3600}\')', '}',
              'checkpoint() {', '    "$GMX" dump -cp "$1" > "$BUNDLE/checkpoint-${SLURM_ARRAY_TASK_ID:-0}.txt"',
              '    TIME_PS=$(awk \'$1=="t" && $2=="=" {print $3; n++} END {if(n!=1) exit 1}\' "$BUNDLE/checkpoint-${SLURM_ARRAY_TASK_ID:-0}.txt")',
              '    awk -v t="$TIME_PS" \'BEGIN {exit !(t ~ /^[0-9.eE+-]+$/ && t+0>=0)}\'', '}', '']
    if c['mode']=='extend':
        lines += _extension(plan)
    if c['workload']=='vanilla':
        previous = 'system_solv_ions.gro'
        for stage,out in [('minim','em'),('nvt','nvt'),('npt','npt'),('prod','prod')]:
            target = duration(Path(c['source'])/c['relative_run']/(stage+'.mdp')) if stage!='minim' else 0
            # Match the supplied vanilla script's checkpoint velocity handoff.
            checkpoint = {'npt':'nvt.cpt','prod':'npt.cpt'}.get(stage)
            lines += ['# '+stage.upper()]+_stage(c,stage,'$RUN',stage+'.mdp','topol.top',previous,'index.ndx',target,out,checkpoint)+['']
            previous = out+'.gro'
    elif c['workload']=='complex-production':
        lines += _hrex(plan)
    else:
        lines += ['windows=('+' '.join(plan['windows'])+')', 'window=${windows[$SLURM_ARRAY_TASK_ID]}',
                  'phase=${window%.*}', 'number=${window##*.}']
        solvent = c['workload']=='solvent'
        prefix = 'lig' if solvent else 'complex'
        coordinates = '$RUN/'+prefix+'_${phase}.gro'
        for stage in ['enmin','nvt','npt_b','npt_pr']+(['prod'] if solvent else []):
            target = _target(plan,stage)
            lines += ['# '+stage.upper()]+_stage(c,stage,'$RUN/$window/'+stage,'${phase}.'+stage+'.${number}.mdp',
                '$RUN/'+prefix+'_${phase}.top',coordinates,'$RUN/'+('index.ndx' if solvent else 'index_${phase}.ndx'),target)+['']
            coordinates = '$RUN/$window/'+stage+'/confout.gro'
            if solvent:
                lines += ['if [ ! -f abflow-centered.gro ]; then',
                          '    printf "0\\n" | "$GMX" trjconv -f confout.gro -s topol.tpr -pbc nojump -o abflow-nojump.gro',
                          '    printf "1\\n0\\n" | "$GMX" trjconv -f abflow-nojump.gro -s topol.tpr -center -pbc mol -o abflow-centered.gro', 'fi']
                coordinates = '$RUN/$window/'+stage+'/abflow-centered.gro'
    return '\n'.join(lines+['finish complete',''])


def _target(plan,stage):
    if stage=='enmin':
        return 0
    if stage=='prod' and plan['config']['mode']=='extend':
        return plan['decision']['next_target_ns']*1000
    values = [duration(Path(plan['config']['source'])/plan['config']['relative_run']/w/stage/
              ('{}.{}.{}.mdp'.format(w.split('.')[0],stage,w.split('.')[1]))) for w in plan['windows']]
    if len(set(values))!=1:
        raise ValueError('Window stage durations differ; review the execution profile')
    return values[0]


def _extension(plan):
    target = plan['decision']['next_target_ns']*1000
    suffix = plan['id']
    return ['# Prepare every window before installing any extended TPR.', '(', 'flock -x 9',
        'if [ ! -f "$BUNDLE/extended" ]; then',
        'test ! -e "$BUNDLE/extension.started"', 'touch "$BUNDLE/extension.started"', 'cd "$RUN"',
        'for window in '+' '.join(plan['windows'])+'; do',
        '    cd "$RUN/$window/prod"', '    phase=${window%.*}; number=${window##*.}',
        '    sha256sum -c abflow-state.inputs.sha256 abflow-state.outputs.sha256',
        f'    "$GMX" convert-tpr -s topol.tpr -until {target} -o extended.tpr',
        '    "$GMX" dump -s extended.tpr -om extended.mdp',
        f'    awk -F "[=;]" \'{{gsub(/[[:space:]]/,"",$1)}} $1=="dt" {{dt=$2}} $1=="nsteps" {{n=$2}} END {{exit !(dt*n=={target})}}\' extended.mdp',
        'done', 'for window in '+' '.join(plan['windows'])+'; do', '    cd "$RUN/$window/prod"',
        '    phase=${window%.*}; number=${window##*.}', '    mdp="${phase}.prod.${number}.mdp"',
        f'    cp -p topol.tpr topol.tpr.before-{suffix}', f'    cp -p "$mdp" "$mdp.before-{suffix}"',
        f'    cp -p abflow-state.inputs.sha256 abflow-state.inputs.sha256.before-{suffix}',
        f'    cp -p abflow-state.outputs.sha256 abflow-state.outputs.sha256.before-{suffix}',
        '    mv extended.tpr topol.tpr',
        f'    awk -F "[=;]" \'$1 ~ /^[[:space:]]*dt[[:space:]]*$/ {{dt=$2}} {{line[NR]=$0}} END {{for(i=1;i<=NR;i++) if(line[i] ~ /^[[:space:]]*nsteps[[:space:]]*=/) print "nsteps = " {target}/dt; else print line[i]}}\' "$mdp" > extended-input.mdp',
        '    mv extended-input.mdp "$mdp"',
        f'    if [ -f abflow-state.done ]; then mv abflow-state.done abflow-state.done.before-{suffix}; fi',
        '    topology="../../'+('lig' if plan['spec']['leg']=='solvent' else 'complex')+'_${phase}.top"',
        '    index="../../'+('index.ndx' if plan['spec']['leg']=='solvent' else 'index_${phase}.ndx')+'"',
        '    sha256sum topol.tpr "$mdp" "$topology" "$index" > abflow-state.inputs.sha256',
        '    sha256sum topol.tpr confout.gro state.cpt abflow-state.time > abflow-state.outputs.sha256',
        'done', 'touch "$BUNDLE/extended"', 'fi', ') 9>"$BUNDLE/extension.lock"']


def _hrex(plan):
    c = plan['config']
    lines = ['directories=()', 'for window in '+' '.join(plan['windows'])+'; do',
             '    cd "$RUN/$window/prod"', '    phase=${window%.*}; number=${window##*.}']
    if c['mode']=='initial':
        lines += ['    test ! -e topol.tpr',
            '    "$GMX" grompp -f "${phase}.prod.${number}.mdp" -c ../npt_pr/confout.gro -r ../npt_pr/confout.gro -t ../npt_pr/state.cpt -p "../../complex_${phase}.top" -n "../../index_${phase}.ndx" -o topol.tpr'+(' -maxwarn '+str(c['maxwarn']['prod']) if c.get('maxwarn',{}).get('prod') else ''),
            '    sha256sum topol.tpr "${phase}.prod.${number}.mdp" "../../complex_${phase}.top" "../../index_${phase}.ndx" > abflow-state.inputs.sha256']
    else:
        lines += ['    sha256sum -c abflow-state.inputs.sha256 abflow-state.outputs.sha256']
    lines += ['    directories+=("$PWD")','done', 'cd "$RUN"', 'budget',
        'srun --cpu-bind=cores "$GMX" mdrun -multidir "${directories[@]}" -replex 200 -hrex -plumed ../../plumed.dat -dlb no -maxh "$MAXH"'+(' -cpi state.cpt -append' if c['mode']!='initial' else ''),
        'result=complete', 'first_time=', 'for directory in "${directories[@]}"; do', '    cd "$directory"',
        '    checkpoint state.cpt', '    if [ -n "$first_time" ]; then',
        '        awk -v t="$TIME_PS" -v first="$first_time" \'BEGIN {d=t-first; exit !(d<0.00001 && d> -0.00001)}\'',
        '    else first_time=$TIME_PS; fi', '    printf "%s\\n" "$TIME_PS" > abflow-state.time',
        '    test -s dhdl.xvg; test -s md.log',
        '    sha256sum topol.tpr confout.gro state.cpt abflow-state.time dhdl.xvg md.log > abflow-state.outputs.sha256',
        f'    if awk -v t="$TIME_PS" \'BEGIN {{exit !(t+0.00001 >= {_target(plan,"prod")})}}\'; then touch abflow-state.done;',
        '    else result=checkpointed; fi', 'done', 'finish "$result"']
    return lines
