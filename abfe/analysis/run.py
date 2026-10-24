import fcntl
import platform
import sys
import uuid
from contextlib import redirect_stderr, redirect_stdout
from importlib import metadata
from pathlib import Path

import abfe.analysis.convergence as analysis_convergence
import abfe.analysis.estimate as analysis_estimate
import abfe.analysis.fingerprints as analysis_fingerprints
import abfe.analysis.incidents as analysis_incidents
import abfe.analysis.inputs as analysis_inputs
import abfe.analysis.policy as analysis_policy
import abfe.analysis.provenance as analysis_provenance
import abfe.analysis.records as analysis_records
import abfe.analysis.values as analysis_values

from . import identity


def analyse(registry, run_id, cutoff_ns, plan=False):
    from abfe.monitoring.jobs import matching_jobs
    from abfe.registry.manifest import check_destination

    registry = Path(registry)
    cutoff_ns = analysis_values.finite(cutoff_ns)
    if cutoff_ns <= 0:
        raise ValueError("Cutoff must be positive")
    metadata_, rows, row = analysis_records.selected(registry, run_id)
    check_destination(registry, metadata_, [r["spec"] for r in rows])
    output_root = registry.parent / "analyses"
    check_destination(output_root, metadata_, [r["spec"] for r in rows])
    blocking = [item for item in row["incidents"] if item["status"] == "open"]
    if blocking:
        return dict(
            action="blocked",
            reason="Open incidents require explicit resolution: "
            + ", ".join(item["code"] for item in blocking),
            next_target_ns=None,
        )
    if matching_jobs(row, metadata_):
        return dict(
            action="blocked",
            reason="A fresh scheduler match exists; establish a stable local snapshot first",
            next_target_ns=None,
        )
    with (registry.parent / "analysis.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ValueError("Another local analysis is active") from exc
        output = output_root / uuid.uuid4().hex
        output.mkdir(parents=True)
        record = dict(
            id=output.name,
            run_id=run_id,
            created_at=analysis_values.timestamp(),
            kind="leg",
            state="failed",
            output=str(output.resolve()),
            requested_cutoff_ns=analysis_values.finite(cutoff_ns),
            open_incidents=[
                item for item in row["incidents"] if item["status"] == "open"
            ],
            software={
                "python": sys.version,
                "executable": sys.executable,
                "platform": platform.platform(),
                "analysis_code_sha256": identity.current(),
            },
        )
        try:
            evidence = analysis_inputs.validate_inputs(
                row["spec"], analysis_values.finite(cutoff_ns) * 1000
            )
            record["evidence"] = evidence
            if plan or evidence["cutoff_ps"] < analysis_policy.protocol()["minimum_ps"]:
                record["state"] = "planned"
                record["decision"] = (
                    dict(
                        action="analyse",
                        reason="Validated input plan; no estimator has run",
                        next_target_ns=None,
                    )
                    if evidence["cutoff_ps"] >= analysis_policy.protocol()["minimum_ps"]
                    else analysis_convergence.decide(
                        evidence["cutoff_ps"], evidence["target_ps"]
                    )
                )
            else:
                for name in (
                    "alchemlyb",
                    "pymbar",
                    "numpy",
                    "scipy",
                    "pandas",
                    "jax",
                    "jaxlib",
                ):
                    try:
                        record["software"][name] = metadata.version(name)
                    except metadata.PackageNotFoundError:
                        record["software"][name] = "not installed"
                from loguru import logger

                with (
                    (output / "analysis.log").open("x") as log,
                    redirect_stdout(log),
                    redirect_stderr(log),
                ):
                    sink = logger.add(log)
                    try:
                        record["result"] = analysis_estimate.run_analysis(
                            evidence, output
                        )
                    finally:
                        logger.remove(sink)
                result = record["result"]
                record["software"]["source_files"] = result.pop("software_sources", [])
                if analysis_values.finite(result["uncertainty_kcal_mol"]) < 0:
                    raise ValueError("Negative MBAR uncertainty")
                analysis_values.finite(result["estimate_kcal_mol"])
                record["half_difference_kcal_mol"] = (
                    analysis_convergence.half_difference(
                        output / "ABFE_convergence.csv"
                    )
                )
                record["artifacts"] = [
                    analysis_fingerprints.fingerprint(path)
                    for path in sorted(output.iterdir())
                    if path.is_file()
                ]
                analysis_provenance.verify(record, row["spec"], rehash=True)
                record["decision"] = analysis_convergence.decide(
                    evidence["cutoff_ps"],
                    evidence["target_ps"],
                    record["half_difference_kcal_mol"],
                )
                record["state"] = "validated"
        except Exception as exc:
            record["error"] = f"{type(exc).__name__}: {exc}"
            record["decision"] = analysis_convergence.decide(
                0, 0, blocked=record["error"]
            )
            analysis_records.persist(registry, record, output)
            raise ValueError(
                f"Analysis failed; incident recorded ({record['id']}): {record['error']}"
            ) from exc
        analysis_records.persist(registry, record, output)
        return record


def current_decision(registry, run_id):
    metadata_, _, row = analysis_records.selected(registry, run_id)
    from abfe.monitoring.jobs import matching_jobs

    if matching_jobs(row, metadata_):
        return analysis_convergence.decide(
            0,
            0,
            blocked="A fresh scheduler match exists; establish stable local data first",
        )
    record = row.get("analysis_record")
    if not record or (
        record["state"] != "validated"
        and not (
            record["state"] == "planned"
            and record["evidence"]["cutoff_ps"]
            < analysis_policy.protocol()["minimum_ps"]
        )
    ):
        return analysis_convergence.decide(
            0, 0, blocked="No validated analysis; run analyse --plan"
        )
    if row["incidents"]:
        for detected in row["incidents"]:
            if detected.get("detected_in_report"):
                analysis_incidents.incident(
                    registry, run_id, detected["code"], detected["message"]
                )
        return analysis_convergence.decide(
            0, 0, blocked="Open incidents require explicit resolution"
        )
    try:
        analysis_provenance.verify(record, row["spec"], rehash=True)
        evidence = record["evidence"]
        difference = (
            analysis_convergence.half_difference(
                Path(record["output"]) / "ABFE_convergence.csv"
            )
            if record["state"] == "validated"
            else None
        )
        return dict(
            analysis_id=record["id"],
            **analysis_convergence.decide(
                evidence["cutoff_ps"], evidence["target_ps"], difference
            ),
        )
    except (OSError, ValueError) as exc:
        analysis_incidents.incident(
            registry, run_id, "stale_analysis:" + record["id"], str(exc)
        )
        return analysis_convergence.decide(
            0, 0, blocked=f"Stale/invalid analysis: {exc}"
        )
