"""Workstation-side SSH/rsync control. Remote commands are shell, Slurm and checksums."""
import hashlib
import json
from pathlib import Path
import re
import shlex
import subprocess
from .campaign import CLUSTER_ROOTS, now
from .execution_files import sha

SSH = ['ssh','-T','-o','BatchMode=yes','-o','ConnectTimeout=15',
       '-o','ServerAliveInterval=15','-o','ServerAliveCountMax=2']


def execute(plan, body):
    path, allowed = Path(plan['remote']), Path(CLUSTER_ROOTS[plan['config']['cluster']])
    if allowed not in path.parents or '..' in path.parts:
        raise ValueError('Remote path outside approved root')
    guard = ['set -euo pipefail', 'root='+shlex.quote(str(path)),
             'case "$(realpath -m "$root")" in "$(realpath '+shlex.quote(str(allowed))+')"/*) ;; *) exit 1;; esac']
    guard += ['test ! -L '+shlex.quote(str(p)) for p in [path]+list(path.parents) if p!=allowed and allowed in p.parents]
    result = subprocess.run(SSH+[plan['config']['cluster'],'bash -s'],input='\n'.join(guard)+'\n'+body+'\n',
                            text=True,capture_output=True,timeout=300)
    with (Path(plan['output'])/'ssh.log').open('a') as log:
        log.write(body+'\n'+result.stdout+result.stderr+'\n')
    if result.returncode:
        raise ValueError('Remote command failed:\n'+result.stdout[-4000:]+result.stderr[-4000:])
    return result.stdout


def command(plan, args):
    return execute(plan, shlex.join([str(a) for a in args]))


def snapshot(plan, subdirectory=''):
    body = r'''test ! -L "$root/SUBDIRECTORY"
cd "$root/SUBDIRECTORY"
find . -mindepth 1 -path '*/.partial' -prune -o -print0 |
while IFS= read -r -d '' path; do
    test ! -L "$path"
    if [ -d "$path" ]; then printf 'D\t%s\0' "${path#./}";
    elif [ -f "$path" ]; then
        before=$(stat -c '%s:%Y:%y' "$path")
        printf 'F\t%s\t' "$(stat -c %s "$path")"
        sha256sum -z -- "$path"
        test "$before" = "$(stat -c '%s:%Y:%y' "$path")"
    else exit 1; fi
done'''.replace('SUBDIRECTORY',subdirectory)
    files, size = {}, 0
    for record in execute(plan,body).split('\0'):
        if not record: continue
        kind, rest = record.split('\t',1)
        if kind=='D':
            files[rest+'/']=None
        else:
            amount, hashed = rest.split('\t',1)
            digest, name = hashed.split('  ',1)
            if not re.fullmatch('[a-f0-9]{64}',digest): raise ValueError('Invalid remote checksum')
            files[name.removeprefix('./')]=digest
            size += int(amount)
    return dict(files=files,bytes=size)


def jobs(plan):
    result=[]
    for line in command(plan,['squeue','--me','--array','--noheader','--format=%i|%T|%Z|%q|%j']).splitlines():
        if not line.strip(): continue
        job,state,work,qos,name=line.strip().split('|')
        if not re.fullmatch(r'[0-9][0-9_+.-]*',job) or not work.startswith('/'):
            raise ValueError('Unrecognized scheduler output')
        result.append(dict(job_id=job,state=state,work_dir=work,qos=qos,name=name))
    return result


def writers(plan,queue):
    root=Path(plan['remote'])
    return [j for j in queue if Path(j['work_dir'])==root or root in Path(j['work_dir']).parents]


def checks(plan, data=False, partial=False):
    names={'plan.json':hashlib.sha256((Path(plan['output'])/'plan.json').read_bytes()).hexdigest(),
           'job.sh':plan['script_sha256']}
    if plan.get('worker_sha256'): names['worker.py']=plan['worker_sha256']
    commands=['cd "$root"']
    for name,digest in names.items():
        commands += ['test ! -L '+name, 'test "$(sha256sum '+name+' | cut -d " " -f1)" = '+digest]
    execute(plan,'\n'.join(commands))
    if data:
        actual=snapshot(plan,'data')['files']
        expected=plan['source_snapshot']['files']
        if any(k not in expected or expected[k]!=v for k,v in actual.items()) or (not partial and actual!=expected):
            raise ValueError('Transfer conflict or changed input snapshot')


