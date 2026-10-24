import json
import math
import re
import uuid
from pathlib import Path

import abfe.analysis.fingerprints as analysis_fingerprints
import abfe.analysis.provenance as analysis_provenance
import abfe.analysis.records as analysis_records
import abfe.analysis.values as analysis_values


def check_compatibility(path, complex_record, solvent_record):
    """Consume an explicit review tied to exact analysis IDs, never infer reuse."""
    identity = analysis_fingerprints.fingerprint(path)
    review = json.loads(Path(path).read_text())
    if (
        review.get("schema_version") != 1
        or not isinstance(review.get("reviewed_by"), str)
        or not review["reviewed_by"].strip()
    ):
        raise ValueError("Solvent reuse needs a named, explicit compatibility review")
    if (
        review.get("complex_analysis_id") != complex_record["id"]
        or review.get("solvent_analysis_id") != solvent_record["id"]
    ):
        raise ValueError("Compatibility review references different analysis versions")
    keys = (
        "ligand_chemical_state",
        "ligand_parameters",
        "force_fields",
        "solvent_conditions",
        "temperature_K",
        "charge_treatment",
    )
    for key in keys:
        a, b = review["complex"].get(key), review["solvent"].get(key)
        if (
            not a
            or a != b
            or (isinstance(a, str) and a.strip().lower() in ("unknown", "unverified"))
        ):
            raise ValueError(f"Unknown/incompatible solvent reuse: {key}")
    if (
        analysis_values.finite(review["complex"]["temperature_K"])
        != complex_record["evidence"]["temperature_K"]
        or complex_record["evidence"]["temperature_K"]
        != solvent_record["evidence"]["temperature_K"]
    ):
        raise ValueError(
            "Compatibility review temperature differs from analysed inputs"
        )
    evidence = review.get("evidence", [])
    if not evidence:
        raise ValueError("Compatibility review needs parameter/preparation file hashes")
    identities = [identity]
    for item in evidence:
        actual = analysis_fingerprints.fingerprint(item["path"])
        if actual["sha256"] != item["sha256"]:
            raise ValueError("Compatibility review evidence changed")
        identities.append(actual)
    return identities


def binding(registry, run_id, compatibility):
    from abfe.registry.manifest import check_destination

    metadata_, rows, row = analysis_records.selected(registry, run_id)
    spec = row["spec"]
    if spec["leg"] != "complex":
        raise ValueError("Binding results require a complex repeat")
    solvent = next(
        (
            other
            for other in rows
            if other["spec"]["id"] in spec["dependencies"]
            and other["spec"]["leg"] == "solvent"
        ),
        None,
    )
    if solvent is None:
        raise ValueError("Missing registered shared solvent dependency")
    output = Path(registry).parent / "analyses" / uuid.uuid4().hex
    check_destination(output, metadata_, [r["spec"] for r in rows])
    if any(item["status"] == "open" for r in (row, solvent) for item in r["incidents"]):
        raise ValueError("Open incidents block binding compilation")
    output.mkdir(parents=True)
    record = dict(
        id=output.name,
        run_id=run_id,
        created_at=analysis_values.timestamp(),
        kind="binding",
        state="failed",
        output=str(output.resolve()),
    )
    try:
        records = [r.get("analysis_record") for r in (row, solvent)]
        for r, analysis in zip((row, solvent), records):
            if (
                not analysis
                or analysis["state"] != "validated"
                or analysis["decision"]["action"] != "stop_converged"
            ):
                raise ValueError(
                    "Both legs need their own validated converged analysis"
                )
            analysis_provenance.verify(analysis, r["spec"], rehash=True)
        complex_record, solvent_record = records
        identities = check_compatibility(compatibility, complex_record, solvent_record)
        correction_path = Path(spec["path"]).parent / "boresch/resgen_output.txt"
        identities.append(analysis_fingerprints.fingerprint(correction_path))
        corrections = re.findall(
            r"^dG_off:\s*([^,\s]+)", correction_path.read_text(), re.M
        )
        if len(corrections) != 1:
            raise ValueError(
                "Expected one MDRestraintsGenerator dG_off correction in kcal/mol"
            )
        correction = analysis_values.finite(corrections[0])
        c, s = complex_record["result"], solvent_record["result"]
        shared_variance = analysis_values.finite(s["uncertainty_kcal_mol"]) ** 2
        for r, leg_record in zip((row, solvent), records):
            analysis_provenance.verify(leg_record, r["spec"], rehash=True)
        for item in identities:
            if analysis_fingerprints.fingerprint(item["path"]) != item:
                raise ValueError(
                    "Compatibility/correction evidence changed during compilation"
                )
        record.update(
            state="validated",
            dependencies={
                row["spec"]["id"]: complex_record["id"],
                solvent["spec"]["id"]: solvent_record["id"],
            },
            evidence=dict(inputs=identities),
            result=dict(
                estimate_kcal_mol=analysis_values.finite(
                    s["estimate_kcal_mol"] + correction - c["estimate_kcal_mol"]
                ),
                uncertainty_kcal_mol=analysis_values.finite(
                    math.sqrt(c["uncertainty_kcal_mol"] ** 2 + shared_variance)
                ),
                restraint_correction_kcal_mol=correction,
                shared_solvent_analysis_id=solvent_record["id"],
                shared_solvent_variance=shared_variance,
                uncertainty_note="Solvent variance is shared across repeats; do not divide it by the number of complex repeats",
            ),
        )
    except Exception as exc:
        record["error"] = f"{type(exc).__name__}: {exc}"
        analysis_records.persist(registry, record, output)
        raise ValueError(
            f"Binding failed; incident recorded ({record['id']}): {record['error']}"
        ) from exc
    analysis_records.persist(registry, record, output)
    return record


def binding_state(record, rows):
    if record["state"] != "validated":
        return dict(state="failed", id=record["id"], reason=record.get("error"))
    by_id = {row["spec"]["id"]: row for row in rows}
    try:
        for run_id, analysis_id in record["dependencies"].items():
            row = by_id[run_id]
            analysis = row.get("analysis_record")
            if not analysis or analysis["id"] != analysis_id or row["incidents"]:
                raise ValueError("A dependent analysis changed or has open incidents")
            analysis_provenance.verify(analysis, row["spec"])
        for item in record["evidence"]["inputs"]:
            if analysis_fingerprints.stamp(item["path"]) != item["stamp"]:
                raise ValueError("Compatibility/correction evidence changed")
        return dict(
            state="validated_snapshot",
            id=record["id"],
            result=record["result"],
            dependencies=record["dependencies"],
        )
    except (OSError, ValueError, KeyError) as exc:
        return dict(state="outdated", id=record["id"], reason=str(exc))
