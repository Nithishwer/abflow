import abfe.console.palette as console_palette
import abfe.console.state as console_state


def summarize_runs(metadata, rows):
    """Aggregate observed stages without promoting legacy evidence to completion."""
    result = {"datasets": [], "compounds": []}
    for dataset in metadata["datasets"]:
        selected = [r for r in rows if r["spec"]["dataset"] == dataset["id"]]
        compounds = sorted({r["spec"]["ligand"] for r in selected})

        def summary(items):
            groups = {
                leg: {stage: 0 for stage in console_palette.STAGES}
                for leg in ("vanilla", "complex", "solvent")
            }
            attention = converged = complete = legacy = 0
            for row in items:
                view = console_state.presentation(row, metadata)
                groups[row["spec"]["leg"]][view["stage"]] += 1
                attention += view["remarks"] == "Attention required"
                converged += view["convergence"] == "converged"
                complete += view["stage"] == "complete"
                legacy += (row["observation"] or {}).get("analysis", {}).get(
                    "state"
                ) in ("legacy_unverified", "possibly_stale")
            return dict(
                groups=groups,
                attention=attention,
                converged=converged,
                complete=complete,
                legacy=legacy,
                runs=len(items),
            )

        result["datasets"].append(
            dict(
                summary(selected),
                id=dataset["id"],
                label=dataset["label"],
                compounds=len(compounds),
            )
        )
        for ligand in compounds:
            items = [r for r in selected if r["spec"]["ligand"] == ligand]
            result["compounds"].append(
                dict(summary(items), dataset=dataset["id"], ligand=ligand)
            )
    return result
