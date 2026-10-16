"""In-place local analysis and evidence-based decisions; no simulation operations."""

from contextlib import closing, redirect_stderr, redirect_stdout
import csv
from datetime import datetime, timezone
from decimal import Decimal
import fcntl
import hashlib
from importlib import metadata
from importlib.resources import files
import json
import math
from pathlib import Path
import platform
import re
import sqlite3
import sys
import uuid


def encode(value):
    return json.dumps(value, sort_keys=True, allow_nan=False)


def digest(value):
    return hashlib.sha256(encode(value).encode()).hexdigest()


def protocol():
    return json.loads((files('abfe') / 'analysis_protocol.json').read_text())


def timestamp():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def stamp(path):
    value = Path(path).stat()
    return dict(size=value.st_size, mtime_ns=value.st_mtime_ns, ctime_ns=value.st_ctime_ns,
                inode=value.st_ino, device=value.st_dev)


def fingerprint(path):
    source_path = str(Path(path).absolute())
    path = Path(path).resolve(strict=True)
    before, sha = stamp(path), hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            sha.update(block)
    if before != stamp(path):
        raise ValueError(f'File changed while hashing: {path}')
    return dict(path=str(path), source_path=source_path, stamp=before, sha256=sha.hexdigest())


def settings(path):
    result = {}
    for line in Path(path).read_text().splitlines():
        line = line.split(';', 1)[0]
        if '=' in line:
            key, value = line.split('=', 1)
            key = key.strip().lower().replace('_', '-')
            if key in result:
                raise ValueError(f'Duplicate MDP setting {key}: {path}')
            result[key] = value.strip()
    return result


def finite(value):
    number = float(value)
    if not math.isfinite(number):
        raise ValueError('Nonnumeric/nonfinite analysis value')
    return number


def close_values(actual, expected):
    # GROMACS XVG lambda vectors are printed to four decimal places.
    return len(actual) == len(expected) and all(abs(a-b) <= 0.000051 for a, b in zip(actual, expected))


def read_window(path, mdp, state, count, cutoff_ps):
    """Validate the entire stream, hashing during the same pass; retain no raw copy."""
    before, sha = stamp(path), hashlib.sha256()
    dt = Decimal(mdp['dt'])
    stride = dt * Decimal(mdp['nstdhdl'])
    if not stride.is_finite() or stride <= 0 or Decimal(mdp['nstdhdl']) % 1:
        raise ValueError('Invalid DHDL output stride')
    header, previous, samples, through, width = [], None, 0, 0, None
    with Path(path).open('rb') as stream:
        for raw in stream:
            sha.update(raw)
            if not raw.endswith(b'\n'):
                raise ValueError(f'Incomplete DHDL line: {path}')
            line = raw.decode('utf-8').strip()
            if not line:
                continue
            if line.startswith(('#', '@')):
                if samples:
                    raise ValueError(f'Unexpected header/restart inside DHDL: {path}')
                header.append(line)
                continue
            parts = line.split()
            values = [finite(part) for part in parts]
            if width is None:
                width = len(parts)
            if len(parts) != width or width < 2:
                raise ValueError(f'Inconsistent DHDL columns: {path}')
            time = Decimal(parts[0])
            if (previous is None and time != 0) or (previous is not None and time - previous != stride):
                raise ValueError(f'Missing, duplicate or out-of-order DHDL sample: {path} at {time} ps')
            previous = time
            samples += 1
            through += int(time <= cutoff_ps)
    if before != stamp(path):
        raise ValueError(f'DHDL changed during validation: {path}')
    if previous is None or previous < cutoff_ps or cutoff_ps % stride:
        raise ValueError(f'DHDL does not contain the exact requested cutoff {cutoff_ps} ps: {path}')
    if previous > protocol()['maximum_ps']:
        raise ValueError(f'Data exceed the 50 ns cap; preserve and review: {path}')
    text = '\n'.join(header)
    if not re.search(r'xaxis\s+label\s+"Time\s*\(ps\)"', text, re.I):
        raise ValueError(f'DHDL time unit must be explicitly ps: {path}')
    match = re.search(r'T\s*=\s*([\d.eE+-]+)\s*\(K\).*state\s+(\d+):\s*\(([^)]+)\)\s*=\s*\(([^)]+)\)', text)
    if not match or int(match[2]) != state:
        raise ValueError(f'DHDL lambda-state does not match its window: {path}')
    temperature = finite(match[1])
    components = [name.strip().removesuffix('-lambda') for name in match[3].split(',')]
    arrays = [[finite(x) for x in mdp[name + '-lambdas'].split()] for name in components]
    if any(len(array) != count for array in arrays):
        raise ValueError(f'MDP lambda schedule does not cover all {count} windows: {path}')
    vectors = [list(row) for row in zip(*arrays)]
    if not close_values([finite(x) for x in match[4].split(',')], vectors[state]):
        raise ValueError(f'DHDL lambda vector differs from MDP: {path}')
    energies = re.findall(r'legend\s+"[^"\n]*H[^"\n]*\bto\s+\(([^)]+)\)"', text)
    if len(energies) != count or any(not close_values([finite(x) for x in row.split(',')], vector)
                                     for row, vector in zip(energies, vectors)):
        raise ValueError(f'DHDL energy columns omit/misidentify lambda endpoints: {path}')
    legends = re.findall(r'@\s+s(\d+)\s+legend', text)
    if len(legends) != width - 1 or set(map(int, legends)) != set(range(width - 1)):
        raise ValueError(f'DHDL legends and numeric columns disagree: {path}')
    if temperature <= 0 or any(not math.isclose(finite(t), temperature, abs_tol=1e-6) for t in mdp['ref-t'].split()):
        raise ValueError(f'MDP/DHDL temperatures differ: {path}')
    return dict(path=str(Path(path).resolve()), source_path=str(Path(path).absolute()), stamp=before, sha256=sha.hexdigest(), samples=samples,
                samples_through_cutoff=through, endpoint_ps=float(previous), state=state,
                temperature_K=temperature, lambda_components=components, lambda_vectors=vectors)


