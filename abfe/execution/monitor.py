import abfe.execution.journal as execution_journal
import abfe.execution.transport.dispatch as execution_transport_dispatch
import abfe.registry.values as registry_values


def action_monitor(registry, plan):
    observed = execution_transport_dispatch.remote(plan, "monitor")
    submission = observed.get("submission") or {}
    if observed["state"] == "unknown" and submission.get("job_id"):
        # A receipt can recover a lost sbatch response, but an absent job still needs application evidence.
        observed["submission_identity"] = "recovered"
    state = observed["state"]
    if state == "failed" or (state == "unknown" and not observed["jobs"]):
        execution_journal.incident(
            registry,
            plan["run_id"],
            "execution:" + plan["id"],
            "Execution outcome requires review: " + registry_values.encode(observed),
        )
    return execution_journal.save(
        registry, plan, state, observation=observed, submission=submission
    )
