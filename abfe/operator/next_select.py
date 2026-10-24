"""Select one immediate operation from verified evidence, without running it."""

import copy
import math
from decimal import Decimal
from datetime import datetime
from pathlib import Path

import abfe.analysis.combine as combine
import abfe.analysis.convergence as convergence
import abfe.analysis.fingerprints as fingerprints
import abfe.analysis.inputs as analysis_inputs
import abfe.analysis.policy as policy
import abfe.analysis.provenance as provenance
import abfe.execution.files as files
import abfe.execution.inputs as execution_inputs
import abfe.execution.plan as execution_plan
import abfe.execution.profile as profile
import abfe.monitoring.scheduler as scheduler
from abfe.monitoring.progress import tail, target_duration
import abfe.preparation.manifest as preparation_manifest
from abfe.monitoring.jobs import matching_jobs

OPERATIONS = (
    "prepare-input",
    "plan-submission",
    "upload-jobs",
    "submit-jobs",
    "download-jobs",
    "analyse-dhdl",
    "calculate-affinity",
)
INTERNAL = {
    "prepare-input": "prepare",
    "plan-submission": "plan",
    "upload-jobs": "stage",
    "submit-jobs": "submit",
    "download-jobs": "retrieve",
    "analyse-dhdl": "analyse",
    "calculate-affinity": "bind",
}


class InvalidEvidence(ValueError):
    """Unexpected production-input evidence must become a visible incident."""


