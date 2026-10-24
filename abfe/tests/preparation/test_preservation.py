import os
from pathlib import Path
from unittest.mock import Mock, patch

import abfe.console.runs as console_runs
import abfe.preparation.build as preparation_build
import abfe.preparation.inputs as preparation_inputs
import abfe.preparation.run as preparation_run
import abfe.registry.read as registry_read
from abfe.preparation.coordinates import copy_complex2membrane
from abfe.preparation.directory import working_directory

from .fixtures import PreparationFixture


class PreservationTests(PreparationFixture):
    def test_helper_failure_reaches_incident_boundary_and_stops_preparation(self):
        before = Path.cwd()
        following_step = Mock()

        def fail(plan):
            with working_directory(self.output):
                Path("protein.gro").write_text("protein\n0\n1 1 1\n")
                copy_complex2membrane()
                following_step()

        with self.assertRaisesRegex(ValueError, "membrane.gro"):
            self.run_staging(fail)
        following_step.assert_not_called()
        self.assertEqual(Path.cwd(), before)
        self.assertTrue((self.output / "complex_membrane.gro").is_file())
        self.assertFalse((self.output / "receipt.json").exists())
        self.assertNotIn("successfully", (self.output / "preparation.log").read_text())
        for row in registry_read.read_registry(self.registry)[1]:
            self.assertEqual(row["preparation"]["state"], "failed")
            self.assertTrue(
                any(item["code"] == "preparation" for item in row["incidents"])
            )

    def test_identical_rerun_reuses_verified_outputs(self):
        first = self.run_staging()
        before = preparation_inputs.snapshot(self.output)
        with patch.object(
            preparation_build,
            "generate",
            side_effect=AssertionError("must not regenerate"),
        ):
            second = preparation_run.prepare(
                self.registry, "test/L1", self.manifest, self.output, "vanilla"
            )
        self.assertEqual(first, second)
        self.assertEqual(before, preparation_inputs.snapshot(self.output))
        rows = registry_read.read_registry(self.registry)[1]
        self.assertTrue(all(row["preparation"]["state"] == "prepared" for row in rows))
        self.assertIn(
            "Staged preparation",
            console_runs.report_html(*registry_read.read_registry(self.registry)),
        )

    def test_changed_output_pauses_and_preserves_evidence(self):
        self.run_staging()
        changed = self.output / "L1/vanilla_rep_2/topol.top"
        changed.write_text("changed")
        with self.assertRaisesRegex(ValueError, "outputs changed"):
            self.run_staging()
        self.assertEqual(changed.read_text(), "changed")
        self.assertTrue(
            all(
                any(i["code"] == "preparation" for i in row["incidents"])
                for row in registry_read.read_registry(self.registry)[1]
            )
        )
        with self.assertRaisesRegex(ValueError, "explicit resolution"):
            self.plan()

    def test_failed_setup_restores_cwd_and_blocks_retry(self):
        before = Path.cwd()

        def fail(plan):
            os.chdir(self.output)
            (self.output / "partial").write_text("retain")
            raise SystemExit("restraint failure")

        with self.assertRaisesRegex(ValueError, "restraint failure"):
            self.run_staging(fail)
        self.assertEqual(Path.cwd(), before)
        self.assertEqual((self.output / "partial").read_text(), "retain")
        self.assertFalse((self.output / "receipt.json").exists())
        self.assertEqual(
            registry_read.read_registry(self.registry)[1][0]["preparation"]["state"],
            "failed",
        )
        with self.assertRaisesRegex(ValueError, "Open preparation incident"):
            self.plan()

    def test_input_change_during_generation_prevents_publication(self):
        def change(plan):
            self.fake_vanilla(plan)
            (self.protein / "topol.top").write_text("changed during setup")

        with self.assertRaisesRegex(ValueError, "inputs changed during"):
            self.run_staging(change)
        self.assertFalse((self.output / "receipt.json").exists())

    def test_invalid_preflight_records_incident_without_creating_staging(self):
        (self.protein / "topol.top").write_text("unreviewed force field")
        with self.assertRaisesRegex(ValueError, "force-field mismatch"):
            self.run_staging()
        self.assertFalse(self.output.exists())
        self.assertTrue(
            all(
                row["preparation"]["state"] == "failed"
                and any(i["code"] == "preparation" for i in row["incidents"])
                for row in registry_read.read_registry(self.registry)[1]
            )
        )

    def test_changed_inputs_cannot_replace_existing_staging(self):
        self.run_staging()
        before = preparation_inputs.snapshot(self.output)
        (self.protein / "protein.gro").write_text("different coordinates")
        with self.assertRaisesRegex(ValueError, "different inputs"):
            self.run_staging()
        self.assertEqual(before, preparation_inputs.snapshot(self.output))

    def test_preparation_lock_prevents_concurrent_generation(self):
        import fcntl

        with Path(str(self.registry) + ".preparation.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with patch.object(
                preparation_build,
                "generate",
                side_effect=AssertionError("must not run"),
            ):
                with self.assertRaises(BlockingIOError):
                    preparation_run.prepare(
                        self.registry, "test/L1", self.manifest, self.output, "vanilla"
                    )
        self.assertFalse(self.output.exists())
