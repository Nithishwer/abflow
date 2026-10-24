import abfe.console.palette as console_palette
from abfe.monitoring.jobs import matching_jobs


def next_action(row):
    """Describe workflow readiness separately from simulation evidence."""
    proposal = row.get("next_step")
    if proposal:
        reason = proposal.get("error") or proposal["reason"]
        status = (
            proposal["current_state"]
            if proposal["outcome"] == "proposed"
            else proposal["outcome"]
        )
        action = proposal["proposed_operation"]
        if not action:
            leg = row["spec"]["leg"]
            if (
                proposal["reason"]
                == f"No reviewed {leg} execution profile for this run"
            ):
                action = f"Prepare {leg} launch"
            elif (
                proposal["reason"]
                == "No complete reviewed preparation inputs or execution profile"
            ):
                action = (
                    "Prepare vanilla inputs"
                    if leg == "vanilla"
                    else f"Prepare {leg} leg"
                )
            else:
                action = {"complete": "None required", "waiting": "Wait"}.get(
                    proposal["current_state"], "Review next action"
                )
        return dict(
            action=action,
            status=status.replace("_", " ").capitalize(),
            blocker=reason if status in ("blocked", "failed") else "",
        )
    operators = row.get("operator", [])
    blocked = "; ".join(v["blocked_reason"] for v in operators if v["blocked_reason"])
    return dict(
        action=(
            "; ".join(v["next_action"] or "Waiting" for v in operators)
            if operators
            else (row["observation"] or {}).get("next_action", "Scan local inventory")
        ).replace(
            "Run decide to verify source/output hashes",
            "Review analysis freshness with next-step --dry-run",
        ),
        status="Blocked" if blocked else "",
        blocker=blocked,
    )


def presentation(row, metadata, at=None):
    spec, observed = row["spec"], row["observation"] or {}
    vanilla = spec["leg"] == "vanilla"
    analysis = observed.get("analysis", {})
    matches = matching_jobs(row, metadata, at=at)
    job_ids = [item["job_id"] for item in matches]
    locations = sorted({item["cluster"] for item in matches})
    scheduler_state = (
        ", ".join(
            dict.fromkeys(item["state"].replace("_", " ").title() for item in matches)
        )
        or "Unknown"
    )
    complete = (
        observed.get("completion") == "validated"
        and observed.get("transfer", {}).get("state") == "verified_local"
    )
    completion_conflict = complete and bool(matches)
    complete = complete and not matches
    verified_analysis = (
        analysis.get("state") == "validated"
        and analysis.get("convergence") == "converged"
    )
    stage = {
        "not_prepared": "not_started",
        "inputs_present": "inputs",
        "directory_present": "prepared",
        "windows_present": "prepared",
        "setup_present": "prepared",
        "production_observed": "production",
        "analysis_files_only": "analysis",
        "equilibrating": "equilibration",
    }.get(observed.get("stage"), "unknown")
    if analysis.get("state") in (
        "legacy_unverified",
        "possibly_stale",
        "invalid",
        "validated",
        "stale",
        "failed",
        "planned",
        "superseded",
        "historical_imported",
    ):
        stage = "analysis"
    if verified_analysis and not vanilla:
        stage = "converged"
    if complete:
        stage = "complete"
        locations = ["absolut"]
    elif matches and all(
        item["state"] in ("PENDING", "CONFIGURING", "REQUEUED") for item in matches
    ):
        stage = "queued"
    if (
        (
            analysis.get("state") in ("failed", "stale")
            and not row.get("accepted_analysis_review")
        )
        or analysis.get("decision", {}).get("action") == "stop_at_limit"
        or row["incidents"]
        or observed.get("issues")
        or observed.get("stage") == "observation_error"
        or completion_conflict
    ):
        remarks = "Attention required"
    elif complete or any(
        item["state"] in ("RUNNING", "COMPLETING") for item in matches
    ):
        remarks = "OK"
    elif stage in ("not_started", "inputs", "prepared", "queued"):
        remarks = "Waiting"
    else:
        remarks = "Unknown"
    convergence = "unverified"
    if analysis.get("state") == "validated":
        convergence = analysis.get("convergence", "unverified")
    elif analysis.get("state") == "historical_imported":
        convergence = {
            "historically_converged": "converged (historical)",
            "historically_unconverged": "unconverged (historical)",
        }.get(analysis.get("convergence"), "unverified")
    if (
        row.get("historical_provenance")
        and convergence == "converged (historical)"
        and not matches
        and remarks == "Unknown"
    ):
        remarks = "OK"
    # shortcut: user-accepted legacy display only; replace with completion validation when available.
    accepted_complete = (
        vanilla
        and bool(spec["path"])
        and not matches
        and remarks != "Attention required"
        and metadata.get("accepted_vanilla_completions", {})
        .get("paths", {})
        .get(spec["id"])
        == spec["path"]
        and observed.get("production_ns") == observed.get("target_ns") == 100
    )
    # Accepted remote completion stays tied to the reviewed execution and registered source.
    remote_completion = (
        metadata.get("accepted_vanilla_completions", {})
        .get("remote", {})
        .get(spec["id"], {})
    )
    latest_execution = (row.get("executions") or [{}])[-1]
    accepted_remote = (
        vanilla
        and bool(remote_completion.get("authorization"))
        and bool(remote_completion.get("evidence_sha256"))
        and bool(latest_execution.get("action_id"))
        and not matches
        and remarks != "Attention required"
        and remote_completion.get("registered_spec") == spec
        and remote_completion.get("action_id") == latest_execution.get("action_id")
        and remote_completion.get("execution_at") == latest_execution.get("at")
        and remote_completion.get("plan_fingerprint")
        == latest_execution.get("plan", {}).get("fingerprint")
        and remote_completion.get("remote")
        == latest_execution.get("plan", {}).get("remote")
        and remote_completion.get("production_ns")
        == remote_completion.get("target_ns")
        == 100
    )
    reviewed_complete = (
        row.get("accepted_analysis_review")
        and not matches
        and remarks != "Attention required"
    )
    if reviewed_complete:
        convergence = "converged (user accepted)"
    if accepted_complete or accepted_remote or reviewed_complete:
        stage, remarks = "complete", "OK"
    stage_label = (
        "Complete (remote accepted)"
        if accepted_remote
        else "Complete (user accepted)"
        if accepted_complete or reviewed_complete
        else console_palette.STAGES[stage][0]
    )
    simulation_state = stage_label
    if any(item["state"] in ("RUNNING", "COMPLETING") for item in matches):
        simulation_state = "Running"
    elif not matches and stage in ("not_started", "inputs"):
        simulation_state = "Not started" if vanilla else "Not prepared"
    elif (
        not matches
        and stage == "prepared"
        and vanilla
        and observed.get("stage") == "setup_present"
    ):
        simulation_state = "Not started"
    return {
        "simulation_type": "Vanilla" if vanilla else "Alchemical",
        "leg": "Solvent" if spec["leg"] == "solvent" else "Complex",
        "repeat": 1 if spec["leg"] == "solvent" else spec["repeat"],
        "stage": stage,
        "stage_label": stage_label,
        "simulation_state": simulation_state,
        "convergence": "N/A" if vanilla else convergence,
        "job_id": ", ".join(dict.fromkeys(job_ids)) or "Unknown",
        "location": ", ".join(locations) or "Unknown",
        "scheduler_state": scheduler_state,
        "remarks": remarks,
        "jobs": matches,
    }
