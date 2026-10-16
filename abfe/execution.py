"""Explicit cluster plans and actions. Remote mutations require --apply."""
from contextlib import closing
import fcntl
import hashlib
import json
import math
from pathlib import Path
import re
import shlex
import sqlite3
import subprocess
import uuid

from . import execution_files as worker
from .execution_script import script
from .execution_transport import remote
from .analysis import current_decision, incident, selected, fingerprint
from .campaign import CLUSTER_ROOTS, check_destination, encode, now, presentation, read_registry, readonly
from .preparation import validate_windows

GPU_PARTITIONS = {'ranvier': {'g-ran': 'A40', 'gpu4-biggin': 'A40', 'gpuL40S-biggin': 'L40S'},
                  'soma': {'gpu3-A40': 'A40'}}
WORKLOADS = ('vanilla', 'complex-equilibration', 'solvent', 'complex-production')
SSH = ['ssh', '-T', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=15',
       '-o', 'ServerAliveInterval=15', '-o', 'ServerAliveCountMax=2']


def save(registry, plan, state, **evidence):
    record = dict(action_id=plan['id'], run_id=plan['run_id'], at=now(), state=state, plan=plan, **evidence)
    with closing(sqlite3.connect(registry)) as db, db:
        db.execute('''CREATE TABLE IF NOT EXISTS execution_events (
            revision INTEGER PRIMARY KEY, action_id TEXT NOT NULL, run_id TEXT NOT NULL, record TEXT NOT NULL)''')
        db.execute('INSERT INTO execution_events(action_id,run_id,record) VALUES (?,?,?)',
                   (plan['id'], plan['run_id'], encode(record)))
    return record


def records(db):
    if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='execution_events'").fetchone():
        return []
    return [json.loads(r[0]) for r in db.execute('SELECT record FROM execution_events ORDER BY revision')]


def load(registry, action_id):
    with closing(readonly(registry)) as db:
        result = [r for r in records(db) if r['action_id'] == action_id]
    if not result:
        raise ValueError('Unknown execution action')
    return result[-1]


def unblocked(registry, run_id, action_id=None):
    metadata, rows, row = selected(registry, run_id)
    if any(i['status'] == 'open' for i in row['incidents']):
        raise ValueError('Open incidents require explicit resolution')
    with closing(readonly(registry)) as db:
        last = {}
        for record in records(db):
            if record['run_id'] == run_id:
                last[record['action_id']] = record
    active = [r for key, r in last.items() if key != action_id and r['state'] not in ('planned', 'retrieved')
              and not (r['state'] == 'resolved' and r.get('resolution'))]
    if active or presentation(row, metadata)['jobs']:
        raise ValueError('An earlier execution or scheduler match must be reconciled/retrieved first')
    return metadata, rows, row


def relative(value):
    if not isinstance(value, str) or not value or not re.fullmatch(r'[A-Za-z0-9_./-]+', value):
        raise ValueError('relative_run must be a simple relative path')
    path = Path(value)
    if path.is_absolute() or '..' in path.parts:
        raise ValueError('relative_run must remain inside source')
    return path


