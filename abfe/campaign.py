"""Campaign registry, console and explicit workflow commands."""

import argparse
import base64
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import csv
from datetime import datetime, timezone
import hashlib
import html
from importlib.resources import files
import json
import math
from pathlib import Path, PurePosixPath
import re
import sqlite3
import subprocess
import sys
import fcntl
import os
import tempfile

from .cluster_capacity import parse_capacity
from .cluster_monitor import parse_limits, parse_ownership, sample_slot
from .cluster_view import cluster_cards


SCHEMA_VERSION = 1
CLUSTER_ROOTS = {"archer": "/work/e280/e280-Biggin/reub0138", "ranvier": "/home/reub0138", "soma": "/home/reub0138"}
CONNECTION_MAX_AGE_SECONDS = 300
DATASET_COLORS = {"deflorian-set1": "#0fffdb", "piersanti": "#ffd166", "yuan": "#9500ff",
                  "heffernan": "#ff0059", "thoma": "#ff8c00", "sigma2": "#b4e600"}
STAGES = {"not_started": ("Not started", "#ffd166"), "inputs": ("Inputs ready", "#0fffdb"),
          "prepared": ("Prepared", "#9500ff"), "queued": ("Queued", "#ffd166"),
          "equilibration": ("Equilibrating", "#ff8c00"), "production": ("Production observed", "#ff0059"),
          "analysis": ("Analysis on file", "#0fffdb"), "converged": ("Converged", "#b4e600"),
          "complete": ("Complete", "#0fffdb"), "unknown": ("Unknown", "#8a9199")}
REMARK_COLORS = {"Attention required": "#ff0059", "Waiting": "#ff8c00", "OK": "#0fffdb", "Unknown": "#8a9199"}


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def encode(value):
    return json.dumps(value, sort_keys=True, allow_nan=False)


def source_path(root, value):
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError("Source paths must be nonempty strings or null")
    path = Path(value)
    if ".." in path.parts:
        raise ValueError("Source paths must not contain '..'")
    return str((root / path).resolve())


def load_manifest(path, source_root=None):
    manifest = json.loads(Path(path).read_text())
    if manifest.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("Unsupported manifest schema_version")
    root = Path(source_root or manifest["source_root"]).expanduser()
    if not root.is_absolute() or not root.is_dir():
        raise ValueError("source_root must be an existing absolute directory")
    root = root.resolve()
    datasets, specs, seen = [], [], set()
    for dataset in manifest["datasets"]:
        dataset_id = dataset["id"]
        if not re.fullmatch(r"[A-Za-z0-9_-]+", dataset_id) or dataset_id in seen:
            raise ValueError("Dataset IDs must be unique simple names")
        seen.add(dataset_id)
        ligands = dataset["ligands"]
        expected = dataset["expected_ligands"]
        if expected is not None and (type(expected) is not int or expected < 1 or expected != len(ligands)):
            raise ValueError(f"{dataset_id}: expected_ligands must match the explicit ligand list")
        datasets.append({"id": dataset_id, "label": dataset["label"], "expected_ligands": expected,
                         "registered_ligands": len(ligands)})
        ligand_ids = set()
        for entry in ligands:
            ligand = {"id": entry} if isinstance(entry, str) else entry
            ligand_id = ligand["id"]
            if not re.fullmatch(r"[A-Za-z0-9_-]+", ligand_id) or ligand_id in ligand_ids:
                raise ValueError(f"{dataset_id}: ligand IDs must be unique simple names")
            ligand_ids.add(ligand_id)
            for key in ("vanilla_paths", "complex_paths"):
                if any(repeat not in ("1", "2", "3") for repeat in ligand.get(key, {})):
                    raise ValueError(f"{dataset_id}/{ligand_id}: {key} may only map repeats 1, 2, 3")
            directory = ligand.get("directory")
            if directory is None and dataset.get("directory"):
                directory = f"{dataset['directory']}/{ligand_id}"
            base = source_path(root, directory)
            input_stem = ligand_id.removeprefix(dataset.get("ligand_prefix", ""))
            inputs = ligand.get("inputs", [])
            if dataset.get("input_directory"):
                inputs = inputs + [f"{dataset['input_directory']}/{input_stem}.sdf"]
            inputs = [source_path(root, item) for item in inputs]
            solvent = source_path(root, ligand.get("solvent_path"))
            if solvent is None and base:
                solvent = str(Path(base) / "abfe_van1_hrex_r1/ligand")
            solvent_id = f"{dataset_id}/{ligand_id}/solvent"
            common = {"dataset": dataset_id, "ligand": ligand_id, "input_files": inputs,
                      "ligand_path": base, "source_root": str(root)}
            for repeat in (1, 2, 3):
                vanilla = source_path(root, ligand.get("vanilla_paths", {}).get(str(repeat)))
                complex_path = source_path(root, ligand.get("complex_paths", {}).get(str(repeat)))
                if base:
                    vanilla = vanilla or str(Path(base) / f"vanilla_rep_{repeat}")
                    complex_path = complex_path or str(Path(base) / f"abfe_van{repeat}_hrex_r1/complex")
                for leg, location, windows, dependencies in (
                    ("vanilla", vanilla, {}, []),
                    ("complex", complex_path, dataset.get("complex_windows", {}),
                     [f"{dataset_id}/{ligand_id}/vanilla/{repeat}", solvent_id]),
                ):
                    specs.append(dict(common, id=f"{dataset_id}/{ligand_id}/{leg}/{repeat}", leg=leg,
                                      repeat=repeat, path=location,
                                      windows=windows, dependencies=dependencies))
            specs.append(dict(common, id=solvent_id, leg="solvent", repeat=None, path=solvent,
                              windows=dataset.get("solvent_windows", {}),
                              dependencies=[], shared_by=[f"{dataset_id}/{ligand_id}/complex/{r}" for r in (1, 2, 3)],
                              compatibility="unverified"))
    if not datasets:
        raise ValueError("Manifest needs at least one dataset")
    used_paths = {}
    for spec in specs:
        if spec["leg"] != "vanilla":
            if not spec["windows"] or any(not re.fullmatch(r"[a-z]+", phase) or type(count) is not int or count < 1
                                          for phase, count in spec["windows"].items()):
                raise ValueError(f"{spec['id']}: explicit positive window counts are required")
        if spec["path"]:
            canonical = str(Path(spec["path"]).resolve())
            if canonical in used_paths:
                raise ValueError(f"Two runs reference the same source directory: {used_paths[canonical]}, {spec['id']}")
            used_paths[canonical] = spec["id"]
    return {"name": manifest["name"], "datasets": datasets, "source_root": str(root),
            "manifest_path": str(Path(path).resolve()), "manifest_sha256": hashlib.sha256(Path(path).read_bytes()).hexdigest()}, specs


def check_destination(path, metadata, specs):
    destination = Path(path).resolve()
    protected = [Path(metadata["source_root"])]
    protected += [Path(item[key]) for item in specs for key in ("path", "ligand_path", "embedding") if item.get(key)]
    protected += [Path(item) for spec in specs for item in spec["input_files"]]
    if any(destination == item or item in destination.parents for item in protected):
        raise ValueError("Registry/report destination must be outside all simulation and input source trees")
    if destination == Path(metadata["manifest_path"]):
        raise ValueError("Cannot overwrite the manifest")


def initialize(registry, manifest, source_root=None):
    metadata, specs = load_manifest(manifest, source_root)
    check_destination(registry, metadata, specs)
    registry = Path(registry)
    registry.parent.mkdir(parents=True, exist_ok=True)
    # Exclusive creation prevents accidentally opening or overwriting an existing registry.
    with registry.open("xb"):
        pass
    try:
        with closing(sqlite3.connect(registry)) as db, db:
            db.executescript("""
                CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE runs (id TEXT PRIMARY KEY, spec TEXT NOT NULL, observation TEXT, observed_at TEXT);
                CREATE TABLE history (revision INTEGER PRIMARY KEY, run_id TEXT NOT NULL, observed_at TEXT NOT NULL, observation TEXT NOT NULL);
                CREATE TABLE incidents (run_id TEXT NOT NULL, code TEXT NOT NULL, message TEXT NOT NULL,
                    first_seen TEXT NOT NULL, last_seen TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'open',
                    PRIMARY KEY(run_id, code));
            """)
            metadata.update(schema_version=SCHEMA_VERSION, created_at=now(), scanned_at=None)
            db.executemany("INSERT INTO metadata VALUES (?, ?)", [(key, encode(value)) for key, value in metadata.items()])
            db.executemany("INSERT INTO runs(id, spec) VALUES (?, ?)", [(item["id"], encode(item)) for item in specs])
    except Exception:
        registry.unlink(missing_ok=True)
        raise
    return len(specs)


