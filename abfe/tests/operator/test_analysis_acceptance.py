import copy
import json
import os
import shutil
import sqlite3
from pathlib import Path
from unittest.mock import patch

from abfe.analysis import fingerprints, history, provenance, records
from abfe.operator.analysis_acceptance import verify_existing
from abfe.registry import read
from abfe.tests.analysis.fixtures import AnalysisFixture

from .test_next_step import NextStepSupport


class AnalysisAcceptanceTests(NextStepSupport, AnalysisFixture):
    def setUp(self):
        super().setUp()
        self.observations()
        self.reference = self.analyse(self.solvent_id)
        self.directory = self.historical_files() / "complex"
        self.csvs = [
            self.directory / name
            for name in ("ABFE_summary.csv", "ABFE_convergence.csv")
        ]
        self.csvs[1].write_text("Forward,Backward,data_fraction\n1,1.1,0.5\n")
        self.newest_dhdl = max(self.dhdl(i).stat().st_mtime_ns for i in range(2))
        self.set_csv_times(self.newest_dhdl + 1000000)

    def set_csv_times(self, timestamp):
        for path in self.csvs:
            os.utime(path, ns=(timestamp, timestamp))

    def verify(self, plan=False):
        return verify_existing(
            self.registry,
            self.complex_id,
            self.reference["id"],
            "User confirms identical scripts/settings and accepts timestamp freshness",
            plan,
        )

    def test_verified_receipt_preserves_history_and_stops_reanalysis(self):
        imported = history.import_analysis(self.registry, self.complex_id)
        before = {path: path.read_bytes() for path in self.csvs}
        with patch(
            "abfe.analysis.estimate.run_analysis",
            side_effect=AssertionError("No estimator"),
        ):
            record = self.verify()
            second = self.verify()
            proposal = self.saved()
        self.assertEqual(record["action"], "verified")
        self.assertEqual(second["action"], "already_verified")
        self.assertEqual(second["id"], record["id"])
        self.assertEqual(record["decision"]["action"], "stop_converged")
        self.assertEqual(record["evidence"]["cutoff_ps"], 20000)
        self.assertEqual(
            record["verification"]["reference_analysis_id"], self.reference["id"]
        )
        self.assertIn("user-confirmed", record["verification"]["basis"])
        self.assertEqual(
            record["result"], dict(estimate_kcal_mol=10, uncertainty_kcal_mol=2)
        )
        self.assertTrue(
            all(
                str(self.source / "set/L1/abfe_van1_hrex_r1/complex") in w["path"]
                for w in record["evidence"]["windows"]
            )
        )
        self.assertFalse(
            any(
                item["path"]
                in {w["path"] for w in self.reference["evidence"]["windows"]}
                for item in record["evidence"]["windows"]
            )
        )
        self.assertEqual(before, {path: path.read_bytes() for path in self.csvs})
        provenance.verify(record, self.spec(), rehash=True)
        self.assertIsNone(self.entry(proposal, self.complex_id)["operation"])
        self.assertEqual(self.entry(proposal, self.complex_id)["state"], "complete")
        with sqlite3.connect(self.registry) as db:
            self.assertEqual(
                json.loads(
                    db.execute(
                        "SELECT record FROM analyses WHERE id=?", (imported["id"],)
                    ).fetchone()[0]
                )["state"],
                "historical_unverified",
            )

    def test_plan_is_readonly(self):
        before = self.registry.read_bytes()
        directories = set((self.work / "analyses").iterdir())
        self.assertEqual(self.verify(plan=True)["action"], "preview")
        self.assertEqual(before, self.registry.read_bytes())
        self.assertEqual(directories, set((self.work / "analyses").iterdir()))

    def test_old_code_receipt_can_be_accepted_without_changing_it(self):
        def backend(evidence, output):
            result = self.fake_backend(evidence, output)
            (output / "ABFE_summary.csv").write_bytes(self.csvs[0].read_bytes())
            return result

        original = self.analyse(backend=backend)
        stale = copy.deepcopy(original)
        stale.update(id="old-code", output=str(self.work / "analyses/old-code"))
        stale["software"]["analysis_code_sha256"] = "previous implementation"
        output = Path(stale["output"])
        output.mkdir()
        for name in ("ABFE_summary.csv", "ABFE_convergence.csv"):
            shutil.copy2(Path(original["output"]) / name, output / name)
        stale["artifacts"] = [fingerprints.fingerprint(p) for p in output.iterdir()]
        records.persist(self.registry, stale, output)
        accepted = self.verify()
        self.assertEqual(accepted["action"], "verified")
        self.assertEqual(accepted["verification"]["supersedes"], "old-code")
        provenance.verify(accepted, self.spec(), rehash=True)
        with sqlite3.connect(self.registry) as db:
            saved = json.loads(
                db.execute(
                    "SELECT record FROM analyses WHERE id='old-code'"
                ).fetchone()[0]
            )
        self.assertEqual(
            saved["software"]["analysis_code_sha256"], "previous implementation"
        )

    def test_every_csv_must_be_strictly_newer_than_every_dhdl(self):
        for offset in (-1, 0):
            with self.subTest(offset=offset):
                self.set_csv_times(self.newest_dhdl + 1000000)
                os.utime(self.csvs[0], ns=(self.newest_dhdl + offset,) * 2)
                with self.assertRaisesRegex(ValueError, "strictly newer"):
                    self.verify()
        self.set_csv_times(self.newest_dhdl + 1000000)
        os.utime(self.dhdl(1), ns=(self.newest_dhdl + 2000000,) * 2)
        with self.assertRaisesRegex(ValueError, "strictly newer"):
            self.verify()

    def test_missing_or_invalid_results_are_not_adopted(self):
        original = self.csvs[1].read_text()
        for text in (
            "Forward,Backward,data_fraction\n1,nan,0.5\n",
            "Forward,Backward,data_fraction\n1,1,0.5\n1,1,0.5\n",
        ):
            self.csvs[1].write_text(text)
            self.set_csv_times(self.newest_dhdl + 1000000)
            with self.assertRaises(ValueError):
                self.verify()
        self.csvs[1].write_text(original)
        self.csvs[0].unlink()
        with self.assertRaises(FileNotFoundError):
            self.verify()

    def test_timestamp_rule_uses_the_fingerprinted_csv_snapshot(self):
        original = fingerprints.fingerprint

        def changed_timestamp(path):
            if Path(path) == self.csvs[0]:
                os.utime(path, ns=(self.newest_dhdl,) * 2)
            return original(path)

        with patch.object(fingerprints, "fingerprint", side_effect=changed_timestamp):
            with self.assertRaisesRegex(ValueError, "strictly newer"):
                self.verify(plan=True)

    def test_unconverged_result_keeps_its_cutoff_and_extension_decision(self):
        self.csvs[1].write_text("Forward,Backward,data_fraction\n1,1.5,0.5\n")
        self.set_csv_times(self.newest_dhdl + 1000000)
        record = self.verify()
        self.assertEqual(record["decision"]["action"], "propose_extension")
        self.assertEqual(record["decision"]["next_target_ns"], 25)
        self.assertEqual(record["half_difference_kcal_mol"], 0.5)
        self.assertNotEqual(
            self.entry(self.saved(), self.complex_id)["operation"], "analyse"
        )

    def test_changed_sources_invalidate_receipt(self):
        record = self.verify()
        self.csvs[0].write_text(self.csvs[0].read_text() + "\n")
        with self.assertRaisesRegex(ValueError, "snapshot changed"):
            provenance.verify(record, self.spec())

    def test_incidents_are_preserved_and_still_block_actions(self):
        with sqlite3.connect(self.registry) as db:
            db.execute(
                "INSERT INTO incidents(run_id,code,message,first_seen,last_seen) VALUES (?,?,?,?,?)",
                (self.complex_id, "existing_failure", "Needs review", "now", "now"),
            )
            before = db.execute("SELECT * FROM incidents").fetchall()
        self.verify()
        proposal = self.saved()
        self.assertEqual(self.entry(proposal, self.complex_id)["state"], "blocked")
        with sqlite3.connect(self.registry) as db:
            self.assertEqual(before, db.execute("SELECT * FROM incidents").fetchall())

    def test_authorization_and_current_reference_are_required(self):
        with self.assertRaisesRegex(ValueError, "authorization"):
            verify_existing(self.registry, self.complex_id, self.reference["id"], "")
        with self.assertRaisesRegex(ValueError, "does not exist"):
            verify_existing(self.registry, self.complex_id, "missing", "authorized")
        self.dhdl(0, leg="ligand").write_text("changed")
        with self.assertRaises(ValueError):
            self.verify()

    def test_console_explains_timestamp_acceptance(self):
        from abfe.console.runs import report_html

        self.verify()
        metadata, rows = read.read_registry(self.registry)
        self.assertIn("Analysis verification basis", report_html(metadata, rows))
        self.assertIn("user-confirmed script equivalence", report_html(metadata, rows))
