import json
import sqlite3
from pathlib import Path

import abfe.console.datasets as console_datasets
import abfe.console.state as console_state
import abfe.console.tiles as console_tiles
import abfe.registry.read as registry_read

from .fixtures import CampaignFixture


class TilesTests(CampaignFixture):
    def test_accepted_dataset_status_is_scoped_and_preserves_incident_gates(self):
        import copy

        self.initialize()
        metadata, rows = registry_read.read_registry(self.registry)
        row = copy.deepcopy(next(r for r in rows if r["spec"]["leg"] == "complex"))
        source = Path(row["spec"]["path"])
        source.mkdir(parents=True)
        row["observation"] = dict(
            stage="production_observed",
            production_ns=30,
            target_ns=30,
            analysis=dict(
                state="historical_imported",
                id="old-analysis",
                analysed_ns=20,
                convergence="historically_unconverged",
            ),
            issues=[dict(code="newer_source_files", message="Await analysis")],
        )
        row["incidents"] = [
            dict(code="newer_source_files", message="Await analysis", status="open")
        ]
        row["dataset_status_acceptance"] = dict(
            status="simulation",
            label="30 ns; awaiting analysis",
            authorization="User accepted dataset colour",
            source_path=str(source),
            state=console_tiles.dataset_status_state(row),
        )
        original = copy.deepcopy(row)
        view = console_state.presentation(row, metadata)
        self.assertEqual(view["remarks"], "Attention required")
        self.assertEqual(console_tiles.compound_status(row, view), "simulation")
        self.assertEqual(row, original)
        report = console_datasets.overview_html(metadata, [row])
        self.assertIn('data-simulation-basis="production"', report)
        self.assertIn("30 ns; awaiting analysis", report)
        for field, value in [
            ("production_ns", 35),
            ("target_ns", 35),
            ("stage", "observation_error"),
        ]:
            changed = copy.deepcopy(row)
            changed["observation"][field] = value
            self.assertEqual(
                console_tiles.compound_status(
                    changed, console_state.presentation(changed, metadata)
                ),
                "attention",
            )
        for change in ("path", "analysis", "incident", "issue"):
            changed = copy.deepcopy(row)
            if change == "path":
                changed["spec"]["path"] = "/different/source"
            if change == "analysis":
                changed["observation"]["analysis"]["id"] = "new-analysis"
            if change == "incident":
                changed["incidents"].append(
                    dict(code="failure", message="New failure", status="open")
                )
            if change == "issue":
                changed["observation"]["issues"].append(
                    dict(code="failure", message="New failure")
                )
            self.assertEqual(
                console_tiles.compound_status(
                    changed, console_state.presentation(changed, metadata)
                ),
                "attention",
            )
        metadata["accepted_dataset_statuses"] = {
            "runs": {row["spec"]["id"]: row["dataset_status_acceptance"]}
        }
        with sqlite3.connect(self.registry) as db:
            db.execute(
                "INSERT INTO metadata VALUES (?,?)",
                (
                    "accepted_dataset_statuses",
                    json.dumps(metadata["accepted_dataset_statuses"]),
                ),
            )
        loaded = next(
            r
            for r in registry_read.read_registry(self.registry)[1]
            if r["spec"]["id"] == row["spec"]["id"]
        )
        self.assertEqual(
            loaded["dataset_status_acceptance"], row["dataset_status_acceptance"]
        )
        row["observation"]["analysis"]["convergence"] = "historically_converged"
        row["spec"]["leg"] = "solvent"
        row["dataset_status_acceptance"].update(
            status="complete",
            label="Converged historical solvent from v1",
            state=console_tiles.dataset_status_state(row),
        )
        self.assertEqual(console_tiles.compound_status(row, view), "complete")
        source.rmdir()
        self.assertEqual(console_tiles.compound_status(row, view), "attention")
