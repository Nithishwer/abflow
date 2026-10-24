from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

import abfe.operator.records as operator_records
import abfe.registry.read as registry_read


def execution_for(entry, history, row):
    action_id = entry.get("action_id")
    if not action_id:
        plans = [
            e.get("result", {}).get("action_id")
            for e in history
            if e["operation"] == "plan" and e["state"] in ("succeeded", "reconciled")
        ]
        action_id = next((p for p in reversed(plans) if p), None)
    return next(
        (e for e in row.get("executions", []) if e["action_id"] == action_id), None
    )


def next_action(scope_id, entry, row, history, metadata):
    def result(operation=None, reason=None, **extra):
        if operation and operation not in entry["operations"]:
            return dict(
                run_id=entry["run_id"],
                next_action=None,
                blocked_reason="Scope does not permit " + operation,
            )
        return dict(
            run_id=entry["run_id"],
            next_action=operation,
            blocked_reason=reason,
            **extra,
        )

    latest = {}
    for item in history:
        latest[item["id"]] = item
    if any(e["state"] == "intent" for e in latest.values()):
        return result(
            reason="Interrupted operation requires explicit review",
        )
    if row["incidents"]:
        return result(
            reason="Open incidents: " + ", ".join(i["code"] for i in row["incidents"])
        )
    if (
        history
        and history[-1]["state"] == "waiting"
        and (
            datetime.now(timezone.utc) - datetime.fromisoformat(history[-1]["at"])
        ).total_seconds()
        < 300
    ):
        return result(reason=history[-1]["reason"])
    current = execution_for(entry, history, row)
    if current:
        state = current["state"]
        if state == "planned":
            return result("stage")
        if state == "staged":
            return result("submit")
        if state in (
            "submitted",
            "submission_uncertain",
            "unknown",
            "active",
            "resolved",
            "failed",
        ):
            if (
                state in ("active", "submitted")
                and history
                and history[-1]["operation"] == "monitor"
            ):
                observed = (current.get("observation") or {}).get("at")
                if (
                    observed
                    and (
                        datetime.now(timezone.utc)
                        - datetime.fromisoformat(observed.replace("Z", "+00:00"))
                    ).total_seconds()
                    < 300
                ):
                    return result(reason="Waiting for the next monitoring interval")
            return result("monitor")
        if state == "paused":
            if (
                current.get("resolution", {}).get("allow_resume")
                and current.get("operation") == "stage"
            ):
                return result("stage")
            return result("monitor", "Paused execution requires explicit review")
        if state in ("complete", "checkpointed"):
            return result("retrieve")
        if state == "retrieved":
            mapping = metadata.get("retrieved_source_mapping:" + current["action_id"])
            path = str(
                Path(current["transfer"]["output"])
                / "data"
                / current["plan"]["config"]["relative_run"]
            )
            if (
                not mapping
                or Path(row["spec"]["path"] or "").resolve() != Path(path).resolve()
            ):
                return result("map-source")
            if row["spec"]["leg"] == "vanilla":
                return result(
                    reason="Vanilla retrieval mapped; further preparation needs a reviewed preparation plan"
                )
    elif entry.get("manifest"):
        return result("plan")
    record = row.get("analysis_record")
    if (
        record
        and record["state"] in ("validated", "planned")
        and (
            "cutoff_ns" not in entry
            or record["evidence"]["cutoff_ps"] == entry["cutoff_ns"] * 1000
        )
    ):
        decided = next(
            (
                e
                for e in reversed(history)
                if e["operation"] == "decide"
                and e.get("result", {}).get("analysis_id") == record["id"]
            ),
            None,
        )
        if not decided:
            return result("decide")
        decision = decided["result"]
        return result(reason=decision["reason"], decision=decision)
    return result("analyse")


def overview(registry, scope_id):
    scope = operator_records.load(registry, scope_id)
    metadata, rows = registry_read.read_registry(registry)
    with closing(registry_read.readonly(registry)) as db:
        history = operator_records.events(db)
    indexed = {r["spec"]["id"]: r for r in rows}
    return dict(
        scope_id=scope_id,
        authorization=scope["authorization"],
        runs=[
            next_action(
                scope_id,
                e,
                indexed[e["run_id"]],
                [
                    h
                    for h in history
                    if h["scope_id"] == scope_id and h["run_id"] == e["run_id"]
                ],
                metadata,
            )
            for e in scope["runs"]
        ],
    )