def quota(plan):
    root=CLUSTER_ROOTS[plan['config']['cluster']]
    fields=command(plan,['df','-Pk',root]).splitlines()[-1].split()
    available=int(fields[3])*1024
    if plan['config']['cluster']=='archer':
        project=command(plan,['lfs','project','-d',root]).split()[0]
        if not project.isdigit(): raise ValueError('Missing ARCHER project quota identity')
        text=command(plan,['lfs','quota','-p',project,root])
        match=re.search(r'^\s*(?:/\S+\s+)?(\d+)\*?\s+(\d+)\s+(\d+)\s+\S+',text,re.M)
        if not match or int(match[3])<=0: raise ValueError('Unrecognized ARCHER project quota: '+text)
        available=min(available,(int(match[3])-int(match[1]))*1024)
    required=plan['source_snapshot']['bytes']*2+plan['config']['output_reserve_bytes']
    if available<required: raise ValueError('Insufficient verified staging space')
    return dict(available_bytes=available,staging_required_bytes=required)


def admission(plan,queue):
    c=plan['config']
    text=command(plan,['scontrol','show','partition',c['partition'],'-o'])
    if 'State=UP' not in text or 'PartitionName='+c['partition'] not in text:
        raise ValueError('Selected partition is not available')
    limit=re.search(r'MaxTime=(\S+)',text)
    if not limit: raise ValueError('Missing partition wall-time limit')
    if limit[1] not in ('UNLIMITED','INFINITE'):
        days,clock=limit[1].split('-') if '-' in limit[1] else ('0',limit[1])
        seconds=int(days)*86400+sum(int(x)*y for x,y in zip(clock.split(':'),(3600,60,1)))
        if seconds<c['walltime_hours']*3600: raise ValueError('Requested wall time exceeds partition limit')
    result=dict(submitted=len(queue),running=sum(j['state']=='RUNNING' for j in queue))
    if c['cluster']=='archer':
        fields=command(plan,['sacctmgr','-n','-P','show','qos',c['qos'],'format=Name,MaxJobsPU,MaxSubmitPU']).strip().splitlines()[0].split('|')
        if fields[0]!=c['qos'] or not fields[1].isdigit() or not fields[2].isdigit():
            raise ValueError('Missing numeric ARCHER QoS ceilings')
        if sum(j['qos']==c['qos'] for j in queue)>=int(fields[2]): raise ValueError('ARCHER submitted-job ceiling reached')
        result.update(maximum_running=int(fields[1]),maximum_submitted=int(fields[2]))
    elif not re.search(r'gpu:'+c['gpu']+r':\d+',command(plan,['sinfo','-h','-p',c['partition'],'-o','%G'])):
        raise ValueError('Selected partition no longer advertises the requested GPU')
    return result


