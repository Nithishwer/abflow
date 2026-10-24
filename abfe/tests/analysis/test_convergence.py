import abfe.analysis.convergence as analysis_convergence

from .fixtures import AnalysisFixture


class ConvergenceTests(AnalysisFixture):
    def test_decision_boundaries_and_increment_from_cutoff(self):
        self.assertEqual(
            analysis_convergence.decide(15999, 20000)["action"], "continue_target"
        )
        for cutoff, target in ((16000, 21), (21000, 26), (46000, 50), (49000, 50)):
            result = analysis_convergence.decide(cutoff, cutoff, 0.5)
            self.assertEqual(result["next_target_ns"], target)
        self.assertEqual(
            analysis_convergence.decide(16000, 30000, 0.5)["action"], "blocked"
        )
        self.assertEqual(
            analysis_convergence.decide(16000, 30000, 0.5)["next_target_ns"], 21
        )
        self.assertEqual(
            analysis_convergence.decide(16000, 20000, 0.499999)["action"],
            "stop_converged",
        )
        self.assertEqual(
            analysis_convergence.decide(50000, 50000, 0.5)["action"], "stop_at_limit"
        )
        self.assertEqual(
            analysis_convergence.decide(50000, 50000, 0.1)["action"], "stop_converged"
        )
        for cutoff, target, difference in (
            (16000, 20000, None),
            (16000, 20000, float("nan")),
            (16000, 20000, -1),
            (51000, 51000, 0.1),
        ):
            with self.assertRaises(ValueError):
                analysis_convergence.decide(cutoff, target, difference)

    def test_half_fraction_errors_and_absolute_difference(self):
        path = self.work / "halves.csv"
        for text in (
            "Forward,Backward,data_fraction\n0,1,0.1\n",
            "Forward,Backward,data_fraction\n0,1,.5\n1,2,.5\n",
            "Forward,Backward,data_fraction\n0,nan,.5\n",
            "other\n1\n",
        ):
            path.write_text(text)
            with self.assertRaises((ValueError, KeyError)):
                analysis_convergence.half_difference(path)
        path.write_text("Forward,Backward,data_fraction\n0,3,.5\n")
        self.assertEqual(analysis_convergence.half_difference(path), 3)
