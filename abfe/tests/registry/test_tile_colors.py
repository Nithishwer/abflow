from html.parser import HTMLParser

import abfe.console.datasets as console_datasets
import abfe.console.palette as console_palette
import abfe.console.state as console_state
import abfe.console.tiles as console_tiles
import abfe.registry.read as registry_read
import abfe.registry.values as registry_values

from .fixtures import CampaignFixture


class TilesTests(CampaignFixture):
    def test_dataset_tiles_preserve_repeat_status_and_incident_visibility(self):
        self.initialize()
        metadata, rows = registry_read.read_registry(self.registry)
        complex_rows = [row for row in rows if row["spec"]["leg"] == "complex"]
        complex_rows[0]["observation"] = {
            "analysis": {"state": "validated", "convergence": "converged"}
        }
        complex_rows[1]["observation"] = {
            "analysis": {
                "state": "historical_imported",
                "convergence": "historically_converged",
            }
        }
        complex_rows[2]["observation"] = {"stage": "production_observed"}
        complex_rows[2]["incidents"] = [{"code": "test", "message": "needs review"}]

        class Tiles(HTMLParser):
            def __init__(self):
                super().__init__()
                self.buttons, self.repeats = [], []

            def handle_starttag(self, tag, attrs):
                attrs = dict(attrs)
                if (
                    tag == "button"
                    and "compound-tile" in attrs.get("class", "").split()
                ):
                    self.buttons.append(attrs)
                if attrs.get("class") == "tile-repeat":
                    self.repeats.append(attrs)

        report = console_datasets.overview_html(metadata, rows)
        tiles = Tiles()
        tiles.feed(report)
        self.assertEqual(
            len(tiles.buttons), 3
        )  # One per leg, no invented Sigma 2 compounds.
        self.assertEqual(len(tiles.repeats), 7)
        self.assertEqual(
            [tile["data-run-kind"] for tile in tiles.buttons],
            ["Vanilla", "Alchemical Complex", "Alchemical Solvent"],
        )
        title = tiles.buttons[1]["title"]
        self.assertIn("Repeat 1: Converged", title)
        self.assertIn("Repeat 2: Analysis on file", title)
        self.assertIn("Repeat 3: Production observed · Attention required", title)
        self.assertIn("Compound status legend", report)
        self.assertNotIn("alchemical legs with validated convergence", report)
        self.assertNotIn("legacy analyses unverified", report)
        self.assertNotIn("Stage counts by leg and repeat.", report)

    def test_compound_colours_require_completion_or_recorded_simulation_evidence(self):
        self.initialize()
        metadata, rows = registry_read.read_registry(self.registry)
        for leg, observation, incidents, expected in (
            ("vanilla", {"stage": "inputs_present"}, [], "not_started"),
            ("vanilla", {"stage": "setup_present"}, [], "not_started"),
            (
                "vanilla",
                {
                    "stage": "production_observed",
                    "production_ns": 100,
                    "target_ns": 100,
                },
                [],
                "attention",
            ),
            (
                "vanilla",
                {"completion": "validated", "transfer": {"state": "verified_local"}},
                [],
                "complete",
            ),
            (
                "complex",
                {"analysis": {"state": "validated", "convergence": "converged"}},
                [],
                "complete",
            ),
            (
                "solvent",
                {"analysis": {"state": "validated", "convergence": "converged"}},
                [],
                "complete",
            ),
            (
                "complex",
                {
                    "analysis": {
                        "state": "historical_imported",
                        "convergence": "historically_converged",
                    }
                },
                [],
                "attention",
            ),
            (
                "complex",
                {"analysis": {"state": "validated", "convergence": "unconverged"}},
                [],
                "attention",
            ),
            (
                "complex",
                {
                    "production_ns": 20,
                    "analysis": {"state": "validated", "convergence": "unconverged"},
                },
                [],
                "simulation",
            ),
            (
                "complex",
                {
                    "stage": "production_observed",
                    "production_ns": 20,
                    "analysis": {"state": "absent"},
                },
                [],
                "simulation",
            ),
            (
                "solvent",
                {
                    "production_ns": 16,
                    "analysis": {
                        "state": "historical_imported",
                        "convergence": "unverified",
                    },
                },
                [],
                "simulation",
            ),
            (
                "complex",
                {
                    "production_ns": 20,
                    "analysis": {"state": "invalid", "convergence": "unconverged"},
                },
                [],
                "attention",
            ),
            (
                "complex",
                {"production_ns": 20, "analysis": {"state": "stale"}},
                [],
                "attention",
            ),
            (
                "complex",
                {"production_ns": 20, "analysis": {"state": "possibly_stale"}},
                [],
                "attention",
            ),
            (
                "complex",
                {"production_ns": 20, "analysis": {"state": "superseded"}},
                [],
                "attention",
            ),
            (
                "complex",
                {"production_ns": 20, "analysis": {"state": "failed"}},
                [],
                "attention",
            ),
            (
                "complex",
                {
                    "production_ns": 20,
                    "issues": [{"code": "missing_input", "message": "Review"}],
                },
                [],
                "attention",
            ),
            (
                "complex",
                {
                    "production_ns": 20,
                    "analysis": {
                        "state": "historical_imported",
                        "convergence": "historically_unconverged",
                    },
                },
                [],
                "simulation",
            ),
            (
                "solvent",
                {
                    "production_ns": 16,
                    "analysis": {"state": "validated", "convergence": "unconverged"},
                },
                [],
                "simulation",
            ),
            (
                "complex",
                {
                    "production_ns": 0,
                    "analysis": {"state": "validated", "convergence": "unconverged"},
                },
                [],
                "attention",
            ),
            (
                "vanilla",
                {
                    "production_ns": 100,
                    "analysis": {"state": "validated", "convergence": "unconverged"},
                },
                [],
                "attention",
            ),
            (
                "complex",
                {
                    "production_ns": 20,
                    "analysis": {"state": "validated", "convergence": "unconverged"},
                },
                [{"code": "failure"}],
                "attention",
            ),
            (
                "complex",
                {
                    "production_ns": 50,
                    "analysis": {
                        "state": "validated",
                        "convergence": "unconverged",
                        "decision": {"action": "stop_at_limit"},
                    },
                },
                [],
                "attention",
            ),
            (
                "complex",
                {
                    "stage": "production_observed",
                    "scheduler": {
                        "job_id": "123",
                        "cluster": "archer",
                        "state": "RUNNING",
                        "observed_at": registry_values.now(),
                    },
                },
                [],
                "simulation",
            ),
            (
                "complex",
                {
                    "stage": "production_observed",
                    "scheduler": {
                        "job_id": "123",
                        "cluster": "archer",
                        "state": "PENDING",
                        "observed_at": registry_values.now(),
                    },
                },
                [],
                "simulation",
            ),
            (
                "complex",
                {
                    "stage": "production_observed",
                    "scheduler": {
                        "job_id": "123",
                        "cluster": "archer",
                        "state": "RUNNING",
                        "observed_at": "2000-01-01T00:00:00+00:00",
                    },
                },
                [],
                "simulation",
            ),
            (
                "complex",
                {"analysis": {"state": "validated", "convergence": "converged"}},
                [{"code": "test", "message": "review"}],
                "attention",
            ),
        ):
            with self.subTest(leg=leg, observation=observation, expected=expected):
                row = dict(
                    next(row for row in rows if row["spec"]["leg"] == leg),
                    observation=observation,
                    incidents=incidents,
                )
                self.assertEqual(
                    console_tiles.compound_status(
                        row, console_state.presentation(row, metadata)
                    ),
                    expected,
                )
        report = console_datasets.overview_html(metadata, rows)
        legend = report.split('aria-label="Compound status legend">')[1].split(
            "</div>"
        )[0]
        self.assertEqual(legend.count('class="legend-item"'), 4)
        self.assertEqual(
            len({colour for _, colour in console_palette.TILE_STATUSES.values()}), 4
        )