def observe(plan):
    checks(plan)
    active=writers(plan,jobs(plan))
    raw=execute(plan,'if [ -f "$root/submission.id" ]; then cat "$root/submission.id"; fi').strip()
    submission=dict(job_id=raw.split(';')[0],state='submitted') if raw else None
    if not raw and plan.get('worker_sha256'):
        raw=execute(plan,'if [ -f "$root/submission.json" ]; then cat "$root/submission.json"; fi').strip()
        submission=json.loads(raw) if raw else None
    text=execute(plan,'for f in "$root"/status/*.state "$root"/status/*.json; do [ -f "$f" ] || continue; printf "%s\\0" "${f##*/}"; cat "$f"; printf "\\0"; done')
    values=text.split('\0')
    application=[]
    for i in range(0,len(values)-1,2):
        name,value=values[i:i+2]
        application.append(json.loads(value) if name.endswith('.json') else dict(task=name.split('.')[0],state=value.split()[0],detail=value.strip()))
    expected=len(plan['windows']) if plan['config']['workload'] in ('solvent','complex-equilibration') else 1
    state='active' if active else 'unknown'
    if not active and len(application)==expected and all(r['state'] in ('complete','checkpointed') for r in application):
        # Completion markers alone are insufficient: retain and check actual outputs/checkpoints.
        evidence=execute(plan,'find "$root/data" -name "abflow-*.outputs.sha256" -type f -print0 | while IFS= read -r -d "" f; do (cd "${f%/*}"; sha256sum -c "${f##*/}"); done')
        if not evidence.strip(): raise ValueError('Missing application/checkpoint evidence')
        state='complete' if all(r['state']=='complete' for r in application) else 'checkpointed'
    if any(r['state']=='failed' for r in application): state='failed'
    scheduler=dict(state='unknown')
    if not active and submission and submission.get('job_id'):
        job=submission['job_id']
        if not re.fullmatch(r'\d+',job): raise ValueError('Unrecognized submission job ID')
        output=execute(plan,'scontrol show job '+job+' -o 2>&1 || true')
        scheduler=dict(state='observed',report=output)
        if re.search(r'JobState=(FAILED|CANCELLED|TIMEOUT|NODE_FAIL|OUT_OF_MEMORY|PREEMPTED)',output): state='failed'
        elif plan['config']['cluster']!='ranvier' and 'Invalid job id' in output:
            output=command(plan,['sacct','-n','-P','-j',job,'--format=JobIDRaw,State,ExitCode'])
            scheduler['accounting']=output
            if re.search(r'\|(FAILED|CANCELLED|TIMEOUT|NODE_FAIL|OUT_OF_MEMORY|PREEMPTED)',output): state='failed'
    logs=execute(plan,'for f in "$root"/slurm-*.out; do [ ! -f "$f" ] || tail -c 4000 "$f"; done') if state in ('failed','unknown') else ''
    return dict(state=state,jobs=active,application=application,submission=submission,scheduler=scheduler,logs=logs,at=now())


def remote(plan,operation):
    if operation=='reserve':
        if writers(plan,jobs(plan)): raise ValueError('Job is writing the destination')
        capacity=quota(plan)
        fingerprint=shlex.quote(plan['fingerprint'])
        execute(plan,'\n'.join(['umask 077','if [ ! -d "$root" ]; then',
            'mkdir -p "${root%/*}"','mkdir "$root"',f'printf "%s\\n" {fingerprint} > "$root/owner"',
            'mkdir "$root/data" "$root/status"','fi',f'test "$(cat "$root/owner")" = {fingerprint}',
            'test ! -e "$root/submission.intent"']))
        actual=snapshot(plan,'data')['files']
        if any(k not in plan['source_snapshot']['files'] or plan['source_snapshot']['files'][k]!=v for k,v in actual.items()):
            raise ValueError('Transfer conflict or changed input snapshot')
        return dict(state='reserved',capacity=capacity)
    checks(plan,data=operation in ('verify','submit'))
    if operation=='verify': return dict(state='staged',source_snapshot=plan['source_snapshot'])
    if operation=='submit':
        queue=jobs(plan)
        if writers(plan,queue): raise ValueError('Destination is already active')
        limits=admission(plan,queue)
        capacity=quota(plan)
        command(plan,['sbatch','--test-only','--chdir='+plan['remote'],plan['remote']+'/job.sh'])
        output=execute(plan,'\n'.join(['cd "$root"','exec 9>submission.lock','flock -n 9',
            'if [ -f submission.id ]; then cat submission.id; exit 0; fi',
            'test ! -e submission.intent','printf "%s\\n" '+shlex.quote(plan['fingerprint'])+' > submission.intent',
            'id=$(sbatch --parsable --chdir="$root" "$root/job.sh")',
            'printf "%s\\n" "$id" > submission.id.tmp','mv submission.id.tmp submission.id','printf "%s\\n" "$id"']))
        match=re.fullmatch(r'(\d+)(?:;[A-Za-z0-9_.-]+)?\s*',output)
        if not match: raise ValueError('Uncertain sbatch response: '+output)
        return dict(state='submitted',job_id=match[1],admission=limits,capacity=capacity,at=now())
    if operation not in ('monitor','retrieve'): raise ValueError('Unknown remote operation')
    result=observe(plan)
    if operation=='retrieve':
        if result['jobs'] or result['state'] not in ('complete','checkpointed','failed'):
            raise ValueError('Retrieval requires stopped jobs and application/checkpoint evidence')
        result['snapshot']=snapshot(plan)
    return result