def validate_inputs(spec, cutoff_ps):
    from .campaign import target_duration
    p = protocol()
    cutoff_ps = finite(cutoff_ps)
    if cutoff_ps <= 0 or cutoff_ps > p['maximum_ps']:
        raise ValueError('Analysis cutoff must be positive and at most 50000 ps')
    if spec['leg'] not in ('complex', 'solvent') or not spec.get('path'):
        raise ValueError('Analysis needs an existing alchemical leg')
    root = Path(spec['path'])
    if set(spec['windows']) - set(p['phase_order']):
        raise ValueError('Unrecognized window phase; provide a reviewed protocol')
    expected = [f'{phase}.{i:02d}' for phase in p['phase_order'] for i in range(spec['windows'].get(phase, 0))]
    observed = {path.name for path in root.iterdir() if path.is_dir() and re.fullmatch(r'[a-z]+\.\d+', path.name)}
    if not expected or observed != set(expected):
        raise ValueError(f'Window inventory mismatch: missing={sorted(set(expected)-observed)}, extra={sorted(observed-set(expected))}')
    inputs = [fingerprint(path) for path in spec['input_files']]
    windows, targets = [], []
    for state, name in enumerate(expected):
        prod = root / name / 'prod'
        mdp_path = prod / f'{name.split(".")[0]}.prod.{name.split(".")[1]}.mdp'
        mdp_identity = fingerprint(mdp_path)
        mdp = settings(mdp_path)
        if int(mdp['init-lambda-state']) != state:
            raise ValueError(f'Incorrect MDP state assignment: {name}')
        if any(Decimal(mdp.get(key, '0')) != 0 for key in ('tinit', 'init-step')):
            raise ValueError('Nonzero production origin needs explicit reviewed mapping')
        targets.append(target_duration(mdp_path) * 1000)
        if targets[-1] > p['maximum_ps']:
            raise ValueError('Configured target exceeds the 50 ns cap; preserve and review')
        inputs.extend([mdp_identity, fingerprint(prod / 'topol.tpr')])
        window = read_window(prod / 'dhdl.xvg', mdp, state, len(expected), Decimal(str(cutoff_ps)))
        window['name'] = name
        windows.append(window)
    if len(set(targets)) != 1 or cutoff_ps > targets[0]:
        raise ValueError('Missing/inconsistent targets or analysis cutoff beyond configured target')
    first = windows[0]
    if any(any(w[key] != first[key] for key in ('temperature_K', 'lambda_components', 'lambda_vectors')) for w in windows):
        raise ValueError('Inconsistent temperature or lambda schedule across windows')
    for item in inputs + windows:
        if stamp(item['path']) != item['stamp']:
            raise ValueError('Inputs changed while other windows were being validated')
    return dict(spec_sha256=digest(spec), protocol=p, protocol_sha256=digest(p), cutoff_ps=cutoff_ps,
                target_ps=targets[0], temperature_K=first['temperature_K'], windows=windows, inputs=inputs)


