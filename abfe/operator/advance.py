import json
import sqlite3
import uuid
from contextlib import closing
from pathlib import Path

import abfe.analysis.combine as analysis_combine
import abfe.analysis.fingerprints as analysis_fingerprints
import abfe.analysis.records as analysis_records
import abfe.analysis.run as analysis_run
import abfe.execution.dispatch as execution_dispatch
import abfe.execution.files as files
import abfe.execution.journal as execution_journal
import abfe.execution.plan as execution_plan
import abfe.monitoring.inventory as inventory
import abfe.preparation.run as preparation_run
import abfe.operator.permissions as operator_permissions
import abfe.operator.records as operator_records
import abfe.operator.select as operator_select
import abfe.operator.sources as operator_sources
import abfe.operator.view as operator_view
import abfe.registry.read as registry_read


def step(
    registry, scope_id, apply=False, run_id=None, monitor=False, expected_operation=None
):
    if not apply:
        return operator_select.overview(registry, scope_id)
    with operator_records.lock(registry):
        scope = operator_records.load(registry, scope_id)
        if expected_operation is not None and scope.get("kind") != "next-step":
            raise ValueError("Select a next-step proposal for this registry")
        if scope.get("kind") == "next-step":
            if expected_operation is None:
                raise ValueError("Use explicit next-step commands for this proposal")
            from abfe.operator.next_execute import frozen

            return frozen(registry, scope_id, scope, run_id, expected_operation)
        _, scoped_rows = registry_read.read_registry(registry)
        for row in scoped_rows:
            if any(e["run_id"] == row["spec"]["id"] for e in scope["runs"]):
                for detected in row["incidents"]:
                    if detected.get("detected_in_report"):
                        operator_records.incident(
                            registry,
                            row["spec"]["id"],
                            detected["code"],
                            detected["message"],
                        )
        with closing(registry_read.readonly(registry)) as db:
            history = [
                h for h in operator_records.events(db) if h["scope_id"] == scope_id
            ]
        latest = {}
        for event in history:
            latest[event["id"]] = event
        pending = next(
            (
                h
                for h in latest.values()
                if h["state"] == "intent" and (run_id is None or h["run_id"] == run_id)
            ),
            None,
        )
        if pending and not monitor:
            message = "Interrupted operation requires explicit review"
            operator_records.incident(
                registry, pending["run_id"], "interrupted-operation", message
            )
            operator_view.publish(registry)
            raise ValueError(message)
        choices = operator_select.overview(registry, scope_id)["runs"]
        if run_id is not None:
            choices = [r for r in choices if r["run_id"] == run_id]
            if not choices:
                raise ValueError("Run is outside this scope")
        choices.sort(
            key=lambda r: max(
                (i for i, h in enumerate(history) if h["run_id"] == r["run_id"]),
                default=-1,
            )
        )
        chosen = next(
            (r for r in choices if r["next_action"] and not r["blocked_reason"]), None
        )
        if monitor:
            if run_id is None:
                raise ValueError("Explicit monitoring requires --run-id")
            chosen = dict(run_id=run_id, next_action="monitor")
        if chosen is None:
            operator_view.publish(registry)
            return dict(state="waiting", runs=choices)
        entry = operator_records.entry_for(scope, chosen["run_id"])
        _, _, row = analysis_records.selected(registry, entry["run_id"])
        per_run = [h for h in history if h["run_id"] == entry["run_id"]]
        current = operator_select.execution_for(entry, per_run, row)
        operation = chosen["next_action"]
        context = dict(
            id=uuid.uuid4().hex,
            scope_id=scope_id,
            run_id=entry["run_id"],
            operation=operation,
            action_id=current["action_id"] if current else None,
            max_submitted=scope["max_submitted"],
            previous_analysis_id=(row.get("analysis_record") or {}).get("id"),
            command=dict(
                operation=operation,
                manifest=entry.get("manifest_identity"),
                cutoff_ns=entry.get("cutoff_ns"),
                retrieval_output=entry.get("retrieval_output"),
            ),
        )
        return run(registry, scope, entry, context)


