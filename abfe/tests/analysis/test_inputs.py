import hashlib

import abfe.analysis.history as analysis_history
import abfe.analysis.inputs as analysis_inputs

from .fixtures import AnalysisFixture


class InputsTests(AnalysisFixture):
    def test_file_identity_and_exact_cutoff(self):
        value = analysis_inputs.validate_inputs(self.spec(), 16000)
        self.assertEqual(len(value["windows"]), 2)
        self.assertEqual(
            value["windows"][0]["sha256"],
            hashlib.sha256(self.dhdl().read_bytes()).hexdigest(),
        )
        self.assertNotIn("samples_through_cutoff", value["windows"][0])
        self.assertEqual(value["windows"][0]["endpoint_ps"], 20000)
        self.assertEqual(value["temperature_K"], 298.15)
        self.assertEqual(value["protocol"]["discard_ps"], 10)
        with self.assertRaisesRegex(ValueError, "exact requested cutoff"):
            analysis_inputs.validate_inputs(self.spec(), 16500)

    def test_missing_and_extra_windows_fail(self):
        self.dhdl(1).parent.parent.rename(
            self.dhdl(1).parent.parent.with_name("coul.02")
        )
        with self.assertRaisesRegex(ValueError, "Window inventory mismatch"):
            analysis_inputs.validate_inputs(self.spec(), 16000)

    def test_interior_numeric_values_are_not_validated(self):
        original = self.dhdl().read_text()
        for bad in ("nan", "broken", ""):
            with self.subTest(bad=bad):
                self.dhdl().write_text(
                    original.replace("1000.0000 0 0 0 0", f"1000.0000 {bad} 0 0 0")
                )
                analysis_inputs.validate_inputs(self.spec(), 16000)

    def test_full_stream_sample_checks_are_removed(self):
        original = self.dhdl().read_text()
        for text in (
            original.replace("1000.0000 0 0 0 0\n", "", 1),
            original.replace("1000.0000", "0.0000", 1),
            original.replace("0.0000 0 0 0 0\n", "500.0000 0 0 0 0\n", 1),
            original[:-1],
        ):
            self.dhdl().write_text(text)
            analysis_inputs.validate_inputs(self.spec(), 16000)

    def test_cutoff_probes_find_samples_and_reject_a_missing_cutoff(self):
        for cutoff in range(1000, 21000, 1000):
            with self.subTest(cutoff=cutoff):
                analysis_inputs.validate_inputs(self.spec(), cutoff)
        self.dhdl().write_text(
            self.dhdl().read_text().replace("16000.0000 0 0 0 0\n", "")
        )
        with self.assertRaisesRegex(ValueError, "exact requested cutoff"):
            analysis_inputs.validate_inputs(self.spec(), 16000)

    def test_units_schedule_state_temperature_and_endpoint_columns_fail(self):
        original = self.dhdl().read_text()
        for text in (
            original.replace("Time (ps)", "Time (ns)"),
            original.replace("state 0:", "state 1:"),
            original.replace("= (0.0000)", "= (1.0000)"),
            original.replace("298.15", "300"),
            original.replace("to (1.0000)", "to (0.5000)"),
        ):
            self.dhdl().write_text(text)
            with self.assertRaises(ValueError):
                analysis_inputs.validate_inputs(self.spec(), 16000)

    def test_missing_tpr_and_inconsistent_targets_fail(self):
        tpr = self.dhdl().parent / "topol.tpr"
        tpr.unlink()
        with self.assertRaises(FileNotFoundError):
            analysis_inputs.validate_inputs(self.spec(), 16000)
        tpr.write_bytes(b"restored test fixture")
        mdp = self.dhdl(1).parent / "coul.prod.01.mdp"
        mdp.write_text(mdp.read_text().replace("10000000", "15000000"))
        with self.assertRaisesRegex(ValueError, "inconsistent targets"):
            analysis_inputs.validate_inputs(self.spec(), 16000)

    def test_existing_data_above_cap_are_preserved_and_rejected(self):
        self.write_dhdl(self.dhdl(), 0, end=51000)
        before = self.dhdl().read_bytes()
        with self.assertRaisesRegex(ValueError, "exceed the 50 ns cap"):
            analysis_inputs.validate_inputs(self.spec(), 16000)
        self.assertEqual(self.dhdl().read_bytes(), before)

    def test_historical_csv_rejects_missing_nonfinite_and_conflicting_results(self):
        path = self.work / "ABFE_summary.csv"
        for text in (
            "other\n1\n",
            "state,MBAR,MBAR_Error\ntotal,nan,1\n",
            "state,MBAR,MBAR_Error\ntotal,1,-1\n",
            "state,MBAR,MBAR_Error\ntotal,1,1\ntotal,2,1\n",
        ):
            path.write_text(text)
            with self.assertRaises(ValueError):
                analysis_history.historical_csv(path)