def readonly(registry):
    db = sqlite3.connect(Path(registry).resolve().as_uri() + "?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    db.execute("BEGIN")
    version = db.execute("SELECT value FROM metadata WHERE key='schema_version'").fetchone()
    if version is None or json.loads(version[0]) != SCHEMA_VERSION:
        db.close()
        raise ValueError("Unsupported registry schema")
    return db


def read_registry(registry):
    with closing(readonly(registry)) as db:
        metadata = {row[0]: json.loads(row[1]) for row in db.execute("SELECT key,value FROM metadata")}
        metadata['cluster_history'] = []
        if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='cluster_samples'").fetchone():
            metadata['cluster_history'] = [dict(slot=item[0], cluster=item[1], record=json.loads(item[2])) for item in
                                           db.execute("SELECT slot,cluster,record FROM cluster_samples WHERE slot>=? ORDER BY slot,cluster", (sample_slot()-86400,))]
        rows = []
        for row in db.execute("SELECT * FROM runs ORDER BY id"):
            rows.append({"spec": json.loads(row["spec"]), "observation": json.loads(row["observation"]) if row["observation"] else None,
                         "observed_at": row["observed_at"], "incidents": [dict(item) for item in
                         db.execute("SELECT * FROM incidents WHERE run_id=? AND status!='retired' ORDER BY code", (row["id"],))],
                         "incident_history": [dict(item) for item in
                         db.execute("SELECT * FROM incidents WHERE run_id=? AND status='retired' ORDER BY code", (row["id"],))],
                         "history": [dict(item) for item in db.execute("SELECT revision,observed_at FROM history WHERE run_id=? ORDER BY revision", (row["id"],))]})
        if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='preparations'").fetchone():
            prepared = {}
            for item in db.execute("SELECT compound,record FROM preparations ORDER BY rowid"):
                prepared[item[0]] = json.loads(item[1])
            for row in rows:
                compound = row['spec']['dataset'] + '/' + row['spec']['ligand']
                if compound in prepared:
                    row['preparation'] = prepared[compound]
        for row in rows:
            assignment = metadata.get('equilibration_labels', {}).get(row['spec']['id'])
            if assignment:
                row['equilibration_assignment'] = dict(assignment, kind='vanilla_equilibration_repeat')
        for row in rows:
            if row['observation']:
                row['observation'].pop('embedding', None)
                row['observation']['issues'] = [item for item in row['observation'].get('issues', [])
                    if item['code'] not in ('embedding_provenance', 'missing_embedding', 'shared_embedding_source')]
        specs = {row['spec']['id']: row['spec'] for row in rows}
        for row in rows:
            row['equilibration'] = equilibration_details(row['spec'], specs)
        from .analysis import attach
        attach(db, rows)
        from .execution import attach as attach_executions
        attach_executions(db, rows)
    return metadata, rows


def equilibration_details(spec, specs):
    if spec['leg'] == 'solvent':
        return dict(state='not_applicable', repeat=None, source=None)
    vanilla_id = f"{spec['dataset']}/{spec['ligand']}/vanilla/{spec['repeat']}"
    source = specs.get(vanilla_id, {}).get('path')
    result = dict(state='unverified', repeat=spec['repeat'], source=source)
    if source:
        path = Path(source)
        if any((path / name).is_file() for name in ('nvt.gro', 'npt.gro')):
            result['state'] = 'outputs_on_file'
    return result


def connection_state(record, at=None):
    if not record or not record.get("checked_at"):
        return "unknown"
    try:
        checked = datetime.fromisoformat(record["checked_at"].replace("Z", "+00:00"))
        age = ((at or datetime.now(timezone.utc)) - checked).total_seconds()
    except (ValueError, TypeError):
        return "unknown"
    if age < 0 or age > CONNECTION_MAX_AGE_SECONDS:
        return "stale"
    return record.get("state", "unknown")


def probe_cluster(alias):
    if alias not in CLUSTER_ROOTS:
        raise ValueError("Only configured cluster aliases may be checked")
    command = ["ssh", "-T", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8",
               "-o", "ServerAliveInterval=5", "-o", "ServerAliveCountMax=1", alias]
    result = {"state": "unreachable", "checked_at": now(), "hostname": None,
              "scheduler": {"state": "unknown", "jobs": []}, "capacity": {"state": "unknown"}}
    try:
        host = subprocess.run(command + ["hostname"], capture_output=True, text=True, timeout=15, check=False)
        if host.returncode:
            result["error"] = host.stderr.strip()[-500:] or "SSH command failed"
            return result
        result.update(state="connected", hostname=host.stdout.strip())
        queue = subprocess.run(command + ["squeue --me --array --noheader --format='%i|%T|%Z|%q|%a|%N|%u'"],
                               capture_output=True, text=True, timeout=15, check=False)
        if queue.returncode:
            result["scheduler"] = {"state": "unavailable", "jobs": [], "error": queue.stderr.strip()[-500:]}
        jobs = []
        for line in (queue.stdout.splitlines() if queue.returncode == 0 else []):
            if not line.strip():
                continue
            job_id, state, directory, qos, account, nodes, user = (part.strip() for part in line.split("|"))
            if not re.fullmatch(r"[0-9][0-9_\[\],%+\-]*", job_id) or not directory.startswith("/"):
                raise ValueError("Unrecognized scheduler output")
            jobs.append({"job_id": job_id, "state": state, "work_dir": directory, "qos": qos, "account": account, "nodes": nodes, "user": user})
        if queue.returncode == 0:
            result["scheduler"] = {"state": "observed", "jobs": jobs}
    except (OSError, subprocess.TimeoutExpired, ValueError) as exc:
        if result["state"] == "connected":
            result["scheduler"] = {"state": "unavailable", "jobs": [], "error": str(exc)}
        else:
            result["error"] = str(exc)
    finally:
        result["checked_at"] = now()
    if result["state"] == "connected":
        try:
            nodes = subprocess.run(command + ["scontrol show nodes -d -o"], capture_output=True, text=True, timeout=15, check=False)
            if nodes.returncode:
                raise ValueError(nodes.stderr.strip()[-500:] or "Capacity query failed")
            result["capacity"] = parse_capacity(nodes.stdout, alias)
        except (OSError, subprocess.TimeoutExpired, ValueError, KeyError) as exc:
            result["capacity"] = {"state": "unavailable", "error": str(exc)}
        result["capacity"]["checked_at"] = now()
        if alias == 'archer':
            try:
                limits = subprocess.run(command + ['sacctmgr -n -P show qos standard,lowpriority format=Name,MaxJobsPU,MaxSubmitPU'], capture_output=True, text=True, timeout=15, check=False)
                if limits.returncode:
                    raise ValueError(limits.stderr.strip()[-500:] or 'QoS query failed')
                result['limits'] = dict(state='observed', qos=parse_limits(limits.stdout), checked_at=now())
            except (OSError, subprocess.TimeoutExpired, ValueError) as exc:
                result['limits'] = dict(state='unavailable', error=str(exc), checked_at=now())
        else:
            result['limits'] = dict(state='unknown')
            try:
                ownership = subprocess.run(command + ['scontrol show jobs -d -o'], capture_output=True, text=True, timeout=15, check=False)
                if ownership.returncode:
                    raise ValueError(ownership.stderr.strip()[-500:] or 'Ownership query failed')
                result['ownership'] = dict(parse_ownership(ownership.stdout), checked_at=now())
            except (OSError, subprocess.TimeoutExpired, ValueError, KeyError) as exc:
                result['ownership'] = dict(state='unavailable', error=str(exc), checked_at=now())
    return result


def check_connections(registry):
    metadata, rows = read_registry(registry)
    check_destination(registry, metadata, [row["spec"] for row in rows])
    slot = sample_slot()
    with ThreadPoolExecutor(max_workers=3) as pool:
        connections = dict(zip(CLUSTER_ROOTS, pool.map(probe_cluster, CLUSTER_ROOTS)))
    with closing(sqlite3.connect(registry)) as db, db:
        db.execute("CREATE TABLE IF NOT EXISTS cluster_samples (slot INTEGER NOT NULL, cluster TEXT NOT NULL, record TEXT NOT NULL, PRIMARY KEY(slot,cluster))")
        db.executemany("INSERT OR IGNORE INTO cluster_samples VALUES (?,?,?)", [(slot, alias, encode(record)) for alias, record in connections.items()])
        db.execute("INSERT INTO metadata(key,value) VALUES ('connections',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                   (encode(connections),))
    return connections


def sample_clusters(registry, publish_dir):
    """One locked sample per five-minute UTC bucket, then atomic local publication."""
    metadata, rows = read_registry(registry)
    outputs = [Path(publish_dir)/name for name in ('campaign.html', 'preview.html')]
    for output in outputs:
        check_destination(output, metadata, [row['spec'] for row in rows])
        if output.resolve() == Path(registry).resolve():
            raise ValueError('Cannot overwrite the registry')
    with Path(registry).with_suffix('.monitor.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        with closing(readonly(registry)) as db:
            exists = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='cluster_samples'").fetchone()
            count = db.execute('SELECT count(*) FROM cluster_samples WHERE slot=?', (sample_slot(),)).fetchone()[0] if exists else 0
        if count < len(CLUSTER_ROOTS):
            check_connections(registry)
        metadata, rows = read_registry(registry)
        content = report_html(metadata, rows)
        for output in outputs:
            output.parent.mkdir(parents=True, exist_ok=True)
            temporary = None
            try:
                with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=output.parent, delete=False) as stream:
                    temporary = Path(stream.name)
                    stream.write(content)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, output)
            finally:
                if temporary:
                    temporary.unlink(missing_ok=True)
    return True


def presentation(row, metadata):
    spec, observed = row["spec"], row["observation"] or {}
    vanilla = spec["leg"] == "vanilla"
    analysis = observed.get("analysis", {})
    matches = []
    if spec["path"] and Path(spec["path"]).is_relative_to(spec["source_root"]):
        relative = Path(spec["path"]).relative_to(spec["source_root"])
        for alias, root in CLUSTER_ROOTS.items():
            connection = metadata.get("connections", {}).get(alias, {})
            if connection_state(connection) != "connected" or connection.get("scheduler", {}).get("state") != "observed":
                continue
            expected = PurePosixPath(root) / relative.as_posix()
            for job in connection["scheduler"]["jobs"]:
                directory = PurePosixPath(job["work_dir"])
                if directory == expected or expected in directory.parents:
                    matches.append(dict(job, cluster=alias))
    for execution in row.get("executions", []):
        observation = execution.get("observation") or {}
        alias = execution['plan']['config']['cluster']
        connection = metadata.get('connections', {}).get(alias, {})
        live = (connection_state(connection) == 'connected' and
                connection.get('scheduler', {}).get('state') == 'observed')
        monitored = (execution['state'] == 'active' and connection_state(
            {'state': 'connected', 'checked_at': observation.get('at')}) == 'connected')
        # Use the newest fresh evidence; a newer empty queue must retire an older match.
        if live and (not monitored or datetime.fromisoformat(connection['checked_at'].replace('Z', '+00:00')) >
                     datetime.fromisoformat(observation['at'].replace('Z', '+00:00'))):
            expected = PurePosixPath(execution['plan']['remote'])
            for job in connection['scheduler']['jobs']:
                directory = PurePosixPath(job['work_dir'])
                if directory == expected or expected in directory.parents:
                    matches.append(dict(job, cluster=alias, observed_at=connection['checked_at']))
        elif monitored:
            matches.extend(dict(job, cluster=alias, observed_at=observation['at']) for job in observation.get('jobs', []))
    # A local file or an absent queue match does not establish where a calculation is running.
    scheduler = observed.get("scheduler", {})
    if not matches and connection_state({"state": "connected", "checked_at": scheduler.get("observed_at")}) == "connected":
        if scheduler.get("job_id") and scheduler.get("cluster") in CLUSTER_ROOTS:
            matches.append(dict(scheduler, job_id=str(scheduler["job_id"]), state=scheduler.get("state", "unknown")))
    job_ids = [item["job_id"] for item in matches]
    locations = sorted({item["cluster"] for item in matches})
    scheduler_state = ', '.join(dict.fromkeys(item['state'].replace('_', ' ').title() for item in matches)) or 'Unknown'
    complete = observed.get("completion") == "validated" and observed.get("transfer", {}).get("state") == "verified_local"
    completion_conflict = complete and bool(matches)
    complete = complete and not matches
    verified_analysis = analysis.get("state") == "validated" and analysis.get("convergence") == "converged"
    stage = {"not_prepared": "not_started", "inputs_present": "inputs", "directory_present": "prepared",
             "windows_present": "prepared", "setup_present": "prepared", "production_observed": "production",
             "analysis_files_only": "analysis", "equilibrating": "equilibration"}.get(observed.get("stage"), "unknown")
    if analysis.get("state") in ("legacy_unverified", "possibly_stale", "invalid", "validated", "stale", "failed", "planned", "historical_imported"):
        stage = "analysis"
    if verified_analysis and not vanilla:
        stage = "converged"
    if complete:
        stage = "complete"
        locations = ["absolut"]
    elif matches and all(item["state"] in ("PENDING", "CONFIGURING", "REQUEUED") for item in matches):
        stage = "queued"
    if analysis.get("state") in ("failed", "stale") or analysis.get("decision", {}).get("action") == "stop_at_limit" or row["incidents"] or observed.get("issues") or observed.get("stage") == "observation_error" or completion_conflict:
        remarks = "Attention required"
    elif complete or any(item["state"] in ("RUNNING", "COMPLETING") for item in matches):
        remarks = "OK"
    elif stage in ("not_started", "inputs", "prepared", "queued"):
        remarks = "Waiting"
    else:
        remarks = "Unknown"
    convergence = 'unverified'
    if analysis.get('state') == 'validated':
        convergence = analysis.get('convergence', 'unverified')
    elif analysis.get('state') == 'historical_imported':
        convergence = {'historically_converged': 'converged (historical)',
                       'historically_unconverged': 'unconverged (historical)'}.get(analysis.get('convergence'), 'unverified')
    return {"simulation_type": "Vanilla" if vanilla else "Alchemical", "leg": "Solvent" if spec["leg"] == "solvent" else "Complex",
            "repeat": 1 if spec["leg"] == "solvent" else spec["repeat"], "stage": stage, "stage_label": STAGES[stage][0],
            "convergence": "N/A" if vanilla else convergence,
            "job_id": ", ".join(dict.fromkeys(job_ids)) or "Unknown", "location": ", ".join(locations) or "Unknown",
            "scheduler_state": scheduler_state, "remarks": remarks, "jobs": matches}


def issue(observation, code, message):
    observation["issues"].append({"code": code, "message": message})


def evidence(observation, path, role):
    if path.is_file():
        stat = path.stat()
        observation["evidence"].append({"path": str(path), "role": role, "size": stat.st_size, "mtime_ns": stat.st_mtime_ns})


def tail(path, size=65536):
    with path.open("rb") as stream:
        stream.seek(0, 2)
        position = max(0, stream.tell() - size)
        stream.seek(position)
        data = stream.read(size)
    if position:
        data = data.split(b"\n", 1)[-1]
    return data.decode("utf-8", errors="replace")


def production_end(path):
    before = path.stat()
    with path.open("rb") as stream:
        if before.st_size:
            stream.seek(-1, 2)
            if stream.read(1) != b"\n":
                raise ValueError("DHDL has an incomplete final line; refresh after the writer finishes")
    with path.open() as stream:
        header = stream.read(16384)
    if not re.search(r'xaxis\s+label\s+"Time\s*\(ps\)"', header, re.IGNORECASE):
        raise ValueError("DHDL time unit is not explicitly ps")
    lines = [line for line in tail(path).splitlines() if line.strip() and not line.lstrip().startswith(("#", "@"))]
    if not lines:
        raise ValueError("No production samples in DHDL tail")
    fields = lines[-1].split()
    values = [float(field) for field in fields]
    if len(values) < 2 or not all(math.isfinite(value) for value in values) or values[0] < 0:
        raise ValueError("Invalid final DHDL sample")
    after = path.stat()
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise ValueError("DHDL changed while it was being observed; refresh the snapshot")
    return values[0] / 1000


def target_duration(path):
    settings = {}
    for line in path.read_text().splitlines():
        line = line.split(";", 1)[0]
        if "=" in line:
            key, value = line.split("=", 1)
            settings[key.strip().replace("_", "-")] = value.strip()
    steps, dt = float(settings["nsteps"]), float(settings["dt"])
    if not all(math.isfinite(value) for value in (steps, dt)) or steps < 0 or not steps.is_integer() or dt <= 0:
        raise ValueError("Invalid production nsteps/dt")
    if float(settings.get("tinit", 0)) != 0 or float(settings.get("init-step", 0)) != 0:
        raise ValueError("Nonzero production time origin requires explicit duration mapping")
    return steps * dt / 1000


def inspect_analysis(observation, directory):
    analysis = observation["analysis"]
    if not directory.exists():
        return
    files = sorted(directory.glob("*.csv")) + sorted(directory.glob("*.pdf"))
    for path in files:
        evidence(observation, path, "analysis")
    if not files:
        return
    analysis["state"] = "legacy_unverified"
    issue(observation, "analysis_provenance", "Analysis files exist without a validated input snapshot/cutoff; convergence remains unverified.")
    convergence = directory / "ABFE_convergence.csv"
    if convergence.is_file():
        try:
            with convergence.open(newline="") as stream:
                halves = [row for row in csv.DictReader(stream) if float(row["data_fraction"]) == 0.5]
            if len(halves) != 1:
                raise ValueError("Expected exactly one data_fraction=0.5 row")
            forward, backward = float(halves[0]["Forward"]), float(halves[0]["Backward"])
            if not all(math.isfinite(value) for value in (forward, backward)):
                raise ValueError("Non-finite convergence estimate")
            difference = abs(forward - backward)
            if not math.isfinite(difference):
                raise ValueError("Non-finite convergence difference")
            analysis["reported_half_difference_kcal_mol"] = difference
        except (ValueError, KeyError, TypeError, csv.Error) as exc:
            analysis["state"] = "invalid"
            issue(observation, "invalid_analysis", str(exc))
    latest_source = max((item["mtime_ns"] for item in observation["evidence"] if item["role"] == "production_data"), default=0)
    if files and latest_source > min(path.stat().st_mtime_ns for path in files):
        analysis["state"] = "possibly_stale" if analysis["state"] != "invalid" else "invalid"
        issue(observation, "newer_source_files", "Production files have newer timestamps than analysis; timestamps alone cannot establish analysis freshness.")


def inspect_run(spec):
    observation = {"stage": "not_prepared", "production_ns": None, "production_max_ns": None,
                   "target_ns": None, "sampled_windows": 0, "expected_windows": sum(spec["windows"].values()),
                   "scheduler": {"state": "unknown", "cluster": None, "job_id": None},
                   "transfer": {"state": "unknown"},
                   "analysis": {"state": "absent", "analysed_ns": None, "convergence": "unverified"},
                   "windows": [], "evidence": [], "issues": [], "next_action": "Inspect inventory; use cluster-plan for explicit execution"}
    for item in spec["input_files"]:
        evidence(observation, Path(item), "input")
        if not Path(item).is_file():
            issue(observation, "missing_input:" + item, f"Declared input is missing: {item}")
    if observation["evidence"]:
        observation["stage"] = "inputs_present"
    location = Path(spec["path"]) if spec["path"] else None
    if not location or not location.exists():
        if location and spec["leg"] != "vanilla":
            inspect_analysis(observation, location.parent / "analysis" / ("ligand" if spec["leg"] == "solvent" else "complex"))
            if observation["analysis"]["state"] != "absent":
                observation["stage"] = "analysis_files_only"
        return observation
    if not location.is_dir():
        issue(observation, "invalid_source", f"Expected a directory: {location}")
        return observation
    observation["stage"] = "directory_present"
    if spec["leg"] == "vanilla":
        log = location / "prod.log"
        mdp = location / "prod.mdp"
        for name in ("topol.top", "index.ndx", "system_solv_ions.gro", "prod.cpt", "prod.gro", "prod.xtc", "prod.log", "prod.mdp"):
            evidence(observation, location / name, "production_log" if name == "prod.log" else "preparation")
        if mdp.is_file():
            observation["target_ns"] = target_duration(mdp)
            observation["stage"] = "setup_present"
        if log.is_file():
            matches = re.findall(r"Step\s+Time\s*\n\s*\d+\s+([\d.eE+-]+)", tail(log, 262144))
            if matches:
                value = float(matches[-1]) / 1000
                if not math.isfinite(value) or value < 0:
                    raise ValueError("Invalid vanilla production time")
                observation.update(production_ns=value, production_max_ns=value, stage="production_observed")
        return observation
    expected = [f"{phase}.{index:02d}" for phase, count in spec["windows"].items() for index in range(count)]
    observed = sorted(path.name for path in location.iterdir() if path.is_dir() and re.fullmatch(r"[a-z]+\.\d+", path.name))
    if observed:
        observation["stage"] = "windows_present"
        if set(observed) != set(expected):
            issue(observation, "window_inventory", f"Missing windows: {sorted(set(expected)-set(observed))}; unexpected: {sorted(set(observed)-set(expected))}")
    times, targets = [], []
    for name in expected:
        prod = location / name / "prod"
        dhdl = prod / "dhdl.xvg"
        phase, index = name.split(".")
        mdp = prod / f"{phase}.prod.{index}.mdp"
        window = {"name": name, "production_ns": None, "target_ns": None, "checkpoint_present": (prod / "state.cpt").is_file()}
        for path, role in ((dhdl, "production_data"), (mdp, "production_input"), (prod / "md.log", "production_log"),
                           (prod / "state.cpt", "checkpoint"), (prod / "topol.tpr", "production_input")):
            evidence(observation, path, role)
        try:
            if dhdl.is_file():
                window["production_ns"] = production_end(dhdl)
                times.append(window["production_ns"])
            if mdp.is_file():
                window["target_ns"] = target_duration(mdp)
                targets.append(window["target_ns"])
        except (OSError, ValueError, KeyError) as exc:
            issue(observation, f"invalid_window:{name}", f"{name}: {exc}")
        observation["windows"].append(window)
    observation["sampled_windows"] = len(times)
    if times:
        observation.update(stage="production_observed", production_max_ns=max(times))
        if len(times) == len(expected):
            observation["production_ns"] = min(times)
        else:
            issue(observation, "incomplete_production_data", f"Production times observed for {len(times)}/{len(expected)} expected windows; common duration is unknown.")
    if len(targets) == len(expected) and len(set(targets)) == 1:
        observation["target_ns"] = targets[0]
    elif targets:
        issue(observation, "inconsistent_targets", "Production MDP targets are missing, invalid, or inconsistent across windows.")
    analysis_directory = location.parent / "analysis" / ("ligand" if spec["leg"] == "solvent" else "complex")
    inspect_analysis(observation, analysis_directory)
    for path in (location.parent / "analysis/abfe_results.csv", location.parent / "boresch/resgen_output.txt"):
        evidence(observation, path, "binding_result" if path.suffix == ".csv" else "restraints")
    if spec["leg"] == "solvent":
        issue(observation, "solvent_compatibility", "Existing solvent calculation is inventoried; compatibility for reuse across complex repeats has not been verified.")
    return observation


def scan(registry, progress=None):
    metadata, rows = read_registry(registry)
    check_destination(registry, metadata, [row["spec"] for row in rows])
    observations = []
    for row in rows:
        spec = row["spec"]
        try:
            observation = inspect_run(spec)
        except (OSError, ValueError, KeyError) as exc:
            observation = {"stage": "observation_error", "production_ns": None, "target_ns": None,
                           "scheduler": {"state": "unknown"}, "transfer": {"state": "unknown"},
                           "analysis": {"state": "unknown", "analysed_ns": None, "convergence": "unverified"},
                           "evidence": [], "issues": [], "next_action": "Inspect observation error"}
            issue(observation, "observation_error", str(exc))
        if row.get("analysis_record", {}).get("state") == "validated" and (row.get("observation") or {}).get("analysis", {}).get("state") == "validated":
            # Old CSVs remain evidence, but a current managed analysis supersedes their missing provenance.
            observation["issues"] = [item for item in observation["issues"]
                                     if item["code"] not in ("analysis_provenance", "newer_source_files", "invalid_analysis")]
        for detected in row["incidents"]:
            if detected.get("detected_in_report"):
                issue(observation, detected["code"], detected["message"])
        previous = row["observation"] or {}
        if previous.get("stage") not in (None, "not_prepared", "inputs_present", "observation_error") and spec["path"] and not Path(spec["path"]).exists():
            issue(observation, "source_disappeared", "A previously observed run directory is now missing or inaccessible; do not infer that work was reset.")
        if previous.get("production_ns") is not None and observation.get("production_ns") is None:
            issue(observation, "production_evidence_lost", "Previously observed common production progress can no longer be established; inspect the source evidence.")
        observations.append((spec, observation))
        if progress and (len(observations) % 25 == 0 or len(observations) == len(rows)):
            progress(len(observations), len(rows))
    for spec, observation in observations:
        if spec["leg"] == "vanilla" and spec["ligand_path"] and Path(spec["ligand_path"]).is_dir():
            expected = {Path(row["spec"]["path"]).parent.name for row in rows
                        if row["spec"]["dataset"] == spec["dataset"] and row["spec"]["ligand"] == spec["ligand"]
                        and row["spec"]["leg"] == "complex" and row["spec"]["path"]}
            extra = sorted(path.name for path in Path(spec["ligand_path"]).glob("abfe*") if path.is_dir() and path.name not in expected)
            if extra:
                observation["legacy_candidates"] = extra
                issue(observation, "unmapped_legacy_runs", f"Additional legacy ABFE directories were preserved without mapping: {extra}")
    observed_at = now()
    with closing(sqlite3.connect(registry)) as db, db:
        for spec, observation in observations:
            value = encode(observation)
            previous = db.execute("SELECT observation FROM runs WHERE id=?", (spec["id"],)).fetchone()[0]
            if previous != value:
                db.execute("INSERT INTO history(run_id,observed_at,observation) VALUES (?,?,?)", (spec["id"], observed_at, value))
            db.execute("UPDATE runs SET observation=?,observed_at=? WHERE id=?", (value, observed_at, spec["id"]))
            for problem in observation["issues"]:
                db.execute("""INSERT INTO incidents(run_id,code,message,first_seen,last_seen) VALUES (?,?,?,?,?)
                    ON CONFLICT(run_id,code) DO UPDATE SET last_seen=excluded.last_seen,message=excluded.message""",
                           (spec["id"], problem["code"], problem["message"], observed_at, observed_at))
        db.execute("UPDATE metadata SET value=? WHERE key='scanned_at'", (encode(observed_at),))
    return len(observations)


def number(value):
    return "?" if value is None else f"{value:.3f}".rstrip("0").rstrip(".")


def table(headers, rows):
    rows = [[str(cell) for cell in row] for row in rows]
    widths = [max([len(headers[i])] + [len(row[i]) for row in rows]) for i in range(len(headers))]
    return "\n".join(["  ".join(cell.ljust(width) for cell, width in zip(headers, widths)),
                      "  ".join("─" * width for width in widths)] +
                     ["  ".join(cell.ljust(width) for cell, width in zip(row, widths)) for row in rows])


def status(metadata, rows):
    result = ["H.A.W.T.O.W.N", f"Last updated: {metadata['scanned_at'] or 'not scanned'}", ""]
    counts = [[item["label"], f"{item['registered_ligands']}/{item['expected_ligands'] or '?'}"] for item in metadata["datasets"]]
    result += [table(["Complexes tracked", "Registered / selected"], counts), ""]
    cells = []
    for row in rows:
        observed = row["observation"] or {}
        view = presentation(row, metadata)
        cells.append([row["spec"]["id"], view["simulation_type"], view["leg"], view["repeat"], view["stage_label"],
                      number(observed.get("production_ns")), number(observed.get("analysis", {}).get("analysed_ns")),
                      view["convergence"], view["scheduler_state"], view["job_id"], view["location"], view["remarks"]])
    result += [table(["Run", "Simulation type", "Leg", "Repeat", "Stage", "Prod ns*", "Analysed ns", "Convergence", "Scheduler", "Job ID", "Location", "Remarks"], cells),
               "", "*Production endpoints are inventory evidence, not validation of samples or an analysed cutoff.",
               "Unknown job IDs/locations are not inferred from local files. Vanilla convergence is N/A.",
               "Use 'show RUN_ID' for evidence, dependencies, incidents and history. Cluster mutations require an explicit cluster-action --apply."]
    return "\n".join(result)


def console_styles():
    assets = files("abfe") / "console_assets"
    faces = []
    for family, weight, filename in (("Inter", 400, "Inter-Regular.ttf"), ("Inter", 600, "Inter-SemiBold.ttf"),
                                      ("JetBrains Mono", 600, "JetBrainsMono-SemiBold.ttf")):
        encoded = base64.b64encode((assets / filename).read_bytes()).decode("ascii")
        faces.append(f"@font-face{{font-family:'{family}';font-weight:{weight};font-style:normal;font-display:swap;src:url(data:font/ttf;base64,{encoded}) format('truetype');}}")
    return "\n".join(faces) + "\n" + (assets / "console.css").read_text()


def display_time(value):
    if not value:
        return "Not checked"
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc).strftime("%d %b %Y · %H:%M UTC")
    except (ValueError, TypeError):
        return "Unknown"