def production_evidence(spec):
    """Inspect production endpoints and metadata without hashing simulation data."""
    strides, ends, targets, inputs = [], [], [], []
    for phase, count in spec["windows"].items():
        for number in range(count):
            prod = Path(spec["path"]) / f"{phase}.{number:02d}/prod"
            mdp = prod / f"{phase}.prod.{number:02d}.mdp"
            for path in (mdp, prod / "topol.tpr", prod / "dhdl.xvg"):
                inputs.append(
                    dict(
                        source_path=str(path),
                        path=str(path.resolve(strict=True)),
                        stamp=fingerprints.stamp(path),
                    )
                )
            settings = policy.settings(mdp)
            stride = Decimal(settings["dt"]) * Decimal(settings["nstdhdl"])
            if (
                not stride.is_finite()
                or stride <= 0
                or Decimal(settings["nstdhdl"]) % 1
            ):
                raise ValueError("Invalid DHDL sample interval")
            lines = [
                line
                for line in tail(prod / "dhdl.xvg", 262144).splitlines()
                if line.strip() and not line.startswith(("#", "@"))
            ]
            end = Decimal(lines[-1].split()[0])
            if not end.is_finite() or end <= 0:
                raise ValueError("Invalid production endpoint")
            strides.append(stride)
            ends.append(end)
            targets.append(target_duration(mdp) * 1000)
    if not ends:
        raise ValueError("No required production windows")
    places = max(max(0, -value.as_tuple().exponent) for value in strides)
    scale = 10**places
    interval = Decimal(math.lcm(*(int(value * scale) for value in strides))) / scale
    cutoff = (min(ends) // interval) * interval
    maximum = policy.protocol()["maximum_ps"]
    if (
        max(ends + targets) > maximum
        or not 0 < cutoff <= min(targets)
        or len(set(targets)) != 1
    ):
        raise ValueError(
            "Production endpoints or targets violate the common cutoff or 50 ns cap"
        )
    for item, end in zip(inputs[2::3], ends):
        if end != cutoff:
            with Path(item["path"]).open("rb") as stream:
                while line := stream.readline():
                    if line.strip() and not line.lstrip().startswith((b"#", b"@")):
                        start = stream.tell() - len(line)
                        break
                if not analysis_inputs.contains_cutoff(
                    stream, start, item["stamp"]["size"], cutoff
                ):
                    raise ValueError("DHDL does not contain the exact common cutoff")
    # shortcut: planning trusts file metadata; full input validation runs with analysis.
    for item in inputs:
        if (
            str(Path(item["source_path"]).resolve(strict=True)) != item["path"]
            or fingerprints.stamp(item["path"]) != item["stamp"]
        ):
            raise ValueError("Production inputs changed during inspection")
    return dict(cutoff_ps=float(cutoff), inputs=inputs)


def configuration(row, reviewed, mode, workload=None, registry=None):
    spec = row["spec"]
    workload = (
        workload
        or {
            "vanilla": "vanilla",
            "complex": "complex-production",
            "solvent": "solvent",
        }[spec["leg"]]
    )
    config = copy.deepcopy(reviewed.get("profiles", {}).get(workload))
    if not config:
        raise ValueError(f"No reviewed {workload} execution profile for this run")
    config["mode"] = mode
    location = spec.get("path")
    prepared = row.get("preparation", {})
    if prepared.get("state") == "prepared" and not (
        location and Path(location).is_dir()
    ):
        plan = prepared["plan"]
        if spec["leg"] == "solvent":
            location = plan["solvent"]
        else:
            mapping = next(
                item for item in plan["mappings"] if item["repeat"] == spec["repeat"]
            )
            location = mapping["vanilla" if spec["leg"] == "vanilla" else "complex"]
    if location and Path(location).is_dir():
        relative = profile.relative(config["relative_run"])
        source = Path(location)
        for name in reversed(relative.parts):
            if source.name != name:
                raise ValueError(
                    "Reviewed execution layout does not match the registered source"
                )
            source = source.parent
        config["source"] = str(source)
    profile.config_check(config, spec)
    source, run, _ = execution_inputs.source_check(config, spec)
    snapshot = files.snapshot(source)
    execution_inputs.source_identity(registry, spec, source, run, snapshot)
    return config, snapshot, reviewed.get("limits", {}).get(workload, {})


def select(row, metadata, reviewed, history, rows):
    spec = row["spec"]
    base = dict(
        run_id=spec["id"],
        spec=spec,
        operation=None,
        state="blocked",
        reason="",
        affected_runs=[spec["id"]],
    )

    def result(operation=None, state="ready", reason="", **values):
        return dict(base, operation=operation, state=state, reason=reason, **values)

    if row["incidents"]:
        return result(
            state="blocked",
            reason="Open incidents: "
            + ", ".join(item["code"] for item in row["incidents"]),
        )
    latest = {}
    for event in history:
        if event["run_id"] == spec["id"]:
            latest[event["id"]] = event
    pending = [event for event in latest.values() if event["state"] == "intent"]
    if pending:
        return result(
            state="blocked",
            reason="Interrupted operation requires explicit review",
            incident=dict(
                code="interrupted-operation",
                message="Interrupted operation requires explicit review",
            ),
        )
    timestamps = [
        item["checked_at"]
        for item in metadata.get("connections", {}).values()
        if item.get("checked_at")
    ]
    timestamp = (
        metadata.get("selection_at")
        or metadata.get("scanned_at")
        or max(timestamps, default="1970-01-01T00:00:00+00:00")
    )
    at = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    if matching_jobs(row, metadata, at=at):
        return result(state="waiting", reason="Job queued or running")
    record = row.get("analysis_record")
    decision = None
    if record and record["state"] == "validated":
        provenance.verify(record, spec)
        difference = convergence.half_difference(
            Path(record["output"]) / "ABFE_convergence.csv"
        )
        decision = convergence.decide(
            record["evidence"]["cutoff_ps"], record["evidence"]["target_ps"], difference
        )
        if decision["action"] == "stop_converged":
            compatibility = reviewed.get("compatibility")
            if (
                spec["leg"] == "complex"
                and compatibility
                and row.get("binding_result", {}).get("state") != "validated_snapshot"
            ):
                solvent = next(
                    (
                        other
                        for other in rows
                        if other["spec"]["id"] in spec["dependencies"]
                        and other["spec"]["leg"] == "solvent"
                    ),
                    None,
                )
                if (
                    solvent
                    and not solvent["incidents"]
                    and solvent.get("analysis_record", {})
                    .get("decision", {})
                    .get("action")
                    == "stop_converged"
                ):
                    provenance.verify(solvent["analysis_record"], solvent["spec"])
                    evidence = combine.check_compatibility(
                        compatibility, record, solvent["analysis_record"]
                    )
                    correction = Path(spec["path"]).parent / "boresch/resgen_output.txt"
                    evidence.append(fingerprints.fingerprint(correction))
                    return result(
                        "calculate-affinity",
                        compatibility=compatibility,
                        binding_inputs=evidence,
                        solvent_id=solvent["spec"]["id"],
                        solvent_analysis_id=solvent["analysis_record"]["id"],
                        reason="Compile converged compatible legs",
                    )
            return result(
                state="complete",
                reason="Current validated converged analysis and local data",
            )
        if decision["action"] in ("blocked", "stop_at_limit"):
            return result(state="blocked", reason=decision["reason"])
    connections = metadata.get("connections", {})
    unknown = [
        cluster
        for cluster in scheduler.CLUSTER_ROOTS
        if scheduler.connection_state(connections.get(cluster), at=at) != "connected"
        or connections.get(cluster, {}).get("scheduler", {}).get("state") != "observed"
    ]
    if unknown:
        return result(
            state="blocked",
            reason="Own-job evidence unavailable: " + ", ".join(unknown),
        )
    executions = row.get("executions", [])
    outstanding = [item for item in executions if item["state"] != "retrieved"]
    if len(outstanding) > 1:
        return result(
            state="blocked",
            reason="Multiple outstanding executions require explicit review",
        )
    current = (
        outstanding[0] if outstanding else (executions[-1] if executions else None)
    )
    if current:
        state = current["state"]
        common = dict(
            action_id=current["action_id"],
            execution_state=state,
            config=current["plan"]["config"],
            action_fingerprint=current["plan"]["fingerprint"],
        )
        if state in ("planned", "staged"):
            execution_plan.recheck(current["plan"])
            return result(
                "upload-jobs" if state == "planned" else "submit-jobs", **common
            )
        if state in ("complete", "checkpointed"):
            return result(
                "download-jobs",
                retrieval_output=str(
                    Path(metadata["registry_parent"])
                    / "retrievals"
                    / current["action_id"]
                ),
                **common,
            )
        if state == "retrieved":
            mapping = metadata.get("retrieved_source_mapping:" + current["action_id"])
            if not mapping or mapping["new_path"] != spec.get("path"):
                return result(
                    "download-jobs",
                    retrieval_output=current["transfer"]["output"],
                    reason="Finish registering the verified local download",
                    **common,
                )
        else:
            return result(
                state="blocked", reason="Execution outcome needs review: " + state
            )
    observed = row.get("observation") or {}
    duration = observed.get("production_ns")
    if spec["leg"] in ("complex", "solvent") and duration is not None:
        try:
            evidence = production_evidence(spec)
        except (OSError, ValueError, IndexError, ArithmeticError) as exc:
            raise InvalidEvidence(str(exc)) from exc
        cutoff = evidence["cutoff_ps"] / 1000
        if cutoff >= policy.protocol()["minimum_ps"] / 1000:
            if decision is None or record["evidence"]["cutoff_ps"] != cutoff * 1000:
                return result(
                    "analyse-dhdl",
                    cutoff_ns=cutoff,
                    analysis_evidence=evidence,
                    reason="Analyse the exact common local production cutoff",
                )
            mode = "extend"
        else:
            mode = "continue"
        config, snapshot, limits = configuration(
            row, reviewed, mode, registry=metadata["registry"]
        )
        return result(
            "plan-submission",
            config=config,
            source_snapshot=snapshot,
            limits=limits,
            decision=decision,
            reason=f"Generate {mode} execution plan",
        )
    if spec["leg"] == "vanilla" and duration is not None and duration >= 100:
        return result(
            state="complete",
            reason="100 ns vanilla observed locally; downstream preparation is assessed separately",
        )
    if (
        observed.get("stage")
        not in ("not_prepared", "inputs_present", "analysis_files_only", None)
        or row.get("preparation", {}).get("state") == "prepared"
    ):
        equilibrated = (
            current
            and current["state"] == "retrieved"
            and current["plan"]["config"]["workload"] == "complex-equilibration"
        )
        workload = (
            ("complex-production" if equilibrated else "complex-equilibration")
            if spec["leg"] == "complex"
            else None
        )
        config, snapshot, limits = configuration(
            row,
            reviewed,
            "continue" if duration else "initial",
            workload,
            metadata["registry"],
        )
        return result(
            "plan-submission",
            config=config,
            source_snapshot=snapshot,
            limits=limits,
            reason="Generate execution plan from reviewed prepared inputs",
        )
    preparation = reviewed.get("preparation")
    if preparation:
        locations = metadata.get("coverage", {}).get("locations", {})
        if any(item.get("state") != "checked" for item in locations.values()):
            return result(
                state="blocked",
                reason="Incomplete location coverage cannot establish missing simulations for preparation",
            )
        aliases = {spec["ligand"], spec["ligand"].split("_")[-1]}
        variants = [
            line
            for item in locations.values()
            for line in item.get("variants_and_native_evidence", [])
            if aliases.intersection(Path(line.split("|", 3)[-1]).parts)
        ]
        if variants:
            return result(
                state="blocked",
                reason="Possible legacy/source variants require explicit review before fresh preparation",
            )
        plan = preparation_manifest.load_plan(
            metadata["registry"],
            preparation["compound"],
            preparation["manifest"],
            preparation["output"],
            preparation["stage"],
        )
        affected = [
            run_id
            for run_id in plan["run_ids"]
            if ("/vanilla/" in run_id) == (plan["stage"] == "vanilla")
        ]
        siblings = [other for other in rows if other["spec"]["id"] in affected]
        if Path(plan["output"]).exists() or any(
            (other.get("observation") or {}).get("stage")
            not in (None, "not_prepared", "inputs_present")
            or other["incidents"]
            for other in siblings
        ):
            return result(
                state="blocked",
                reason="Compound preparation would repeat existing work or affect an incident",
            )
        return result(
            "prepare-input",
            preparation=preparation,
            preparation_plan=plan,
            affected_runs=sorted(affected),
            reason="Prepare reviewed inputs once for the affected runs",
        )
    return result(
        state="blocked",
        reason="No complete reviewed preparation inputs or execution profile",
    )