def config_check(config, spec):
    if config['schema_version'] != 1 or config['run_id'] != spec['id']:
        raise ValueError('Execution manifest version/run mismatch')
    workload, cluster, mode = config['workload'], config['cluster'], config['mode']
    if workload not in WORKLOADS or mode not in ('initial', 'continue', 'extend'):
        raise ValueError('Unknown workload/mode')
    if set(spec['windows']) - {'rest', 'coul', 'vdw'}:
        raise ValueError('Unsupported window phases; review the execution profile')
    expected_leg = {'vanilla': 'vanilla', 'complex-equilibration': 'complex',
                    'complex-production': 'complex', 'solvent': 'solvent'}[workload]
    if spec['leg'] != expected_leg:
        raise ValueError('Workload does not match the registered leg')
    if type(config.get('output_reserve_bytes')) is not int or config['output_reserve_bytes'] <= 0:
        raise ValueError('Provide a positive output_reserve_bytes storage estimate')
    hours = config['walltime_hours']
    if type(hours) not in (int, float) or not 0.5 <= hours <= 24:
        raise ValueError('Use a wall time between 0.5 and 24 hours')
    for value in [config['partition'], config.get('account') or '', config.get('qos') or '']:
        if value and not re.fullmatch(r'[A-Za-z0-9_-]+', value):
            raise ValueError('Unsafe scheduler profile field')
    if not config['partition'] or not config['gromacs_version']:
        raise ValueError('Explicit partition and GROMACS version required')
    binary = Path(config['gromacs'])
    if not binary.is_absolute() or '..' in binary.parts or any(c in str(binary) for c in '\n\r'):
        raise ValueError('Provide an absolute GROMACS executable')
    if workload == 'complex-production':
        if cluster != 'archer' or config['partition'] != 'standard' or config['qos'] not in ('standard', 'lowpriority') or config['account'] != 'e280-biggin':
            raise ValueError('Unsupported ARCHER profile')
        if Path(CLUSTER_ROOTS['archer']) not in binary.parents:
            raise ValueError('ARCHER campaign executable must be under the approved work root')
        if spec['windows'] != {'rest': 12, 'coul': 11, 'vdw': 21}:
            raise ValueError('The reference ARCHER allocation supports exactly the reviewed 44-window schedule')
    else:
        if cluster not in GPU_PARTITIONS or GPU_PARTITIONS[cluster].get(config['partition']) != config.get('gpu'):
            raise ValueError('Select a routine A40/L40S partition; conditional/short queues need a scoped implementation review')
        if cluster == 'soma' and config.get('account') != 'biggingroup':
            raise ValueError('Soma profile requires biggingroup')
    if mode == 'extend' and workload not in ('solvent', 'complex-production'):
        raise ValueError('Only alchemical production can extend')
    for module in config.get('modules', []):
        if not isinstance(module, str) or not re.fullmatch(r'[A-Za-z0-9_./+-]+', module):
            raise ValueError('Unsafe module name')
    for path in config.get('library_dirs', []) + config.get('path_dirs', []):
        path = Path(path)
        if not path.is_absolute() or '..' in path.parts or ':' in str(path) or '\n' in str(path):
            raise ValueError('Unsafe environment directory')
        if cluster == 'archer' and Path(CLUSTER_ROOTS['archer']) not in path.parents:
            raise ValueError('ARCHER managed software must be under the approved work root')
    for stage, count in config.get('maxwarn', {}).items():
        if stage not in ('vanilla', 'minim', 'enmin', 'nvt', 'npt', 'npt_b', 'npt_pr', 'prod') or type(count) is not int or count < 0:
            raise ValueError('Invalid explicit grompp warning allowance')
    if any(config.get('maxwarn', {}).values()) and not config.get('warning_review'):
        raise ValueError('Nonzero maxwarn requires a recorded warning review')


def source_check(config, spec):
    source = Path(config['source'])
    if not source.is_absolute():
        raise ValueError('source must be absolute')
    run = source / relative(config['relative_run'])
    if not run.is_dir():
        raise ValueError('Missing prepared run directory')
    for topology in list(source.rglob('*.top')) + list(source.rglob('*.itp')):
        for reference in re.findall(r'^\s*#include\s*[<"]([^>"]+)[>"]', topology.read_text(), re.M):
            included = (topology.parent / reference).resolve()
            if not included.is_file() or source.resolve() not in included.parents:
                raise ValueError('Bundle all topology includes inside source: ' + str(topology) + ': ' + reference)
    windows = [f'{phase}.{i:02d}' for phase in ('rest', 'coul', 'vdw') for i in range(spec['windows'].get(phase, 0))]
    if spec['leg'] == 'vanilla':
        for name in ('minim.mdp', 'nvt.mdp', 'npt.mdp', 'prod.mdp', 'system_solv_ions.gro', 'topol.top', 'index.ndx'):
            if not (run / name).is_file():
                raise ValueError('Missing vanilla input: ' + name)
        if worker.duration(run / 'prod.mdp') != 100000:
            raise ValueError('Vanilla target must remain 100 ns')
    else:
        first = run / windows[0] / 'prod' / (windows[0].split('.')[0] + '.prod.00.mdp')
        temperature = float(worker.mdp(first)['ref-t'].split()[0])
        if not math.isfinite(temperature) or temperature <= 0:
            raise ValueError('Invalid production temperature')
        targets = [worker.duration(run / name / 'prod' / '{}.prod.{}.mdp'.format(*name.split('.'))) for name in windows]
        validate_windows(run, {p: spec['windows'][p] for p in ('rest', 'coul', 'vdw') if p in spec['windows']}, temperature, targets[0])
        if len(set(targets)) != 1 or targets[0] > 50000:
            raise ValueError('Inconsistent alchemical targets or target above cap')
        if config['mode'] == 'initial' and targets[0] != 20000:
            raise ValueError('Initial alchemical target must be 20 ns')
        if config['workload'] == 'complex-production' and not (run / 'plumed.dat').is_file():
            raise ValueError('Missing complex plumed.dat')
        for phase in spec['windows']:
            prefix = 'lig' if spec['leg'] == 'solvent' else 'complex'
            for name in (f'{prefix}_{phase}.top', f'{prefix}_{phase}.gro', 'index.ndx' if prefix == 'lig' else f'index_{phase}.ndx'):
                if not (run / name).is_file():
                    raise ValueError('Missing alchemical input: ' + name)
    from .execution_script import check_source
    check_source(config, spec, run, windows)
    return source, run, windows


