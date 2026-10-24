import subprocess
from pathlib import Path
from unittest.mock import patch

import abfe.execution.dispatch as execution_dispatch
import abfe.execution.files as worker
import abfe.execution.transport.dispatch as execution_transport_dispatch
import abfe.execution.transport.files as execution_transport_files
import abfe.execution.transport.limits as execution_transport_limits
import abfe.execution.transport.ssh as execution_transport_ssh
import abfe.registry.read as registry_read

from .fixtures import ExecutionFixture


class ExecutionTests(ExecutionFixture):
    def test_plan_is_local_plain_gromacs_and_preserves_source(self):
        before = worker.snapshot(self.source)
        with patch.object(
            execution_transport_dispatch, "remote", side_effect=AssertionError("no SSH")
        ):
            plan = self.plan()
            self.assertFalse(
                execution_dispatch.action(self.registry, plan["id"], "stage")["apply"]
            )
        text = (Path(plan["output"]) / "job.sh").read_text()
        self.assertIn("--job-name=test-L1-vanilla-1", text)
        self.assertIn('"$GMX" grompp -f "prod.mdp"', text)
        self.assertIn('-t "npt.cpt"', text)
        self.assertIn("--gres=gpu:A40:1", text)
        for unwanted in ("python", "worker.py", "nvidia-smi", "base64"):
            self.assertNotIn(unwanted, text)
        self.assertFalse((Path(plan["output"]) / "worker.py").exists())
        self.assertEqual(before, worker.snapshot(self.source))
        subprocess.run(["bash", "-n", str(Path(plan["output"]) / "job.sh")], check=True)

    def test_partitions_and_warning_allowances_are_explicit(self):
        for cluster, partition, gpu in [
            ("soma", "gpu4-2080", "2080"),
            ("ranvier", "g-kha", "A40"),
            ("ranvier", "gpu-khalid", "A40"),
            ("ranvier", "g-ran", "L40S"),
        ]:
            self.config.update(cluster=cluster, partition=partition, gpu=gpu)
            with self.assertRaisesRegex(ValueError, "routine A40/L40S"):
                self.plan()
        self.config.update(
            cluster="ranvier", partition="g-ran", gpu="A40", maxwarn={"minim": 2}
        )
        with self.assertRaisesRegex(ValueError, "warning review"):
            self.plan()

    def test_changed_source_and_script_raise_incidents(self):
        plan = self.plan()
        (self.source / "topol.top").write_text("changed")
        with patch.object(
            execution_transport_dispatch,
            "remote",
            side_effect=AssertionError("must stop locally"),
        ):
            with self.assertRaisesRegex(ValueError, "Source snapshot changed"):
                execution_dispatch.action(self.registry, plan["id"], "stage", True)
        (Path(plan["output"]) / "job.sh").write_text("changed")
        with self.assertRaisesRegex(ValueError, "changed"):
            execution_dispatch.action(self.registry, plan["id"], "submit")
        row = next(
            r
            for r in registry_read.read_registry(self.registry)[1]
            if r["spec"]["id"] == self.run_id
        )
        self.assertTrue(row["incidents"])

    def test_remote_manifest_preserves_spaces_and_empty_directories(self):
        plan = self.plan()
        digest = "f" * 64
        output = "D\tempty\0F\t42\t" + digest + "  ./file with space\0"
        with patch.object(execution_transport_ssh, "execute", return_value=output):
            self.assertEqual(
                execution_transport_files.snapshot(plan),
                dict(files={"empty/": None, "file with space": digest}, bytes=42),
            )
        plan["remote"] = "/work/e280/shared/unsafe"
        with self.assertRaisesRegex(ValueError, "approved root"):
            execution_transport_ssh.execute(plan, "true")

    def test_gpu_and_archer_admission_limits_remain_checked(self):
        plan = self.plan()
        with patch.object(
            execution_transport_ssh,
            "command",
            side_effect=[
                "PartitionName=g-ran State=UP MaxTime=UNLIMITED",
                "gpu:RTX2080:4",
            ],
        ):
            with self.assertRaisesRegex(ValueError, "requested GPU"):
                execution_transport_limits.admission(plan, [])
        plan["config"].update(cluster="archer", partition="standard", qos="standard")
        with patch.object(
            execution_transport_ssh,
            "command",
            side_effect=[
                "PartitionName=standard State=UP MaxTime=1-00:00:00",
                "standard|16|64|",
            ],
        ):
            with self.assertRaisesRegex(ValueError, "ceiling"):
                execution_transport_limits.admission(
                    plan, [dict(qos="standard", state="PENDING")] * 64
                )
        with patch.object(
            execution_transport_ssh,
            "command",
            side_effect=[
                "Filesystem 1024-blocks Used Available Capacity Mounted\nx 1000 900 100 90% /work",
                "1506 project",
                "/work 100 0 200 - 1 0 0 -",
            ],
        ):
            with self.assertRaisesRegex(ValueError, "Insufficient"):
                execution_transport_limits.quota(plan)