def perform(registry, scope, entry, context):
    operation, run_id = context["operation"], entry["run_id"]
    if operation == "plan":
        if scope.get("kind") == "next-step":
            output = (
                Path(registry).parent / "executions" / ("next-step-" + context["id"])
            )
            output.mkdir(parents=True, exist_ok=True)
            manifest = output / "execution.json"
            if (
                manifest.exists()
                and json.loads(manifest.read_text()) != entry["config"]
            ):
                raise ValueError(
                    "Existing execution manifest conflicts with the saved plan"
                )
            files.write(manifest, entry["config"])
        else:
            manifest = entry["manifest"]
            if (
                analysis_fingerprints.fingerprint(manifest)
                != entry["manifest_identity"]
                or files.snapshot(entry["config"]["source"]) != entry["source_snapshot"]
            ):
                raise ValueError("Reviewed execution manifest/source changed")
        return execution_plan.make_plan(registry, run_id, manifest)
    if operation in ("stage", "submit", "monitor", "retrieve", "map-source"):
        if not context.get("action_id"):
            raise ValueError("No execution to operate")
        if operation == "map-source":
            return operator_sources.map_source(registry, context["action_id"])
        if operation == "retrieve":
            current = execution_journal.load(registry, context["action_id"])
            if current["state"] != "retrieved":
                current = execution_dispatch.action(
                    registry,
                    context["action_id"],
                    operation,
                    apply=True,
                    output=entry.get("retrieval_output"),
                )
            if current["state"] == "retrieved":
                operator_sources.map_source(registry, context["action_id"])
            return current
        return execution_dispatch.action(
            registry,
            context["action_id"],
            operation,
            apply=True,
            output=entry.get("retrieval_output"),
        )
    if operation == "analyse":
        return analysis_run.analyse(registry, run_id, entry["cutoff_ns"])
    if operation == "bind":
        return analysis_combine.binding(registry, run_id, entry["compatibility"])
    if operation == "prepare":
        return preparation_run.prepare(registry, **entry["preparation"])
    if operation == "decide":
        return analysis_run.current_decision(registry, run_id)
    raise ValueError("Unknown operator operation: " + operation)


def run(registry, scope, entry, context):
    """Both interfaces share execution, journaling and publication."""
    frozen = scope.get("kind") == "next-step"
    token = operator_records.CURRENT.set(context)
    publication_failed = False
    try:
        operator_permissions.authorize(
            registry,
            entry["run_id"],
            context["operation"],
            cutoff_ns=entry.get("cutoff_ns")
            if context["operation"] == "analyse"
            else None,
        )
        if frozen:
            from abfe.operator.next_execute import recheck

            row = recheck(registry, entry)
            context["previous_binding_id"] = (row.get("binding_result") or {}).get("id")
        operator_records.journal(registry, context, "intent")
        try:
            operator_view.publish(registry)
        except Exception:
            publication_failed = True
            operator_records.journal(
                registry,
                context,
                "waiting",
                reason="Console publication failed before operation; nothing executed",
            )
            raise
        result = perform(registry, scope, entry, context)
        if result.get("waiting"):
            return operator_records.journal(
                registry, context, "waiting", reason=result["waiting"]
            )
        if (
            result.get("state") in ("failed", "paused", "unknown")
            or result.get("action") == "blocked"
        ):
            raise ValueError(
                result.get("reason")
                or result.get("error")
                or "Execution needs review; see execution journal"
            )
        return operator_records.journal(
            registry, context, "succeeded", result=operator_records.summarize(result)
        )
    except BlockingIOError:
        return operator_records.journal(
            registry, context, "waiting", reason="Another execution operation is active"
        )
    except (ValueError, OSError, KeyError, TypeError, sqlite3.Error) as exc:
        if publication_failed:
            raise
        if (
            context["operation"] == "analyse"
            and str(exc) == "Another local analysis is active"
        ):
            return operator_records.journal(
                registry, context, "waiting", reason=str(exc)
            )
        _, _, row = analysis_records.selected(registry, entry["run_id"])
        codes = [item["code"] for item in row["incidents"]]
        analysis = row.get("analysis_record") or {}
        if "execution:" + str(context.get("action_id")) not in codes and not (
            analysis.get("state") == "failed"
            and analysis.get("id") != context.get("previous_analysis_id")
        ):
            code = ("next-step:" if frozen else "operator:") + context["id"]
            operator_records.incident(registry, entry["run_id"], code, str(exc))
            codes.append(code)
        operator_records.journal(
            registry, context, "failed", error=str(exc), incident_codes=codes
        )
        raise ValueError(str(exc)) from exc
    finally:
        operator_records.CURRENT.reset(token)
        try:
            if frozen:
                inventory.scan(registry, run_ids=entry["affected_runs"])
            operator_view.publish(registry)
        except Exception as exc:
            raise ValueError(
                "Console/evidence refresh failed; inspect receipts before retrying: "
                + str(exc)
            ) from exc