def half_difference(path):
    with Path(path).open(newline='') as stream:
        reader = csv.DictReader(stream)
        if not {'Forward', 'Backward', 'data_fraction'} <= set(reader.fieldnames or []):
            raise ValueError('Missing convergence columns')
        halves = [row for row in reader if finite(row['data_fraction']) == 0.5]
    if len(halves) != 1:
        raise ValueError('Expected exactly one half-fraction row')
    return finite(abs(finite(halves[0]['Forward']) - finite(halves[0]['Backward'])))


def decide(cutoff_ps, target_ps, difference=None, blocked=None):
    p = protocol()
    if blocked:
        return dict(action='blocked', reason=str(blocked), next_target_ns=None)
    cutoff_ps, target_ps = finite(cutoff_ps), finite(target_ps)
    if not 0 < cutoff_ps <= target_ps <= p['maximum_ps']:
        raise ValueError('Invalid cutoff/target or production beyond the cap')
    if cutoff_ps < p['minimum_ps']:
        return dict(action='continue_target', reason='Below the 16 ns analysis minimum', next_target_ns=target_ps / 1000)
    if difference is None:
        raise ValueError('An eligible decision requires a valid half-difference')
    difference = finite(difference)
    if difference < 0:
        raise ValueError('An eligible decision requires a valid half-difference')
    if difference < p['half_difference_kcal_mol']:
        return dict(action='stop_converged', reason='Absolute MBAR half-difference <0.5 kcal/mol', next_target_ns=None)
    if cutoff_ps == p['maximum_ps']:
        return dict(action='stop_at_limit', reason='Unconverged at 50 ns; needs attention', next_target_ns=None)
    next_target = min(cutoff_ps + p['increment_ps'], p['maximum_ps'])
    if next_target <= target_ps:
        return dict(action='blocked', reason='The cutoff +5 ns target does not extend the configured target; review continuation versus reanalysis', next_target_ns=next_target / 1000)
    return dict(action='propose_extension', reason='Eligible unconverged analysis; analysed cutoff +5 ns', next_target_ns=next_target / 1000)


def verify(record, spec, rehash=False):
    """A report's cheap metadata check is never permission to execute a decision."""
    evidence = record['evidence']
    if digest(spec) != evidence['spec_sha256'] or digest(protocol()) != evidence['protocol_sha256']:
        raise ValueError('Run mapping or analysis protocol changed')
    if record['software']['analysis_code_sha256'] != hashlib.sha256(Path(__file__).read_bytes()).hexdigest():
        raise ValueError('Analysis implementation changed; explicit reanalysis/review required')
    expected = {w['name'] for w in evidence['windows']}
    observed = {path.name for path in Path(spec['path']).iterdir() if path.is_dir() and re.fullmatch(r'[a-z]+\.\d+', path.name)}
    if observed != expected:
        raise ValueError('Window inventory changed')
    for window in evidence['windows']:
        if str((Path(spec['path']) / window['name'] / 'prod/dhdl.xvg').resolve(strict=True)) != window['path']:
            raise ValueError('Window source mapping changed')
    items = evidence['inputs'] + evidence['windows'] + record.get('artifacts', []) + record['software'].get('source_files', [])
    for item in items:
        if str(Path(item['source_path']).resolve(strict=True)) != item['path']:
            raise ValueError('Source symlink mapping changed')
        if stamp(item['path']) != item['stamp']:
            raise ValueError(f'Input/output snapshot changed: {item["path"]}')
        if rehash and fingerprint(item['path'])['sha256'] != item['sha256']:
            raise ValueError(f'Input/output content changed: {item["path"]}')
    if any(stamp(item['path']) != item['stamp'] for item in items):
        raise ValueError('Inputs changed during integrity verification')


