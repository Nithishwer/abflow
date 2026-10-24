import json

import abfe.console.datasets as console_datasets
import abfe.console.runs as console_runs
import abfe.console.state as console_state
import abfe.console.tiles as console_tiles
import abfe.registry.read as registry_read
import abfe.registry.values as registry_values

from .fixtures import CampaignFixture


class TilesTests(CampaignFixture):
    def test_started_unconverged_tile_stays_yellow_without_claiming_active_job(self):
        self.initialize()
        metadata, rows = registry_read.read_registry(self.registry)
        row = next(r for r in rows if r["spec"]["leg"] == "complex")
        row["observation"] = {
            "stage": "production_observed",
            "production_ns": 20,
            "analysis": {"state": "validated", "convergence": "unconverged"},
        }
        view = console_state.presentation(row, metadata)
        self.assertEqual(
            (view["scheduler_state"], view["job_id"], view["location"]),
            ("Unknown", "Unknown", "Unknown"),
        )
        self.assertEqual(console_tiles.compound_status(row, view), "simulation")
        report = console_runs.report_html(metadata, rows)
        self.assertIn(
            'data-status="simulation" data-simulation-basis="production" data-simulation-checked=""',
            report,
        )
        self.assertIn("Under simulation · Production started", report)
        self.assertNotIn("ageJobEvidence", report)
        self.assertNotIn(
            "querySelectorAll('.tile-repeat[data-status=\"simulation\"]')", report
        )
        row["observation"]["scheduler"] = {
            "job_id": "123",
            "cluster": "archer",
            "state": "RUNNING",
            "observed_at": registry_values.now(),
        }
        report = console_runs.report_html(metadata, rows)
        self.assertIn(
            'data-status="simulation" data-simulation-basis="production" data-simulation-checked=""',
            report,
        )

    def test_started_fep_awaiting_analysis_stays_in_simulation(self):
        self.initialize()
        metadata, rows = registry_read.read_registry(self.registry)
        for leg in ("complex", "solvent"):
            row = next(r for r in rows if r["spec"]["leg"] == leg)
            for analysis in (
                {},
                {"state": "absent"},
                {"state": "historical_imported", "convergence": "unverified"},
            ):
                with self.subTest(leg=leg, analysis=analysis):
                    row["observation"] = {
                        "stage": "production_observed",
                        "production_ns": 20,
                        "analysis": analysis,
                    }
                    original = json.dumps(row, sort_keys=True)
                    view = console_state.presentation(row, metadata)
                    self.assertEqual(view["scheduler_state"], "Unknown")
                    self.assertEqual(
                        console_tiles.compound_status(row, view), "simulation"
                    )
                    report = console_datasets.overview_html(metadata, [row])
                    self.assertIn(
                        'data-status="simulation" data-simulation-basis="production" data-simulation-checked=""',
                        report,
                    )
                    self.assertIn("Under simulation · Production started", report)
                    self.assertEqual(json.dumps(row, sort_keys=True), original)
