import tempfile
import unittest
from pathlib import Path

import yaml

import abfe.registry.manifest as registry_manifest
import abfe.registry.read as registry_read


class CampaignFixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.work = Path(self.temp.name)
        self.source = self.work / "source"
        self.source.mkdir()
        self.registry = self.work / "registry.sqlite"
        self.manifest = self.work / "campaign.yaml"
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
        self.manifest.write_text(yaml.safe_dump(self.config, sort_keys=False))

    def initialize(self):
        return registry_manifest.initialize(self.registry, self.manifest)

    def rows(self):
        return registry_read.read_registry(self.registry)[1]

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

    def managed_execution(self, action_id="uncertain", state="submission_uncertain"):
        return {
            "action_id": action_id,
            "run_id": "test/L1/complex/1",
            "state": state,
            "plan": {
                "id": action_id,
                "remote": "/home/reub0138/abflow/test/" + action_id,
                "output": str(self.work / action_id),
                "fingerprint": "fixture",
                "config": {
                    "cluster": "ranvier",
                    "workload": "complex-production",
                    "mode": "initial",
                },
            },
        }
