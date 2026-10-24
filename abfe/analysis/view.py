import abfe.analysis.combine as analysis_combine
import abfe.analysis.convergence as analysis_convergence
import abfe.analysis.provenance as analysis_provenance
import abfe.analysis.records as analysis_records
import abfe.analysis.values as analysis_values


def attach(db, rows):
    """Overlay immutable records; never replace the inventory's stored observations."""
    for row in rows:
        imported = analysis_records.latest(db, row["spec"]["id"], kind="historical")
        if imported:
            row["historical_analysis"] = imported
        record = analysis_records.latest(db, row["spec"]["id"])
        if imported and not record:
            observed = row["observation"] = dict(row["observation"] or {})
            observed["analysis"] = dict(
                state="historical_imported",
                analysed_ns=imported.get("analysed_ns"),
                convergence=imported.get("convergence", "unverified"),
                id=imported["id"],
            )
        if not record:
            continue
        row["analysis_record"] = record
        observed = row["observation"] = dict(row["observation"] or {})
        analysis = dict(
            id=record["id"],
            state=record["state"],
            analysed_ns=None,
            convergence="unverified",
            decision=record.get("decision"),
            output=record["output"],
            freshness="unchecked",
        )
        if record["state"] == "validated":
            analysis.update(
                analysed_ns=record["evidence"]["cutoff_ps"] / 1000,
                half_difference_kcal_mol=record["half_difference_kcal_mol"],
            )
            try:
                analysis_provenance.verify(record, row["spec"])
                analysis.update(
                    state="validated",
                    freshness="metadata unchanged; content recheck required before action",
                    convergence="converged"
                    if record["decision"]["action"] == "stop_converged"
                    else "unconverged",
                )
            except (OSError, ValueError) as exc:
                analysis.update(
                    state="stale",
                    freshness=str(exc),
                    decision=analysis_convergence.decide(0, 0, blocked=str(exc)),
                )
                code = "stale_analysis:" + record["id"]
                if not any(item["code"] == code for item in row["incidents"]):
                    row["incidents"].append(
                        dict(
                            run_id=row["spec"]["id"],
                            code=code,
                            message=str(exc),
                            status="open",
                            first_seen=analysis_values.timestamp(),
                            last_seen=analysis_values.timestamp(),
                            detected_in_report=True,
                        )
                    )
        observed["analysis"] = analysis
        observed["next_action"] = (
            "Inspect failed/stale analysis and incidents"
            if analysis["state"] in ("failed", "stale")
            else "Run decide to verify source/output hashes before using the recorded decision"
        )

    for row in rows:
        bound = analysis_records.latest(db, row["spec"]["id"], kind="binding")
        if bound:
            row["binding_result"] = analysis_combine.binding_state(bound, rows)
