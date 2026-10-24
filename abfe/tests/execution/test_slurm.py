import json
import os
import shutil
import subprocess
from unittest.mock import patch

import abfe.analysis.run as analysis_run
import abfe.execution.files as worker
import abfe.execution.jobs.generate as execution_jobs_generate

from .fixtures import FAKE_GROMACS, ExecutionFixture


class NativeScriptTests(ExecutionFixture):
    def fake_engine(self):
        binary = self.root / "bin/gmx"
        binary.parent.mkdir()
        binary.write_text(FAKE_GROMACS)
        binary.chmod(0o700)
        self.config["gromacs"] = str(binary)
        return binary

    def launch(self, plan, root, task=None, **environment):
        plan = dict(plan, remote=str(root))
        script = execution_jobs_generate.script(plan)
        env = dict(
            os.environ,
            SLURM_CPUS_PER_TASK="8",
            FAKE_COMMAND_LOG=str(root / "commands.jsonl"),
            **environment,
        )
        if task is not None:
            env["SLURM_ARRAY_TASK_ID"] = str(task)
        result = subprocess.run(
            ["bash"], input=script, text=True, env=env, capture_output=True
        )
        if result.returncode:
            self.fail(result.stdout + "\n" + result.stderr)
        return script

    def test_native_vanilla_and_checkpoint_continuation(self):
        self.fake_engine()
        plan = self.plan()
        root = self.bundle(plan)
        self.launch(plan, root, FAKE_GMX_STOP_PS="16000")
        self.assertEqual((root / "status/0.state").read_text().strip(), "checkpointed")
        tpr = worker.sha(root / "data/prod.tpr")
        self.config.update(source=str(root / "data"), mode="continue")
        self.source = root / "data"
        self.register_source()
        continued = self.plan()
        continued_root = self.bundle(continued)
        self.launch(continued, continued_root)
        self.assertEqual(
            (continued_root / "status/0.state").read_text().strip(), "complete"
        )
        self.assertEqual(tpr, worker.sha(continued_root / "data/prod.tpr"))
        commands = [
            json.loads(x)
            for folder in (root, continued_root)
            for x in (folder / "commands.jsonl").read_text().splitlines()
        ]
        self.assertTrue(
            any("-cpi" in c and "prod.cpt" in c and "-append" in c for c in commands)
        )
        self.assertTrue(
            any(c[0] == "grompp" and "-t" in c and "npt.cpt" in c for c in commands)
        )
        (self.source / "prod.cpt").write_text("tampered")
        with self.assertRaisesRegex(ValueError, "checkpoint evidence"):
            self.plan()

    def test_native_failure_is_visible_and_stops_before_md(self):
        self.fake_engine()
        plan = self.plan()
        root = self.bundle(plan)
        result = subprocess.run(
            ["bash"],
            input=execution_jobs_generate.script(dict(plan, remote=str(root))),
            text=True,
            capture_output=True,
            env=dict(os.environ, SLURM_CPUS_PER_TASK="8", FAKE_FAIL="grompp"),
        )
        self.assertEqual(result.returncode, 9)
        self.assertIn("fake failure detail", result.stderr)
        self.assertEqual((root / "status/0.state").read_text().strip(), "failed 9")
        self.assertFalse((root / "data/em.gro").exists())

    def test_an_allocation_is_never_silently_retried(self):
        self.fake_engine()
        plan = self.plan()
        root = self.bundle(plan)
        script = self.launch(plan, root)
        before = worker.snapshot(root / "data")
        repeated = subprocess.run(
            ["bash"],
            input=script,
            text=True,
            capture_output=True,
            env=dict(os.environ, SLURM_CPUS_PER_TASK="8"),
        )
        self.assertNotEqual(repeated.returncode, 0)
        self.assertEqual(before, worker.snapshot(root / "data"))
        self.assertIn("#SBATCH --no-requeue", script)

    def test_solvent_windows_are_independent(self):
        self.windows("solvent")
        self.fake_engine()
        plan = self.plan()
        root = self.bundle(plan)
        script = self.launch(plan, root, task=0)
        self.assertNotIn("-hrex", script)
        self.assertTrue((root / "data/coul.00/prod/abflow-centered.gro").exists())
        self.assertFalse((root / "data/coul.01/prod/topol.tpr").exists())

    def test_gpu_equilibration_then_production_only_hrex(self):
        self.windows()
        binary = self.fake_engine()
        plan = self.plan()
        root = self.bundle(plan)
        # Generate and run each independent window exactly as the Slurm array would.
        for task in range(44):
            self.launch(plan, root, task=task)
        self.assertFalse(list((root / "data").glob("*/prod/topol.tpr")))
        self.config.update(
            source=str(root / "data"),
            workload="complex-production",
            cluster="archer",
            partition="standard",
            qos="standard",
            account="e280-biggin",
        )
        self.source = root / "data"
        self.register_source()
        # The production generator is tested with the fake binary; policy validation still rejects
        # external ARCHER executables in the controller tests.
        c = dict(self.config, gromacs=str(binary))
        cpu_root = self.root / "cpu-initial"
        cpu_root.mkdir()
        shutil.copytree(root / "data", cpu_root / "data")
        production = dict(plan, config=c, remote=str(cpu_root))
        srun = binary.parent / "srun"
        srun.write_text('#!/bin/bash\nshift\nexec "$@"\n')
        srun.chmod(0o700)
        self.launch(
            production, cpu_root, PATH=str(binary.parent) + ":" + os.environ["PATH"]
        )
        self.assertEqual(len(list((cpu_root / "data").glob("*/prod/topol.tpr"))), 44)
        self.assertEqual((cpu_root / "status/0.state").read_text().strip(), "complete")
        commands = [
            json.loads(x)
            for x in (cpu_root / "commands.jsonl").read_text().splitlines()
        ]
        hrex = [c for c in commands if "-hrex" in c]
        self.assertEqual(len(hrex), 1)
        self.assertNotIn("-cpi", hrex[0])
        production["config"] = dict(c, mode="continue", source=str(cpu_root / "data"))
        continued_root = self.root / "cpu-continue"
        continued_root.mkdir()
        shutil.copytree(cpu_root / "data", continued_root / "data")
        self.launch(
            production,
            continued_root,
            PATH=str(binary.parent) + ":" + os.environ["PATH"],
        )
        commands = [
            json.loads(x)
            for x in (continued_root / "commands.jsonl").read_text().splitlines()
        ]
        self.assertIn("-cpi", [c for c in commands if "-hrex" in c][-1])

    def test_extension_requires_analysis_and_preserves_all_previous_tprs(self):
        self.windows("solvent")
        binary = self.fake_engine()
        plan = self.plan()
        root = self.bundle(plan)
        run = root / "data"
        originals = {}
        for window in plan["windows"]:
            phase, number = window.split(".")
            directory = run / window / "prod"
            mdp = directory / f"{phase}.prod.{number}.mdp"
            (directory / "topol.tpr").write_text(mdp.read_text())
            (directory / "state.cpt").write_text("20000")
            (directory / "confout.gro").write_text("coordinates")
            (directory / "abflow-state.time").write_text("20000\n")
            (directory / "abflow-state.done").touch()

            def hashes(names):
                return "".join(
                    worker.sha(directory / name) + "  " + name + "\n" for name in names
                )

            (directory / "abflow-state.inputs.sha256").write_text(
                hashes(
                    [
                        "topol.tpr",
                        mdp.name,
                        "../../lig_" + phase + ".top",
                        "../../index.ndx",
                    ]
                )
            )
            (directory / "abflow-state.outputs.sha256").write_text(
                hashes(["topol.tpr", "state.cpt", "confout.gro", "abflow-state.time"])
            )
            originals[window] = worker.sha(directory / "topol.tpr")
        (run / "abflow-gromacs-ranvier.sha256").write_text(
            worker.sha(binary) + "  " + str(binary) + "\n"
        )
        self.source = run
        self.config.update(source=str(run), mode="extend")
        self.register_source()
        with patch.object(
            analysis_run,
            "current_decision",
            return_value=dict(action="stop_converged", reason="converged"),
        ):
            with self.assertRaisesRegex(ValueError, "No fresh eligible"):
                self.plan()
        with patch.object(
            analysis_run,
            "current_decision",
            return_value=dict(
                action="propose_extension",
                reason="extend",
                next_target_ns=25,
                analysis_id="verified",
            ),
        ):
            extension = self.plan()
        failed_root = self.root / "failed-extension"
        failed_root.mkdir()
        shutil.copytree(run, failed_root / "data")
        failed_script = execution_jobs_generate.script(
            dict(extension, remote=str(failed_root))
        )
        result = subprocess.run(
            ["bash"],
            input=failed_script,
            text=True,
            capture_output=True,
            env=dict(
                os.environ,
                SLURM_CPUS_PER_TASK="8",
                SLURM_ARRAY_TASK_ID="0",
                FAKE_FAIL="convert-tpr",
            ),
        )
        self.assertNotEqual(result.returncode, 0)
        for window in extension["windows"]:
            self.assertEqual(
                worker.sha(failed_root / "data" / window / "prod/topol.tpr"),
                originals[window],
            )
        # Another array task must stop rather than repair an interrupted extension.
        result = subprocess.run(
            ["bash"],
            input=failed_script,
            text=True,
            capture_output=True,
            env=dict(os.environ, SLURM_CPUS_PER_TASK="8", SLURM_ARRAY_TASK_ID="1"),
        )
        self.assertNotEqual(result.returncode, 0)
        self.launch(extension, root, task=0)
        for window in extension["windows"]:
            directory = run / window / "prod"
            self.assertEqual(
                worker.sha(directory / ("topol.tpr.before-" + extension["id"])),
                originals[window],
            )
            self.assertEqual(worker.duration(directory / "topol.tpr"), 25000)
        self.assertTrue((root / "extended").exists())