def source_identity(registry, spec, source, run, snapshot):
    if spec.get('path') and run.resolve() == Path(spec['path']).resolve():
        return dict(kind='registered_source', run_id=spec['id'])
    candidates = []
    with closing(readonly(registry)) as db:
        if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='preparations'").fetchone():
            for (text,) in db.execute('SELECT record FROM preparations'):
                record = json.loads(text)
                if record.get('state') != 'prepared' or spec['id'] not in record['plan']['run_ids']:
                    continue
                prepared = record['plan']
                if spec['leg'] == 'vanilla':
                    expected = Path(prepared['output']) / prepared['ligand'] / ('vanilla_rep_' + str(spec['repeat']))
                elif spec['leg'] == 'solvent':
                    expected = Path(prepared['solvent'])
                else:
                    expected = Path(next(m['complex'] for m in prepared['mappings'] if m['repeat'] == spec['repeat']))
                if expected.resolve() == run.resolve():
                    candidates.append((Path(record['outputs']['path']), record['outputs']['files'],
                                       dict(kind='preparation', receipt_sha256=worker.digest(record))))
        for record in records(db):
            if record['run_id'] != spec['id'] or record['state'] != 'retrieved':
                continue
            transfer, previous = record['transfer'], record['plan']
            base = Path(transfer['output'])
            if (base / 'data' / previous['config']['relative_run']).resolve() == run.resolve():
                candidates.append((base, transfer['snapshot']['files'],
                                   dict(kind='verified_retrieval', action_id=previous['id'])))
    for base, files, identity in candidates:
        base = base.resolve()
        if source.resolve() != base and base not in source.resolve().parents:
            continue
        prefix = source.resolve().relative_to(base).as_posix()
        prefix = '' if prefix == '.' else prefix + '/'
        expected = {name[len(prefix):]: value for name, value in files.items() if name.startswith(prefix) and name != prefix}
        if expected == snapshot['files']:
            return identity
    raise ValueError('Source is not mapped to this run; use registered inputs, recorded preparation or verified retrieval')


def make_plan(registry, run_id, manifest):
    metadata, rows, row = unblocked(registry, run_id)
    config = json.loads(Path(manifest).read_text())
    spec = row['spec']
    config_check(config, spec)
    source, run, windows = source_check(config, spec)
    plan_id = uuid.uuid4().hex
    output = Path(registry).resolve().parent / 'executions' / plan_id
    check_destination(output, metadata, [r['spec'] for r in rows])
    if source.resolve() == output or source.resolve() in output.parents or output in source.resolve().parents:
        raise ValueError('Plan directory must not overlap its source')
    source_snapshot = worker.snapshot(source)
    identity = source_identity(registry, spec, source, run, source_snapshot)
    decision = None
    if config['mode'] == 'extend':
        if not spec.get('path') or run.resolve() != Path(spec['path']).resolve():
            raise ValueError('Extension must use the exact local run that was analysed')
        decision = current_decision(registry, run_id)
        if decision['action'] != 'propose_extension':
            raise ValueError('No fresh eligible extension decision: ' + decision['reason'])
    elif config['mode'] == 'continue' and spec['leg'] != 'vanilla':
        decision = current_decision(registry, run_id)
        if row.get('analysis_record') and decision['action'] != 'continue_target':
            raise ValueError('Analysis does not permit continuation: ' + decision['reason'])
        from .campaign import production_end
        progress = [production_end(run / name / 'prod/dhdl.xvg') for name in windows if (run / name / 'prod/dhdl.xvg').exists()]
        if any(value > 50 for value in progress):
            raise ValueError('Production exceeds the 50 ns cap; preserve and review')
        if len(progress) == len(windows) and min(progress) >= 16 and decision['action'] != 'continue_target':
            raise ValueError('Analyse available production before continuing beyond convergence eligibility')
    plan = dict(id=plan_id, run_id=run_id, created_at=now(), config=config, spec=spec,
                source_snapshot=source_snapshot, source_identity=identity, inputs=[fingerprint(p) for p in spec['input_files']],
                windows=windows, decision=decision, output=str(output),
                remote=CLUSTER_ROOTS[config['cluster']] + '/abflow/' + run_id + '/' + plan_id,
                executor='slurm-shell-v1')
    job_script = script(plan)
    plan['script_sha256'] = hashlib.sha256(job_script.encode()).hexdigest()
    plan['fingerprint'] = worker.digest(plan)
    output.mkdir(parents=True)
    (output / 'job.sh').write_text(job_script)
    (output / 'plan.json').write_text(encode(plan) + '\n')
    save(registry, plan, 'planned')
    return plan


