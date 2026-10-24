"""Adopt existing result tables under an explicitly authorized timestamp rule."""

import copy
import fcntl
import json
import shutil
from contextlib import closing
from pathlib import Path

from abfe.analysis import convergence, fingerprints, history, incidents, inputs
from abfe.analysis import policy, provenance, records, values
from abfe.operator.next_select import production_evidence
from abfe.registry import manifest, read


def verify_existing(registry, run_id, reference_id, authorization, plan=False):
    if not authorization.strip():
        raise ValueError(
            "Timestamp verification needs explicit script-equivalence authorization"
        )
    registry = Path(registry)
    metadata, rows, row = records.selected(registry, run_id)
    spec = row["spec"]
    if spec["leg"] not in ("complex", "solvent") or not spec.get("path"):
        raise ValueError("Verification requires a mapped alchemical leg")
    with closing(read.readonly(registry)) as db:
        found = db.execute(
            "SELECT record FROM analyses WHERE id=?", (reference_id,)
        ).fetchone()
    if not found:
        raise ValueError("Reference analysis does not exist")
    reference = json.loads(found[0])
    if reference["kind"] != "leg" or reference["state"] != "validated":
        raise ValueError("Reference must be a validated leg analysis")
    reference_spec = next(
        r["spec"] for r in rows if r["spec"]["id"] == reference["run_id"]
    )
    provenance.verify(reference, reference_spec)
    if reference["evidence"]["protocol"] != policy.protocol():
        raise ValueError(
            "Reference processing settings differ from the current protocol"
        )

    previous = row.get("analysis_record")
    if previous and previous["state"] == "validated":
        try:
            provenance.verify(previous, spec)
        except (OSError, ValueError):
            pass
        else:
            return dict(action="already_verified", **previous)
    directory = (
        Path(previous["output"])
        if previous and previous["state"] == "validated"
        else Path(spec["path"]).parent
        / "analysis"
        / ("complex" if spec["leg"] == "complex" else "ligand")
    )
    csvs = [directory / name for name in ("ABFE_summary.csv", "ABFE_convergence.csv")]
    snapshot = production_evidence(spec)
    dhdl_stamps = snapshot["inputs"][2::3]
    newest_dhdl = max(item["stamp"]["mtime_ns"] for item in dhdl_stamps)
    sources = [fingerprints.fingerprint(path) for path in csvs]
    if min(item["stamp"]["mtime_ns"] for item in sources) <= newest_dhdl:
        raise ValueError(
            "Both analysis CSVs must be strictly newer than every required DHDL"
        )
    summary, checked_convergence = [history.historical_csv(path) for path in csvs]
    labels = [i for i, name in enumerate(summary["columns"]) if not name]
    totals = [
        r for r in summary["rows"] if [r[i] for i in labels] == ["Stages", "TOTAL"]
    ]
    if len(totals) != 1:
        raise ValueError("Expected exactly one Stages/TOTAL summary row")
    total = totals[0]
    result = dict(
        estimate_kcal_mol=values.finite(total[summary["columns"].index("MBAR")]),
        uncertainty_kcal_mol=values.finite(
            total[summary["columns"].index("MBAR_Error")]
        ),
    )
    cutoff = (
        previous["evidence"]["cutoff_ps"]
        if previous and previous["state"] == "validated"
        else snapshot["cutoff_ps"]
    )
    historical = row.get("historical_analysis") or {}
    if not previous and historical.get("analysed_ns") is not None:
        cutoff = historical["analysed_ns"] * 1000

    def collect():
        evidence = inputs.validate_inputs(spec, cutoff)
        if production_evidence(spec) != snapshot:
            raise ValueError("Production inputs changed during timestamp verification")
        for item in sources:
            if fingerprints.fingerprint(item["source_path"]) != item:
                raise ValueError("Analysis CSV changed during timestamp verification")
        evidence["inputs"].extend(sources)
        difference = checked_convergence["reported_absolute_half_difference"]
        key = values.digest(
            dict(
                spec=spec, sources=sources, evidence=evidence, reference_id=reference_id
            )
        )[:32]
        # shortcut: timestamps and user attestation replace missing execution receipts;
        # use the original execution receipts if they become available.
        return dict(
            id="verified-" + key,
            run_id=run_id,
            created_at=values.timestamp(),
            kind="leg",
            state="validated",
            output=str((registry.parent / "analyses" / ("verified-" + key)).resolve()),
            requested_cutoff_ns=cutoff / 1000,
            evidence=evidence,
            result=result,
            half_difference_kcal_mol=difference,
            decision=convergence.decide(cutoff, evidence["target_ps"], difference),
            software=dict(
                analysis_code_sha256=reference["software"]["analysis_code_sha256"],
                source_files=copy.deepcopy(
                    reference["software"].get("source_files", [])
                ),
            ),
            verification=dict(
                rule_version=1,
                basis="File timestamps plus user-confirmed script equivalence",
                authorization=authorization,
                reference_analysis_id=reference_id,
                common_processing=copy.deepcopy(reference["evidence"]["protocol"]),
                common_software=copy.deepcopy(reference["software"]),
                units="kcal/mol (user-confirmed common analysis settings)",
                original_execution_details="Not reconstructed; common settings accepted from the reference",
                cutoff_basis="Existing recorded cutoff"
                if (previous and previous["state"] == "validated")
                or (not previous and historical.get("analysed_ns") is not None)
                else "Common production endpoint under the accepted timestamp rule",
                oldest_csv_mtime_ns=min(item["stamp"]["mtime_ns"] for item in sources),
                newest_dhdl_mtime_ns=newest_dhdl,
                source_csvs=sources,
                supersedes=previous["id"] if previous else historical.get("id"),
            ),
        )

    if plan:
        return dict(action="preview", **collect())
    manifest.check_destination(registry, metadata, [r["spec"] for r in rows])
    manifest.check_destination(
        registry.parent / "analyses", metadata, [r["spec"] for r in rows]
    )
    with (registry.parent / "analysis.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        record = collect()
        output = Path(record["output"])
        if output.exists():
            raise ValueError(
                "An existing verification output requires receipt reconciliation"
            )
        output.mkdir(parents=True)
        try:
            for path in csvs:
                shutil.copy2(path, output / path.name)
            record["artifacts"] = [
                fingerprints.fingerprint(output / path.name) for path in csvs
            ]
            if [item["sha256"] for item in record["artifacts"]] != [
                item["sha256"] for item in sources
            ]:
                raise ValueError("Copied analysis CSV content differs from its source")
            provenance.verify(record, spec)
        except (OSError, ValueError) as exc:
            incidents.incident(registry, run_id, "analysis_verification", str(exc))
            raise
        records.persist(registry, record, output)
    return dict(action="verified", **record)
