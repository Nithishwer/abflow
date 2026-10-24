import shutil
from pathlib import Path
from unittest.mock import patch

import abfe.preparation.build as preparation_build
import abfe.preparation.compile as preparation_compile
import abfe.preparation.inputs as preparation_inputs
import abfe.preparation.manifest as preparation_manifest
import abfe.preparation.run as preparation_run
import abfe.preparation.validate as preparation_validate
from abfe import preparation as prep

from .fixtures import PreparationFixture


class GenerateTests(PreparationFixture):
    def test_vanilla_generation_uses_membrane_input_for_each_repeat(self):
        from unittest.mock import Mock

        from rdkit import Chem

        sdf = Path(self.config["inputs"]["sdf"])
        with Chem.SDWriter(str(sdf)) as writer:
            writer.write(Chem.MolFromSmiles("C"))
        calls = []

        def setup_factory(**kwargs):
            calls.append(kwargs)

            def setup_simulation(compound_name, repeat, output_path):
                path = Path(output_path)
                path.mkdir(parents=True)
                for name in ("system_solv_ions.gro", "topol.top", "index.ndx"):
                    (path / name).write_text("synthetic fixture output")
                (path / "prod.mdp").write_text(
                    "dt=0.002\nnsteps=50000000\nref-t=298.15 298.15\n"
                )
                template = (
                    Path(prep.__file__).parents[1]
                    / "utils/data/mdps/mdps_vanilla_memprot/nvt.mdp"
                )
                shutil.copyfile(template, path / "nvt.mdp")

            return Mock(setup_simulation=setup_simulation)

        with (
            patch(
                "abfe.preparation.vanilla.SimulationSetup", side_effect=setup_factory
            ),
            patch.object(preparation_compile, "compile_input"),
        ):
            preparation_build.generate(self.plan())
        self.assertEqual(len({call["charmm_folder"] for call in calls}), 1)

    def test_solvent_default_template_copies_the_required_forcefield(self):
        from abfe.preparation.directory import working_directory
        from abfe.preparation.solvent import LigandOnlySetup

        self.make_alchemical_inputs()
        helper = LigandOnlySetup(
            "L1",
            self.root / "solvent-test",
            vanilla_path=self.config["repeats"]["1"]["vanilla"],
            forcefield_path=self.ff,
            write_job_scripts=False,
        )
        with working_directory(self.root):
            helper._make_dirs()
            helper._copy_files()
            helper._replace_lig_name()
        text = (helper.lig_dir / "lig.top").read_text()
        for name in ("forcefield.itp", "tip3p.itp", "ions.itp"):
            self.assertIn("amber99sb-star-ildn-mut.ff/" + name, text)
        self.assertNotIn("amber99sb-ildn.ff/", text)
        self.assertEqual(
            preparation_inputs.snapshot(helper.lig_dir / self.ff.name)["files"],
            preparation_inputs.snapshot(self.ff)["files"],
        )

    def test_solvent_helper_rejects_other_forcefield_directory(self):
        from abfe.preparation.solvent import LigandOnlySetup

        helper = LigandOnlySetup(
            "L1",
            self.root / "solvent-test",
            forcefield_path=self.ff.parent / "amber99sb-ildn.ff",
        )
        with self.assertRaisesRegex(ValueError, "amber99sb-star-ildn-mut.ff"):
            helper._copy_files()
        self.assertFalse(helper.lig_dir.exists())

    def test_existing_abfe_setup_creates_three_not_nine(self):
        from abfe.preparation.complex import ABFESetup

        base = self.root / "legacy"
        for r in (1, 2, 3):
            (base / "L1" / f"vanilla_rep_{r}").mkdir(parents=True)
        extra = base / "L1/abfe_van1_hrex_r2"
        extra.mkdir()
        (extra / "historical").write_text("preserve")
        with patch.object(ABFESetup, "_setup_abfe_folder") as setup:
            ABFESetup(base, ["L1"], 3).setup()
        self.assertEqual(
            [call.args[1] for call in setup.call_args_list],
            ["van1_hrex_r1", "van2_hrex_r1", "van3_hrex_r1"],
        )
        self.assertEqual((extra / "historical").read_text(), "preserve")

    def test_real_templates_generate_44_complex_and_32_solvent_windows(self):
        from abfe.preparation.directory import working_directory
        from abfe.preparation.solvent import LigandOnlySetup
        from abfe.preparation.windows import (
            create_directories_with_MDP_pertuball_smaller_dumps,
        )

        complex_path = self.root / "complex"
        complex_path.mkdir()
        with working_directory(complex_path):
            for phase in preparation_manifest.COMPLEX_WINDOWS:
                create_directories_with_MDP_pertuball_smaller_dumps(
                    phase, "Protein_unk_PA_PC_OL", "Water_and_ions"
                )
        preparation_validate.validate_windows(
            complex_path, preparation_manifest.COMPLEX_WINDOWS, 298.15
        )
        lig = LigandOnlySetup(
            "L1", self.root / "abfe", forcefield_path=self.ff, write_job_scripts=False
        )
        lig.lig_dir.mkdir(parents=True)
        lig._write_mdps()
        preparation_validate.validate_windows(
            lig.lig_dir, preparation_manifest.SOLVENT_WINDOWS, 298.15
        )
        self.assertFalse((lig.lig_dir / "rest.00").exists())
        self.assertEqual(len(list(complex_path.glob("*/prod/*.mdp"))), 44)
        self.assertEqual(len(list(lig.lig_dir.glob("*/prod/*.mdp"))), 32)

    def test_alchemical_staging_uses_each_trajectory_and_one_solvent(self):
        from abfe.preparation.solvent import LigandOnlySetup

        self.make_alchemical_inputs()
        seen = []

        def restraints(vanilla_folder_name, abfe_path, **kwargs):
            seen.append(str(vanilla_folder_name))
            path = abfe_path / "boresch"
            path.mkdir()
            for name in ("ClosestRestraintFrame.gro", "BoreschRestraint.top"):
                (path / name).write_text("synthetic restraint fixture")
            (path / "resgen_output.txt").write_text("dG_off: -3.0, dG_on: 3.0\n")

        def complex_fep(*args, **kwargs):
            for phase in preparation_manifest.COMPLEX_WINDOWS:
                Path(f"complex_{phase}.top").write_text("synthetic topology")
                Path(f"complex_{phase}.gro").write_text("synthetic structure")

        def index(*args):
            for name in (
                "index.ndx",
                "index_rest.ndx",
                "index_coul.ndx",
                "index_vdw.ndx",
            ):
                Path(name).write_text("synthetic index")

        def solvent_fep(self):
            for phase in preparation_manifest.SOLVENT_WINDOWS:
                (self.lig_dir / f"lig_{phase}.top").write_text("synthetic topology")
                (self.lig_dir / f"lig_{phase}.gro").write_text("synthetic coordinates")

        with (
            patch("MDAnalysis.Universe") as universe,
            patch(
                "abfe.preparation.restraints.generate_boresch_restraints",
                side_effect=restraints,
            ),
            patch(
                "abfe.preparation.alchemical_system.create_fep_system",
                side_effect=complex_fep,
            ),
            patch("abfe.preparation.alchemical_system.create_index", side_effect=index),
            patch.object(LigandOnlySetup, "_run_gromacs_prep"),
            patch.object(LigandOnlySetup, "_create_fep_system", solvent_fep),
            patch.object(LigandOnlySetup, "_make_index", side_effect=index),
            patch.object(preparation_compile.subprocess, "run") as command,
        ):
            universe.return_value.trajectory.__enter__.return_value.__getitem__.return_value.time = 100000
            from types import SimpleNamespace

            def compiler(args, **kwargs):
                self.assertNotIn("mdrun", args)
                if "-o" in args:
                    Path(args[args.index("-o") + 1]).write_bytes(
                        b"synthetic compile result"
                    )
                return SimpleNamespace(stdout="test gmx", stderr="")

            command.side_effect = compiler
            result = preparation_run.prepare(
                self.registry, "test/L1", self.manifest, self.output, "alchemical"
            )
        self.assertEqual(result["state"], "prepared")
        self.assertEqual(
            seen, [self.config["repeats"][str(r)]["vanilla"] for r in (1, 2, 3)]
        )
        self.assertEqual(
            len(list((self.output / "L1").glob("*/complex/*/prod/*.mdp"))), 132
        )
        self.assertEqual(
            len(list((self.output / "L1").glob("*/ligand/*/prod/*.mdp"))), 32
        )
        self.assertEqual(len(list((self.output / "L1").glob("*/ligand"))), 1)
        self.assertFalse(list((self.output / "L1").rglob("*.sh")))