def rsync(plan, source, destination, retrieval=False):
    transport = shlex.join(SSH[0:1] + [x for x in SSH[1:] if x != '-T'])
    args = ['rsync', '-rlt', '--protect-args', '--ignore-existing', '--partial-dir=.partial', '-e', transport]
    if not retrieval:
        args += ['--chmod=D700,F600']
    args += [source, destination]
    output = Path(plan['output']) / ('retrieve.log' if retrieval else 'stage.log')
    with output.open('a') as log:
        log.write(encode(args) + '\n')
        log.flush()
        result = subprocess.run(args, stdout=log, stderr=subprocess.STDOUT, timeout=3600)
    if result.returncode:
        raise ValueError('Transfer interrupted; preserve staging and inspect ' + str(output))


def recheck(plan):
    payload = dict(plan)
    fingerprint_ = payload.pop('fingerprint')
    if worker.digest(payload) != fingerprint_:
        raise ValueError('Execution plan fingerprint differs')
    output = Path(plan['output'])
    if (json.loads((output / 'plan.json').read_text()) != plan
            or worker.sha(output / 'job.sh') != plan['script_sha256']
            or ('worker_sha256' in plan and worker.sha(output / 'worker.py') != plan['worker_sha256'])):
        raise ValueError('Execution plan/script/worker changed')


def action(registry, action_id, operation, apply=False, output=None):
    if operation not in ('stage', 'submit', 'monitor', 'retrieve'):
        raise ValueError('Unknown cluster operation')
    current = load(registry, action_id)
    plan = current['plan']
    try:
        recheck(plan)
    except (ValueError, OSError) as exc:
        incident(registry, plan['run_id'], 'execution:' + plan['id'], str(exc))
        raise
    if operation != 'monitor' and not apply:
        return dict(operation=operation, apply=False, action_id=action_id, cluster=plan['config']['cluster'],
                    remote=plan['remote'], script=str(Path(plan['output']) / 'job.sh'), output=str(output) if output else None)
    if plan.get('executor') != 'slurm-shell-v1' and operation in ('stage', 'submit'):
        raise ValueError('Historical worker plans are preserved; create a new plain Slurm plan')
    lock_path = Path(str(registry) + '.execution.lock')
    with lock_path.open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            current = load(registry, action_id)
            if operation in ('stage', 'submit'):
                unblocked(registry, plan['run_id'], action_id)
                if worker.snapshot(plan['config']['source']) != plan['source_snapshot']:
                    raise ValueError('Source snapshot changed; create/review a new plan')
                if [fingerprint(p['path']) for p in plan['inputs']] != plan['inputs']:
                    raise ValueError('Declared campaign inputs changed')
                if plan['decision'] and current_decision(registry, plan['run_id']) != plan['decision']:
                    raise ValueError('Analysis decision changed since planning')
            if operation == 'stage':
                if current['state'] not in ('planned', 'staging', 'staged', 'paused'):
                    raise ValueError('Execution already dispatched; staging is closed')
                save(registry, plan, 'staging')
                reservation = remote(plan, 'reserve')
                alias = plan['config']['cluster'] + ':' + plan['remote']
                rsync(plan, plan['config']['source'].rstrip('/') + '/', alias + '/data/')
                for name in ('plan.json', 'job.sh'):
                    rsync(plan, str(Path(plan['output']) / name), alias + '/' + name)
                verified = remote(plan, 'verify')
                return save(registry, plan, 'staged', reservation=reservation, transfer=verified)
            if operation == 'submit':
                if current['state'] in ('submitted', 'submission_uncertain', 'active', 'complete', 'checkpointed', 'unknown', 'retrieved'):
                    return action_monitor(registry, plan)
                if current['state'] != 'staged':
                    raise ValueError('Submit requires verified staging')
                save(registry, plan, 'submission_uncertain')
                submission = remote(plan, 'submit')
                return save(registry, plan, 'submitted', submission=submission)
            if operation == 'monitor':
                return action_monitor(registry, plan)
            return retrieve(registry, plan, output)
        except (ValueError, OSError, subprocess.SubprocessError, KeyError) as exc:
            if operation == 'monitor':
                return save(registry, plan, 'unknown', error=str(exc), submission=current.get('submission'), observation=current.get('observation'))
            save(registry, plan, 'paused', operation=operation, error=str(exc))
            incident(registry, plan['run_id'], 'execution:' + plan['id'],
                     '{} paused: {}. Evidence preserved; explicit resolution required.'.format(operation, exc))
            raise ValueError(str(exc)) from exc