def summarize_runs(metadata, rows):
    """Aggregate observed stages without promoting legacy evidence to completion."""
    result = {'datasets': [], 'compounds': []}
    for dataset in metadata['datasets']:
        selected = [r for r in rows if r['spec']['dataset'] == dataset['id']]
        compounds = sorted({r['spec']['ligand'] for r in selected})
        def summary(items):
            groups = {leg: {stage: 0 for stage in STAGES} for leg in ('vanilla', 'complex', 'solvent')}
            attention = converged = complete = legacy = 0
            for row in items:
                view = presentation(row, metadata)
                groups[row['spec']['leg']][view['stage']] += 1
                attention += view['remarks'] == 'Attention required'
                converged += view['convergence'] == 'converged'
                complete += view['stage'] == 'complete'
                legacy += (row['observation'] or {}).get('analysis', {}).get('state') in ('legacy_unverified', 'possibly_stale')
            return dict(groups=groups, attention=attention, converged=converged, complete=complete, legacy=legacy, runs=len(items))
        result['datasets'].append(dict(summary(selected), id=dataset['id'], label=dataset['label'], compounds=len(compounds)))
        for ligand in compounds:
            items = [r for r in selected if r['spec']['ligand'] == ligand]
            result['compounds'].append(dict(summary(items), dataset=dataset['id'], ligand=ligand))
    return result


