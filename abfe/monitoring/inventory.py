import sqlite3
from contextlib import closing
from pathlib import Path

import abfe.monitoring.inspect as monitoring_inspect
import abfe.monitoring.progress as monitoring_progress
import abfe.registry.manifest as registry_manifest
import abfe.registry.read as registry_read
import abfe.registry.values as registry_values


def scan(registry, progress=None, run_ids=None):
    """Inspect all runs, or the selected runs and their direct dependencies/dependants."""
    metadata, rows = registry_read.read_registry(registry)
    registry_manifest.check_destination(
        registry, metadata, [row["spec"] for row in rows]
    )
    observations = []
    selected = rows
    if run_ids is not None:
        requested = set(run_ids)
        if requested - {row["spec"]["id"] for row in rows}:
            raise ValueError("Unknown run selected for evidence refresh")
        related = requested | {
            dependency
            for row in rows
            if row["spec"]["id"] in requested
            for dependency in row["spec"]["dependencies"]
        }
        selected = [
            row
            for row in rows
            if row["spec"]["id"] in related
            or requested.intersection(row["spec"]["dependencies"])
        ]
    for row in selected:
        spec = row["spec"]
        try:
            observation = monitoring_inspect.inspect_run(spec)
        except (OSError, ValueError, KeyError) as exc:
            observation = {
                "stage": "observation_error",
                "production_ns": None,
                "target_ns": None,
                "scheduler": {"state": "unknown"},
                "transfer": {"state": "unknown"},
                "analysis": {
                    "state": "unknown",
                    "analysed_ns": None,
                    "convergence": "unverified",
                },
                "evidence": [],
                "issues": [],
                "next_action": "Inspect observation error",
            }
            monitoring_progress.issue(observation, "observation_error", str(exc))
        if (
            row.get("analysis_record", {}).get("state") == "validated"
            and (row.get("observation") or {}).get("analysis", {}).get("state")
            == "validated"
        ):
            # Old CSVs remain evidence, but a current managed analysis supersedes their missing provenance.
            observation["issues"] = [
                item
                for item in observation["issues"]
                if item["code"]
                not in ("analysis_provenance", "newer_source_files", "invalid_analysis")
            ]
        if row.get("historical_provenance"):
            observation["issues"] = [
                item
                for item in observation["issues"]
                if item["code"] != "analysis_provenance"
            ]
        for detected in row["incidents"]:
            if detected.get("detected_in_report"):
                monitoring_progress.issue(
                    observation, detected["code"], detected["message"]
                )
        previous = row["observation"] or {}
        if (
            previous.get("stage")
            not in (None, "not_prepared", "inputs_present", "observation_error")
            and spec["path"]
            and not Path(spec["path"]).exists()
        ):
            monitoring_progress.issue(
                observation,
                "source_disappeared",
                "A previously observed run directory is now missing or inaccessible; do not infer that work was reset.",
            )
        if (
            previous.get("production_ns") is not None
            and observation.get("production_ns") is None
        ):
            monitoring_progress.issue(
                observation,
                "production_evidence_lost",
                "Previously observed common production progress can no longer be established; inspect the source evidence.",
            )
        observations.append((spec, observation))
        if progress and (
            len(observations) % 25 == 0 or len(observations) == len(selected)
        ):
            progress(len(observations), len(selected))
    for spec, observation in observations:
        if (
            spec["leg"] == "vanilla"
            and spec["ligand_path"]
            and Path(spec["ligand_path"]).is_dir()
        ):
            expected = {
                Path(row["spec"]["path"]).parent.name
                for row in rows
                if row["spec"]["dataset"] == spec["dataset"]
                and row["spec"]["ligand"] == spec["ligand"]
                and row["spec"]["leg"] == "complex"
                and row["spec"]["path"]
            }
            extra = sorted(
                path.name
                for path in Path(spec["ligand_path"]).glob("abfe*")
                if path.is_dir() and path.name not in expected
            )
            if extra:
                observation["legacy_candidates"] = extra
                monitoring_progress.issue(
                    observation,
                    "unmapped_legacy_runs",
                    f"Additional legacy ABFE directories were preserved without mapping: {extra}",
                )
    observed_at = registry_values.now()
    with closing(sqlite3.connect(registry)) as db, db:
        for spec, observation in observations:
            value = registry_values.encode(observation)
            previous = db.execute(
                "SELECT observation FROM runs WHERE id=?", (spec["id"],)
            ).fetchone()[0]
            if previous != value:
                db.execute(
                    "INSERT INTO history(run_id,observed_at,observation) VALUES (?,?,?)",
                    (spec["id"], observed_at, value),
                )
            db.execute(
                "UPDATE runs SET observation=?,observed_at=? WHERE id=?",
                (value, observed_at, spec["id"]),
            )
            for problem in observation["issues"]:
                db.execute(
                    """INSERT INTO incidents(run_id,code,message,first_seen,last_seen) VALUES (?,?,?,?,?)
                    ON CONFLICT(run_id,code) DO UPDATE SET last_seen=excluded.last_seen,message=excluded.message""",
                    (
                        spec["id"],
                        problem["code"],
                        problem["message"],
                        observed_at,
                        observed_at,
                    ),
                )
        if run_ids is None:
            db.execute(
                "UPDATE metadata SET value=? WHERE key='scanned_at'",
                (registry_values.encode(observed_at),),
            )
    return len(observations)
