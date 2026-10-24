import hashlib
import json
from pathlib import Path

import abfe.monitoring.inventory as monitoring_inventory
import abfe.registry.manifest as registry_manifest

from .fixtures import CampaignFixture


class ManifestTests(CampaignFixture):
    def test_yaml_comments_and_explicit_repeat_paths(self):
        self.manifest.write_text(
            f"""# Readable campaign configuration
schema_version: 1
name: Test campaign
source_root: {self.source}
datasets:
  - id: test
    label: Test dataset
    expected_ligands: 1
    complex_windows: {{rest: 1, coul: 1}}
    solvent_windows: {{coul: 1}}
    ligands:
      - id: '19'
        vanilla_paths:
          '1': custom/vanilla1
        complex_paths:
          '1': custom/complex1
        solvent_path: custom/solvent
"""
        )
        self.assertEqual(self.initialize(), 7)
        specs = {row["spec"]["id"]: row["spec"] for row in self.rows()}
        self.assertEqual(
            specs["test/19/complex/1"]["path"], str(self.source / "custom/complex1")
        )
        self.assertEqual(
            specs["test/19/vanilla/1"]["path"], str(self.source / "custom/vanilla1")
        )

    def test_legacy_json_has_identical_run_specs(self):
        legacy = self.work / "campaign.json"
        legacy.write_text(json.dumps(self.config))
        yaml_metadata, yaml_specs = registry_manifest.load_manifest(self.manifest)
        json_metadata, json_specs = registry_manifest.load_manifest(legacy)
        self.assertEqual(yaml_specs, json_specs)
        for key in ("name", "datasets", "source_root"):
            self.assertEqual(yaml_metadata[key], json_metadata[key])
        self.assertEqual(
            yaml_metadata["manifest_sha256"],
            hashlib.sha256(self.manifest.read_bytes()).hexdigest(),
        )

    def test_invalid_yaml_does_not_create_registry(self):
        for text in ("datasets: [", "!!python/object/apply:builtins.eval ['1 + 1']"):
            with self.subTest(text=text):
                self.manifest.write_text(text)
                with self.assertRaisesRegex(ValueError, "Invalid campaign YAML"):
                    self.initialize()
                self.assertFalse(self.registry.exists())

    def test_yaml_manifest_must_be_a_mapping(self):
        for text in ("", "plain text", "- item"):
            with self.subTest(text=text):
                self.manifest.write_text(text)
                with self.assertRaisesRegex(ValueError, "YAML mapping"):
                    self.initialize()
                self.assertFalse(self.registry.exists())

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
        monitoring_inventory.scan(self.registry)
        self.assertEqual(hashes(), before)
        self.assertFalse(
            any(path.suffix == ".sqlite" for path in self.source.rglob("*"))
        )

    def test_registry_cannot_be_in_source_tree(self):
        with self.assertRaisesRegex(ValueError, "outside"):
            registry_manifest.initialize(self.source / "registry.sqlite", self.manifest)

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

    def test_selected_campaign_manifest_has_expected_coverage(self):
        example = Path(__file__).parents[3] / "examples/membrane-campaign.yaml"
        metadata, specs = registry_manifest.load_manifest(example, self.source)
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
