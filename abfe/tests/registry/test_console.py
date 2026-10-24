import copy

import abfe.console.palette as console_palette
import abfe.console.runs as console_runs
import abfe.console.summary as console_summary
import abfe.console.state as console_state
import abfe.console.tiles as console_tiles
import abfe.registry.read as registry_read
import abfe.registry.values as registry_values

from .fixtures import CampaignFixture


class ConsoleTests(CampaignFixture):
    def test_unstarted_simulations_keep_configuration_blockers_separate(self):
        self.initialize()
        metadata, rows = registry_read.read_registry(self.registry)
        for row in rows:
            vanilla = row["spec"]["leg"] == "vanilla"
            row["observation"] = {
                "stage": "setup_present" if vanilla else "inputs_present"
            }
            row["next_step"] = dict(
                current_state="blocked",
                proposed_operation=None,
                reason="No reviewed vanilla execution profile for this run"
                if vanilla
                else "No complete reviewed preparation inputs or execution profile",
                outcome="proposed",
            )
        before = copy.deepcopy(rows)
        for row in rows:
            view = console_state.presentation(row, metadata)
            action = console_state.next_action(row)
            vanilla = row["spec"]["leg"] == "vanilla"
            self.assertEqual(
                view["simulation_state"], "Not started" if vanilla else "Not prepared"
            )
            self.assertEqual(view["remarks"], "Waiting")
            self.assertEqual(console_tiles.compound_status(row, view), "not_started")
            self.assertEqual(action["status"], "Blocked")
            self.assertEqual(action["blocker"], row["next_step"]["reason"])
            self.assertEqual(
                action["action"],
                "Prepare vanilla launch"
                if vanilla
                else f"Prepare {row['spec']['leg']} leg",
            )
        report = console_runs.report_html(metadata, rows)
        for label in (
            "Simulation state",
            "Next action",
            "Action blocker",
            "Not started",
            "Not prepared",
            "Prepare vanilla launch",
            "Prepare complex leg",
            "Prepare solvent leg",
        ):
            self.assertIn(label, report)
        self.assertIn("Action status: Blocked", report)
        self.assertIn("Action blocker: No reviewed vanilla execution profile", report)
        self.assertNotIn("Next action: blocked", report)
        self.assertEqual(rows, before)

    def test_workflow_blockers_do_not_hide_production_incidents_or_unknown_progress(
        self,
    ):
        self.initialize()
        metadata, rows = registry_read.read_registry(self.registry)
        row = rows[0]
        self.assertEqual(
            console_state.presentation(row, metadata)["simulation_state"], "Unknown"
        )
        row["observation"] = {"stage": "production_observed", "production_ns": 20}
        row["incidents"] = [{"code": "test", "message": "Needs review"}]
        row["next_step"] = dict(
            current_state="blocked",
            proposed_operation=None,
            reason="Open incidents: test <review>",
            outcome="proposed",
        )
        view = console_state.presentation(row, metadata)
        self.assertEqual(view["simulation_state"], "Production observed")
        self.assertEqual(view["remarks"], "Attention required")
        self.assertEqual(console_tiles.compound_status(row, view), "attention")
        self.assertEqual(console_state.next_action(row)["action"], "Review next action")
        report = console_runs.report_html(metadata, rows)
        self.assertIn("Open incidents: test &lt;review&gt;", report)
        self.assertNotIn("test <review>", report)
        row["next_step"].update(current_state="ready", proposed_operation="submit")
        for outcome in ("proposed", "waiting", "succeeded", "failed"):
            with self.subTest(outcome=outcome):
                row["next_step"].update(
                    outcome=outcome,
                    error="Submission needs review" if outcome == "failed" else None,
                )
                action = console_state.next_action(row)
                self.assertEqual(action["action"], "submit")
                self.assertEqual(
                    action["status"],
                    "Ready" if outcome == "proposed" else outcome.capitalize(),
                )
                self.assertEqual(
                    action["blocker"],
                    "Submission needs review" if outcome == "failed" else "",
                )

    def test_fresh_running_job_takes_precedence_over_local_unstarted_inputs(self):
        self.initialize()
        metadata, rows = registry_read.read_registry(self.registry)
        row = next(row for row in rows if row["spec"]["leg"] == "vanilla")
        row["observation"] = dict(
            stage="setup_present",
            scheduler=dict(
                cluster="ranvier",
                job_id="123",
                state="RUNNING",
                observed_at=registry_values.now(),
            ),
        )
        self.assertEqual(
            console_state.presentation(row, metadata)["simulation_state"], "Running"
        )

    def test_html_escapes_values_and_does_not_enable_actions(self):
        self.initialize()
        metadata, rows = registry_read.read_registry(self.registry)
        report = console_runs.report_html(metadata, rows)
        self.assertTrue(
            "Test &lt;campaign&gt;" in report, "Campaign name must be escaped"
        )
        self.assertTrue("Inspect" in report, "Missing inspect control")
        self.assertTrue("Read-only inventory" in report, "Missing read-only footer")
        self.assertTrue(
            "<form" not in report, "Inventory must not expose mutation forms"
        )

    def test_hawton_design_headers_and_embedded_fonts(self):
        self.initialize()
        metadata, rows = registry_read.read_registry(self.registry)
        report = console_runs.report_html(metadata, rows)
        for text in (
            "H.A.W.T.O.W.N",
            "Hyperspecific ABFE Workflow That Only Works for Nithish",
            "Last updated:",
            "Complexes tracked",
            "Sort by Simulation type, ascending",
            "Sort by Leg, ascending",
            "Sort by Repeat, ascending",
            "Sort by Job ID, ascending",
            "Sort by Location, ascending",
            "Sort by Remarks, ascending",
            "JetBrains Mono",
            "Inter",
            "data:font/ttf;base64,",
        ):
            self.assertTrue(
                text in report, f"Missing requested console element: {text}"
            )
        self.assertTrue(
            "<th>Transfer</th>" not in report, "Transfer must not be a primary column"
        )
        self.assertTrue("color-scheme: dark" in report, "Console must use dark mode")
        self.assertEqual(len(set(console_palette.DATASET_COLORS.values())), 6)

    def test_overview_counts_legs_and_keeps_unverified_analysis_unconverged(self):
        self.initialize()
        metadata, rows = registry_read.read_registry(self.registry)
        for row in rows:
            row["observation"] = {"stage": "production_observed"}
        complex_row = next(r for r in rows if r["spec"]["leg"] == "complex")
        complex_row["observation"]["analysis"] = {
            "state": "legacy_unverified",
            "convergence": "converged",
        }
        complex_row["incidents"] = [{"code": "test", "message": "needs review"}]
        summary = console_summary.summarize_runs(metadata, rows)
        dataset = summary["datasets"][0]
        self.assertEqual(
            (
                dataset["runs"],
                dataset["compounds"],
                dataset["converged"],
                dataset["attention"],
                dataset["legacy"],
            ),
            (7, 1, 0, 1, 1),
        )
        self.assertEqual(sum(dataset["groups"]["vanilla"].values()), 3)
        self.assertEqual(sum(dataset["groups"]["complex"].values()), 3)
        self.assertEqual(sum(dataset["groups"]["solvent"].values()), 1)
        self.assertEqual(summary["compounds"][0]["groups"], dataset["groups"])
        self.assertEqual(summary["datasets"][1]["runs"], 0)
        self.assertEqual(
            len(summary["compounds"]), 1, "Do not invent Sigma 2 compounds"
        )

    def test_overview_counts_only_validated_alchemical_convergence(self):
        self.initialize()
        metadata, rows = registry_read.read_registry(self.registry)
        for row in rows:
            row["observation"] = {
                "analysis": {"state": "validated", "convergence": "converged"}
            }
        summary = console_summary.summarize_runs(metadata, rows)
        self.assertEqual(summary["compounds"][0]["converged"], 4)
        self.assertEqual(summary["compounds"][0]["complete"], 0)

    def test_overview_views_do_not_mix_their_run_tables(self):
        self.initialize()
        metadata, rows = registry_read.read_registry(self.registry)
        report = console_runs.report_html(metadata, rows)
        for name in ("runs", "datasets", "compounds"):
            self.assertIn(f'id="view-{name}"', report)
            self.assertIn(f'data-view="{name}"', report)
        self.assertIn("document.querySelectorAll('.run-table tbody tr')", report)
        self.assertIn('data-dataset="test" data-ligand="L1"', report)
        self.assertNotIn('id="view-clusters"', report)
        self.assertNotIn("Cluster SSH connections", report)
        self.assertNotIn("history-chart", report)
        self.assertNotIn("data-capacity-checked", report)

    def test_datasets_is_first_and_default_view(self):
        self.initialize()
        metadata, rows = registry_read.read_registry(self.registry)
        report = console_runs.report_html(metadata, rows)
        self.assertLess(
            report.index('data-view="datasets"'), report.index('data-view="runs"')
        )
        self.assertIn('data-view="datasets" aria-pressed="true"', report)
        self.assertIn('id="view-datasets" class="view">', report)
        self.assertIn('id="view-runs" class="view" hidden', report)
        self.assertIn(
            "showView(sessionStorage.getItem('hawtown-view')||'datasets')", report
        )
