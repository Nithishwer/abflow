import hashlib
import re
import sqlite3
from contextlib import closing
from pathlib import Path

import yaml

import abfe.registry.values as registry_values

SCHEMA_VERSION = 1


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
    try:
        manifest = yaml.safe_load(Path(path).read_text())
    except yaml.YAMLError as exc:
        raise ValueError(f"Invalid campaign YAML: {exc}") from exc
    if not isinstance(manifest, dict):
        raise ValueError("Campaign manifest must be a YAML mapping")
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
        if expected is not None and (
            type(expected) is not int or expected < 1 or expected != len(ligands)
        ):
            raise ValueError(
                f"{dataset_id}: expected_ligands must match the explicit ligand list"
            )
        datasets.append(
            {
                "id": dataset_id,
                "label": dataset["label"],
                "expected_ligands": expected,
                "registered_ligands": len(ligands),
            }
        )
        ligand_ids = set()
        for entry in ligands:
            ligand = {"id": entry} if isinstance(entry, str) else entry
            ligand_id = ligand["id"]
            if (
                not re.fullmatch(r"[A-Za-z0-9_-]+", ligand_id)
                or ligand_id in ligand_ids
            ):
                raise ValueError(
                    f"{dataset_id}: ligand IDs must be unique simple names"
                )
            ligand_ids.add(ligand_id)
            for key in ("vanilla_paths", "complex_paths"):
                if any(repeat not in ("1", "2", "3") for repeat in ligand.get(key, {})):
                    raise ValueError(
                        f"{dataset_id}/{ligand_id}: {key} may only map repeats 1, 2, 3"
                    )
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
            common = {
                "dataset": dataset_id,
                "ligand": ligand_id,
                "input_files": inputs,
                "ligand_path": base,
                "source_root": str(root),
            }
            for repeat in (1, 2, 3):
                vanilla = source_path(
                    root, ligand.get("vanilla_paths", {}).get(str(repeat))
                )
                complex_path = source_path(
                    root, ligand.get("complex_paths", {}).get(str(repeat))
                )
                if base:
                    vanilla = vanilla or str(Path(base) / f"vanilla_rep_{repeat}")
                    complex_path = complex_path or str(
                        Path(base) / f"abfe_van{repeat}_hrex_r1/complex"
                    )
                for leg, location, windows, dependencies in (
                    ("vanilla", vanilla, {}, []),
                    (
                        "complex",
                        complex_path,
                        dataset.get("complex_windows", {}),
                        [f"{dataset_id}/{ligand_id}/vanilla/{repeat}", solvent_id],
                    ),
                ):
                    specs.append(
                        dict(
                            common,
                            id=f"{dataset_id}/{ligand_id}/{leg}/{repeat}",
                            leg=leg,
                            repeat=repeat,
                            path=location,
                            windows=windows,
                            dependencies=dependencies,
                        )
                    )
            specs.append(
                dict(
                    common,
                    id=solvent_id,
                    leg="solvent",
                    repeat=None,
                    path=solvent,
                    windows=dataset.get("solvent_windows", {}),
                    dependencies=[],
                    shared_by=[
                        f"{dataset_id}/{ligand_id}/complex/{r}" for r in (1, 2, 3)
                    ],
                    compatibility="unverified",
                )
            )
    if not datasets:
        raise ValueError("Manifest needs at least one dataset")
    used_paths = {}
    for spec in specs:
        if spec["leg"] != "vanilla":
            if not spec["windows"] or any(
                not re.fullmatch(r"[a-z]+", phase)
                or type(count) is not int
                or count < 1
                for phase, count in spec["windows"].items()
            ):
                raise ValueError(
                    f"{spec['id']}: explicit positive window counts are required"
                )
        if spec["path"]:
            canonical = str(Path(spec["path"]).resolve())
            if canonical in used_paths:
                raise ValueError(
                    f"Two runs reference the same source directory: {used_paths[canonical]}, {spec['id']}"
                )
            used_paths[canonical] = spec["id"]
    return {
        "name": manifest["name"],
        "datasets": datasets,
        "source_root": str(root),
        "manifest_path": str(Path(path).resolve()),
        "manifest_sha256": hashlib.sha256(Path(path).read_bytes()).hexdigest(),
    }, specs


def check_destination(path, metadata, specs):
    destination = Path(path).resolve()
    protected = [Path(metadata["source_root"])]
    protected += [
        Path(item[key])
        for item in specs
        for key in ("path", "ligand_path", "embedding")
        if item.get(key)
    ]
    protected += [Path(item) for spec in specs for item in spec["input_files"]]
    if any(destination == item or item in destination.parents for item in protected):
        raise ValueError(
            "Registry/report destination must be outside all simulation and input source trees"
        )
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
            metadata.update(
                schema_version=SCHEMA_VERSION,
                created_at=registry_values.now(),
                scanned_at=None,
            )
            db.executemany(
                "INSERT INTO metadata VALUES (?, ?)",
                [
                    (key, registry_values.encode(value))
                    for key, value in metadata.items()
                ],
            )
            db.executemany(
                "INSERT INTO runs(id, spec) VALUES (?, ?)",
                [(item["id"], registry_values.encode(item)) for item in specs],
            )
    except Exception:
        registry.unlink(missing_ok=True)
        raise
    return len(specs)