def overview_html(metadata, rows):
    escape = lambda value: html.escape(str(value), quote=True)
    summaries = summarize_runs(metadata, rows)
    def stages(groups):
        return ''.join(f'<span class="stage-chip" style="--stage-color:{STAGES[key][1]}">{escape(STAGES[key][0])} <strong>{count}</strong></span>'
                       for key, count in groups.items() if count) or '<span class="subtle">Not started</span>'
    def jump(dataset, ligand=''):
        return f'<button type="button" class="jump" data-dataset="{escape(dataset)}" data-ligand="{escape(ligand)}">View runs →</button>'
    datasets = []
    for item in summaries['datasets']:
        legs = ''.join(f'<div class="leg-summary"><h3>{label}</h3><div class="stage-chips">{stages(item["groups"][leg])}</div></div>'
                       for leg, label in (('vanilla', 'Vanilla repeats'), ('complex', 'Complex legs'), ('solvent', 'Shared solvent legs')))
        datasets.append(f'<article class="dataset-card" style="--dataset-color:{DATASET_COLORS.get(item["id"], "#0fffdb")}">'
                        f'<div class="topline"><h2>{escape(item["label"])}</h2>{jump(item["id"])}</div>'
                        f'<p class="subtle">{item["compounds"]} compounds · {item["runs"]} run records</p>{legs}'
                        f'<p class="summary-counts"><span>{item["converged"]} alchemical legs with validated convergence</span><span>{item["complete"]} runs complete</span>'
                        f'<span class="attention">{item["attention"]} runs need attention</span></p>'
                        f'<p class="subtle">{item["legacy"]} legacy analyses unverified</p></article>')
    compounds = []
    for item in summaries['compounds']:
        cells = ''.join(f'<td><div class="stage-chips">{stages(item["groups"][leg])}</div></td>' for leg in ('vanilla', 'complex', 'solvent'))
        compounds.append(f'<tr data-search="{escape((item["dataset"] + " " + item["ligand"]).lower())}">'
                         f'<td class="edge" style="--edge-color:{DATASET_COLORS.get(item["dataset"], "#0fffdb")}">{escape(item["dataset"])}</td>'
                         f'<td>{escape(item["ligand"])}</td>{cells}<td>{item["converged"]} / 4</td>'
                         f'<td class="attention">{item["attention"]} / {item["runs"]}</td><td>{jump(item["dataset"], item["ligand"])}</td></tr>')
    return (f'<section id="view-clusters" class="view" hidden><h2>Cluster activity</h2>'
            + cluster_cards(metadata, CLUSTER_ROOTS, display_time, connection_state) + '</section>'
            f'<section id="view-datasets" class="view" hidden><h2>Dataset progress</h2><p class="view-note">Stage counts by leg and repeat. A production endpoint or legacy analysis does not establish validated convergence or completion.</p><div class="dataset-grid">{"".join(datasets)}</div></section>'
            f'<section id="view-compounds" class="view" hidden><h2>Compound progress</h2><p class="view-note">One row per dataset–compound pair; stage chips count the three vanilla repeats, three complex legs and one shared solvent leg. Converged counts validated alchemical legs out of four.</p>'
            '<div class="toolbar"><div class="search"><label for="compound-filter">Filter compounds</label><input type="search" id="compound-filter" placeholder="Search dataset or compound…"></div>'
            f'<span class="subtle" id="compound-count" role="status">{len(compounds)} compounds</span></div><div class="table"><table class="compound-table"><thead><tr>'
            + ''.join(f'<th scope="col">{label}</th>' for label in ('Dataset', 'Compound', 'Vanilla · 3 repeats', 'Complex · 3 repeats', 'Solvent · 1 leg', 'Converged legs', 'Runs need attention', 'Details'))
            + '</tr></thead><tbody>' + ''.join(compounds) + '</tbody></table></div></section>')


