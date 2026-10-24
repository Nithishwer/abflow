import json
from pathlib import Path

import abfe.registry.values as registry_values


def dataset_status_state(row):
    observed = row["observation"] or {}
    return json.loads(
        registry_values.encode(
            dict(
                path=row["spec"]["path"],
                analysis=observed.get("analysis", {}),
                production_ns=observed.get("production_ns"),
                target_ns=observed.get("target_ns"),
                stage=observed.get("stage"),
                issues=observed.get("issues", []),
                incidents=[
                    {key: incident.get(key) for key in ("code", "message", "status")}
                    for incident in row["incidents"]
                ],
            )
        )
    )


def accepted_dataset_status(row):
    accepted = row.get("dataset_status_acceptance", {})
    if (
        accepted.get("status") not in ("simulation", "complete")
        or not accepted.get("authorization")
        or accepted.get("state") != dataset_status_state(row)
        or not accepted.get("source_path")
        or not Path(accepted["source_path"]).is_dir()
    ):
        return None
    analysis = (row["observation"] or {}).get("analysis", {})
    if (
        analysis.get("state") != "historical_imported"
        or row["spec"]["leg"] == "vanilla"
        or analysis.get("convergence")
        != (
            "historically_converged"
            if accepted["status"] == "complete"
            else "historically_unconverged"
        )
    ):
        return None
    # shortcut: reviewed dataset colour only; scientific gates and incidents stay unchanged.
    return accepted


def compound_status(row, view):
    """Summarize display evidence without promoting old results or endpoints."""
    accepted = accepted_dataset_status(row)
    if accepted and not view["jobs"]:
        return accepted["status"]
    if (
        row.get("accepted_analysis_review")
        and view["stage"] == "complete"
        and view["remarks"] != "Attention required"
    ):
        return "complete"
    if view["remarks"] == "Attention required" or (row["observation"] or {}).get(
        "analysis", {}
    ).get("state") in ("invalid", "failed", "stale", "possibly_stale", "superseded"):
        return "attention"
    if any(
        job["state"] in ("PENDING", "CONFIGURING", "REQUEUED", "RUNNING", "COMPLETING")
        for job in view["jobs"]
    ):
        return "simulation"
    if (row["spec"]["leg"] == "vanilla" and view["stage"] == "complete") or (
        row["spec"]["leg"] != "vanilla"
        and (
            view["convergence"] == "converged"
            or (
                row.get("historical_provenance")
                and view["convergence"] == "converged (historical)"
            )
        )
    ):
        return "complete"
    if (
        row["spec"]["leg"] != "vanilla"
        and ((row["observation"] or {}).get("production_ns") or 0) > 0
    ):
        return "simulation"
    if not row["observation"] or view["stage"] in ("not_started", "inputs", "prepared"):
        return "not_started"
    return "attention"
