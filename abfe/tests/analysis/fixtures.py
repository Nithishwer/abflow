import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import abfe.analysis.fingerprints as analysis_fingerprints
import abfe.analysis.run as analysis_run
import abfe.registry.manifest as registry_manifest
import abfe.registry.read as registry_read


class AnalysisFixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.work = Path(self.temp.name)
        self.source = self.work / "source"
        self.source.mkdir()
        self.registry = self.work / "registry.sqlite"
        self.manifest = self.work / "manifest.json"
        self.manifest.write_text(
            json.dumps(
                dict(
                    schema_version=1,
                    name="test",
                    source_root=str(self.source),
                    datasets=[
                        dict(
                            id="test",
                            label="Test",
                            expected_ligands=1,
                            directory="set",
                            ligands=["L1"],
                            complex_windows={"coul": 2},
                            solvent_windows={"coul": 2},
                        )
                    ],
                )
            )
        )
        registry_manifest.initialize(self.registry, self.manifest)
        self.complex_id = "test/L1/complex/1"
        self.solvent_id = "test/L1/solvent"
        for leg in ("complex", "ligand"):
            root = self.source / "set/L1/abfe_van1_hrex_r1" / leg
            for i in range(2):
                prod = root / f"coul.{i:02d}/prod"
                prod.mkdir(parents=True)
                (prod / f"coul.prod.{i:02d}.mdp").write_text(
                    f"dt = 0.002\nnsteps = 10000000\nnstdhdl = 500000\nref-t = 298.15\n"
                    f"init-lambda-state = {i}\ncoul-lambdas = 0 1\n"
                )
                (prod / "topol.tpr").write_bytes(b"test identity only")
                self.write_dhdl(prod / "dhdl.xvg", i)
        self.correction = (
            self.source / "set/L1/abfe_van1_hrex_r1/boresch/resgen_output.txt"
        )
        self.correction.parent.mkdir()
        self.correction.write_text("dG_off: -3.0, dG_on: 3.0\n")

    def write_dhdl(self, path, state, end=20000, step=1000):
        text = (
            '@ xaxis label "Time (ps)"\n'
            f'@ subtitle "T = 298.15 (K) state {state}: (coul-lambda) = ({state:.4f})"\n'
            '@ s0 legend "Total Energy (kJ/mol)"\n'
            '@ s1 legend "dH/dl coul-lambda"\n'
            '@ s2 legend "Delta H to (0.0000)"\n'
            '@ s3 legend "Delta H to (1.0000)"\n'
        )
        path.write_text(
            text + "".join(f"{t:.4f} 0 0 0 0\n" for t in range(0, end + 1, step))
        )

    def spec(self, run_id=None):
        return next(
            r["spec"]
            for r in registry_read.read_registry(self.registry)[1]
            if r["spec"]["id"] == (run_id or self.complex_id)
        )

    def dhdl(self, window=0, leg="complex"):
        return (
            self.source
            / f"set/L1/abfe_van1_hrex_r1/{leg}/coul.{window:02d}/prod/dhdl.xvg"
        )

    def fake_backend(self, evidence, output, forward=1.0, backward=1.1):
        (output / "ABFE_convergence.csv").write_text(
            f"Forward,Backward,data_fraction\n{forward},{backward},0.5\n"
        )
        (output / "ABFE_summary.csv").write_text("synthetic backend results\n")
        return dict(
            estimate_kcal_mol=10.0, uncertainty_kcal_mol=2.0, retained_samples=[20, 20]
        )

    def analyse(self, run_id=None, cutoff=20, backend=None):
        with patch(
            "abfe.analysis.estimate.run_analysis",
            side_effect=backend or self.fake_backend,
        ):
            return analysis_run.analyse(
                self.registry, run_id or self.complex_id, cutoff
            )

    def review(self, c, s):
        identity = dict(
            ligand_chemical_state="reviewed state",
            ligand_parameters="sha256:parameters",
            force_fields="historical protocol",
            solvent_conditions="reviewed conditions",
            temperature_K=298.15,
            charge_treatment="reviewed treatment",
        )
        path = self.work / "compatibility.json"
        path.write_text(
            json.dumps(
                dict(
                    schema_version=1,
                    reviewed_by="test reviewer",
                    complex_analysis_id=c["id"],
                    solvent_analysis_id=s["id"],
                    complex=identity,
                    solvent=dict(identity),
                    evidence=[analysis_fingerprints.fingerprint(self.correction)],
                )
            )
        )
        return path

    def historical_files(self):
        root = Path(self.spec()["path"]).parent / "analysis"
        directory = root / "complex"
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "ABFE_summary.csv").write_text(
            ",,MBAR,MBAR_Error\nStages,TOTAL,10.0,2.0\n"
        )
        (directory / "ABFE_convergence.csv").write_text(
            ",Forward,Backward,data_fraction\n0,0,3,.5\n"
        )
        (root / "abfe_results.csv").write_text("state,MBAR,MBAR_Error\ntotal,-3,4\n")
        return root
