import csv
import fcntl
import json
import sqlite3
from contextlib import closing
from pathlib import Path

import abfe.analysis.convergence as analysis_convergence
import abfe.analysis.fingerprints as analysis_fingerprints
import abfe.analysis.incidents as analysis_incidents
import abfe.analysis.records as analysis_records
import abfe.analysis.values as analysis_values


def historical_csv(path):
    with Path(path).open(newline="") as stream:
        table = list(csv.reader(stream, strict=True))
    if len(table) < 2:
        raise ValueError(f"Empty historical CSV: {path}")
    columns, rows = table[0], table[1:]
    names = [name for name in columns if name]
    if len(names) != len(set(names)):
        raise ValueError(f"Duplicate CSV columns: {path}")
    convergence = Path(path).name == "ABFE_convergence.csv"
    required = (
        {"Forward", "Backward", "data_fraction"}
        if convergence
        else {"MBAR", "MBAR_Error"}
    )
    if not required <= set(columns):
        raise ValueError(f"Missing result columns: {path}")
    numeric = {
        i: name
        for i, name in enumerate(columns)
        if name
        in {
            "MBAR",
            "BAR",
            "TI",
            "MBAR_Error",
            "BAR_Error",
            "TI_Error",
            "Forward",
            "Backward",
            "Forward_Error",
            "Backward_Error",
            "data_fraction",
        }
    }
    for row in rows:
        if len(row) != len(columns):
            raise ValueError(f"Inconsistent CSV row width: {path}")
        for i, name in numeric.items():
            value = analysis_values.finite(row[i])
            if (name.endswith("_Error") and value < 0) or (
                name == "data_fraction" and not 0 < value <= 1
            ):
                raise ValueError(f"Invalid {name}: {path}")
    labels = [i for i in range(len(columns)) if i not in numeric]
    keys = [tuple(row[i] for i in labels) for row in rows]
    if not convergence and labels and len(keys) != len(set(keys)):
        raise ValueError(f"Duplicate result rows: {path}")
    result = dict(columns=columns, rows=rows)
    if convergence:
        fractions = [
            analysis_values.finite(row[columns.index("data_fraction")]) for row in rows
        ]
        if len(fractions) != len(set(fractions)):
            raise ValueError(f"Duplicate convergence fractions: {path}")
        result["reported_absolute_half_difference"] = (
            analysis_convergence.half_difference(path)
        )
    return result


def import_analysis(registry, run_id, plan=False):
    """Archive small historical result tables separately from validated analyses."""
    from abfe.registry.manifest import check_destination

    registry = Path(registry)
    metadata_, rows, row = analysis_records.selected(registry, run_id)
    spec = row["spec"]
    if spec["leg"] not in ("complex", "solvent") or not spec.get("path"):
        raise ValueError("Import requires a mapped complex or solvent run")
    root = Path(spec["path"]).parent / "analysis"
    directory = root / ("complex" if spec["leg"] == "complex" else "ligand")

    def collect():
        paths = [
            directory / name for name in ("ABFE_summary.csv", "ABFE_convergence.csv")
        ]
        if spec["leg"] == "complex":
            paths.append(root / "abfe_results.csv")
        imported = []
        for path in paths:
            if path.is_file():
                identity = analysis_fingerprints.fingerprint(path)
                imported.append(dict(identity=identity, table=historical_csv(path)))
        if not imported:
            raise ValueError(f"No historical result CSVs found for {run_id}")
        solvent = root / "ligand"
        references = (
            [
                analysis_fingerprints.fingerprint(path)
                for name in ("ABFE_summary.csv", "ABFE_convergence.csv")
                if (path := solvent / name).is_file()
            ]
            if spec["leg"] == "complex"
            else []
        )
        identities = [item["identity"] for item in imported] + references
        for identity in identities:
            if analysis_fingerprints.fingerprint(identity["source_path"]) != identity:
                raise ValueError("Historical sources changed during import")
        sources = [
            dict(path=item["path"], sha256=item["sha256"]) for item in identities
        ]
        return dict(
            id="historical-"
            + analysis_values.digest(
                dict(
                    run_id=run_id, mapping=analysis_values.digest(spec), sources=sources
                )
            )[:32],
            run_id=run_id,
            kind="historical",
            state="historical_unverified",
            analysed_ns=None,
            source_mapping_sha256=analysis_values.digest(spec),
            processing=None,
            units=None,
            uncertainty_convention="legacy_as_reported",
            files=imported,
            shared_solvent=dict(
                path=str(solvent.resolve()),
                files=references,
                compatibility="unverified",
            )
            if references
            else None,
        )

    if plan:
        return dict(action="preview", **collect())
    output_root = registry.parent / "analyses"
    check_destination(registry, metadata_, [r["spec"] for r in rows])
    check_destination(output_root, metadata_, [r["spec"] for r in rows])
    if any(
        item["status"] == "open" and item["code"] == "analysis_import"
        for item in row["incidents"]
    ):
        raise ValueError(
            "Resolve the previous analysis_import incident before retrying this import"
        )
    with (registry.parent / "analysis.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            record = collect()
            with closing(sqlite3.connect(registry)) as db:
                if db.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='analyses'"
                ).fetchone():
                    existing = db.execute(
                        "SELECT record FROM analyses WHERE id=?", (record["id"],)
                    ).fetchone()
                    if existing:
                        return dict(
                            action="already_imported", **json.loads(existing[0])
                        )
            output = output_root / record["id"]
            if output.exists():
                raise ValueError(
                    "An unregistered import directory exists; review it before retrying"
                )
            record.update(
                created_at=analysis_values.timestamp(), output=str(output.resolve())
            )
            output.mkdir(parents=True)
            analysis_records.persist(registry, record, output)
            return dict(action="imported", **record)
        except (
            OSError,
            ValueError,
            KeyError,
            TypeError,
            csv.Error,
            sqlite3.Error,
        ) as exc:
            analysis_incidents.incident(registry, run_id, "analysis_import", str(exc))
            raise ValueError(
                f"Historical import blocked; analysis_import incident recorded: {exc}"
            ) from exc
