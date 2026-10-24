import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import abfe.preparation.build as preparation_build
import abfe.preparation.compile as preparation_compile
import abfe.preparation.manifest as preparation_manifest
import abfe.preparation.run as preparation_run
import abfe.preparation.validate as preparation_validate
import abfe.registry.manifest as registry_manifest


class PreparationFixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.registry = self.root / "registry.sqlite"
        self.source = self.root / "campaign"
        self.source.mkdir()
        manifest = self.root / "campaign.json"
        manifest.write_text(
            json.dumps(
                dict(
                    schema_version=1,
                    name="test",
                    source_root=str(self.source),
                    datasets=[
                        dict(
                            id="test",
                            label="test",
                            directory="set",
                            expected_ligands=1,
                            ligands=["L1"],
                            complex_windows=preparation_manifest.COMPLEX_WINDOWS,
                            solvent_windows=preparation_manifest.SOLVENT_WINDOWS,
                        )
                    ],
                )
            )
        )
        registry_manifest.initialize(self.registry, manifest)
        self.output = self.root / "staged"
        inputs = self.root / "inputs"
        inputs.mkdir()
        self.ff = inputs / "amber99sb-star-ildn-mut.ff"
        self.ff.mkdir()
        for name in ("forcefield.itp", "tip3p.itp", "ions.itp"):
            (self.ff / name).write_text("; reviewed synthetic fixture\n")
        self.lipid = inputs / "lipid21"
        self.lipid.mkdir()
        for name in preparation_manifest.LIPID_FILES:
            (self.lipid / name).write_text("; synthetic lipid fixture\n")
        self.protein = inputs / "protein"
        self.protein.mkdir()
        topology = '#include "amber99sb-star-ildn-mut.ff/forcefield.itp"\n'
        (self.protein / "topol.top").write_text(topology)
        (self.protein / "protein.gro").write_text("synthetic protein coordinates")
        for name in ("L1.sdf", "sage.offxml", "waters.gro", "waterbox.gro"):
            (inputs / name).write_text("synthetic fixture only\n")
        (inputs / "solvent.top").write_text(topology)
        (inputs / "restraint.py").write_text("# synthetic fixture script\n")
        charmm = inputs / "charmm_gmx"
        charmm.mkdir()
        (charmm / "step5_input.gro").write_text("membrane input\n")
        (charmm / "topol.top").write_text("POPC 100\n")
        repeats = {}
        for r in (1, 2, 3):
            repeats[str(r)] = dict(
                vanilla=str(self.output / "L1" / f"vanilla_rep_{r}"),
                topology=str(self.output / "L1" / f"vanilla_rep_{r}" / "topol.top"),
            )
        self.config = dict(
            schema_version=1,
            compound="test/L1",
            repeats=repeats,
            protocol=dict(
                reviewed_by="fixture author",
                reference="test preparation",
                chemical_state="test molecule",
                waters_and_ions="test reference",
                charge_treatment="ABFlow co-annihilation",
                expected_charge=0,
                water_model="TIP3P",
                salt_molar=0.15,
                temperature_K=298.15,
                protein_forcefield=dict(
                    name="amber99sb-star-ildn-mut.ff",
                    version="fixture",
                    path=str(self.ff),
                ),
                ligand_forcefield=dict(
                    name="Sage", version="fixture", path=str(inputs / "sage.offxml")
                ),
                lipid_forcefield=dict(
                    name="Lipid21", version="fixture", path=str(self.lipid)
                ),
            ),
            inputs=dict(
                sdf=str(inputs / "L1.sdf"),
                protein=str(self.protein),
                charmm_folder=str(charmm),
                restraint_script=str(inputs / "restraint.py"),
                crystal_water_gro=str(inputs / "waters.gro"),
                solvent_box=str(inputs / "waterbox.gro"),
                solvent_topology=str(inputs / "solvent.top"),
                solvate_water_count=10,
                crystal_water_count=2,
            ),
        )
        self.manifest = self.root / "preparation.json"
        self.write_config()

    def write_config(self):
        self.manifest.write_text(json.dumps(self.config))

    def plan(self):
        return preparation_run.prepare(
            self.registry, "test/L1", self.manifest, self.output, "vanilla", True
        )

    def fake_vanilla(self, plan):
        for r in (1, 2, 3):
            path = self.output / "L1" / f"vanilla_rep_{r}"
            path.mkdir(parents=True)
            for name in ("system_solv_ions.gro", "topol.top", "index.ndx"):
                (path / name).write_text("synthetic output")
            (path / "prod.mdp").write_text(
                "dt=0.002\nnsteps=50000000\nref-t=298.15 298.15\n"
            )
        preparation_validate.validate_outputs(plan)

    def run_staging(self, generator=None):
        with (
            patch.object(
                preparation_build,
                "generate",
                side_effect=generator or self.fake_vanilla,
            ),
            patch.object(preparation_compile.subprocess, "run") as command,
        ):
            command.return_value.stdout = "test gmx"
            command.return_value.stderr = ""
            return preparation_run.prepare(
                self.registry, "test/L1", self.manifest, self.output, "vanilla"
            )

    def make_alchemical_inputs(self):
        from rdkit import Chem

        sdf = Path(self.config["inputs"]["sdf"])
        with Chem.SDWriter(str(sdf)) as writer:
            writer.write(Chem.MolFromSmiles("C"))
        for r in (1, 2, 3):
            path = self.root / "inputs" / f"vanilla{r}"
            path.mkdir()
            for name in ("prod.tpr", "npt.gro", "index.ndx"):
                (path / name).write_text("synthetic test fixture")
            (path / "prod.xtc").write_text(f"synthetic trajectory {r}")
            (path / "prod.mdp").write_text(
                "dt=0.002\nnsteps=50000000\nref-t=298.15 298.15\n"
            )
            shutil.copyfile(self.protein / "topol.top", path / "topol.top")
            shutil.copytree(self.ff, path / self.ff.name)
            (path / "toppar").mkdir()
            for name in ("L1.itp", "L1_atomtypes.itp", "L1_posre.itp"):
                (path / "toppar" / name).write_text("; shared ligand fixture\n")
            (path / "smirnoff").mkdir()
            shutil.copyfile(sdf, path / "smirnoff/L1.sdf")
            (path / "smirnoff/L1_mod.gro").write_text("synthetic ligand coordinates")
            self.config["repeats"][str(r)].update(
                vanilla=str(path), topology=str(path / "topol.top")
            )
        self.write_config()
