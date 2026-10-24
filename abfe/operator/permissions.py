import json
from contextlib import closing
from pathlib import Path

import abfe.analysis.fingerprints as analysis_fingerprints
import abfe.analysis.records as analysis_records
import abfe.execution.files as files
import abfe.execution.journal as execution_journal
import abfe.operator.records as operator_records
import abfe.registry.read as registry_read


def verified_snapshot(current):
    receipt = current["transfer"]
    output = Path(receipt["output"])
    if json.loads((output / "retrieval.json").read_text()) != receipt:
        raise ValueError("Retrieval receipt changed")
    if json.loads((output / ".retrieval-owner.json").read_text()) != dict(
        action_id=current["action_id"], snapshot=receipt["snapshot"]
    ):
        raise ValueError("Retrieval ownership changed")
    actual = files.snapshot(output)["files"]
    actual.pop(".retrieval-owner.json", None)
    actual.pop("retrieval.json", None)
    if actual != receipt["snapshot"]["files"]:
        raise ValueError("Retrieved files changed before source use")
    return output


def authorize(
    registry,
    run_id,
    operation,
    config=None,
    action_id=None,
    cutoff_ns=None,
    output=None,
):
    """Called by the underlying operations, as well as the dispatcher."""
    context = operator_records.CURRENT.get()
    if context is None:
        return (
            None  # Existing explicitly invoked manual commands retain their interface.
        )
    scope = operator_records.load(registry, context["scope_id"])
    if scope.get("kind") == "next-step" and not context.get("explicit_next_step"):
        raise ValueError("A proposal requires an explicit next-step invocation")
    if context["run_id"] != run_id:
        raise ValueError("Operation is outside the authorized run")
    entry = operator_records.entry_for(scope, run_id)
    internal_decision = (
        operation == "decide"
        and context["operation"] in ("plan", "stage", "submit")
        and context["operation"] in entry["operations"]
    )
    internal_mapping = (
        operation == "map-source"
        and context["operation"] == "retrieve"
        and "retrieve" in entry["operations"]
    )
    if (
        operation not in entry["operations"]
        and not internal_decision
        and not internal_mapping
    ):
        raise ValueError("Operation is outside the authorized scope: " + operation)
    if cutoff_ns is not None and cutoff_ns != entry.get("cutoff_ns"):
        raise ValueError("Analysis cutoff differs from the reviewed scope")
    if (
        output is not None
        and operation == "retrieve"
        and Path(output).resolve() != Path(entry["retrieval_output"]).resolve()
    ):
        raise ValueError("Retrieval destination differs from the reviewed scope")
    if config is not None:
        if config != entry["config"]:
            raise ValueError("Execution profile differs from the reviewed scope")
        if (
            operation in ("plan", "stage", "submit")
            and config["mode"] in ("continue", "extend")
            and config["mode"] not in entry["operations"]
        ):
            raise ValueError("Continuation/extension needs explicit scope permission")
    if action_id and entry.get("action_id") and entry["action_id"] != action_id:
        raise ValueError("Execution identity differs from the reviewed scope")
    if (
        action_id
        and entry.get("action_fingerprint")
        and execution_journal.load(registry, action_id)["plan"]["fingerprint"]
        != entry["action_fingerprint"]
    ):
        raise ValueError("Reviewed execution identity changed")
    _, _, row = analysis_records.selected(registry, run_id)
    expected = dict(entry["spec"])
    mapping = row["spec"].get("path")
    if mapping != expected.get("path"):
        with closing(registry_read.readonly(registry)) as db:
            approved = [
                json.loads(r[0])
                for r in db.execute(
                    "SELECT value FROM metadata WHERE key LIKE 'retrieved_source_mapping:%'"
                )
            ]
        if not any(
            m.get("scope_id") == context["scope_id"]
            and m.get("run_id") == run_id
            and m.get("new_path") == mapping
            for m in approved
        ):
            raise ValueError("Registered source changed outside this scope")
        expected["path"] = mapping
    if expected != row["spec"]:
        raise ValueError("Registered scientific specification changed")
    if operation not in ("monitor", "retrieve"):
        if row["incidents"]:
            raise ValueError("Open incidents block advancement")
        if [
            analysis_fingerprints.fingerprint(p["source_path"]) for p in entry["inputs"]
        ] != entry["inputs"]:
            raise ValueError("Declared inputs changed since authorization")
        if (
            operation in ("analyse", "decide")
            or operation == "plan"
            and config is not None
        ):
            metadata, _ = registry_read.read_registry(registry)
            for key, mapping in metadata.items():
                if (
                    key.startswith("retrieved_source_mapping:")
                    and mapping.get("new_path") == row["spec"].get("path")
                    and mapping.get("receipt_sha256")
                ):
                    with closing(registry_read.readonly(registry)) as db:
                        receipts = [
                            r
                            for r in execution_journal.records(db)
                            if r["action_id"] == mapping["action_id"]
                            and files.digest(r.get("transfer"))
                            == mapping["receipt_sha256"]
                        ]
                    if not receipts:
                        raise ValueError(
                            "Mapped retrieval receipt is no longer recorded"
                        )
                    verified_snapshot(receipts[-1])
    return scope
