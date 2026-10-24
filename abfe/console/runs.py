import html
import json
from importlib.resources import files
from pathlib import Path

import abfe.console.assets as console_assets
import abfe.console.datasets as console_datasets
import abfe.console.palette as console_palette
import abfe.console.state as console_state
import abfe.console.terminal as console_terminal
import abfe.console.tiles as console_tiles


def report_html(metadata, rows):
    escape = lambda value: html.escape(str(value), quote=True)
    coverage_detail = (
        '<details class="notes"><summary>Dry-run location coverage</summary><pre>'
        + escape(json.dumps(metadata["next_step_coverage"], indent=2))
        + "</pre></details>"
        if metadata.get("next_step_coverage")
        else ""
    )
    body = []
    for row in rows:
        spec, observed = row["spec"], row["observation"] or {}
        analysis, view = (
            observed.get("analysis", {}),
            console_state.presentation(row, metadata),
        )
        problems = "".join(
            f"<li><strong>{escape(item['code'])}</strong>: {escape(item['message'])}</li>"
            for item in row["incidents"]
        )
        evidence_links = "".join(
            f'<li><a href="{escape(Path(item["path"]).as_uri())}">{escape(item["path"])}</a> ({escape(item["role"])})</li>'
            for item in observed.get("evidence", [])
        )
        history = "".join(
            f"<li>Revision {item['revision']}: {escape(item['observed_at'])}</li>"
            for item in row["history"]
        )
        dependencies = "".join(
            f"<li>{escape(item)}</li>" for item in spec["dependencies"]
        )
        jobs = "".join(
            f"<li>{escape(item['cluster'])} · {escape(item['job_id'])} · {escape(item['state'])}</li>"
            for item in view["jobs"]
        )
        action = console_state.next_action(row)
        proposal = row.get("next_step")
        detail = (
            f"<h3>{escape(spec['id'])}</h3><p>Source: {escape(spec['path'] or 'not prepared')}<br>"
            f"Simulation state: {escape(view['simulation_state'])}<br>"
            f"Observed stage: {escape(view['stage_label'])}<br>"
            f"Equilibration repeat: {escape((row['equilibration_assignment']['label'] + ' (run name)') if row.get('equilibration_assignment') else ('van' + str(spec['repeat']) if spec['leg'] != 'solvent' else 'not applicable'))}<br>"
            f"Equilibration source: {escape(row.get('equilibration', {}).get('source') or 'not applicable')}<br>"
            f"Equilibration evidence: {escape(row.get('equilibration', {}).get('state', 'unverified'))}<br>"
            f"Observed windows: {escape(observed.get('sampled_windows', 0))}/{escape(observed.get('expected_windows', 0))}<br>"
            f"Observed maximum: {escape(console_terminal.number(observed.get('production_max_ns')))} ns<br>"
            f"Transfer evidence: {escape(observed.get('transfer', {}).get('state', 'unknown'))}<br>"
            f"Next action: {escape(action['action'])}<br>"
            f"Action status: {escape(action['status'] or 'Not assessed')}<br>"
            f"Action blocker: {escape(action['blocker'] or 'None recorded')}</p>"
            f"<h4>Open incidents ({len(row['incidents'])})</h4><ul>{problems or '<li>None recorded</li>'}</ul>"
            f"<h4>Observed jobs</h4><ul>{jobs or '<li>No recorded job association is available</li>'}</ul>"
            f"<h4>Dependencies</h4><ul>{dependencies or '<li>None</li>'}</ul>"
            f"<details><summary>File evidence ({len(observed.get('evidence', []))})</summary><ul>{evidence_links}</ul></details>"
            f"<details><summary>Observation history ({len(row['history'])})</summary><ol>{history}</ol></details>"
        )
        if row.get("analysis_record"):
            detail += (
                "<h4>Analysis and decision</h4><pre>"
                + escape(json.dumps(analysis, indent=2, allow_nan=False))
                + "</pre>"
            )
            if row["analysis_record"].get("verification"):
                detail += (
                    "<h4>Analysis verification basis</h4><pre>"
                    + escape(
                        json.dumps(row["analysis_record"]["verification"], indent=2)
                    )
                    + "</pre>"
                )
        if proposal:
            detail += (
                "<h4>Saved next-step proposal and outcome</h4><pre>"
                + escape(json.dumps(proposal, indent=2))
                + "</pre>"
            )
        if row.get("historical_analysis"):
            imported = row["historical_analysis"]
            links = "".join(
                f'<li><a href="{escape(Path(item["identity"]["path"]).as_uri())}">{escape(item["identity"]["path"])}</a></li>'
                for item in imported["files"]
            )
            detail += (
                f"<h4>Historical analysis imported</h4><p>Record: {escape(imported['id'])}<br>"
                f"Analysed cutoff: {escape(console_terminal.number(imported.get('analysed_ns')))} ns · uncertainties: legacy as reported<br>"
                f"Cutoff basis: {escape(imported.get('duration_assignment', {}).get('rule', 'unknown'))}<br>"
                f"Convergence: {escape(imported.get('convergence', 'unverified').replace('_', ' '))}</p>"
                + "<pre>"
                + escape(
                    json.dumps(imported.get("convergence_assessment", {}), indent=2)
                )
                + f"</pre><ul>{links}</ul>"
            )
        if row.get("historical_provenance"):
            detail += (
                "<h4>Historical provenance (user accepted)</h4><p>Placeholder linked to the existing historical analysis; "
                "original execution details remain unknown. This is not a current validated analysis.</p>"
            )
        if row.get("accepted_analysis_review"):
            detail += (
                "<h4>Analysis review (user accepted)</h4><pre>"
                + escape(json.dumps(row["accepted_analysis_review"], indent=2))
                + "</pre>"
            )
        if console_tiles.accepted_dataset_status(row):
            detail += (
                "<h4>Dataset status (user accepted)</h4><pre>"
                + escape(json.dumps(row["dataset_status_acceptance"], indent=2))
                + "</pre>"
            )
        if row.get("executions"):
            from abfe.execution.view import summary

            detail += (
                "<h4>Cluster execution and transfers</h4><pre>"
                + escape(
                    json.dumps(
                        [summary(r) for r in row["executions"]],
                        indent=2,
                        allow_nan=False,
                    )
                )
                + "</pre>"
            )
        if row.get("operator") or row.get("operator_history"):
            detail += (
                "<h4>Operator next action and blocked reason</h4><pre>"
                + escape(json.dumps(row.get("operator", []), indent=2))
                + "</pre>"
            )
            detail += (
                "<details><summary>Operator and incident history</summary><pre>"
                + escape(json.dumps(row.get("operator_history", []), indent=2))
                + "</pre></details>"
            )
        if row.get("preparation"):
            prep = row["preparation"]
            detail += (
                "<h4>Staged preparation</h4><pre>"
                + escape(
                    json.dumps(
                        {
                            key: prep.get(key)
                            for key in ("state", "at", "stage", "output", "error")
                        },
                        indent=2,
                    )
                )
                + "</pre>"
            )
        if row.get("binding_result"):
            detail += (
                "<h4>Binding result and shared solvent</h4><pre>"
                + escape(json.dumps(row["binding_result"], indent=2, allow_nan=False))
                + "</pre>"
            )
        dataset_color = console_palette.DATASET_COLORS.get(spec["dataset"], "#0fffdb")
        stage_color = console_palette.STAGES[view["stage"]][1]
        remark_color = console_palette.REMARK_COLORS[view["remarks"]]
        cells = [
            f'<td class="edge" style="--edge-color:{dataset_color}">{escape(spec["dataset"])}</td>',
            f"<td>{escape(spec['ligand'])}</td>",
            f'<td class="type {"vanilla" if view["simulation_type"] == "Vanilla" else ""}">{escape(view["simulation_type"])}</td>',
            f"<td>{escape(view['leg'])}</td>",
            f'<td class="numeric">{escape(view["repeat"])}</td>',
            f'<td class="edge stage" style="--edge-color:{stage_color}">{escape(view["simulation_state"])}</td>',
            f'<td class="numeric">{escape(console_terminal.number(observed.get("production_ns")))}</td>',
            f'<td class="numeric subtle">{escape(console_terminal.number(analysis.get("analysed_ns")))}</td>',
            f'<td class="subtle">{escape(("Historical analysis imported" if analysis.get("state") == "historical_imported" else analysis.get("state", "unknown").replace("_", " ")))}</td>',
            f'<td class="subtle">{escape(view["convergence"])}</td>',
            f'<td class="scheduler">{escape(view["scheduler_state"])}</td>',
            f'<td class="job" title="{escape(view["job_id"])}">{escape(view["job_id"])}</td>',
            f'<td class="location">{escape(view["location"])}</td>',
            f'<td><span class="remark" style="--remark-color:{remark_color}"><i class="dot" aria-hidden="true"></i>{escape(view["remarks"])}</span></td>',
            f'<td class="action-detail">{escape(action["action"])}<br><span class="subtle">{escape(action["status"])}</span></td>',
            f'<td class="action-detail">{escape(action["blocker"] or "None recorded")}</td>',
        ]
        search = " ".join(
            [
                spec["dataset"],
                spec["ligand"],
                view["simulation_type"],
                view["leg"],
                str(view["repeat"]),
                view["stage_label"],
                view["simulation_state"],
                view["remarks"],
                view["convergence"],
                view["scheduler_state"],
                view["job_id"],
                view["location"],
                action["action"],
                action["status"],
                action["blocker"],
            ]
            + [f"{item['code']} {item['message']}" for item in row["incidents"]]
        )
        body.append(
            f'<tr data-search="{escape(search.lower())}"><td><details class="run"><summary aria-label="Inspect {escape(spec["id"])}">Inspect</summary><section>{detail}</section></details></td>'
            + "".join(cells)
            + "</tr>"
        )
    dataset_items = []
    for item in metadata["datasets"]:
        count = item["registered_ligands"]
        description = (
            f"{count} complexes · {count * 3} planned complex repeats"
            if count
            else "Not started · no complexes selected"
        )
        dataset_items.append(
            f'<li style="--dataset-color:{console_palette.DATASET_COLORS.get(item["id"], "#0fffdb")}"><strong>{escape(item["label"])}</strong><span>{escape(description)}</span></li>'
        )
    legend = "".join(
        f'<span class="legend-item"><i class="dot" style="--color:{color}" aria-hidden="true"></i>{escape(label)}</span>'
        for label, color in console_palette.REMARK_COLORS.items()
    )
    stage_legend = "".join(
        f'<span class="legend-item"><i class="dot" style="--color:{color}" aria-hidden="true"></i>{escape(label)}</span>'
        for label, color in console_palette.STAGES.values()
    )
    columns = (
        "Dataset",
        "Ligand",
        "Simulation type",
        "Leg",
        "Repeat",
        "Simulation state",
        "Prod ns",
        "Analysed ns",
        "Analysis",
        "Convergence",
        "Scheduler",
        "Job ID",
        "Location",
        "Remarks",
        "Next action",
        "Action blocker",
    )
    headers = '<th scope="col">Details</th>' + "".join(
        f'<th scope="col" aria-sort="none"><button type="button" class="sort" data-column="{index}" '
        f'aria-label="Sort by {label}, ascending">{label}<span aria-hidden="true">↕</span></button></th>'
        for index, label in enumerate(columns, start=1)
    )
    total = sum(item["registered_ligands"] for item in metadata["datasets"])
    licenses = "\n".join(
        (files("abfe") / "console_assets" / name).read_text()
        for name in ("Inter-LICENSE.txt", "JetBrainsMono-LICENSE.txt")
    )
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>H.A.W.T.O.W.N</title><style>{console_assets.console_styles()}</style></head><body><main>
<header class="hero"><div class="brand">Hyperspecific ABFE Workflow That Only Works for Nithish</div>
<div class="topline"><h1>H.A.W.T.O.W.N</h1><div class="view-controls"><nav aria-label="Console views">{"".join(f'<button type="button" class="view-button" data-view="{key}" aria-pressed="{str(key == "datasets").lower()}">{label}</button>' for key, label in (("datasets", "Datasets"), ("runs", "Runs"), ("compounds", "Compounds")))}</nav><p class="updated">Last updated: {escape(console_assets.display_time(metadata["scanned_at"]))}</p></div></div>
</header>{coverage_detail}
<section id="view-runs" class="view" hidden><section class="tracked"><h2>Complexes tracked</h2><p>{total} protein–ligand complexes · three planned complex repeats · one shared solvent calculation per ligand</p><ul class="datasets">{"".join(dataset_items)}</ul></section>
<div class="toolbar"><div class="search"><label for="filter">Filter runs <span class="subtle" id="row-count" role="status" aria-live="polite">{len(rows)} records</span></label><input id="filter" placeholder="Search dataset, ligand, simulation type or remarks…" type="search"></div><div class="legend" aria-label="Remarks legend">{legend}</div></div>
<div class="table"><table class="run-table"><thead><tr>{headers}</tr></thead><tbody>{"".join(body)}</tbody></table></div>
<details class="notes"><summary>Stage key and inventory notes</summary><div class="legend stages">{stage_legend}</div><p>Simulation state describes observed progress. Next action and Action blocker describe workflow readiness separately: a run can be Not started while its next action is Blocked by missing execution configuration. Inspect shows the underlying observed stage. Attention required means open incidents; Waiting means preparation or a queued step; OK requires observed activity or validated local completion; Unknown means there is insufficient evidence.</p><p>Production ns is the common observed endpoint across every expected window. It does not validate samples or establish an analysed cutoff. Vanilla convergence is N/A. Running location and job IDs use recorded scheduler evidence; absolut is reserved for validated completion with verified local retrieval. Unmatched jobs remain unassigned. Scheduler observations do not expire with age; refresh to update them.</p></details>
</section>{console_datasets.overview_html(metadata, rows)}
<footer><span>Read-only inventory · {escape(metadata["name"])}</span><span>Job evidence and local inventory have separate timestamps.</span></footer></main>
<script>
{console_assets.console_script()}
</script><!-- Font licenses
{licenses}
--></body></html>"""