def action_monitor(registry, plan):
    observed = remote(plan, 'monitor')
    submission = observed.get('submission') or {}
    if observed['state'] == 'unknown' and submission.get('job_id'):
        # A receipt can recover a lost sbatch response, but an absent job still needs application evidence.
        observed['submission_identity'] = 'recovered'
    state = observed['state']
    if state == 'failed' or (state == 'unknown' and not observed['jobs']):
        incident(registry, plan['run_id'], 'execution:' + plan['id'],
                 'Execution outcome requires review: ' + encode(observed))
    return save(registry, plan, state, observation=observed, submission=submission)


def retrieve(registry, plan, output):
    if output is None or not Path(output).is_absolute():
        raise ValueError('Retrieval needs an absolute --output directory')
    output = Path(output).resolve()
    metadata, rows = read_registry(registry)
    check_destination(output, metadata, [row['spec'] for row in rows])
    protected = [Path(plan['output']), Path(plan['config']['source']).resolve(), Path(registry).resolve()]
    if any(output == p or output in p.parents or p in output.parents for p in protected):
        raise ValueError('Retrieval destination overlaps protected files')
    before = remote(plan, 'retrieve')
    expected = before['snapshot']
    output.mkdir(parents=True, exist_ok=True)
    owner = output / '.retrieval-owner.json'
    identity = dict(action_id=plan['id'], snapshot=expected)
    if owner.exists():
        if json.loads(owner.read_text()) != identity:
            raise ValueError('Retrieval destination belongs to another snapshot')
    elif list(output.iterdir()):
        raise ValueError('Retrieval destination is not empty')
    else:
        worker.write(owner, identity)
    actual = worker.snapshot(output)['files']
    actual.pop('.retrieval-owner.json', None)
    actual.pop('retrieval.json', None)
    if any(name not in expected['files'] or expected['files'][name] != value for name, value in actual.items()):
        raise ValueError('Local retrieval conflict; preserve both versions')
    alias = plan['config']['cluster'] + ':' + plan['remote'].rstrip('/') + '/'
    rsync(plan, alias, str(output) + '/', retrieval=True)
    after = remote(plan, 'retrieve')
    actual = worker.snapshot(output)['files']
    actual.pop('.retrieval-owner.json', None)
    actual.pop('retrieval.json', None)
    if after['snapshot'] != expected or actual != expected['files']:
        raise ValueError('Retrieval snapshot changed or checksum verification failed')
    receipt = dict(action_id=plan['id'], run_id=plan['run_id'], state='verified_local', at=now(),
                   source=plan['remote'], cluster=plan['config']['cluster'], snapshot=expected,
                   output=str(output), application_state=before['state'])
    worker.write(output / 'retrieval.json', receipt)
    return save(registry, plan, 'retrieved', transfer=receipt, observation=before)


def attach(db, rows):
    latest = {}
    for record in records(db):
        latest.setdefault(record['run_id'], {})[record['action_id']] = record
    for row in rows:
        row['executions'] = list(latest.get(row['spec']['id'], {}).values())


def summary(record):
    plan = record['plan']
    observation = record.get('observation') or {}
    transfer = record.get('transfer') or {}
    return dict(action_id=plan['id'], state=record['state'], cluster=plan['config']['cluster'],
                workload=plan['config']['workload'], mode=plan['config']['mode'],
                job_id=(record.get('submission') or {}).get('job_id'),
                observed_at=observation.get('at'), jobs=observation.get('jobs', []),
                application_state=observation.get('state'), error=record.get('error'),
                remote=plan['remote'], plan=str(Path(plan['output']) / 'plan.json'),
                fingerprint=plan['fingerprint'], transfer_state=transfer.get('state'),
                retrieved_to=transfer.get('output'), analysis_id=(plan.get('decision') or {}).get('analysis_id'))
