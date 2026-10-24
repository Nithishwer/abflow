import tempfile
import unittest
from pathlib import Path

from abfe.analysis.policy import settings
from abfe.execution.files import duration, mdp
from abfe.mdp import read_settings
from abfe.monitoring.progress import target_duration


class MDPTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "prod.mdp"

    def test_comments_whitespace_case_and_underscores_keep_existing_rules(self):
        self.path.write_text(
            "; comment\n ignored\n REF_T = 298.15 ; note\n dt = 0.002\n"
        )
        self.assertEqual(settings(self.path), {"ref-t": "298.15", "dt": "0.002"})
        expected = {"REF-T": "298.15", "dt": "0.002"}
        self.assertEqual(read_settings(self.path), expected)
        self.assertEqual(mdp(self.path), expected)

    def test_strict_duplicates_include_normalized_case_and_underscores(self):
        for text in ("dt=0.001\ndt=0.002\n", "REF_T=298\nref-t=299\n"):
            with self.subTest(text=text):
                self.path.write_text(text)
                with self.assertRaisesRegex(ValueError, "Duplicate MDP setting"):
                    settings(self.path)
                with self.assertRaisesRegex(ValueError, "Duplicate MDP setting"):
                    read_settings(self.path, strict=True)

    def test_permissive_duplicates_and_duration_units_are_unchanged(self):
        self.path.write_text("dt=0.001\ndt=0.002\nnsteps=10000000\n")
        self.assertEqual(mdp(self.path)["dt"], "0.002")
        self.assertEqual(duration(self.path), 20000)
        self.assertEqual(target_duration(self.path), 20)

    def test_duration_validation_stays_with_each_caller(self):
        for extra in ("tinit=1\n", "init_step=1\n", "dt=nan\n"):
            with self.subTest(extra=extra):
                self.path.write_text("dt=0.002\nnsteps=10000000\n" + extra)
                for reader in (duration, target_duration):
                    with self.assertRaises(ValueError):
                        reader(self.path)
        self.path.write_text("dt=0.002\nnsteps=0\n")
        self.assertEqual(target_duration(self.path), 0)
        with self.assertRaises(ValueError):
            duration(self.path)
        self.path.write_text("dt=0.002\nnsteps=10000000.0\n")
        self.assertEqual(target_duration(self.path), 20)
        with self.assertRaises(ValueError):
            duration(self.path)
