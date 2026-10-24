import hashlib
import json
import sqlite3
from contextlib import closing
from pathlib import Path

import abfe.registry.manifest as registry_manifest
import abfe.registry.values as registry_values
from abfe.analysis import identity


def readonly(registry):
    db = sqlite3.connect(Path(registry).resolve().as_uri() + "?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    db.execute("BEGIN")
    version = db.execute(
        "SELECT value FROM metadata WHERE key='schema_version'"
    ).fetchone()
    if version is None or json.loads(version[0]) != registry_manifest.SCHEMA_VERSION:
        db.close()
        raise ValueError("Unsupported registry schema")
    return db


def read_registry(registry):
    with closing(readonly(registry)) as db:
        metadata = {
            row[0]: json.loads(row[1])
            for row in db.execute("SELECT key,value FROM metadata")
        }
        rows = []
        for row in db.execute("SELECT * FROM runs ORDER BY id"):
            rows.append(
                {
                    "spec": json.loads(row["spec"]),
                    "observation": json.loads(row["observation"])
                    if row["observation"]
                    else None,
                    "observed_at": row["observed_at"],
                    "incidents": [
                        dict(item)
                        for item in db.execute(
                            "SELECT * FROM incidents WHERE run_id=? AND status!='retired' ORDER BY code",
                            (row["id"],),
                        )
                    ],
                    "incident_history": [
                        dict(item)
                        for item in db.execute(
                            "SELECT * FROM incidents WHERE run_id=? AND status='retired' ORDER BY code",
                            (row["id"],),
                        )
                    ],
                    "history": [
                        dict(item)
                        for item in db.execute(
                            "SELECT revision,observed_at FROM history WHERE run_id=? ORDER BY revision",
                            (row["id"],),
                        )
                    ],
                }
            )
        if db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='preparations'"
        ).fetchone():
            prepared = {}
            for item in db.execute(
                "SELECT compound,record FROM preparations ORDER BY rowid"
            ):
                prepared[item[0]] = json.loads(item[1])
            for row in rows:
                compound = row["spec"]["dataset"] + "/" + row["spec"]["ligand"]
                if compound in prepared:
                    row["preparation"] = prepared[compound]
        for row in rows:
            assignment = metadata.get("equilibration_labels", {}).get(row["spec"]["id"])
            if assignment:
                row["equilibration_assignment"] = dict(
                    assignment, kind="vanilla_equilibration_repeat"
                )
        for row in rows:
            if row["observation"]:
                row["observation"].pop("embedding", None)
                row["observation"]["issues"] = [
                    item
                    for item in row["observation"].get("issues", [])
                    if item["code"]
                    not in (
                        "embedding_provenance",
                        "missing_embedding",
                        "shared_embedding_source",
                    )
                ]
        specs = {row["spec"]["id"]: row["spec"] for row in rows}
        for row in rows:
            row["equilibration"] = equilibration_details(row["spec"], specs)
        from abfe.analysis.provenance import verify
        from abfe.analysis.view import attach

        attach(db, rows)
        for row in rows:
            review = metadata.get("accepted_analysis_reviews", {}).get(
                row["spec"]["id"], {}
            )
            record = row.get("analysis_record", {})
            if (
                not review
                or not review.get("authorization")
                or record.get("state") != "validated"
                or record.get("decision", {}).get("action") != "stop_converged"
            ):
                continue
            current_code = identity.current()
            if (
                review.get("analysis_id") != record["id"]
                or review.get("spec_sha256")
                != hashlib.sha256(
                    registry_values.encode(row["spec"]).encode()
                ).hexdigest()
                or not identity.matches(review.get("analysis_code_sha256"))
            ):
                continue
            try:
                # The user waived only this code mismatch; every other snapshot check still applies.
                verify(
                    dict(
                        record,
                        software=dict(
                            record["software"], analysis_code_sha256=current_code
                        ),
                    ),
                    row["spec"],
                )
            except (OSError, ValueError):
                continue
            row["accepted_analysis_review"] = review
            code = "stale_analysis:" + record["id"]
            row["incidents"] = [
                item
                for item in row["incidents"]
                if not (
                    item["code"] == code
                    and item["message"]
                    == "Analysis implementation changed; explicit reanalysis/review required"
                )
            ]
        for row in rows:
            accepted = (
                metadata.get("accepted_historical_provenance", {})
                .get("runs", {})
                .get(row["spec"]["id"])
            )
            if (
                accepted
                and accepted
                == {
                    "analysis_id": (row.get("historical_analysis") or {}).get("id"),
                    "path": row["spec"]["path"],
                }
                and (row["observation"] or {}).get("analysis", {}).get("state")
                == "historical_imported"
            ):
                row["historical_provenance"] = accepted
                row["observation"]["issues"] = [
                    item
                    for item in row["observation"].get("issues", [])
                    if item["code"] != "analysis_provenance"
                ]
        from abfe.execution.view import attach as attach_executions

        attach_executions(db, rows)
        from abfe.operator.view import attach as attach_operator

        attach_operator(db, metadata, rows)
        for row in rows:
            accepted = (
                metadata.get("accepted_dataset_statuses", {})
                .get("runs", {})
                .get(row["spec"]["id"])
            )
            if accepted:
                row["dataset_status_acceptance"] = accepted
    return metadata, rows


def equilibration_details(spec, specs):
    if spec["leg"] == "solvent":
        return dict(state="not_applicable", repeat=None, source=None)
    vanilla_id = f"{spec['dataset']}/{spec['ligand']}/vanilla/{spec['repeat']}"
    source = specs.get(vanilla_id, {}).get("path")
    result = dict(state="unverified", repeat=spec["repeat"], source=source)
    if source:
        path = Path(source)
        if any((path / name).is_file() for name in ("nvt.gro", "npt.gro")):
            result["state"] = "outputs_on_file"
    return result