def run_analysis(evidence, output):
    """Use alchemlyb's existing preprocessing, loading the validated files in place."""
    from functools import partial
    import numpy as np
    import matplotlib
    matplotlib.use('Agg')
    from matplotlib import pyplot as plt
    from alchemlyb.workflows import ABFE
    from alchemlyb.parsing.gmx import extract_u_nk, extract_dHdl
    p = evidence['protocol']
    workflow = ABFE(T=evidence['temperature_K'], units='kcal/mol', software='GROMACS',
                    dir=str(Path(evidence['windows'][0]['path']).parent), prefix='dhdl', suffix='xvg', outdirectory=str(output))
    workflow.file_list = [w['path'] for w in evidence['windows']]
    workflow._extract_u_nk = partial(extract_u_nk, filter=False)
    workflow._extract_dHdl = partial(extract_dHdl, filter=False)
    workflow.read()
    for attribute in ('u_nk_list', 'dHdl_list'):
        setattr(workflow, attribute, [frame.loc[frame.index.get_level_values('time') <= evidence['cutoff_ps']]
                                      for frame in getattr(workflow, attribute)])
    workflow.preprocess(skiptime=p['discard_ps'], uncorr=p['uncorr'], threshold=p['threshold'])
    random_state = np.random.get_state()
    try:
        np.random.seed(p['seed'])
        estimates = {}
        for method in p['methods']:
            workflow.estimate((method,), **({'n_bootstraps': p['n_bootstraps']} if method == 'MBAR' else {}))
            estimates.update(workflow.estimator)
        workflow.estimator = estimates
        workflow.generate_result()
        workflow.summary.to_csv(output / 'ABFE_summary.csv')
        workflow.check_convergence(p['fractions'], estimator='MBAR', n_bootstraps=p['n_bootstraps'])
        workflow.convergence.to_csv(output / 'ABFE_convergence.csv')
        workflow.plot_overlap_matrix()
        workflow.plot_ti_dhdl()
        workflow.plot_dF_state()
        total = workflow.summary.loc[('Stages', 'TOTAL')]
        sources = [fingerprint(sys.modules[name].__file__) for name in (
            'alchemlyb.workflows.abfe', 'alchemlyb.parsing.gmx', 'alchemlyb.estimators.mbar_',
            'alchemlyb.estimators.bar_', 'alchemlyb.estimators.ti_', 'alchemlyb.preprocessing.subsampling',
            'alchemlyb.convergence.convergence', 'pymbar.mbar')]
        return dict(software_sources=sources, estimate_kcal_mol=finite(total['MBAR']), uncertainty_kcal_mol=finite(total['MBAR_Error']),
                    retained_samples=[len(frame) for frame in workflow.u_nk_sample_list])
    finally:
        np.random.set_state(random_state)
        plt.close('all')


def tables(db):
    db.execute('CREATE TABLE IF NOT EXISTS analyses (id TEXT PRIMARY KEY, run_id TEXT NOT NULL, created_at TEXT NOT NULL, record TEXT NOT NULL)')
    for operation in ('UPDATE', 'DELETE'):
        db.execute(f"CREATE TRIGGER IF NOT EXISTS immutable_analysis_{operation.lower()} BEFORE {operation} ON analyses BEGIN SELECT RAISE(ABORT, 'Analysis records are immutable'); END")


def persist(registry, record, output):
    record['finished_at'] = timestamp()
    with (output / 'record.json').open('x') as stream:
        stream.write(json.dumps(record, indent=2, sort_keys=True, allow_nan=False) + '\n')
    with closing(sqlite3.connect(registry)) as db, db:
        tables(db)
        db.execute('INSERT INTO analyses VALUES (?,?,?,?)', (record['id'], record['run_id'], record['created_at'], encode(record)))
        if record['state'] == 'failed' or record.get('decision', {}).get('action') in ('stop_at_limit', 'blocked'):
            message = record.get('error') or record['decision']['reason']
            db.execute("""INSERT INTO incidents(run_id,code,message,first_seen,last_seen) VALUES (?,?,?,?,?)
                ON CONFLICT(run_id,code) DO UPDATE SET message=excluded.message,last_seen=excluded.last_seen""",
                       (record['run_id'], 'analysis:' + record['id'], message, record['created_at'], record['created_at']))


def latest(db, run_id, kind='leg'):
    if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='analyses'").fetchone():
        return None
    planned = None
    for row in db.execute('SELECT record FROM analyses WHERE run_id=? ORDER BY rowid DESC', (run_id,)):
        record = json.loads(row[0])
        if record['kind'] != kind:
            continue
        if record['state'] != 'planned':
            return record
        if planned is None:
            planned = record
    return planned


def selected(registry, run_id):
    from .campaign import read_registry
    metadata_, rows = read_registry(registry)
    row = next((row for row in rows if row['spec']['id'] == run_id), None)
    if row is None:
        raise ValueError(f'Unknown run: {run_id}')
    return metadata_, rows, row


