"""Inventory tests use tiny synthetic files, never a simulation or SSH."""

from contextlib import redirect_stderr, redirect_stdout
import hashlib
import io
import json
from pathlib import Path
import shutil
import sqlite3
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from datetime import datetime, timedelta, timezone

from abfe import campaign


class CampaignTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.work = Path(self.temp.name)
        self.source = self.work / "source"
        self.source.mkdir()
        self.registry = self.work / "registry.sqlite"
        self.manifest = self.work / "campaign.json"
        self.config = {
            "schema_version": 1,
            "name": "Test <campaign>",
            "source_root": str(self.source),
            "datasets": [
                {
                    "id": "test",
                    "label": "Test dataset",
                    "expected_ligands": 1,
                    "directory": "set",
                    "ligands": ["L1"],
                    "complex_windows": {"rest": 1, "coul": 1},
                    "solvent_windows": {"coul": 1},
                },
                {
                    "id": "sigma2",
                    "label": "Sigma 2",
                    "expected_ligands": None,
                    "ligands": [],
                },
            ],
        }
        self.write_manifest()

    def write_manifest(self):
        self.manifest.write_text(json.dumps(self.config))

    def initialize(self):
        return campaign.initialize(self.registry, self.manifest)

    def rows(self):
        return campaign.read_registry(self.registry)[1]

    def complex_row(self):
        return next(
            row for row in self.rows() if row["spec"]["id"] == "test/L1/complex/1"
        )

    def production(self, name="rest.00", time_ps=16000, target_ps=20000):
        path = self.source / "set/L1/abfe_van1_hrex_r1/complex" / name / "prod"
        path.mkdir(parents=True, exist_ok=True)
        (path / "dhdl.xvg").write_text(
            f'@ xaxis label "Time (ps)"\n0 2 3\n{time_ps} 3 4\n'
        )
        phase, index = name.split(".")
        (path / f"{phase}.prod.{index}.mdp").write_text(
            f"nsteps = {target_ps / 0.002}\ndt = 0.002\n"
        )
        return path

    def analysis(self, text=",Forward,Backward,data_fraction\n0,2.0,1.9,0.5\n"):
        path = self.source / "set/L1/abfe_van1_hrex_r1/analysis/complex"
        path.mkdir(parents=True)
        (path / "ABFE_convergence.csv").write_text(text)
        return path

    def test_registers_three_repeats_and_one_shared_solvent(self):
        self.assertEqual(self.initialize(), 7)
        rows = self.rows()
        self.assertEqual(sum(row["spec"]["leg"] == "complex" for row in rows), 3)
        solvent = next(row for row in rows if row["spec"]["leg"] == "solvent")
        self.assertEqual(len(solvent["spec"]["shared_by"]), 3)
        for row in rows:
            if row["spec"]["leg"] == "complex":
                self.assertIn(solvent["spec"]["id"], row["spec"]["dependencies"])

    def test_existing_registry_is_not_overwritten(self):
        self.initialize()
        before = self.registry.read_bytes()
        with self.assertRaises(FileExistsError):
            self.initialize()
        self.assertEqual(self.registry.read_bytes(), before)

    def test_no_writes_in_source_tree(self):
        self.production()
        self.production("coul.00")

        def hashes():
            return {
                str(path): hashlib.sha256(path.read_bytes()).hexdigest()
                for path in self.source.rglob("*")
                if path.is_file()
            }

        before = hashes()
        self.initialize()
        campaign.scan(self.registry)
        self.assertEqual(hashes(), before)
        self.assertFalse(
            any(path.suffix == ".sqlite" for path in self.source.rglob("*"))
        )

    def test_registry_cannot_be_in_source_tree(self):
        with self.assertRaisesRegex(ValueError, "outside"):
            campaign.initialize(self.source / "registry.sqlite", self.manifest)

    def test_duplicate_ligands_and_count_mismatch_rejected(self):
        self.config["datasets"][0]["ligands"] = ["L1", "L1"]
        self.config["datasets"][0]["expected_ligands"] = 2
        self.write_manifest()
        with self.assertRaisesRegex(ValueError, "unique"):
            self.initialize()
        self.config["datasets"][0]["ligands"] = ["L1"]
        self.write_manifest()
        with self.assertRaisesRegex(ValueError, "expected_ligands"):
            self.initialize()

    def test_invalid_window_counts_rejected(self):
        self.config["datasets"][0]["complex_windows"] = {"rest": -1}
        self.write_manifest()
        with self.assertRaisesRegex(ValueError, "window counts"):
            self.initialize()

    def test_traversal_rejected(self):
        self.config["datasets"][0]["directory"] = "../outside"
        self.write_manifest()
        with self.assertRaisesRegex(ValueError, "must not contain"):
            self.initialize()

    def test_duplicate_source_directories_rejected(self):
        self.config["datasets"][0]["ligands"] = [
            {"id": "L1", "complex_paths": {"1": "same", "2": "same"}}
        ]
        self.write_manifest()
        with self.assertRaisesRegex(ValueError, "same source"):
            self.initialize()

    def test_all_window_minimum_is_observed_not_converged(self):
        self.production(time_ps=16000)
        self.production("coul.00", time_ps=18000)
        self.initialize()
        campaign.scan(self.registry)
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
        campaign.scan(self.registry)
        row = self.complex_row()
        self.assertIsNone(row["observation"]["production_ns"])
        self.assertIn(
            "incomplete_production_data", [item["code"] for item in row["incidents"]]
        )

    def test_inconsistent_targets_are_visible(self):
        self.production()
        self.production("coul.00", target_ps=30000)
        self.initialize()
        campaign.scan(self.registry)
        row = self.complex_row()
        self.assertIsNone(row["observation"]["target_ns"])
        self.assertIn(
            "inconsistent_targets", [item["code"] for item in row["incidents"]]
        )

    def test_invalid_and_ambiguous_time_data_are_visible(self):
        path = self.production()
        (path / "dhdl.xvg").write_text('@ xaxis label "Time (ns)"\n16 2 3\n')
        self.initialize()
        campaign.scan(self.registry)
        self.assertIn(
            "invalid_window:rest.00",
            [item["code"] for item in self.complex_row()["incidents"]],
        )
        (path / "dhdl.xvg").write_text('@ xaxis label "Time (ps)"\n16000 nan 3\n')
        with self.assertRaisesRegex(ValueError, "Invalid final"):
            campaign.production_end(path / "dhdl.xvg")

    def test_nonzero_time_origin_is_not_silently_assumed_zero(self):
        path = self.production()
        mdp = path / "rest.prod.00.mdp"
        mdp.write_text(mdp.read_text() + "tinit = 100\n")
        with self.assertRaisesRegex(ValueError, "time origin"):
            campaign.target_duration(mdp)

    def test_partial_last_line_is_not_treated_as_a_complete_sample(self):
        path = self.production() / "dhdl.xvg"
        path.write_text('@ xaxis label "Time (ps)"\n0 1 2\n16000 2')
        with self.assertRaisesRegex(ValueError, "incomplete final line"):
            campaign.production_end(path)

    def test_disappearing_sources_are_incidents_not_reset_progress(self):
        self.production()
        self.production("coul.00")
        self.initialize()
        campaign.scan(self.registry)
        shutil.rmtree(self.source / "set/L1/abfe_van1_hrex_r1/complex")
        campaign.scan(self.registry)
        codes = [item["code"] for item in self.complex_row()["incidents"]]
        self.assertIn("source_disappeared", codes)
        self.assertIn("production_evidence_lost", codes)

    def test_legacy_analysis_never_supplies_cutoff_or_convergence(self):
        self.production()
        self.production("coul.00")
        self.analysis()
        self.initialize()
        campaign.scan(self.registry)
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
        self.assertEqual(campaign.scan(self.registry), 7)
        self.assertEqual(
            self.complex_row()["observation"]["analysis"]["state"], "invalid"
        )

    def test_analysis_is_imported_even_without_physical_production(self):
        self.analysis()
        self.initialize()
        campaign.scan(self.registry)
        self.assertEqual(
            self.complex_row()["observation"]["analysis"]["state"], "legacy_unverified"
        )

    def test_history_is_append_only_for_changes_and_incidents_stay_open(self):
        path = self.production()
        self.initialize()
        campaign.scan(self.registry)
        history = self.complex_row()["history"]
        campaign.scan(self.registry)
        self.assertEqual(self.complex_row()["history"], history)
        self.production("coul.00")
        campaign.scan(self.registry)
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
        campaign.scan(self.registry)
        metadata, rows = campaign.read_registry(self.registry)
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
        report = campaign.report_html(metadata, rows)
        self.assertIn("Equilibration repeat: van1 (run name)", report)
        self.assertIn("Equilibration source:", report)
        self.assertNotIn("Embedding source:", report)
        self.assertNotIn("Embedding: van1", report)

    def test_equilibration_source_and_outputs_are_observed_without_claiming_completion(self):
        self.initialize()
        path = self.source / 'set/L1/vanilla_rep_1'
        path.mkdir(parents=True)
        row = self.complex_row()
        self.assertEqual(row['equilibration'], dict(source=str(path), repeat=1, state='unverified'))
        (path / 'npt.gro').write_text('test evidence')
        self.assertEqual(self.complex_row()['equilibration']['state'], 'outputs_on_file')

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
        campaign.scan(self.registry)
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
        campaign.scan(self.registry)
        row = next(
            row for row in self.rows() if row["spec"]["id"] == "test/L1/vanilla/1"
        )
        self.assertEqual(row["observation"]["production_ns"], 100)
        self.assertEqual(row["observation"]["scheduler"]["state"], "unknown")

    def test_readonly_commands_do_not_write_registry(self):
        self.initialize()
        campaign.scan(self.registry)
        before = self.registry.read_bytes()
        for args in (("status", "--dataset", "sigma2"), ("show", "test/L1/complex/1")):
            with redirect_stdout(io.StringIO()):
                campaign.main(["--registry", str(self.registry), *args])
        self.assertEqual(self.registry.read_bytes(), before)

    def test_html_escapes_values_and_does_not_enable_actions(self):
        self.initialize()
        metadata, rows = campaign.read_registry(self.registry)
        report = campaign.report_html(metadata, rows)
        self.assertTrue(
            "Test &lt;campaign&gt;" in report, "Campaign name must be escaped"
        )
        self.assertTrue("Inspect" in report, "Missing inspect control")
        self.assertTrue("Read-only inventory" in report, "Missing read-only footer")
        self.assertTrue(
            "<form" not in report, "Inventory must not expose mutation forms"
        )

    def test_report_cannot_overwrite_sources_or_registry(self):
        self.initialize()
        for destination in (self.source / "report.html", self.registry):
            with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as exit:
                campaign.main(
                    [
                        "--registry",
                        str(self.registry),
                        "report",
                        "--output",
                        str(destination),
                    ]
                )
            self.assertEqual(exit.exception.code, 2)

    def test_selected_campaign_manifest_has_expected_coverage(self):
        example = Path(__file__).parents[2] / "examples/membrane-campaign.json"
        metadata, specs = campaign.load_manifest(example, self.source)
        self.assertEqual(
            sum(item["registered_ligands"] for item in metadata["datasets"]), 62
        )
        self.assertEqual(len(specs), 434)
        self.assertEqual(
            next(item for item in metadata["datasets"] if item["id"] == "sigma2")[
                "expected_ligands"
            ],
            None,
        )

    def test_hawton_design_headers_and_embedded_fonts(self):
        self.initialize()
        metadata, rows = campaign.read_registry(self.registry)
        report = campaign.report_html(metadata, rows)
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
        self.assertEqual(len(set(campaign.DATASET_COLORS.values())), 6)

    def test_overview_counts_legs_and_keeps_unverified_analysis_unconverged(self):
        self.initialize()
        metadata, rows = campaign.read_registry(self.registry)
        for row in rows:
            row["observation"] = {"stage": "production_observed"}
        complex_row = next(r for r in rows if r["spec"]["leg"] == "complex")
        complex_row["observation"]["analysis"] = {
            "state": "legacy_unverified",
            "convergence": "converged",
        }
        complex_row["incidents"] = [{"code": "test", "message": "needs review"}]
        summary = campaign.summarize_runs(metadata, rows)
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
        metadata, rows = campaign.read_registry(self.registry)
        for row in rows:
            row["observation"] = {
                "analysis": {"state": "validated", "convergence": "converged"}
            }
        summary = campaign.summarize_runs(metadata, rows)
        self.assertEqual(summary["compounds"][0]["converged"], 4)
        self.assertEqual(summary["compounds"][0]["complete"], 0)

    def test_overview_views_do_not_mix_their_run_tables(self):
        self.initialize()
        metadata, rows = campaign.read_registry(self.registry)
        report = campaign.report_html(metadata, rows)
        for name in ("runs", "clusters", "datasets", "compounds"):
            self.assertIn(f'id="view-{name}"', report)
            self.assertIn(f'data-view="{name}"', report)
        self.assertIn("document.querySelectorAll('.run-table tbody tr')", report)
        self.assertIn('data-dataset="test" data-ligand="L1"', report)
        self.assertTrue("Submission ceiling: not reported" in report)
        self.assertTrue(
            "Slurm allocation snapshots, not GPU utilisation." not in report
        )
        self.assertIn(
            "Explicit approval",
            campaign.overview_html(
                dict(
                    metadata,
                    connections={
                        "ranvier": {
                            "capacity": {
                                "state": "observed",
                                "checked_at": campaign.now(),
                                "totals": dict(
                                    total=4, used=1, available=3, unavailable=0
                                ),
                                "pools": [
                                    dict(
                                        partition="g-kha",
                                        unit="A40 GPUs",
                                        used=1,
                                        available=3,
                                        unavailable=0,
                                        total=4,
                                        policy="Explicit approval required",
                                    )
                                ],
                            }
                        }
                    },
                ),
                rows,
            ),
        )

    def test_vanilla_and_shared_solvent_display_mapping(self):
        self.initialize()
        metadata, rows = campaign.read_registry(self.registry)
        vanilla = next(row for row in rows if row["spec"]["leg"] == "vanilla")
        view = campaign.presentation(vanilla, metadata)
        self.assertEqual(
            (view["simulation_type"], view["leg"], view["repeat"], view["convergence"]),
            ("Vanilla", "Complex", 1, "N/A"),
        )
        solvent = next(row for row in rows if row["spec"]["leg"] == "solvent")
        view = campaign.presentation(solvent, metadata)
        self.assertEqual(
            (view["simulation_type"], view["leg"], view["repeat"]),
            ("Alchemical", "Solvent", 1),
        )
        self.assertIsNone(
            solvent["spec"]["repeat"],
            "Presentation must not duplicate the shared calculation",
        )

    def test_location_requires_evidence_and_local_completion_requires_retrieval(self):
        self.initialize()
        metadata, rows = campaign.read_registry(self.registry)
        row = rows[0]
        self.assertEqual(campaign.presentation(row, metadata)["location"], "Unknown")
        row["observation"] = {
            "completion": "validated",
            "transfer": {"state": "unknown"},
        }
        self.assertEqual(campaign.presentation(row, metadata)["location"], "Unknown")
        row["observation"]["transfer"]["state"] = "verified_local"
        self.assertEqual(campaign.presentation(row, metadata)["location"], "absolut")
        self.assertEqual(campaign.presentation(row, metadata)["remarks"], "OK")

    def test_only_fresh_exact_work_directory_matches_supply_job_id_and_location(self):
        self.initialize()
        metadata, rows = campaign.read_registry(self.registry)
        row = self.complex_row()
        metadata["connections"] = {
            "ranvier": {
                "state": "connected",
                "checked_at": campaign.now(),
                "scheduler": {
                    "state": "observed",
                    "jobs": [
                        {
                            "job_id": "1234",
                            "state": "RUNNING",
                            "work_dir": "/home/reub0138/set/L1/abfe_van1_hrex_r1/complex",
                        },
                        {
                            "job_id": "9999",
                            "state": "RUNNING",
                            "work_dir": "/home/reub0138/set/L1/abfe_van1_hrex_r1/complex-other",
                        },
                    ],
                },
            }
        }
        view = campaign.presentation(row, metadata)
        self.assertEqual(
            (view["job_id"], view["location"], view["remarks"]),
            ("1234", "ranvier", "OK"),
        )
        metadata["connections"]["ranvier"]["checked_at"] = (
            datetime.now(timezone.utc) - timedelta(minutes=6)
        ).isoformat()
        view = campaign.presentation(row, metadata)
        self.assertEqual((view["job_id"], view["location"]), ("Unknown", "Unknown"))

    def test_array_job_work_directories_can_match_a_window(self):
        self.initialize()
        metadata, rows = campaign.read_registry(self.registry)
        row = self.complex_row()
        metadata["connections"] = {
            "soma": {
                "state": "connected",
                "checked_at": campaign.now(),
                "scheduler": {
                    "state": "observed",
                    "jobs": [
                        {
                            "job_id": "12_3",
                            "state": "PENDING",
                            "work_dir": "/home/reub0138/set/L1/abfe_van1_hrex_r1/complex/coul.00/prod",
                        }
                    ],
                },
            }
        }
        view = campaign.presentation(row, metadata)
        self.assertEqual(
            (view["job_id"], view["location"], view["remarks"], view["stage"]),
            ("12_3", "soma", "Waiting", "queued"),
        )

    def test_fresh_recorded_scheduler_identity_is_displayed(self):
        self.initialize()
        metadata, rows = campaign.read_registry(self.registry)
        row = self.complex_row()
        row["observation"] = {
            "stage": "production_observed",
            "scheduler": {
                "state": "RUNNING",
                "job_id": "123",
                "cluster": "archer",
                "observed_at": campaign.now(),
            },
        }
        view = campaign.presentation(row, metadata)
        self.assertEqual(
            (view["job_id"], view["location"], view["remarks"]), ("123", "archer", "OK")
        )

    def test_live_jobs_conflict_with_claimed_local_completion(self):
        self.initialize()
        metadata, rows = campaign.read_registry(self.registry)
        row = self.complex_row()
        row["observation"] = {
            "completion": "validated",
            "transfer": {"state": "verified_local"},
            "scheduler": {
                "state": "RUNNING",
                "job_id": "123",
                "cluster": "archer",
                "observed_at": campaign.now(),
            },
        }
        view = campaign.presentation(row, metadata)
        self.assertEqual(view["location"], "archer")
        self.assertEqual(view["remarks"], "Attention required")
        self.assertNotEqual(view["stage"], "complete")

    def test_remarks_do_not_hide_open_incidents_or_unverified_convergence(self):
        self.initialize()
        metadata, rows = campaign.read_registry(self.registry)
        row = self.complex_row()
        row["incidents"] = [{"code": "problem"}]
        row["observation"] = {
            "stage": "production_observed",
            "analysis": {"state": "legacy_unverified", "convergence": "converged"},
        }
        view = campaign.presentation(row, metadata)
        self.assertEqual(view["remarks"], "Attention required")
        self.assertEqual(view["convergence"], "unverified")
        row["incidents"] = []
        row["observation"]["analysis"] = {}
        self.assertEqual(campaign.presentation(row, metadata)["remarks"], "Unknown")

    @patch("abfe.campaign.probe_cluster")
    @patch("abfe.campaign.sample_slot", return_value=1200)
    def test_samples_are_bucketed_and_run_history_is_untouched(self, slot, probe):
        self.initialize()
        probe.return_value = {
            "state": "connected",
            "checked_at": campaign.now(),
            "scheduler": {"state": "observed", "jobs": []},
        }
        campaign.check_connections(self.registry)
        campaign.check_connections(self.registry)
        with sqlite3.connect(self.registry) as db:
            self.assertEqual(
                db.execute("SELECT count(*) FROM cluster_samples").fetchone()[0], 3
            )
            self.assertEqual(
                db.execute("SELECT count(*) FROM history").fetchone()[0], 0
            )
        output = self.work / "console"
        self.assertTrue(campaign.sample_clusters(self.registry, output))
        self.assertEqual(
            (output / "campaign.html").read_bytes(),
            (output / "preview.html").read_bytes(),
        )
        self.assertEqual(
            probe.call_count, 6, "Publishing in the same bucket must not repeat SSH"
        )

    @patch("abfe.campaign.probe_cluster")
    def test_sampler_rejects_publishing_into_simulations(self, probe):
        self.initialize()
        with self.assertRaises(ValueError):
            campaign.sample_clusters(self.registry, self.source)
        probe.assert_not_called()

    def test_connection_status_handles_old_failed_and_unchecked_probes(self):
        self.assertEqual(campaign.connection_state({}), "unknown")
        self.assertEqual(
            campaign.connection_state({"checked_at": "invalid"}), "unknown"
        )
        self.assertEqual(
            campaign.connection_state(
                {"state": "unreachable", "checked_at": campaign.now()}
            ),
            "unreachable",
        )
        checked = datetime.now(timezone.utc) - timedelta(minutes=6)
        self.assertEqual(
            campaign.connection_state(
                {"state": "connected", "checked_at": checked.isoformat()}
            ),
            "stale",
        )

    @patch("abfe.campaign.subprocess.run")
    def test_probe_uses_bounded_noninteractive_ssh_and_parses_own_jobs(self, run):
        run.side_effect = [
            subprocess.CompletedProcess([], 0, "ranvier\n", ""),
            subprocess.CompletedProcess(
                [],
                0,
                "1234|RUNNING|/home/reub0138/set/L1|normal|biggin|ran-01|reub0138\n",
                "",
            ),
            subprocess.CompletedProcess([], 1, "", "capacity unavailable"),
            subprocess.CompletedProcess([], 1, "", "ownership unavailable"),
        ]
        result = campaign.probe_cluster("ranvier")
        self.assertEqual(result["state"], "connected")
        self.assertEqual(result["scheduler"]["jobs"][0]["job_id"], "1234")
        self.assertTrue(
            all(call.kwargs["timeout"] == 15 for call in run.call_args_list)
        )
        self.assertIn("BatchMode=yes", run.call_args_list[0].args[0])
        self.assertTrue("--me" in run.call_args_list[1].args[0][-1])
        self.assertEqual(run.call_args_list[2].args[0][-1], "scontrol show nodes -d -o")
        self.assertEqual(result["capacity"]["state"], "unavailable")

    @patch("abfe.campaign.subprocess.run")
    def test_scheduler_failure_does_not_mean_ssh_disconnected(self, run):
        run.side_effect = [
            subprocess.CompletedProcess([], 0, "soma\n", ""),
            subprocess.CompletedProcess([], 1, "", "scheduler unavailable"),
            subprocess.CompletedProcess([], 1, "", "capacity unavailable"),
            subprocess.CompletedProcess([], 1, "", "ownership unavailable"),
        ]
        result = campaign.probe_cluster("soma")
        self.assertEqual(result["state"], "connected")
        self.assertEqual(result["scheduler"]["state"], "unavailable")

    @patch("abfe.campaign.subprocess.run")
    def test_probe_timeout_is_not_success(self, run):
        run.side_effect = subprocess.TimeoutExpired("ssh", 15)
        self.assertEqual(campaign.probe_cluster("archer")["state"], "unreachable")

    @patch("abfe.campaign.probe_cluster")
    def test_connection_check_persists_health_without_touching_run_history(self, probe):
        self.initialize()
        probe.return_value = {
            "state": "connected",
            "checked_at": campaign.now(),
            "scheduler": {"state": "observed", "jobs": []},
        }
        campaign.check_connections(self.registry)
        metadata, rows = campaign.read_registry(self.registry)
        self.assertEqual(set(metadata["connections"]), {"archer", "ranvier", "soma"})
        self.assertTrue(all(not row["history"] for row in rows))


    def test_refresh_matches_managed_paths_after_manual_monitor_expires(self):
        self.initialize()
        metadata, rows = campaign.read_registry(self.registry)
        row = next(r for r in rows if r['spec']['id'] == 'test/L1/complex/1')
        remote = '/home/reub0138/abflow/test/L1/complex/1/action'
        older = (datetime.now(timezone.utc) - timedelta(minutes=11)).isoformat()
        checked = campaign.now()
        row['executions'] = [dict(state='active', plan=dict(id='action', remote=remote,
            output=str(self.work/'action'), fingerprint='fixture', config=dict(cluster='ranvier',
            workload='complex-equilibration',mode='initial')), observation=dict(at=older,
            jobs=[dict(job_id='42',state='PENDING',work_dir=remote)]))]
        metadata['connections'] = dict(ranvier=dict(state='connected',checked_at=checked,
            scheduler=dict(state='observed',jobs=[dict(job_id='42',state='RUNNING',work_dir=remote+'/coul.00'),
                dict(job_id='99',state='RUNNING',work_dir=remote+'-other')])) )
        view = campaign.presentation(row, metadata)
        self.assertEqual((view['job_id'],view['location'],view['scheduler_state'],view['remarks']),
                         ('42','ranvier','Running','OK'))
        self.assertEqual(view['jobs'][0]['observed_at'],checked)
        self.assertEqual(view['convergence'],'unverified')
        report = campaign.report_html(metadata,rows)
        self.assertIn('Sort by Scheduler, ascending',report)
        self.assertIn('<td class="scheduler">Running</td>',report)
        self.assertIn("querySelector('.scheduler').textContent='Unknown'",report)
        self.assertIn('Scheduler',campaign.status(metadata,rows))
        metadata['connections']['ranvier']['checked_at'] = older
        self.assertEqual(campaign.presentation(row,metadata)['scheduler_state'],'Unknown')

    def test_latest_queue_observation_overrides_older_pending_execution(self):
        self.initialize()
        metadata,rows = campaign.read_registry(self.registry)
        row = next(r for r in rows if r['spec']['id'] == 'test/L1/complex/1')
        remote = '/home/reub0138/abflow/test/L1/complex/1/action'
        older = (datetime.now(timezone.utc)-timedelta(minutes=2)).isoformat()
        newer = campaign.now()
        execution = dict(state='active',plan=dict(remote=remote,config=dict(cluster='ranvier')),
            observation=dict(at=older,jobs=[dict(job_id='42',state='PENDING',work_dir=remote)]))
        row['executions'] = [execution]
        metadata['connections'] = dict(ranvier=dict(state='connected',checked_at=newer,
            scheduler=dict(state='observed',jobs=[])))
        self.assertEqual(campaign.presentation(row,metadata)['scheduler_state'],'Unknown')
        # Submission followed by a manual observation can be newer than the cached cluster sample.
        execution['observation']['at'] = newer
        metadata['connections']['ranvier']['checked_at'] = older
        view = campaign.presentation(row,metadata)
        self.assertEqual((view['scheduler_state'],view['stage']),('Pending','queued'))
        self.assertEqual(view['jobs'][0]['observed_at'],newer)
        metadata['connections']['ranvier']['checked_at'] = newer
        self.assertEqual(campaign.presentation(row,metadata)['scheduler_state'],'Pending')

    @patch('abfe.campaign.probe_cluster')
    @patch('abfe.campaign.sample_slot', return_value=1200)
    def test_periodic_sampler_republishes_managed_job_identity(self,slot,probe):
        self.initialize()
        from abfe.execution import save
        remote='/home/reub0138/abflow/test/L1/complex/1/action'
        old=(datetime.now(timezone.utc)-timedelta(minutes=11)).isoformat()
        plan=dict(id='action',run_id='test/L1/complex/1',remote=remote,output=str(self.work/'action'),
            fingerprint='fixture',config=dict(cluster='ranvier',workload='complex-equilibration',mode='initial'))
        save(self.registry,plan,'active',observation=dict(at=old,jobs=[]))
        probe.return_value=dict(state='connected',checked_at=campaign.now(),scheduler=dict(state='observed',
            jobs=[dict(job_id='42',state='RUNNING',work_dir=remote)]))
        output=self.work/'published'
        self.assertTrue(campaign.sample_clusters(self.registry,output))
        report=(output/'preview.html').read_text()
        self.assertIn('<td class="scheduler">Running</td>',report)
        self.assertIn('ranvier · 42 · RUNNING',report)
        self.assertEqual(probe.call_count,3)


if __name__ == "__main__":
    unittest.main()
