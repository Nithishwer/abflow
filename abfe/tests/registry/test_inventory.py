import json
import shutil
import sqlite3
from pathlib import Path

import abfe.console.runs as console_runs
import abfe.monitoring.inventory as monitoring_inventory
import abfe.monitoring.progress as monitoring_progress
import abfe.registry.read as registry_read

from .fixtures import CampaignFixture


class InventoryTests(CampaignFixture):
    def test_all_window_minimum_is_observed_not_converged(self):
        self.production(time_ps=16000)
        self.production("coul.00", time_ps=18000)
        self.initialize()
        monitoring_inventory.scan(self.registry)
        observation = self.complex_row()["observation"]
        self.assertEqual(observation["production_ns"], 16)
        self.assertEqual(observation["production_max_ns"], 18)
        self.assertEqual(observation["target_ns"], 20)
        self.assertEqual(observation["analysis"]["convergence"], "unverified")
        self.assertEqual(observation["scheduler"]["state"], "unknown")
        self.assertEqual(observation["transfer"]["state"], "unknown")

    def test_missing_windows_do_not_report_a_common_duration(self):
        self.production()
        self.initialize()
        monitoring_inventory.scan(self.registry)
        row = self.complex_row()
        self.assertIsNone(row["observation"]["production_ns"])
        self.assertIn(
            "incomplete_production_data", [item["code"] for item in row["incidents"]]
        )

    def test_inconsistent_targets_are_visible(self):
        self.production()
        self.production("coul.00", target_ps=30000)
        self.initialize()
        monitoring_inventory.scan(self.registry)
        row = self.complex_row()
        self.assertIsNone(row["observation"]["target_ns"])
        self.assertIn(
            "inconsistent_targets", [item["code"] for item in row["incidents"]]
        )

    def test_invalid_and_ambiguous_time_data_are_visible(self):
        path = self.production()
        (path / "dhdl.xvg").write_text('@ xaxis label "Time (ns)"\n16 2 3\n')
        self.initialize()
        monitoring_inventory.scan(self.registry)
        self.assertIn(
            "invalid_window:rest.00",
            [item["code"] for item in self.complex_row()["incidents"]],
        )
        (path / "dhdl.xvg").write_text('@ xaxis label "Time (ps)"\n16000 nan 3\n')
        with self.assertRaisesRegex(ValueError, "Invalid final"):
            monitoring_progress.production_end(path / "dhdl.xvg")

    def test_nonzero_time_origin_is_not_silently_assumed_zero(self):
        path = self.production()
        mdp = path / "rest.prod.00.mdp"
        mdp.write_text(mdp.read_text() + "tinit = 100\n")
        with self.assertRaisesRegex(ValueError, "time origin"):
            monitoring_progress.target_duration(mdp)

    def test_partial_last_line_is_not_treated_as_a_complete_sample(self):
        path = self.production() / "dhdl.xvg"
        path.write_text('@ xaxis label "Time (ps)"\n0 1 2\n16000 2')
        with self.assertRaisesRegex(ValueError, "incomplete final line"):
            monitoring_progress.production_end(path)

    def test_disappearing_sources_are_incidents_not_reset_progress(self):
        self.production()
        self.production("coul.00")
        self.initialize()
        monitoring_inventory.scan(self.registry)
        shutil.rmtree(self.source / "set/L1/abfe_van1_hrex_r1/complex")
        monitoring_inventory.scan(self.registry)
        codes = [item["code"] for item in self.complex_row()["incidents"]]
        self.assertIn("source_disappeared", codes)
        self.assertIn("production_evidence_lost", codes)

    def test_legacy_analysis_never_supplies_cutoff_or_convergence(self):
        self.production()
        self.production("coul.00")
        self.analysis()
        self.initialize()
        monitoring_inventory.scan(self.registry)
        observed = self.complex_row()["observation"]
        self.assertEqual(observed["analysis"]["state"], "legacy_unverified")
        self.assertIsNone(observed["analysis"]["analysed_ns"])
        self.assertEqual(observed["analysis"]["convergence"], "unverified")
        self.assertAlmostEqual(
            observed["analysis"]["reported_half_difference_kcal_mol"], 0.1
        )

    def test_invalid_analysis_is_visible_and_does_not_abort_inventory(self):
        self.production()
        self.analysis(",Forward,Backward,data_fraction\n0,nan,1.9,0.5\n")
        self.initialize()
        self.assertEqual(monitoring_inventory.scan(self.registry), 7)
        self.assertEqual(
            self.complex_row()["observation"]["analysis"]["state"], "invalid"
        )

    def test_analysis_is_imported_even_without_physical_production(self):
        self.analysis()
        self.initialize()
        monitoring_inventory.scan(self.registry)
        self.assertEqual(
            self.complex_row()["observation"]["analysis"]["state"], "legacy_unverified"
        )

    def test_history_is_append_only_for_changes_and_incidents_stay_open(self):
        path = self.production()
        self.initialize()
        monitoring_inventory.scan(self.registry)
        history = self.complex_row()["history"]
        monitoring_inventory.scan(self.registry)
        self.assertEqual(self.complex_row()["history"], history)
        self.production("coul.00")
        monitoring_inventory.scan(self.registry)
        row = self.complex_row()
        self.assertEqual(len(row["history"]), len(history) + 1)
        self.assertIn(
            "incomplete_production_data", [item["code"] for item in row["incidents"]]
        )
        self.assertNotIn(
            "incomplete_production_data",
            [item["code"] for item in row["observation"]["issues"]],
        )
        self.assertTrue(all(item["status"] == "open" for item in row["incidents"]))

    def test_manual_equilibration_label_is_displayed_and_preserved(self):
        self.production()
        self.initialize()
        run_id = "test/L1/complex/1"
        spec_before = self.complex_row()["spec"]
        assignment = dict(
            label="van1",
            basis="user_assigned_from_run_directory_name",
            source=str(Path(spec_before["path"]).parent),
        )
        with sqlite3.connect(self.registry) as db:
            db.execute(
                "insert into metadata values(?,?)",
                ("equilibration_labels", json.dumps({run_id: assignment})),
            )
        monitoring_inventory.scan(self.registry)
        metadata, rows = registry_read.read_registry(self.registry)
        row = next(r for r in rows if r["spec"]["id"] == run_id)
        self.assertEqual(row["equilibration_assignment"]["label"], "van1")
        self.assertNotIn("embedding_assignment", row)
        self.assertEqual(
            row["equilibration_assignment"]["kind"], "vanilla_equilibration_repeat"
        )
        self.assertEqual(row["spec"], spec_before)
        self.assertNotIn("embedding", row["observation"])
        self.assertNotIn("embedding", row["spec"])
        codes = [i["code"] for i in row["incidents"]]
        self.assertNotIn("missing_embedding", codes)
        self.assertNotIn("embedding_provenance", codes)
        report = console_runs.report_html(metadata, rows)
        self.assertIn("Equilibration repeat: van1 (run name)", report)
        self.assertIn("Equilibration source:", report)
        self.assertNotIn("Embedding source:", report)
        self.assertNotIn("Embedding: van1", report)

    def test_equilibration_source_and_outputs_are_observed_without_claiming_completion(
        self,
    ):
        self.initialize()
        path = self.source / "set/L1/vanilla_rep_1"
        path.mkdir(parents=True)
        row = self.complex_row()
        self.assertEqual(
            row["equilibration"], dict(source=str(path), repeat=1, state="unverified")
        )
        (path / "npt.gro").write_text("test evidence")
        self.assertEqual(
            self.complex_row()["equilibration"]["state"], "outputs_on_file"
        )

    def test_retired_checks_and_observations_stay_in_history(self):
        self.production()
        self.initialize()
        run_id = "test/L1/complex/1"
        with sqlite3.connect(self.registry) as db:
            observation = {
                "issues": [{"code": "embedding_provenance", "message": "old policy"}],
                "embedding": {"path": None},
            }
            db.execute(
                "UPDATE runs SET observation=? WHERE id=?",
                (json.dumps(observation), run_id),
            )
            db.execute(
                "INSERT INTO incidents VALUES (?,?,?,?,?,?)",
                (
                    run_id,
                    "embedding_provenance",
                    "old policy",
                    "before",
                    "before",
                    "retired",
                ),
            )
        row = self.complex_row()
        self.assertFalse(row["incidents"])
        self.assertEqual(row["incident_history"][0]["status"], "retired")
        self.assertFalse(row["observation"]["issues"])
        self.assertNotIn("embedding", row["observation"])
        with sqlite3.connect(self.registry) as db:
            self.assertIn(
                "embedding",
                json.loads(
                    db.execute(
                        "SELECT observation FROM runs WHERE id=?", (run_id,)
                    ).fetchone()[0]
                ),
            )

    def test_extra_legacy_repeats_are_preserved_and_flagged(self):
        legacy = self.source / "set/L1/abfe_van1_hrex_r2"
        legacy.mkdir(parents=True)
        self.initialize()
        monitoring_inventory.scan(self.registry)
        vanilla = next(
            row for row in self.rows() if row["spec"]["id"] == "test/L1/vanilla/1"
        )
        self.assertEqual(vanilla["observation"]["legacy_candidates"], [legacy.name])
        self.assertTrue(legacy.exists())

    def test_vanilla_log_time_does_not_imply_scheduler_completion(self):
        vanilla = self.source / "set/L1/vanilla_rep_1"
        vanilla.mkdir(parents=True)
        (vanilla / "prod.mdp").write_text("nsteps=50000000\ndt=0.002\n")
        (vanilla / "prod.log").write_text(
            "Step           Time\n50000000 100000.00000\nFinished mdrun\n"
        )
        self.initialize()
        monitoring_inventory.scan(self.registry)
        row = next(
            row for row in self.rows() if row["spec"]["id"] == "test/L1/vanilla/1"
        )
        self.assertEqual(row["observation"]["production_ns"], 100)
        self.assertEqual(row["observation"]["scheduler"]["state"], "unknown")