def report_html(metadata, rows):
    escape = lambda value: html.escape(str(value), quote=True)
    body = []
    for row in rows:
        spec, observed = row["spec"], row["observation"] or {}
        analysis, view = observed.get("analysis", {}), presentation(row, metadata)
        problems = ''.join(f"<li><strong>{escape(item['code'])}</strong>: {escape(item['message'])}</li>" for item in row["incidents"])
        evidence_links = ''.join(f'<li><a href="{escape(Path(item["path"]).as_uri())}">{escape(item["path"])}</a> ({escape(item["role"])})</li>' for item in observed.get("evidence", []))
        history = ''.join(f"<li>Revision {item['revision']}: {escape(item['observed_at'])}</li>" for item in row["history"])
        dependencies = ''.join(f"<li>{escape(item)}</li>" for item in spec["dependencies"])
        jobs = ''.join(f"<li>{escape(item['cluster'])} · {escape(item['job_id'])} · {escape(item['state'])}</li>" for item in view["jobs"])
        detail = (f"<h3>{escape(spec['id'])}</h3><p>Source: {escape(spec['path'] or 'not prepared')}<br>"
                  f"Equilibration repeat: {escape((row['equilibration_assignment']['label'] + ' (run name)') if row.get('equilibration_assignment') else ('van' + str(spec['repeat']) if spec['leg'] != 'solvent' else 'not applicable'))}<br>"
                  f"Equilibration source: {escape(row.get('equilibration', {}).get('source') or 'not applicable')}<br>"
                  f"Equilibration evidence: {escape(row.get('equilibration', {}).get('state', 'unverified'))}<br>"
                  f"Observed windows: {escape(observed.get('sampled_windows', 0))}/{escape(observed.get('expected_windows', 0))}<br>"
                  f"Observed maximum: {escape(number(observed.get('production_max_ns')))} ns<br>"
                  f"Transfer evidence: {escape(observed.get('transfer', {}).get('state', 'unknown'))}<br>"
                  f"Next action: {escape(observed.get('next_action', 'Scan local inventory'))}</p>"
                  f"<h4>Open incidents ({len(row['incidents'])})</h4><ul>{problems or '<li>None recorded</li>'}</ul>"
                  f"<h4>Observed jobs</h4><ul>{jobs or '<li>No fresh job association is available</li>'}</ul>"
                  f"<h4>Dependencies</h4><ul>{dependencies or '<li>None</li>'}</ul>"
                  f"<details><summary>File evidence ({len(observed.get('evidence', []))})</summary><ul>{evidence_links}</ul></details>"
                  f"<details><summary>Observation history ({len(row['history'])})</summary><ol>{history}</ol></details>")
        if row.get("analysis_record"):
            detail += ("<h4>Analysis and decision</h4><pre>" +
                       escape(json.dumps(analysis, indent=2, allow_nan=False)) + "</pre>")
        if row.get("historical_analysis"):
            imported = row["historical_analysis"]
            links = ''.join(f'<li><a href="{escape(Path(item["identity"]["path"]).as_uri())}">{escape(item["identity"]["path"])}</a></li>' for item in imported["files"])
            detail += (f"<h4>Historical analysis imported</h4><p>Record: {escape(imported['id'])}<br>"
                       f"Analysed cutoff: {escape(number(imported.get('analysed_ns')))} ns · uncertainties: legacy as reported<br>"
                       f"Cutoff basis: {escape(imported.get('duration_assignment', {}).get('rule', 'unknown'))}<br>"
                       f"Convergence: {escape(imported.get('convergence', 'unverified').replace('_', ' '))}</p>"
                       + '<pre>' + escape(json.dumps(imported.get('convergence_assessment', {}), indent=2)) + f'</pre><ul>{links}</ul>')
        if row.get("executions"):
            from .execution import summary
            detail += "<h4>Cluster execution and transfers</h4><pre>" + escape(json.dumps([summary(r) for r in row["executions"]], indent=2, allow_nan=False)) + "</pre>"
        if row.get("preparation"):
            prep = row["preparation"]
            detail += ("<h4>Staged preparation</h4><pre>" + escape(json.dumps({
                key: prep.get(key) for key in ("state", "at", "stage", "output", "error")
            }, indent=2)) + "</pre>")
        if row.get("binding_result"):
            detail += ("<h4>Binding result and shared solvent</h4><pre>" +
                       escape(json.dumps(row["binding_result"], indent=2, allow_nan=False)) + "</pre>")
        dataset_color = DATASET_COLORS.get(spec["dataset"], "#0fffdb")
        stage_color = STAGES[view["stage"]][1]
        remark_color = REMARK_COLORS[view["remarks"]]
        cells = [f'<td class="edge" style="--edge-color:{dataset_color}">{escape(spec["dataset"])}</td>',
                 f'<td>{escape(spec["ligand"])}</td>',
                 f'<td class="type {"vanilla" if view["simulation_type"] == "Vanilla" else ""}">{escape(view["simulation_type"])}</td>',
                 f'<td>{escape(view["leg"])}</td>', f'<td class="numeric">{escape(view["repeat"])}</td>',
                 f'<td class="edge stage" style="--edge-color:{stage_color}">{escape(view["stage_label"])}</td>',
                 f'<td class="numeric">{escape(number(observed.get("production_ns")))}</td>',
                 f'<td class="numeric subtle">{escape(number(analysis.get("analysed_ns")))}</td>',
                 f'<td class="subtle">{escape(("Historical analysis imported" if analysis.get("state") == "historical_imported" else analysis.get("state", "unknown").replace("_", " ")))}</td>',
                 f'<td class="subtle">{escape(view["convergence"])}</td>',
                 f'<td class="scheduler">{escape(view["scheduler_state"])}</td>',
                 f'<td class="job" title="{escape(view["job_id"])}">{escape(view["job_id"])}</td>',
                 f'<td class="location">{escape(view["location"])}</td>',
                 f'<td><span class="remark" style="--remark-color:{remark_color}"><i class="dot" aria-hidden="true"></i>{escape(view["remarks"])}</span></td>']
        search = " ".join([spec["dataset"], spec["ligand"], view["simulation_type"], view["leg"], str(view["repeat"]),
                           view["stage_label"], view["remarks"], view["convergence"], view["scheduler_state"], view["job_id"], view["location"]] +
                          [f"{item['code']} {item['message']}" for item in row["incidents"]])
        checked = [job.get("observed_at") or metadata.get("connections", {}).get(job["cluster"], {}).get("checked_at", "") for job in view["jobs"]]
        job_checked = min(checked) if checked else ""
        body.append(f'<tr data-search="{escape(search.lower())}" data-job-checked="{escape(job_checked)}" data-remarks="{escape(view["remarks"])}"><td><details class="run"><summary aria-label="Inspect {escape(spec["id"])}">Inspect</summary><section>{detail}</section></details></td>' + ''.join(cells) + '</tr>')
    dataset_items = []
    for item in metadata["datasets"]:
        count = item["registered_ligands"]
        description = f"{count} complexes · {count * 3} planned complex repeats" if count else "Not started · no complexes selected"
        dataset_items.append(f'<li style="--dataset-color:{DATASET_COLORS.get(item["id"], "#0fffdb")}"><strong>{escape(item["label"])}</strong><span>{escape(description)}</span></li>')
    health = []
    for alias in CLUSTER_ROOTS:
        record = metadata.get("connections", {}).get(alias, {})
        state = connection_state(record)
        icon = {"connected": "✓", "unreachable": "✕", "stale": "◷", "unknown": "?"}.get(state, "?")
        note = f"SSH: {state}. Last checked: {display_time(record.get('checked_at'))}. Scheduler: {record.get('scheduler', {}).get('state', 'unknown')}."
        health.append(f'<span class="connection" data-state="{escape(state)}" data-checked="{escape(record.get("checked_at", ""))}" title="{escape(note)}"><span class="indicator" aria-hidden="true">{icon}</span><strong>{alias}</strong><span class="state">{escape(state)}</span></span>')
    legend = ''.join(f'<span class="legend-item"><i class="dot" style="--color:{color}" aria-hidden="true"></i>{escape(label)}</span>' for label, color in REMARK_COLORS.items())
    stage_legend = ''.join(f'<span class="legend-item"><i class="dot" style="--color:{color}" aria-hidden="true"></i>{escape(label)}</span>' for label, color in STAGES.values())
    columns = ("Dataset", "Ligand", "Simulation type", "Leg", "Repeat", "Stage", "Prod ns", "Analysed ns", "Analysis", "Convergence", "Scheduler", "Job ID", "Location", "Remarks")
    headers = '<th scope="col">Details</th>' + ''.join(
        f'<th scope="col" aria-sort="none"><button type="button" class="sort" data-column="{index}" '
        f'aria-label="Sort by {label}, ascending">{label}<span aria-hidden="true">↕</span></button></th>'
        for index, label in enumerate(columns, start=1))
    total = sum(item["registered_ligands"] for item in metadata["datasets"])
    licenses = "\n".join((files("abfe") / "console_assets" / name).read_text() for name in ("Inter-LICENSE.txt", "JetBrainsMono-LICENSE.txt"))
    return f'''<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>H.A.W.T.O.W.N</title><style>{console_styles()}</style></head><body><main>
<header class="hero"><div class="brand">Hyperspecific ABFE Workflow That Only Works for Nithish</div>
<div class="topline"><h1>H.A.W.T.O.W.N</h1><div class="view-controls"><nav aria-label="Console views">{''.join(f'<button type="button" class="view-button" data-view="{key}" aria-pressed="{str(key == "runs").lower()}">{label}</button>' for key, label in (("runs", "Runs"), ("clusters", "Clusters"), ("datasets", "Datasets"), ("compounds", "Compounds")))}</nav><p class="updated">Last updated: {escape(display_time(metadata['scanned_at']))}</p></div></div>
<div class="health" aria-label="Cluster SSH connections" aria-live="polite">{''.join(health)}</div></header>
<section id="view-runs" class="view"><section class="tracked"><h2>Complexes tracked</h2><p>{total} protein–ligand complexes · three planned complex repeats · one shared solvent calculation per ligand</p><ul class="datasets">{''.join(dataset_items)}</ul></section>
<div class="toolbar"><div class="search"><label for="filter">Filter runs <span class="subtle" id="row-count" role="status" aria-live="polite">{len(rows)} records</span></label><input id="filter" placeholder="Search dataset, ligand, simulation type or remarks…" type="search"></div><div class="legend" aria-label="Remarks legend">{legend}</div></div>
<div class="table"><table class="run-table"><thead><tr>{headers}</tr></thead><tbody>{''.join(body)}</tbody></table></div>
<details class="notes"><summary>Stage key and inventory notes</summary><div class="legend stages">{stage_legend}</div><p>Stage describes the latest observed evidence, not a claim that a job is currently running. Attention required means open incidents; Waiting means preparation or a queued step; OK requires observed activity or validated local completion; Unknown means there is insufficient evidence.</p><p>Production ns is the common observed endpoint across every expected window. It does not validate samples or establish an analysed cutoff. Vanilla convergence is N/A. Running location and job IDs require fresh scheduler evidence; absolut is reserved for validated completion with verified local retrieval. Unmatched jobs remain unassigned. SSH indicators become stale after five minutes.</p></details>
</section>{overview_html(metadata, rows)}
<footer><span>Read-only inventory · {escape(metadata['name'])}</span><span>Cluster checks and run inventory have separate timestamps.</span></footer></main>
<script>
const input=document.getElementById('filter');
const viewButtons=[...document.querySelectorAll('.view-button')];
function showView(name){{
  if(!viewButtons.some(button=>button.dataset.view===name))name='runs';
  sessionStorage.setItem('hawtown-view',name);
  document.querySelectorAll('main > .view').forEach(view=>view.hidden=view.id!=='view-'+name);
  viewButtons.forEach(button=>button.setAttribute('aria-pressed',String(button.dataset.view===name)));
}}
viewButtons.forEach(button=>button.addEventListener('click',()=>showView(button.dataset.view)));
showView(sessionStorage.getItem('hawtown-view')||'runs');
document.querySelectorAll('.jump').forEach(button=>button.addEventListener('click',()=>{{
  showView('runs');
  input.value=(button.dataset.dataset+' '+button.dataset.ligand).trim();
  input.dispatchEvent(new Event('input'));input.focus();
}}));
const compoundInput=document.getElementById('compound-filter');
compoundInput.addEventListener('input',()=>{{
  sessionStorage.setItem('hawtown-compound-filter',compoundInput.value);
  const rows=[...document.querySelectorAll('.compound-table tbody tr')];
  rows.forEach(row=>row.hidden=!row.dataset.search.includes(compoundInput.value.trim().toLowerCase()));
  document.getElementById('compound-count').textContent=rows.filter(row=>!row.hidden).length+' / '+rows.length+' compounds';
}});

input.addEventListener('input',()=>{{sessionStorage.setItem('hawtown-run-filter',input.value);const q=input.value.trim().toLowerCase();let visible=0;document.querySelectorAll('.run-table tbody tr').forEach(row=>{{row.hidden=!row.dataset.search.includes(q);if(!row.hidden)visible++;}});document.getElementById('row-count').textContent=visible+' / '+document.querySelectorAll('.run-table tbody tr').length+' records';}});
const tbody=document.querySelector('.run-table tbody');
const sortButtons=[...document.querySelectorAll('button.sort')];
const collator=new Intl.Collator(undefined,{{numeric:true,sensitivity:'base'}});
sortButtons.forEach(button=>button.addEventListener('click',()=>{{
  const header=button.closest('th');
  const ascending=header.getAttribute('aria-sort')!=='ascending';
  const column=Number(button.dataset.column);
  sessionStorage.setItem('hawtown-sort',JSON.stringify({{column,ascending}}));
  const numeric=[5,7,8].includes(column);
  const value=row=>row.cells[column].textContent.trim();
  const missing=text=>['','?','unknown','n/a'].includes(text.toLowerCase());
  const rows=[...tbody.rows];
  rows.sort((a,b)=>{{
    const left=value(a),right=value(b);
    if(missing(left)||missing(right))return Number(missing(left))-Number(missing(right));
    const result=numeric?Number(left)-Number(right):collator.compare(left,right);
    return ascending?result:-result;
  }});
  rows.forEach(row=>tbody.appendChild(row));
  sortButtons.forEach(other=>{{
    const active=other===button;
    other.closest('th').setAttribute('aria-sort',active?(ascending?'ascending':'descending'):'none');
    other.querySelector('span').textContent=active?(ascending?'↑':'↓'):'↕';
    other.setAttribute('aria-label','Sort by '+other.childNodes[0].textContent+', '+(active&&ascending?'descending':'ascending'));
  }});
}}));
input.value=sessionStorage.getItem('hawtown-run-filter')||'';input.dispatchEvent(new Event('input'));
compoundInput.value=sessionStorage.getItem('hawtown-compound-filter')||'';compoundInput.dispatchEvent(new Event('input'));
try{{const saved=JSON.parse(sessionStorage.getItem('hawtown-sort')||'null');if(saved){{const button=sortButtons.find(item=>Number(item.dataset.column)===saved.column);if(button){{button.click();if(!saved.ascending)button.click();}}}}}}catch{{sessionStorage.removeItem('hawtown-sort');}}
function ageConnections(){{
document.querySelectorAll('[data-capacity-checked]').forEach(card=>{{const checked=Date.parse(card.dataset.capacityChecked);if(Number.isFinite(checked)&&Date.now()-checked>{CONNECTION_MAX_AGE_SECONDS * 1000})card.querySelector('.snapshot-state').textContent='stale snapshot';}});
document.querySelectorAll('.connection').forEach(pill=>{{const checked=Date.parse(pill.dataset.checked);if(Number.isFinite(checked)&&Date.now()-checked>{CONNECTION_MAX_AGE_SECONDS * 1000}){{pill.dataset.state='stale';pill.querySelector('.indicator').textContent='◷';pill.querySelector('.state').textContent='stale';}}}});
document.querySelectorAll('tr[data-job-checked]').forEach(row=>{{const checked=Date.parse(row.dataset.jobChecked);if(Number.isFinite(checked)&&Date.now()-checked>{CONNECTION_MAX_AGE_SECONDS * 1000}){{row.querySelector('.job').textContent='Unknown';row.querySelector('.scheduler').textContent='Unknown';row.querySelector('.location').textContent='Unknown';if(row.dataset.remarks!=='Attention required'){{row.querySelector('.remark').textContent='Unknown';}}if(row.querySelector('.stage').textContent==='Queued'){{row.querySelector('.stage').textContent='Queued (stale)';}}}}}});
}}
ageConnections();setInterval(ageConnections,60000);
if(location.hostname==='127.0.0.1'&&location.port==='8765')setInterval(()=>{{if(!document.querySelector('details.run[open]')&&document.activeElement.tagName!=='INPUT')location.reload();}},300000);
</script><!-- Font licenses
{licenses}
--></body></html>'''


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--registry", required=True, type=Path, help="Registry SQLite file outside simulation source trees")
    sub = parser.add_subparsers(dest="command", required=True)
    init = sub.add_parser("init", help="Register an explicit manifest without modifying simulations")
    init.add_argument("--manifest", required=True, type=Path)
    init.add_argument("--source-root", type=Path)
    sub.add_parser("scan", help="Read local source files and persist observations/incidents")
    sub.add_parser("check-connections", help="Check SSH, own-job queues and Slurm capacity on the three clusters")
    analysis = sub.add_parser("analyse", help="Validate and analyse one stable local alchemical leg in place")
    analysis.add_argument("run_id")
    analysis.add_argument("--cutoff-ns", required=True, type=float)
    analysis.add_argument("--plan", action="store_true", help="Validate inputs and record a plan without running estimators")
    preparation = sub.add_parser("prepare", help="Prepare one compound in separate local staging; no simulation execution")
    preparation.add_argument("compound", help="DATASET/LIGAND")
    preparation.add_argument("--manifest", required=True, type=Path)
    preparation.add_argument("--output", required=True, type=Path)
    preparation.add_argument("--stage", required=True, choices=("vanilla", "alchemical"))
    preparation.add_argument("--plan", action="store_true", help="Validate mappings/inputs without generating anything")
    cluster_plan = sub.add_parser("cluster-plan", help="Save an inspectable execution plan; no remote actions")
    cluster_plan.add_argument("run_id")
    cluster_plan.add_argument("--manifest", required=True, type=Path)
    cluster_action = sub.add_parser("cluster-action", help="Inspect or explicitly apply an execution action")
    cluster_action.add_argument("action_id")
    cluster_action.add_argument("--operation", required=True, choices=("stage", "submit", "monitor", "retrieve"))
    cluster_action.add_argument("--output", type=Path)
    cluster_action.add_argument("--apply", action="store_true", help="Perform remote mutation/transfer; monitor is read-only")
    historical = sub.add_parser("import-analysis", help="Import historical result CSVs with their original provenance")
    historical.add_argument("run_id")
    historical.add_argument("--plan", action="store_true", help="Preview historical records without writing")
    decision = sub.add_parser("decide", help="Rehash analysis evidence and show the permitted decision; no execution")
    decision.add_argument("run_id")
    bind = sub.add_parser("bind", help="Compile one binding result using an explicitly reviewed shared solvent")
    bind.add_argument("run_id")
    bind.add_argument("--compatibility", required=True, type=Path)
    sampling = sub.add_parser("sample-clusters", help="Record one five-minute cluster snapshot and atomically refresh local reports")
    sampling.add_argument("--publish-dir", required=True, type=Path)
    view = sub.add_parser("status", help="Show the overview table or JSON")
    view.add_argument("--dataset")
    view.add_argument("--issues-only", action="store_true")
    view.add_argument("--json", action="store_true")
    show = sub.add_parser("show", help="Inspect a run and its observation history")
    show.add_argument("run_id")
    report = sub.add_parser("report", help="Write a standalone read-only HTML console")
    report.add_argument("--output", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command == "init":
            print(f"Registered {initialize(args.registry, args.manifest, args.source_root)} runs; no source files changed.")
        elif args.command == "cluster-plan":
            from .execution import make_plan
            print(json.dumps(make_plan(args.registry, args.run_id, args.manifest), indent=2, allow_nan=False))
        elif args.command == "cluster-action":
            from .execution import action
            result = action(args.registry, args.action_id, args.operation, args.apply, args.output)
            print(json.dumps(result, indent=2, allow_nan=False))
            if result.get("state") in ("failed", "unknown", "paused"):
                parser.exit(1, "Cluster action needs attention; inspect the evidence above.\n")
        elif args.command == "prepare":
            from .preparation import prepare
            result = prepare(args.registry, args.compound, args.manifest, args.output, args.stage, args.plan)
            print(json.dumps(result, indent=2, allow_nan=False))
        elif args.command == "import-analysis":
            from .analysis import import_analysis
            print(json.dumps(import_analysis(args.registry, args.run_id, args.plan), indent=2, allow_nan=False))
        elif args.command == "bind":
            from .analysis import binding
            print(json.dumps(binding(args.registry, args.run_id, args.compatibility), indent=2, allow_nan=False))
        elif args.command in ("analyse", "decide"):
            from .analysis import analyse, current_decision
            result = (analyse(args.registry, args.run_id, args.cutoff_ns, args.plan) if args.command == "analyse"
                      else current_decision(args.registry, args.run_id))
            print(json.dumps(result, indent=2, allow_nan=False))
            if result.get("action") == "blocked":
                parser.exit(1, "Analysis/decision blocked; inspect the reason above.\n")
        elif args.command == "scan":
            count = scan(args.registry, lambda done, total: print(f"Observing local files: {done}/{total} runs", file=sys.stderr, flush=True))
            print(f"Observed {count} runs; no source files changed.")
        elif args.command == "sample-clusters":
            refreshed = sample_clusters(args.registry, args.publish_dir)
            print("Cluster snapshots and console refreshed." if refreshed else "Another sampler is active; skipped.")
            if refreshed:
                metadata, _ = read_registry(args.registry)
                failures = [alias for alias, record in metadata.get('connections', {}).items()
                            if record.get('state') != 'connected' or any(record.get(key, {}).get('state') != 'observed'
                            for key in ('scheduler', 'capacity', 'limits' if alias == 'archer' else 'ownership'))]
                if failures:
                    parser.exit(1, 'Collection incomplete (console updated): ' + ', '.join(failures) + '\n')
        elif args.command == "check-connections":
            for alias, record in check_connections(args.registry).items():
                print(f"{alias}: {record['state']} · scheduler {record['scheduler']['state']}")
        else:
            metadata, rows = read_registry(args.registry)
            if args.command == "status":
                if args.dataset:
                    if args.dataset not in {item["id"] for item in metadata["datasets"]}:
                        raise ValueError(f"Unknown dataset: {args.dataset}")
                    rows = [row for row in rows if row["spec"]["dataset"] == args.dataset]
                if args.issues_only:
                    rows = [row for row in rows if row["incidents"]]
                print(json.dumps({"metadata": metadata, "runs": rows}, indent=2, allow_nan=False) if args.json else status(metadata, rows))
            elif args.command == "show":
                selected = next((row for row in rows if row["spec"]["id"] == args.run_id), None)
                if selected is None:
                    raise ValueError(f"Unknown run ID: {args.run_id}")
                with closing(readonly(args.registry)) as db:
                    selected["history"] = [{"revision": row["revision"], "observed_at": row["observed_at"],
                                             "observation": json.loads(row["observation"])} for row in
                                            db.execute("SELECT * FROM history WHERE run_id=? ORDER BY revision", (args.run_id,))]
                print(json.dumps(selected, indent=2, allow_nan=False))
            elif args.command == "report":
                check_destination(args.output, metadata, [row["spec"] for row in rows])
                if args.output.resolve() == args.registry.resolve():
                    raise ValueError("Cannot overwrite the registry with a report")
                with args.output.open("x") as stream:
                    stream.write(report_html(metadata, rows))
                print(f"Read-only HTML console: {args.output.resolve()}")
    except (OSError, ValueError, KeyError, TypeError, sqlite3.Error) as exc:
        parser.exit(2, f"abflow-campaign: {exc}\n")


if __name__ == "__main__":
    main()
