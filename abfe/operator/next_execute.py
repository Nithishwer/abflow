"""Frozen-step prerequisites and adaptation to the shared operator dispatcher."""

import sqlite3
from contextlib import closing
from pathlib import Path

import abfe.analysis.fingerprints as fingerprints
import abfe.analysis.records as analysis_records
import abfe.execution.files as files
import abfe.execution.journal as execution_journal
import abfe.execution.transport.dispatch as transport
import abfe.monitoring.scheduler as scheduler
import abfe.operator.advance as advance
import abfe.operator.next_plan as next_plan
import abfe.operator.next_select as selector
import abfe.operator.permissions as permissions
import abfe.operator.records as records
import abfe.operator.view as view
import abfe.registry.read as registry_read


def recheck(registry, entry):
    _, rows, row = analysis_records.selected(registry, entry["run_id"])
    if (row.get("analysis_record") or {}).get("id") != entry.get("analysis_id"):
        raise ValueError("Analysis identity changed since the dry run")
    inventory_ = [
        (item["action_id"], item["state"], item["plan"]["fingerprint"])
        for item in row.get("executions", [])
    ]
    if files.digest(inventory_) != entry["execution_inventory"]:
        raise ValueError("Execution inventory changed since the dry run")
    operation = selector.INTERNAL.get(entry["operation"], entry["operation"])
    if operation != "bind":
        connections = scheduler.check_connections(registry)
        if any(
            scheduler.connection_state(value) != "connected"
            or value.get("scheduler", {}).get("state") != "observed"
            for value in connections.values()
        ) or set(connections) != set(scheduler.CLUSTER_ROOTS):
            raise ValueError("Fresh own-job evidence unavailable before execution")
        from abfe.monitoring.jobs import matching_jobs

        metadata, fresh = registry_read.read_registry(registry)
        if matching_jobs(
            next(item for item in fresh if item["spec"]["id"] == entry["run_id"]),
            metadata,
        ):
            raise ValueError("A job is now writing the saved run")
    if entry.get("action_id"):
        current = execution_journal.load(registry, entry["action_id"])
        if (
            current["state"] != entry["execution_state"]
            or current["plan"]["fingerprint"] != entry["action_fingerprint"]
        ):
            raise ValueError("Execution state or identity changed since the dry run")
        if operation == "retrieve":
            if current["state"] == "retrieved":
                permissions.verified_snapshot(current)
                if files.digest(current["transfer"]) != entry["transfer_digest"]:
                    raise ValueError("Retrieval receipt changed")
            elif (
                transport.remote(current["plan"], "retrieve")["snapshot"]
                != entry["remote_snapshot"]
            ):
                raise ValueError("Remote retrieval snapshot changed since the dry run")
    if (
        operation == "plan"
        and files.snapshot(entry["config"]["source"]) != entry["source_snapshot"]
    ):
        raise ValueError("Execution source changed since the dry run")
    if operation == "analyse":
        if selector.production_evidence(row["spec"]) != entry["analysis_evidence"]:
            raise ValueError("Analysis inputs changed since the dry run")
    if operation == "prepare":
        from abfe.preparation.manifest import load_plan
        from abfe.monitoring.inspect import inspect_run

        if load_plan(registry, **entry["preparation"]) != entry["preparation_plan"]:
            raise ValueError("Preparation plan changed since the dry run")
        for sibling in rows:
            expected = entry["affected_evidence"].get(sibling["spec"]["id"])
            if expected and (
                sibling["incidents"]
                or sibling["spec"] != expected["spec"]
                or (sibling.get("analysis_record") or {}).get("id")
                != expected["analysis_id"]
                or inspect_run(sibling["spec"])["stage"] != expected["stage"]
            ):
                raise ValueError(
                    "An affected preparation sibling changed since the dry run"
                )
    if operation == "bind":
        solvent = next(
            item for item in rows if item["spec"]["id"] == entry["solvent_id"]
        )
        if (
            solvent["incidents"]
            or (solvent.get("analysis_record") or {}).get("id")
            != entry["solvent_analysis_id"]
        ):
            raise ValueError("Shared solvent analysis changed")
        for item in entry["binding_inputs"]:
            if fingerprints.fingerprint(item["source_path"]) != item:
                raise ValueError("Binding evidence changed")
    return row


def frozen(registry, scope_id, scope, run_id, operation):
    if scope.get("kind") != "next-step" or scope["registry"] != str(registry):
        raise ValueError("Select a next-step proposal for this registry")
    entry = records.entry_for(scope, run_id)
    if operation != entry["operation"]:
        raise ValueError("Command does not match the frozen operation")
    if scope["code_identity"] != next_plan.code_identity():
        raise ValueError("ABFlow implementation changed; generate a new dry run")
    with closing(registry_read.readonly(registry)) as db:
        history = [
            event
            for event in records.events(db)
            if event["scope_id"] == scope_id and event["run_id"] == run_id
        ]
    previous = history[-1] if history else None
    if previous and previous["state"] in ("succeeded", "reconciled"):
        view.publish(registry)
        return dict(state="already_completed", run_id=run_id, operation=operation)
    if previous and previous["state"] == "intent":
        message = "Interrupted operation requires explicit review"
        records.incident(registry, run_id, "interrupted-operation", message)
        view.publish(registry)
        raise ValueError(message)
    if previous and previous["state"] == "failed":
        raise ValueError(
            "Saved operation failed; resolve its incident and generate a new dry run"
        )
    context = dict(
        id=files.digest(dict(scope=scope_id, run_id=run_id)),
        scope_id=scope_id,
        run_id=run_id,
        operation=selector.INTERNAL.get(operation, operation),
        explicit_next_step=True,
        action_id=entry.get("action_id"),
        max_submitted=entry.get("limits", {}),
        previous_analysis_id=entry.get("analysis_id"),
        authorization="Explicit next-step invocation for saved plan " + scope_id,
    )
    return advance.run(registry, scope, entry, context)


def execute(registry, scope_id, run_id, operation):
    return advance.step(
        Path(registry).resolve(),
        scope_id,
        apply=True,
        run_id=run_id,
        expected_operation=operation,
    )


def campaign(registry, dry_run=False):
    """Preview or execute the same fixed selection, without automatic progression."""
    scope = next_plan.propose(registry)
    result = dict(state="preview", **next_plan.summarize(scope))
    if dry_run:
        return result
    scope_id = next_plan.save(registry, scope)
    outcomes = []
    for entry in scope["runs"]:
        if not entry["operation"]:
            continue
        try:
            outcome = execute(registry, scope_id, entry["run_id"], entry["operation"])
        except (OSError, ValueError, KeyError, TypeError, sqlite3.Error) as exc:
            outcome = dict(state="failed", error=str(exc))
        outcomes.append(
            dict(outcome, run_id=entry["run_id"], operation=entry["operation"])
        )
    view.publish(registry)
    incomplete = result["counts"]["blocked"] or any(
        outcome["state"] not in ("succeeded", "reconciled", "already_completed")
        for outcome in outcomes
    )
    return dict(
        result,
        state="incomplete" if incomplete else "completed",
        plan_id=scope_id,
        outcomes=outcomes,
    )
