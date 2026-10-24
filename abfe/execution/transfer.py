import json
import shlex
import subprocess
from pathlib import Path

import abfe.execution.files as worker
import abfe.execution.journal as execution_journal
import abfe.execution.profile as execution_profile
import abfe.execution.transport.dispatch as execution_transport_dispatch
import abfe.registry.manifest as registry_manifest
import abfe.registry.read as registry_read
import abfe.registry.values as registry_values


def rsync(plan, source, destination, retrieval=False):
    transport = shlex.join(
        execution_profile.SSH[0:1] + [x for x in execution_profile.SSH[1:] if x != "-T"]
    )
    args = [
        "rsync",
        "-rlt",
        "--protect-args",
        "--ignore-existing",
        "--partial-dir=.partial",
        "-e",
        transport,
    ]
    if not retrieval:
        args += ["--chmod=D700,F600"]
    args += [source, destination]
    output = Path(plan["output"]) / ("retrieve.log" if retrieval else "stage.log")
    with output.open("a") as log:
        log.write(registry_values.encode(args) + "\n")
        log.flush()
        result = subprocess.run(
            args, stdout=log, stderr=subprocess.STDOUT, timeout=3600
        )
    if result.returncode:
        raise ValueError(
            "Transfer interrupted; preserve staging and inspect " + str(output)
        )


def retrieve(registry, plan, output):
    if output is None or not Path(output).is_absolute():
        raise ValueError("Retrieval needs an absolute --output directory")
    output = Path(output).resolve()
    metadata, rows = registry_read.read_registry(registry)
    registry_manifest.check_destination(output, metadata, [row["spec"] for row in rows])
    protected = [
        Path(plan["output"]),
        Path(plan["config"]["source"]).resolve(),
        Path(registry).resolve(),
    ]
    if any(
        output == p or output in p.parents or p in output.parents for p in protected
    ):
        raise ValueError("Retrieval destination overlaps protected files")
    before = execution_transport_dispatch.remote(plan, "retrieve")
    expected = before["snapshot"]
    output.mkdir(parents=True, exist_ok=True)
    owner = output / ".retrieval-owner.json"
    identity = dict(action_id=plan["id"], snapshot=expected)
    if owner.exists():
        if json.loads(owner.read_text()) != identity:
            raise ValueError("Retrieval destination belongs to another snapshot")
    elif list(output.iterdir()):
        raise ValueError("Retrieval destination is not empty")
    else:
        worker.write(owner, identity)
    actual = worker.snapshot(output)["files"]
    actual.pop(".retrieval-owner.json", None)
    actual.pop("retrieval.json", None)
    if any(
        name not in expected["files"] or expected["files"][name] != value
        for name, value in actual.items()
    ):
        raise ValueError("Local retrieval conflict; preserve both versions")
    alias = plan["config"]["cluster"] + ":" + plan["remote"].rstrip("/") + "/"
    rsync(plan, alias, str(output) + "/", retrieval=True)
    after = execution_transport_dispatch.remote(plan, "retrieve")
    actual = worker.snapshot(output)["files"]
    actual.pop(".retrieval-owner.json", None)
    actual.pop("retrieval.json", None)
    if after["snapshot"] != expected or actual != expected["files"]:
        raise ValueError("Retrieval snapshot changed or checksum verification failed")
    receipt = dict(
        action_id=plan["id"],
        run_id=plan["run_id"],
        state="verified_local",
        at=registry_values.now(),
        source=plan["remote"],
        cluster=plan["config"]["cluster"],
        snapshot=expected,
        output=str(output),
        application_state=before["state"],
    )
    worker.write(output / "retrieval.json", receipt)
    return execution_journal.save(
        registry, plan, "retrieved", transfer=receipt, observation=before
    )
