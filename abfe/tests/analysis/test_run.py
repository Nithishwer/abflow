import os
import sqlite3
import unittest
from pathlib import Path
from unittest.mock import patch

import abfe.analysis.incidents as analysis_incidents
import abfe.analysis.run as analysis_run
import abfe.registry.read as registry_read

from .fixtures import AnalysisFixture


class RunTests(AnalysisFixture):
    def test_plan_and_below_minimum_never_run_estimator(self):
        with patch("abfe.analysis.estimate.run_analysis") as backend:
            planned = analysis_run.analyse(
                self.registry, self.complex_id, 16, plan=True
            )
            small = analysis_run.analyse(self.registry, self.complex_id, 15)
            backend.assert_not_called()
        self.assertEqual(planned["decision"]["action"], "analyse")
        self.assertEqual(small["decision"]["action"], "continue_target")
        self.assertEqual(
            analysis_run.current_decision(self.registry, self.complex_id)["action"],
            "continue_target",
        )

    def test_retired_checks_do_not_block_analysis_and_other_incidents_still_do(self):
        analysis_incidents.incident(
            self.registry,
            self.complex_id,
            "embedding_provenance",
            "Embedding source unknown",
        )
        with sqlite3.connect(self.registry) as db:
            db.execute(
                "UPDATE incidents SET status='retired' WHERE code='embedding_provenance'"
            )
        planned = analysis_run.analyse(self.registry, self.complex_id, 20, plan=True)
        self.assertEqual(planned["decision"]["action"], "analyse")
        record = self.analyse()
        self.assertEqual(record["state"], "validated")
        self.assertFalse(record["open_incidents"])
        row = next(
            r
            for r in registry_read.read_registry(self.registry)[1]
            if r["spec"]["id"] == self.complex_id
        )
        self.assertFalse(row["incidents"])
        self.assertEqual(row["incident_history"][0]["status"], "retired")
        self.assertEqual(
            analysis_run.current_decision(self.registry, self.complex_id)["action"],
            "stop_converged",
        )
        analysis_incidents.incident(
            self.registry, self.complex_id, "unexpected_data_issue", "Needs review"
        )
        with patch("abfe.analysis.estimate.run_analysis") as backend:
            result = analysis_run.analyse(self.registry, self.complex_id, 20)
            self.assertEqual(result["action"], "blocked")
            self.assertIn("unexpected_data_issue", result["reason"])
            backend.assert_not_called()

    def test_no_copies_no_source_writes_and_immutable_analysis(self):
        before = {str(p): p.read_bytes() for p in self.source.rglob("*") if p.is_file()}
        record = self.analyse()
        self.assertEqual(
            before,
            {str(p): p.read_bytes() for p in self.source.rglob("*") if p.is_file()},
        )
        self.assertFalse(list(Path(record["output"]).glob("*.xvg")))
        self.assertEqual(
            analysis_run.current_decision(self.registry, self.complex_id)["action"],
            "stop_converged",
        )
        with sqlite3.connect(self.registry) as db:
            for statement in (
                "UPDATE analyses SET record=record",
                "DELETE FROM analyses",
            ):
                with self.assertRaisesRegex(sqlite3.IntegrityError, "immutable"):
                    db.execute(statement)

    def test_new_plan_does_not_replace_validated_analysis(self):
        record = self.analyse()
        analysis_run.analyse(self.registry, self.complex_id, 16, plan=True)
        self.assertEqual(
            next(
                r
                for r in registry_read.read_registry(self.registry)[1]
                if r["spec"]["id"] == self.complex_id
            )["analysis_record"]["id"],
            record["id"],
        )

    def test_estimator_failure_is_retained_and_incident_blocks_retry(self):
        def broken(*args):
            raise RuntimeError("scientific failure")

        with self.assertRaisesRegex(ValueError, "incident recorded"):
            self.analyse(backend=broken)
        metadata, rows = registry_read.read_registry(self.registry)
        row = next(r for r in rows if r["spec"]["id"] == self.complex_id)
        self.assertEqual(row["analysis_record"]["state"], "failed")
        self.assertEqual(len(row["incidents"]), 1)
        with patch("abfe.analysis.estimate.run_analysis") as backend:
            self.assertEqual(
                analysis_run.analyse(self.registry, self.complex_id, 20)["action"],
                "blocked",
            )
            backend.assert_not_called()

    def test_change_during_analysis_is_an_incident_not_a_decision(self):
        def changing(evidence, output):
            result = self.fake_backend(evidence, output)
            self.dhdl().write_text(
                self.dhdl().read_text().replace("1000.0000 0", "1000.0000 1")
            )
            return result

        with self.assertRaisesRegex(ValueError, "snapshot changed"):
            self.analyse(backend=changing)
        self.assertEqual(
            analysis_run.current_decision(self.registry, self.complex_id)["action"],
            "blocked",
        )

    def test_malformed_backend_results_cannot_authorize_extension(self):
        def invalid(evidence, output):
            result = self.fake_backend(evidence, output)
            (output / "ABFE_convergence.csv").write_text(
                "Forward,Backward,data_fraction\n0,1,.5\n0,1,.5\n"
            )
            return result

        with self.assertRaisesRegex(ValueError, "exactly one"):
            self.analyse(backend=invalid)
        self.assertEqual(
            analysis_run.current_decision(self.registry, self.complex_id)["action"],
            "blocked",
        )

    @unittest.skipUnless(
        os.environ.get("ABFLOW_SCIENTIFIC_TEST") == "1",
        "set ABFLOW_SCIENTIFIC_TEST=1 for the alchemlyb integration test",
    )
    def test_scientific_backend_reads_in_place_and_honours_cutoff(self):
        import numpy as np

        rng = np.random.default_rng(20261016)
        for state in range(2):
            path = self.dhdl(state)
            mdp = path.parent / f"coul.prod.{state:02d}.mdp"
            mdp.write_text(
                mdp.read_text().replace("nstdhdl = 500000", "nstdhdl = 50000")
                + "vdw-lambdas = 0 0\n"
            )
            text = (
                '@ xaxis label "Time (ps)"\n'
                f'@ subtitle "T = 298.15 (K) state {state}: (coul-lambda, vdw-lambda) = ({state:.4f}, 0.0000)"\n'
                '@ s0 legend "Total Energy (kJ/mol)"\n'
                f'@ s1 legend "dH/d\\xl\\f{{}} coul-lambda = {state:.4f}"\n'
                '@ s2 legend "dH/d\\xl\\f{} vdw-lambda = 0.0000"\n'
                '@ s3 legend "\\xD\\f{}H \\xl\\f{} to (0.0000, 0.0000)"\n'
                '@ s4 legend "\\xD\\f{}H \\xl\\f{} to (1.0000, 0.0000)"\n'
            )
            rt = 0.00831446261815324 * 298.15
            for t, x in zip(range(0, 20001, 100), rng.normal(state, 1, 201)):
                u = 0.5 * (x - state) ** 2
                text += f"{t:.4f} {u * rt:.10f} {(state - x) * rt:.10f} 0 {(0.5 * x * x - u) * rt:.10f} {(0.5 * (x - 1) ** 2 - u) * rt:.10f}\n"
            path.write_text(text)
        before = [self.dhdl(i).read_bytes() for i in range(2)]
        record = analysis_run.analyse(self.registry, self.complex_id, 16)
        self.assertEqual(record["state"], "validated")
        self.assertEqual(record["evidence"]["cutoff_ps"], 16000)
        self.assertTrue(
            all(w["endpoint_ps"] == 20000 for w in record["evidence"]["windows"])
        )
        self.assertTrue(all(0 < n <= 160 for n in record["result"]["retained_samples"]))
        self.assertLess(abs(record["result"]["estimate_kcal_mol"]), 0.2)
        self.assertEqual(record["decision"]["action"], "stop_converged")
        self.assertEqual([self.dhdl(i).read_bytes() for i in range(2)], before)
        self.assertFalse(list(Path(record["output"]).glob("*.xvg")))
        self.assertTrue(record["software"]["source_files"])
