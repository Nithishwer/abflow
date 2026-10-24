import io
import json
import shutil
from contextlib import redirect_stdout
from pathlib import Path

import abfe.commands.main as commands_main
import abfe.preparation.run as preparation_run
import abfe.preparation.manifest as preparation_manifest

from .fixtures import PreparationFixture


class ManifestTests(PreparationFixture):
    def test_shared_mdp_reader_is_in_preparation_sources(self):
        plan = self.plan()
        shared = Path(preparation_manifest.__file__).parents[1] / "mdp.py"
        self.assertIn(str(shared), [item["path"] for item in plan["sources"]])

    def test_plan_has_exact_mapping_and_writes_nothing(self):
        before = self.registry.read_bytes()
        plan = self.plan()
        self.assertEqual(
            plan["config"]["protocol"]["protein_forcefield"]["name"],
            "amber99sb-star-ildn-mut.ff",
        )
        self.assertEqual(len(plan["mappings"]), 3)
        self.assertEqual(
            plan["mappings"][2]["complex"],
            str(self.output / "L1/abfe_van3_hrex_r1/complex"),
        )
        self.assertEqual(
            plan["solvent"], str(self.output / "L1/abfe_van1_hrex_r1/ligand")
        )
        self.assertEqual(self.registry.read_bytes(), before)
        self.assertFalse(self.output.exists())
        self.assertFalse(Path(str(self.registry) + ".preparation.lock").exists())

    def test_preparation_tracks_equilibration_without_source_identity_labels(self):
        plan = self.plan()
        self.assertEqual(
            [item["equilibration_repeat"] for item in plan["mappings"]],
            ["van1", "van2", "van3"],
        )
        for item in plan["mappings"]:
            self.assertEqual(
                set(item),
                {"repeat", "equilibration_repeat", "vanilla", "topology", "complex"},
            )
            self.assertNotIn("embedding", item)
            self.assertNotIn("embedding_id", item)
        membrane = self.config["inputs"]["charmm_folder"]
        self.assertEqual(
            len([item for item in plan["sources"] if item["path"] == membrane]), 1
        )

    def test_alchemical_preparation_needs_no_membrane_source_or_identity(self):
        self.make_alchemical_inputs()
        self.config["inputs"].pop("charmm_folder")
        self.write_config()
        plan = preparation_run.prepare(
            self.registry, "test/L1", self.manifest, self.output, "alchemical", True
        )
        self.assertEqual(len(plan["mappings"]), 3)

    def test_rejects_protocol_mismatch_before_generation(self):
        (self.protein / "topol.top").write_text(
            '#include "amber99sb-ildn.ff/forcefield.itp"'
        )
        with self.assertRaisesRegex(ValueError, "mismatch"):
            self.plan()
        self.assertFalse(self.output.exists())

    def test_rejects_normal_ildn_manifest_name_and_directory(self):
        self.config["protocol"]["protein_forcefield"]["name"] = "AMBER99SB-ILDN"
        self.write_config()
        with self.assertRaisesRegex(
            ValueError, "Expected protein_forcefield: amber99sb-star-ildn-mut.ff"
        ):
            self.plan()
        self.config["protocol"]["protein_forcefield"]["name"] = (
            "amber99sb-star-ildn-mut.ff"
        )
        self.config["protocol"]["protein_forcefield"]["path"] = str(
            self.ff.parent / "amber99sb-ildn.ff"
        )
        self.write_config()
        with self.assertRaisesRegex(
            ValueError, "Supply reviewed amber99sb-star-ildn-mut.ff"
        ):
            self.plan()

    def test_alchemical_repeat_rejects_normal_ildn_topology(self):
        self.make_alchemical_inputs()
        topology = Path(self.config["repeats"]["2"]["topology"])
        topology.write_text('#include "amber99sb-ildn.ff/forcefield.itp"\n')
        with self.assertRaisesRegex(ValueError, "Vanilla protein force-field mismatch"):
            preparation_run.prepare(
                self.registry, "test/L1", self.manifest, self.output, "alchemical", True
            )

    def test_rejects_relative_and_overlapping_destinations(self):
        with self.assertRaisesRegex(ValueError, "absolute"):
            preparation_run.prepare(
                self.registry,
                "test/L1",
                self.manifest,
                Path("relative"),
                "vanilla",
                True,
            )
        self.output = self.source / "set/L1"
        for r in (1, 2, 3):
            self.config["repeats"][str(r)]["vanilla"] = str(
                self.output / "L1" / f"vanilla_rep_{r}"
            )
        self.write_config()
        with self.assertRaisesRegex(ValueError, "outside"):
            self.plan()

    def test_cli_plan(self):
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            commands_main.main(
                [
                    "--registry",
                    str(self.registry),
                    "prepare",
                    "test/L1",
                    "--manifest",
                    str(self.manifest),
                    "--stage",
                    "vanilla",
                    "--output",
                    str(self.output),
                    "--plan",
                ]
            )
        self.assertEqual(json.loads(stdout.getvalue())["compound"], "test/L1")

    def test_equilibration_repeats_cannot_reuse_one_vanilla_trajectory(self):
        self.make_alchemical_inputs()
        first = Path(self.config["repeats"]["1"]["vanilla"]) / "prod.xtc"
        second = Path(self.config["repeats"]["2"]["vanilla"]) / "prod.xtc"
        shutil.copyfile(first, second)
        with self.assertRaisesRegex(ValueError, "Identical vanilla trajectories"):
            preparation_run.prepare(
                self.registry, "test/L1", self.manifest, self.output, "alchemical", True
            )

    def test_shared_solvent_rejects_mismatched_parameters(self):
        self.make_alchemical_inputs()
        Path(self.config["repeats"]["2"]["vanilla"], "toppar/L1.itp").write_text(
            "different parameters"
        )
        with self.assertRaisesRegex(ValueError, "parameters differ"):
            preparation_run.prepare(
                self.registry, "test/L1", self.manifest, self.output, "alchemical", True
            )