def analyse(registry, run_id, cutoff_ns, plan=False):
    from .campaign import check_destination, presentation
    registry = Path(registry)
    cutoff_ns = finite(cutoff_ns)
    if cutoff_ns <= 0:
        raise ValueError('Cutoff must be positive')
    metadata_, rows, row = selected(registry, run_id)
    check_destination(registry, metadata_, [r['spec'] for r in rows])
    output_root = registry.parent / 'analyses'
    check_destination(output_root, metadata_, [r['spec'] for r in rows])
    blocking = [item for item in row['incidents'] if item['status'] == 'open']
    if blocking:
        return dict(action='blocked', reason='Open incidents require explicit resolution: ' +
                    ', '.join(item['code'] for item in blocking), next_target_ns=None)
    if presentation(row, metadata_)['jobs']:
        return dict(action='blocked', reason='A fresh scheduler match exists; establish a stable local snapshot first', next_target_ns=None)
    with (registry.parent / 'analysis.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ValueError('Another local analysis is active') from exc
        output = output_root / uuid.uuid4().hex
        output.mkdir(parents=True)
        record = dict(id=output.name, run_id=run_id, created_at=timestamp(), kind='leg', state='failed',
                      output=str(output.resolve()), requested_cutoff_ns=finite(cutoff_ns),
                      open_incidents=[item for item in row['incidents'] if item['status'] == 'open'],
                      software={'python': sys.version, 'executable': sys.executable, 'platform': platform.platform(),
                                'analysis_code_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest()})
        try:
            evidence = validate_inputs(row['spec'], finite(cutoff_ns) * 1000)
            record['evidence'] = evidence
            if plan or evidence['cutoff_ps'] < protocol()['minimum_ps']:
                record['state'] = 'planned'
                record['decision'] = (dict(action='analyse', reason='Validated input plan; no estimator has run', next_target_ns=None)
                                      if evidence['cutoff_ps'] >= protocol()['minimum_ps'] else decide(evidence['cutoff_ps'], evidence['target_ps']))
            else:
                for name in ('alchemlyb', 'pymbar', 'numpy', 'scipy', 'pandas', 'jax', 'jaxlib'):
                    try:
                        record['software'][name] = metadata.version(name)
                    except metadata.PackageNotFoundError:
                        record['software'][name] = 'not installed'
                from loguru import logger
                with (output / 'analysis.log').open('x') as log, redirect_stdout(log), redirect_stderr(log):
                    sink = logger.add(log)
                    try:
                        record['result'] = run_analysis(evidence, output)
                    finally:
                        logger.remove(sink)
                result = record['result']
                record['software']['source_files'] = result.pop('software_sources', [])
                if finite(result['uncertainty_kcal_mol']) < 0:
                    raise ValueError('Negative MBAR uncertainty')
                finite(result['estimate_kcal_mol'])
                record['half_difference_kcal_mol'] = half_difference(output / 'ABFE_convergence.csv')
                record['artifacts'] = [fingerprint(path) for path in sorted(output.iterdir()) if path.is_file()]
                verify(record, row['spec'], rehash=True)
                record['decision'] = decide(evidence['cutoff_ps'], evidence['target_ps'], record['half_difference_kcal_mol'])
                record['state'] = 'validated'
        except Exception as exc:
            record['error'] = f'{type(exc).__name__}: {exc}'
            record['decision'] = decide(0, 0, blocked=record['error'])
            persist(registry, record, output)
            raise ValueError(f'Analysis failed; incident recorded ({record["id"]}): {record["error"]}') from exc
        persist(registry, record, output)
        return record


def current_decision(registry, run_id):
    metadata_, _, row = selected(registry, run_id)
    from .campaign import presentation
    if presentation(row, metadata_)['jobs']:
        return decide(0, 0, blocked='A fresh scheduler match exists; establish stable local data first')
    record = row.get('analysis_record')
    if not record or (record['state'] != 'validated' and not (record['state'] == 'planned' and record['evidence']['cutoff_ps'] < protocol()['minimum_ps'])):
        return decide(0, 0, blocked='No validated analysis; run analyse --plan')
    if row['incidents']:
        for detected in row['incidents']:
            if detected.get('detected_in_report'):
                incident(registry, run_id, detected['code'], detected['message'])
        return decide(0, 0, blocked='Open incidents require explicit resolution')
    try:
        verify(record, row['spec'], rehash=True)
        evidence = record['evidence']
        difference = (half_difference(Path(record['output']) / 'ABFE_convergence.csv')
                      if record['state'] == 'validated' else None)
        return dict(analysis_id=record['id'], **decide(evidence['cutoff_ps'], evidence['target_ps'], difference))
    except (OSError, ValueError) as exc:
        incident(registry, run_id, 'stale_analysis:' + record['id'], str(exc))
        return decide(0, 0, blocked=f'Stale/invalid analysis: {exc}')


def attach(db, rows):
    """Overlay immutable records; never replace the inventory's stored observations."""
    for row in rows:
        imported = latest(db, row['spec']['id'], kind='historical')
        if imported:
            row['historical_analysis'] = imported
        record = latest(db, row['spec']['id'])
        if imported and not record:
            observed = row['observation'] = dict(row['observation'] or {})
            observed['analysis'] = dict(state='historical_imported', analysed_ns=imported.get('analysed_ns'), convergence=imported.get('convergence', 'unverified'), id=imported['id'])
        if not record:
            continue
        row['analysis_record'] = record
        observed = row['observation'] = dict(row['observation'] or {})
        analysis = dict(id=record['id'], state=record['state'], analysed_ns=None, convergence='unverified',
                        decision=record.get('decision'), output=record['output'], freshness='unchecked')
        if record['state'] == 'validated':
            analysis.update(analysed_ns=record['evidence']['cutoff_ps'] / 1000,
                            half_difference_kcal_mol=record['half_difference_kcal_mol'])
            try:
                verify(record, row['spec'])
                analysis.update(state='validated', freshness='metadata unchanged; content recheck required before action',
                                convergence='converged' if record['decision']['action'] == 'stop_converged' else 'unconverged')
            except (OSError, ValueError) as exc:
                analysis.update(state='stale', freshness=str(exc), decision=decide(0, 0, blocked=str(exc)))
                code = 'stale_analysis:' + record['id']
                if not any(item['code'] == code for item in row['incidents']):
                    row['incidents'].append(dict(run_id=row['spec']['id'], code=code, message=str(exc), status='open',
                                                 first_seen=timestamp(), last_seen=timestamp(), detected_in_report=True))
        observed['analysis'] = analysis
        observed['next_action'] = ('Inspect failed/stale analysis and incidents' if analysis['state'] in ('failed', 'stale') else
                                   'Run decide to verify source/output hashes before using the recorded decision')

    for row in rows:
        bound = latest(db, row['spec']['id'], kind='binding')
        if bound:
            row['binding_result'] = binding_state(bound, rows)


def check_compatibility(path, complex_record, solvent_record):
    """Consume an explicit review tied to exact analysis IDs, never infer reuse."""
    identity = fingerprint(path)
    review = json.loads(Path(path).read_text())
    if review.get('schema_version') != 1 or not isinstance(review.get('reviewed_by'), str) or not review['reviewed_by'].strip():
        raise ValueError('Solvent reuse needs a named, explicit compatibility review')
    if review.get('complex_analysis_id') != complex_record['id'] or review.get('solvent_analysis_id') != solvent_record['id']:
        raise ValueError('Compatibility review references different analysis versions')
    keys = ('ligand_chemical_state', 'ligand_parameters', 'force_fields', 'solvent_conditions', 'temperature_K', 'charge_treatment')
    for key in keys:
        a, b = review['complex'].get(key), review['solvent'].get(key)
        if not a or a != b or (isinstance(a, str) and a.strip().lower() in ('unknown', 'unverified')):
            raise ValueError(f'Unknown/incompatible solvent reuse: {key}')
    if finite(review['complex']['temperature_K']) != complex_record['evidence']['temperature_K'] or complex_record['evidence']['temperature_K'] != solvent_record['evidence']['temperature_K']:
        raise ValueError('Compatibility review temperature differs from analysed inputs')
    evidence = review.get('evidence', [])
    if not evidence:
        raise ValueError('Compatibility review needs parameter/preparation file hashes')
    identities = [identity]
    for item in evidence:
        actual = fingerprint(item['path'])
        if actual['sha256'] != item['sha256']:
            raise ValueError('Compatibility review evidence changed')
        identities.append(actual)
    return identities


def binding(registry, run_id, compatibility):
    from .campaign import check_destination
    metadata_, rows, row = selected(registry, run_id)
    spec = row['spec']
    if spec['leg'] != 'complex':
        raise ValueError('Binding results require a complex repeat')
    solvent = next((other for other in rows if other['spec']['id'] in spec['dependencies'] and other['spec']['leg'] == 'solvent'), None)
    if solvent is None:
        raise ValueError('Missing registered shared solvent dependency')
    output = Path(registry).parent / 'analyses' / uuid.uuid4().hex
    check_destination(output, metadata_, [r['spec'] for r in rows])
    if any(item['status'] == 'open' for r in (row, solvent) for item in r['incidents']):
        raise ValueError('Open incidents block binding compilation')
    output.mkdir(parents=True)
    record = dict(id=output.name, run_id=run_id, created_at=timestamp(), kind='binding', state='failed', output=str(output.resolve()))
    try:
        records = [r.get('analysis_record') for r in (row, solvent)]
        for r, analysis in zip((row, solvent), records):
            if not analysis or analysis['state'] != 'validated' or analysis['decision']['action'] != 'stop_converged':
                raise ValueError('Both legs need their own validated converged analysis')
            verify(analysis, r['spec'], rehash=True)
        complex_record, solvent_record = records
        identities = check_compatibility(compatibility, complex_record, solvent_record)
        correction_path = Path(spec['path']).parent / 'boresch/resgen_output.txt'
        identities.append(fingerprint(correction_path))
        corrections = re.findall(r'^dG_off:\s*([^,\s]+)', correction_path.read_text(), re.M)
        if len(corrections) != 1:
            raise ValueError('Expected one MDRestraintsGenerator dG_off correction in kcal/mol')
        correction = finite(corrections[0])
        c, s = complex_record['result'], solvent_record['result']
        shared_variance = finite(s['uncertainty_kcal_mol']) ** 2
        for r, leg_record in zip((row, solvent), records):
            verify(leg_record, r['spec'], rehash=True)
        for item in identities:
            if fingerprint(item['path']) != item:
                raise ValueError('Compatibility/correction evidence changed during compilation')
        record.update(state='validated', dependencies={row['spec']['id']: complex_record['id'], solvent['spec']['id']: solvent_record['id']},
                      evidence=dict(inputs=identities), result=dict(
                          estimate_kcal_mol=finite(s['estimate_kcal_mol'] + correction - c['estimate_kcal_mol']),
                          uncertainty_kcal_mol=finite(math.sqrt(c['uncertainty_kcal_mol'] ** 2 + shared_variance)),
                          restraint_correction_kcal_mol=correction, shared_solvent_analysis_id=solvent_record['id'],
                          shared_solvent_variance=shared_variance,
                          uncertainty_note='Solvent variance is shared across repeats; do not divide it by the number of complex repeats'))
    except Exception as exc:
        record['error'] = f'{type(exc).__name__}: {exc}'
        persist(registry, record, output)
        raise ValueError(f'Binding failed; incident recorded ({record["id"]}): {record["error"]}') from exc
    persist(registry, record, output)
    return record


def binding_state(record, rows):
    if record['state'] != 'validated':
        return dict(state='failed', id=record['id'], reason=record.get('error'))
    by_id = {row['spec']['id']: row for row in rows}
    try:
        for run_id, analysis_id in record['dependencies'].items():
            row = by_id[run_id]
            analysis = row.get('analysis_record')
            if not analysis or analysis['id'] != analysis_id or row['incidents']:
                raise ValueError('A dependent analysis changed or has open incidents')
            verify(analysis, row['spec'])
        for item in record['evidence']['inputs']:
            if stamp(item['path']) != item['stamp']:
                raise ValueError('Compatibility/correction evidence changed')
        return dict(state='validated_snapshot', id=record['id'], result=record['result'], dependencies=record['dependencies'])
    except (OSError, ValueError, KeyError) as exc:
        return dict(state='outdated', id=record['id'], reason=str(exc))


def incident(registry, run_id, code, message):
    at = timestamp()
    with closing(sqlite3.connect(registry)) as db, db:
        db.execute("""INSERT INTO incidents(run_id,code,message,first_seen,last_seen) VALUES (?,?,?,?,?)
            ON CONFLICT(run_id,code) DO UPDATE SET message=excluded.message,last_seen=excluded.last_seen""",
                   (run_id, code, message, at, at))


def historical_csv(path):
    with Path(path).open(newline='') as stream:
        table = list(csv.reader(stream, strict=True))
    if len(table) < 2:
        raise ValueError(f'Empty historical CSV: {path}')
    columns, rows = table[0], table[1:]
    names = [name for name in columns if name]
    if len(names) != len(set(names)):
        raise ValueError(f'Duplicate CSV columns: {path}')
    convergence = Path(path).name == 'ABFE_convergence.csv'
    required = {'Forward', 'Backward', 'data_fraction'} if convergence else {'MBAR', 'MBAR_Error'}
    if not required <= set(columns):
        raise ValueError(f'Missing result columns: {path}')
    numeric = {i: name for i, name in enumerate(columns) if name in
               {'MBAR', 'BAR', 'TI', 'MBAR_Error', 'BAR_Error', 'TI_Error',
                'Forward', 'Backward', 'Forward_Error', 'Backward_Error', 'data_fraction'}}
    for row in rows:
        if len(row) != len(columns):
            raise ValueError(f'Inconsistent CSV row width: {path}')
        for i, name in numeric.items():
            value = finite(row[i])
            if (name.endswith('_Error') and value < 0) or (name == 'data_fraction' and not 0 < value <= 1):
                raise ValueError(f'Invalid {name}: {path}')
    labels = [i for i in range(len(columns)) if i not in numeric]
    keys = [tuple(row[i] for i in labels) for row in rows]
    if not convergence and labels and len(keys) != len(set(keys)):
        raise ValueError(f'Duplicate result rows: {path}')
    result = dict(columns=columns, rows=rows)
    if convergence:
        fractions = [finite(row[columns.index('data_fraction')]) for row in rows]
        if len(fractions) != len(set(fractions)):
            raise ValueError(f'Duplicate convergence fractions: {path}')
        result['reported_absolute_half_difference'] = half_difference(path)
    return result


def import_analysis(registry, run_id, plan=False):
    """Archive small historical result tables separately from validated analyses."""
    from .campaign import check_destination
    registry = Path(registry)
    metadata_, rows, row = selected(registry, run_id)
    spec = row['spec']
    if spec['leg'] not in ('complex', 'solvent') or not spec.get('path'):
        raise ValueError('Import requires a mapped complex or solvent run')
    root = Path(spec['path']).parent / 'analysis'
    directory = root / ('complex' if spec['leg'] == 'complex' else 'ligand')

    def collect():
        paths = [directory / name for name in ('ABFE_summary.csv', 'ABFE_convergence.csv')]
        if spec['leg'] == 'complex':
            paths.append(root / 'abfe_results.csv')
        imported = []
        for path in paths:
            if path.is_file():
                identity = fingerprint(path)
                imported.append(dict(identity=identity, table=historical_csv(path)))
        if not imported:
            raise ValueError(f'No historical result CSVs found for {run_id}')
        solvent = root / 'ligand'
        references = [fingerprint(path) for name in ('ABFE_summary.csv', 'ABFE_convergence.csv')
                      if (path := solvent / name).is_file()] if spec['leg'] == 'complex' else []
        identities = [item['identity'] for item in imported] + references
        for identity in identities:
            if fingerprint(identity['source_path']) != identity:
                raise ValueError('Historical sources changed during import')
        sources = [dict(path=item['path'], sha256=item['sha256']) for item in identities]
        return dict(id='historical-' + digest(dict(run_id=run_id, mapping=digest(spec), sources=sources))[:32],
                    run_id=run_id, kind='historical', state='historical_unverified', analysed_ns=None,
                    source_mapping_sha256=digest(spec), processing=None, units=None,
                    uncertainty_convention='legacy_as_reported', files=imported,
                    shared_solvent=dict(path=str(solvent.resolve()), files=references, compatibility='unverified') if references else None)

    if plan:
        return dict(action='preview', **collect())
    output_root = registry.parent / 'analyses'
    check_destination(registry, metadata_, [r['spec'] for r in rows])
    check_destination(output_root, metadata_, [r['spec'] for r in rows])
    if any(item['status'] == 'open' and item['code'] == 'analysis_import' for item in row['incidents']):
        raise ValueError('Resolve the previous analysis_import incident before retrying this import')
    with (registry.parent / 'analysis.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            record = collect()
            with closing(sqlite3.connect(registry)) as db:
                if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='analyses'").fetchone():
                    existing = db.execute('SELECT record FROM analyses WHERE id=?', (record['id'],)).fetchone()
                    if existing:
                        return dict(action='already_imported', **json.loads(existing[0]))
            output = output_root / record['id']
            if output.exists():
                raise ValueError('An unregistered import directory exists; review it before retrying')
            record.update(created_at=timestamp(), output=str(output.resolve()))
            output.mkdir(parents=True)
            persist(registry, record, output)
            return dict(action='imported', **record)
        except (OSError, ValueError, KeyError, TypeError, csv.Error, sqlite3.Error) as exc:
            incident(registry, run_id, 'analysis_import', str(exc))
            raise ValueError(f'Historical import blocked; analysis_import incident recorded: {exc}') from exc
