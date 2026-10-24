import json
import sqlite3

import abfe.analysis.history as analysis_history
import abfe.analysis.incidents as analysis_incidents
import abfe.analysis.records as analysis_records
import abfe.analysis.run as analysis_run
import abfe.console.runs as console_runs
import abfe.console.state as console_state
import abfe.console.summary as console_summary
import abfe.console.tiles as console_tiles
import abfe.monitoring.inventory as monitoring_inventory
import abfe.registry.read as registry_read

from .fixtures import AnalysisFixture


class HistoryTests(AnalysisFixture):
    def test_historical_preview_is_readonly_and_import_is_idempotent(self):
        root = self.historical_files()
        before = self.registry.read_bytes()
        source = {str(p): p.read_bytes() for p in root.rglob("*.csv")}
        preview = analysis_history.import_analysis(
            self.registry, self.complex_id, plan=True
        )
        self.assertEqual(self.registry.read_bytes(), before)
        self.assertFalse((self.work / "analyses").exists())
        self.assertEqual(preview["analysed_ns"], None)
        self.assertEqual(
            preview["files"][1]["table"]["reported_absolute_half_difference"], 3
        )
        first = analysis_history.import_analysis(self.registry, self.complex_id)
        second = analysis_history.import_analysis(self.registry, self.complex_id)
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(second["action"], "already_imported")
        with sqlite3.connect(self.registry) as db:
            self.assertEqual(
                db.execute("select count(*) from analyses").fetchone()[0], 1
            )
        self.assertEqual(source, {str(p): p.read_bytes() for p in root.rglob("*.csv")})
        metadata, rows = registry_read.read_registry(self.registry)
        row = next(r for r in rows if r["spec"]["id"] == self.complex_id)
        self.assertEqual(row["observation"]["analysis"]["state"], "historical_imported")
        self.assertEqual(
            console_state.presentation(row, metadata)["convergence"], "unverified"
        )
        self.assertIn(
            "Historical analysis imported", console_runs.report_html(metadata, rows)
        )
        self.assertEqual(
            analysis_run.current_decision(self.registry, self.complex_id)["action"],
            "blocked",
        )

    def test_historical_duration_amendment_is_displayed_without_convergence(self):
        self.historical_files()
        original = analysis_history.import_analysis(self.registry, self.complex_id)
        amended = dict(
            original,
            id="historical-duration-test",
            analysed_ns=20,
            supersedes=original["id"],
            duration_assignment=dict(rule="same_calendar_date"),
        )
        amended.pop("action", None)
        output = self.work / "amended"
        output.mkdir()
        amended["output"] = str(output)
        analysis_records.persist(self.registry, amended, output)
        metadata, rows = registry_read.read_registry(self.registry)
        row = next(r for r in rows if r["spec"]["id"] == self.complex_id)
        self.assertEqual(row["observation"]["analysis"]["analysed_ns"], 20)
        self.assertEqual(
            console_state.presentation(row, metadata)["convergence"], "unverified"
        )
        self.assertIn(
            "Analysed cutoff: 20 ns", console_runs.report_html(metadata, rows)
        )
        self.assertIn("same_calendar_date", console_runs.report_html(metadata, rows))
        self.assertEqual(
            analysis_run.current_decision(self.registry, self.complex_id)["action"],
            "blocked",
        )
        with sqlite3.connect(self.registry) as db:
            self.assertEqual(
                db.execute("select count(*) from analyses").fetchone()[0], 2
            )

    def test_historical_convergence_labels_do_not_authorize_actions(self):
        self.historical_files()
        original = analysis_history.import_analysis(self.registry, self.complex_id)
        for index, state in enumerate(
            ("historically_converged", "historically_unconverged")
        ):
            amended = dict(
                original,
                id=f"historical-convergence-{index}",
                analysed_ns=20,
                convergence=state,
            )
            amended.pop("action", None)
            output = self.work / f"amended-{index}"
            output.mkdir()
            amended["output"] = str(output)
            analysis_records.persist(self.registry, amended, output)
            metadata, rows = registry_read.read_registry(self.registry)
            row = next(r for r in rows if r["spec"]["id"] == self.complex_id)
            label = (
                "converged (historical)" if index == 0 else "unconverged (historical)"
            )
            self.assertEqual(
                console_state.presentation(row, metadata)["convergence"], label
            )
            self.assertEqual(
                console_state.presentation(row, metadata)["stage"], "analysis"
            )
            self.assertIn(label, console_runs.report_html(metadata, rows))
            self.assertEqual(
                console_summary.summarize_runs(metadata, rows)["datasets"][0][
                    "converged"
                ],
                0,
            )
            self.assertEqual(
                analysis_run.current_decision(self.registry, self.complex_id)["action"],
                "blocked",
            )

    def test_accepted_historical_provenance_is_scoped_and_never_authorizes_decisions(
        self,
    ):
        self.historical_files()
        original = analysis_history.import_analysis(self.registry, self.complex_id)
        for index, state in enumerate(
            ("historically_converged", "historically_unconverged", "unverified")
        ):
            amended = dict(
                original, id=f"accepted-historical-{index}", convergence=state
            )
            amended.pop("action", None)
            output = self.work / f"accepted-{index}"
            output.mkdir()
            amended["output"] = str(output)
            analysis_records.persist(self.registry, amended, output)
            accepted = {"analysis_id": amended["id"], "path": self.spec()["path"]}
            with sqlite3.connect(self.registry) as db:
                db.execute(
                    "INSERT OR REPLACE INTO metadata VALUES (?,?)",
                    (
                        "accepted_historical_provenance",
                        json.dumps({"runs": {self.complex_id: accepted}}),
                    ),
                )
                db.execute(
                    "UPDATE incidents SET status='retired' WHERE run_id=? AND code='analysis_provenance'",
                    (self.complex_id,),
                )
            metadata, rows = registry_read.read_registry(self.registry)
            row = next(r for r in rows if r["spec"]["id"] == self.complex_id)
            self.assertEqual(row["historical_provenance"], accepted)
            self.assertNotIn(
                "analysis_provenance", [i["code"] for i in row["observation"]["issues"]]
            )
            self.assertEqual(
                console_tiles.compound_status(
                    row, console_state.presentation(row, metadata)
                ),
                "complete" if state == "historically_converged" else "attention",
            )
            self.assertEqual(
                analysis_run.current_decision(self.registry, self.complex_id)["action"],
                "blocked",
            )
            self.assertIn(
                "Historical provenance (user accepted)",
                console_runs.report_html(metadata, rows),
            )
            for changed in (
                dict(row, incidents=[{"code": "new_failure", "message": "Review"}]),
                dict(
                    row,
                    observation=dict(
                        row["observation"], issues=[{"code": "invalid_analysis"}]
                    ),
                ),
            ):
                self.assertEqual(
                    console_tiles.compound_status(
                        changed, console_state.presentation(changed, metadata)
                    ),
                    "attention",
                )
        monitoring_inventory.scan(self.registry)
        metadata, rows = registry_read.read_registry(self.registry)
        row = next(r for r in rows if r["spec"]["id"] == self.complex_id)
        self.assertNotIn(
            "analysis_provenance", [i["code"] for i in row["observation"]["issues"]]
        )
        with sqlite3.connect(self.registry) as db:
            db.execute(
                "UPDATE metadata SET value=? WHERE key=?",
                (
                    json.dumps(
                        {"runs": {self.complex_id: dict(accepted, analysis_id="other")}}
                    ),
                    "accepted_historical_provenance",
                ),
            )
        row = next(
            r
            for r in registry_read.read_registry(self.registry)[1]
            if r["spec"]["id"] == self.complex_id
        )
        self.assertNotIn("historical_provenance", row)

    def test_historical_import_preserves_validated_analysis_and_open_incidents(self):
        managed = self.analyse()
        self.historical_files()
        analysis_incidents.incident(
            self.registry,
            self.complex_id,
            "restraint_provenance",
            "Existing unresolved provenance",
        )
        analysis_history.import_analysis(self.registry, self.complex_id)
        row = next(
            r
            for r in registry_read.read_registry(self.registry)[1]
            if r["spec"]["id"] == self.complex_id
        )
        self.assertEqual(row["analysis_record"]["id"], managed["id"])
        self.assertEqual(row["observation"]["analysis"]["state"], "validated")
        self.assertEqual(row["incidents"][0]["status"], "open")
        self.assertEqual(row["historical_analysis"]["kind"], "historical")

    def test_historical_import_preserves_linked_solvent_origin(self):
        root = self.historical_files()
        old = self.source / "legacy-v1/analysis/ligand"
        old.mkdir(parents=True)
        (old / "ABFE_summary.csv").write_text(",,MBAR,MBAR_Error\nStages,TOTAL,5,1\n")
        (root / "ligand").symlink_to(old, target_is_directory=True)
        record = analysis_history.import_analysis(self.registry, self.complex_id)
        reference = record["shared_solvent"]
        self.assertEqual(reference["path"], str(old))
        self.assertEqual(reference["compatibility"], "unverified")
        self.assertEqual(reference["files"][0]["path"], str(old / "ABFE_summary.csv"))
        solvent = analysis_history.import_analysis(self.registry, self.solvent_id)
        self.assertEqual(
            solvent["files"][0]["identity"]["path"], reference["files"][0]["path"]
        )

    def test_bad_historical_results_block_and_record_incident(self):
        root = self.historical_files()
        path = root / "complex/ABFE_convergence.csv"
        path.write_text("Forward,Backward,data_fraction\n0,1,.5\n0,1,.5\n")
        before = self.registry.read_bytes()
        with self.assertRaisesRegex(ValueError, "Duplicate convergence"):
            analysis_history.import_analysis(self.registry, self.complex_id, plan=True)
        self.assertEqual(self.registry.read_bytes(), before)
        with self.assertRaisesRegex(ValueError, "incident recorded"):
            analysis_history.import_analysis(self.registry, self.complex_id)
        with sqlite3.connect(self.registry) as db:
            self.assertEqual(
                db.execute(
                    "select status from incidents where code='analysis_import'"
                ).fetchone()[0],
                "open",
            )
        self.historical_files()
        with self.assertRaisesRegex(ValueError, "Resolve the previous"):
            analysis_history.import_analysis(self.registry, self.complex_id)
