import hashlib
import json
import uuid
from pathlib import Path

import abfe.analysis.fingerprints as analysis_fingerprints
import abfe.analysis.run as analysis_run
import abfe.execution.admission as execution_admission
import abfe.execution.files as worker
import abfe.execution.inputs as execution_inputs
import abfe.execution.jobs.generate as execution_jobs_generate
import abfe.execution.journal as execution_journal
import abfe.execution.profile as execution_profile
import abfe.monitoring.scheduler as monitoring_scheduler
import abfe.registry.manifest as registry_manifest
import abfe.registry.values as registry_values


def make_plan(registry, run_id, manifest):
    metadata, rows, row = execution_admission.unblocked(registry, run_id)
    config = json.loads(Path(manifest).read_text())
    from abfe.operator.permissions import authorize
    from abfe.operator.records import CURRENT

    scope = authorize(registry, run_id, "plan", config=config)
    spec = row["spec"]
    execution_profile.config_check(config, spec)
    source, run, windows = execution_inputs.source_check(config, spec)
    plan_id = uuid.uuid4().hex
    output = Path(registry).resolve().parent / "executions" / plan_id
    registry_manifest.check_destination(output, metadata, [r["spec"] for r in rows])
    if (
        source.resolve() == output
        or source.resolve() in output.parents
        or output in source.resolve().parents
    ):
        raise ValueError("Plan directory must not overlap its source")
    source_snapshot = worker.snapshot(source)
    identity = execution_inputs.source_identity(
        registry, spec, source, run, source_snapshot
    )
    decision = None
    if (
        config["mode"] == "initial"
        and config["workload"] in ("solvent", "complex-production")
        and row.get("analysis_record")
    ):
        decision = analysis_run.current_decision(registry, run_id)
        raise ValueError(
            "Existing analysis requires an explicit continuation/extension decision: "
            + decision["reason"]
        )
    if config["mode"] == "extend":
        if not spec.get("path") or run.resolve() != Path(spec["path"]).resolve():
            raise ValueError("Extension must use the exact local run that was analysed")
        decision = analysis_run.current_decision(registry, run_id)
        if decision["action"] != "propose_extension":
            raise ValueError(
                "No fresh eligible extension decision: " + decision["reason"]
            )
    elif config["mode"] == "continue" and spec["leg"] != "vanilla":
        decision = analysis_run.current_decision(registry, run_id)
        if row.get("analysis_record") and decision["action"] != "continue_target":
            raise ValueError(
                "Analysis does not permit continuation: " + decision["reason"]
            )
        from abfe.monitoring.progress import production_end

        progress = [
            production_end(run / name / "prod/dhdl.xvg")
            for name in windows
            if (run / name / "prod/dhdl.xvg").exists()
        ]
        if any(value > 50 for value in progress):
            raise ValueError("Production exceeds the 50 ns cap; preserve and review")
        if (
            len(progress) == len(windows)
            and min(progress) >= 16
            and decision["action"] != "continue_target"
        ):
            raise ValueError(
                "Analyse available production before continuing beyond convergence eligibility"
            )
    plan = dict(
        id=plan_id,
        run_id=run_id,
        created_at=registry_values.now(),
        config=config,
        spec=spec,
        source_snapshot=source_snapshot,
        source_identity=identity,
        inputs=[analysis_fingerprints.fingerprint(p) for p in spec["input_files"]],
        windows=windows,
        decision=decision,
        output=str(output),
        remote=monitoring_scheduler.CLUSTER_ROOTS[config["cluster"]]
        + "/abflow/"
        + run_id
        + "/"
        + plan_id,
        executor="slurm-shell-v1",
    )
    if scope:
        plan["operator"] = dict(
            scope_id=CURRENT.get()["scope_id"], intent_id=CURRENT.get()["id"]
        )
    job_script = execution_jobs_generate.script(plan)
    plan["script_sha256"] = hashlib.sha256(job_script.encode()).hexdigest()
    plan["fingerprint"] = worker.digest(plan)
    output.mkdir(parents=True)
    (output / "job.sh").write_text(job_script)
    (output / "plan.json").write_text(registry_values.encode(plan) + "\n")
    execution_journal.save(registry, plan, "planned")
    return plan


def recheck(plan):
    payload = dict(plan)
    fingerprint_ = payload.pop("fingerprint")
    if worker.digest(payload) != fingerprint_:
        raise ValueError("Execution plan fingerprint differs")
    output = Path(plan["output"])
    if (
        json.loads((output / "plan.json").read_text()) != plan
        or worker.sha(output / "job.sh") != plan["script_sha256"]
        or (
            "worker_sha256" in plan
            and worker.sha(output / "worker.py") != plan["worker_sha256"]
        )
    ):
        raise ValueError("Execution plan/script/worker changed")
