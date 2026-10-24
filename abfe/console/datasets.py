import html

import abfe.console.palette as console_palette
import abfe.console.state as console_state
import abfe.console.summary as console_summary
import abfe.console.tiles as console_tiles


def overview_html(metadata, rows):
    escape = lambda value: html.escape(str(value), quote=True)
    summaries = console_summary.summarize_runs(metadata, rows)

    def stages(groups):
        return (
            "".join(
                f'<span class="stage-chip" style="--stage-color:{console_palette.STAGES[key][1]}">{escape(console_palette.STAGES[key][0])} <strong>{count}</strong></span>'
                for key, count in groups.items()
                if count
            )
            or '<span class="subtle">Not started</span>'
        )

    def jump(dataset, ligand=""):
        return f'<button type="button" class="jump" data-dataset="{escape(dataset)}" data-ligand="{escape(ligand)}">View runs →</button>'

    def compound_tiles(dataset, leg):
        selected = [
            row
            for row in rows
            if row["spec"]["dataset"] == dataset and row["spec"]["leg"] == leg
        ]
        tiles = []
        for ligand in sorted({row["spec"]["ligand"] for row in selected}):
            repeats = sorted(
                (row for row in selected if row["spec"]["ligand"] == ligand),
                key=lambda row: row["spec"]["repeat"],
            )
            segments, descriptions = [], []
            attention = False
            for row in repeats:
                view = console_state.presentation(row, metadata)
                status = console_tiles.compound_status(row, view)
                accepted = console_tiles.accepted_dataset_status(row)
                production_started = status == "simulation" and (
                    (accepted and not view["jobs"])
                    or (
                        row["spec"]["leg"] != "vanilla"
                        and ((row["observation"] or {}).get("production_ns") or 0) > 0
                    )
                )
                description = f"Repeat {view['repeat']}: {view['simulation_state']}"
                if accepted and not view["jobs"]:
                    description += " · " + accepted["label"]
                if status == "attention":
                    description += " · Attention required"
                    attention = True
                elif status == "simulation":
                    description += " · " + (
                        "Under simulation · Production started"
                        if production_started
                        else view["scheduler_state"]
                    )
                action = console_state.next_action(row)
                description += f" · Next action: {action['action']}"
                if action["status"]:
                    description += f" · Action status: {action['status']}"
                if action["blocker"]:
                    description += f" · Action blocker: {action['blocker']}"
                descriptions.append(description)
                checked = (
                    min(
                        (job.get("observed_at", "") for job in view["jobs"]), default=""
                    )
                    if status == "simulation" and not production_started
                    else ""
                )
                segments.append(
                    f'<span class="tile-repeat" data-status="{status}" data-simulation-basis="{"production" if production_started else "scheduler"}" data-simulation-checked="{escape(checked)}" style="--stage-color:{console_palette.TILE_STATUSES[status][1]}" title="{escape(description)}">{view["repeat"]}</span>'
                )
            kind = {
                "vanilla": "Vanilla",
                "complex": "Alchemical Complex",
                "solvent": "Alchemical Solvent",
            }[leg]
            description = f"{ligand} · {kind} · " + "; ".join(descriptions)
            badge = (
                '<span class="tile-attention"'
                + ("" if attention else " hidden")
                + ">!</span>"
            )
            tiles.append(
                f'<button type="button" class="jump compound-tile" data-dataset="{escape(dataset)}" data-ligand="{escape(ligand)}" data-run-kind="{kind}" title="{escape(description)}" aria-label="View runs: {escape(description)}">'
                f'<span class="tile-name" aria-hidden="true">{escape(ligand)}{badge}</span><span class="tile-repeats" aria-hidden="true">{"".join(segments)}</span></button>'
            )
        return "".join(tiles) or '<span class="subtle">Not started</span>'

    stage_legend = "".join(
        f'<span class="legend-item"><i class="dot" style="--color:{color}" aria-hidden="true"></i>{escape(label)}</span>'
        for label, color in console_palette.TILE_STATUSES.values()
    )
    datasets = []
    for item in summaries["datasets"]:
        attention_count = sum(
            console_tiles.compound_status(
                row, console_state.presentation(row, metadata)
            )
            == "attention"
            for row in rows
            if row["spec"]["dataset"] == item["id"]
        )
        legs = "".join(
            f'<section class="dataset-leg" aria-label="{escape(item["label"])} · {label}"><h3>{label}</h3><div class="compound-tiles">{compound_tiles(item["id"], leg)}</div></section>'
            for leg, label in (
                ("vanilla", "Vanilla"),
                ("complex", "Complex"),
                ("solvent", "Solvent"),
            )
        )
        datasets.append(
            f'<article class="dataset-card" style="--dataset-color:{console_palette.DATASET_COLORS.get(item["id"], "#0fffdb")}">'
            f'<div class="topline"><h2>{escape(item["label"])}</h2>{jump(item["id"])}</div>'
            f'<p class="subtle">{item["compounds"]} compounds · {item["runs"]} run records</p><div class="dataset-legs">{legs}</div>'
            f'<p class="dataset-attention attention">{attention_count} runs need attention</p></article>'
        )
    compounds = []
    for item in summaries["compounds"]:
        cells = "".join(
            f'<td><div class="stage-chips">{stages(item["groups"][leg])}</div></td>'
            for leg in ("vanilla", "complex", "solvent")
        )
        compounds.append(
            f'<tr data-search="{escape((item["dataset"] + " " + item["ligand"]).lower())}">'
            f'<td class="edge" style="--edge-color:{console_palette.DATASET_COLORS.get(item["dataset"], "#0fffdb")}">{escape(item["dataset"])}</td>'
            f"<td>{escape(item['ligand'])}</td>{cells}<td>{item['converged']} / 4</td>"
            f'<td class="attention">{item["attention"]} / {item["runs"]}</td><td>{jump(item["dataset"], item["ligand"])}</td></tr>'
        )
    return (
        f'<section id="view-datasets" class="view"><div class="dataset-heading"><h2>Dataset progress</h2><div class="dataset-key"><div class="legend" aria-label="Compound status legend">{stage_legend}</div><p class="subtle">Numbers identify repeats · Select a compound to view its runs<br>Under simulation includes started ABFE awaiting convergence · Scheduler shows active jobs</p></div></div><div class="dataset-grid">{"".join(datasets)}</div></section>'
        f'<section id="view-compounds" class="view" hidden><h2>Compound progress</h2><p class="view-note">One row per dataset–compound pair; stage chips count the three vanilla repeats, three complex legs and one shared solvent leg. Converged counts validated alchemical legs out of four.</p>'
        '<div class="toolbar"><div class="search"><label for="compound-filter">Filter compounds</label><input type="search" id="compound-filter" placeholder="Search dataset or compound…"></div>'
        f'<span class="subtle" id="compound-count" role="status">{len(compounds)} compounds</span></div><div class="table"><table class="compound-table"><thead><tr>'
        + "".join(
            f'<th scope="col">{label}</th>'
            for label in (
                "Dataset",
                "Compound",
                "Vanilla · 3 repeats",
                "Complex · 3 repeats",
                "Solvent · 1 leg",
                "Converged legs",
                "Runs need attention",
                "Details",
            )
        )
        + "</tr></thead><tbody>"
        + "".join(compounds)
        + "</tbody></table></div></section>"
    )
