import os
import shutil
import subprocess
from pathlib import Path
from unittest.mock import patch

import abfe.execution.files as worker
import abfe.execution.transport.dispatch as execution_transport_dispatch
import abfe.execution.transport.files as execution_transport_files
import abfe.execution.transport.observation as execution_transport_observation
import abfe.execution.transport.ssh as execution_transport_ssh
import abfe.monitoring.scheduler as monitoring_scheduler

from .fixtures import FAKE_GROMACS, ExecutionFixture


class TransportIntegrationTests(ExecutionFixture):
    def setUp(self):
        super().setUp()
        self.binary = self.root / "bin/gmx"
        self.binary.parent.mkdir()
        self.binary.write_text(FAKE_GROMACS)
        self.binary.chmod(0o700)
        self.config["gromacs"] = str(self.binary)
        fake = self.binary.parent / "slurm"
        fake.write_text("""#!/usr/bin/env python3
import os,sys
from pathlib import Path
name=Path(sys.argv[0]).name
if name=='scontrol':
    print('PartitionName=g-ran State=UP MaxTime=UNLIMITED' if sys.argv[2]=='partition' else 'JobId=42 JobState=COMPLETED ExitCode=0:0')
elif name=='sinfo': print('gpu:A40:4')
elif name=='sbatch' and '--test-only' not in sys.argv:
    if os.environ.get('FAKE_SUBMIT_FAIL'): raise SystemExit(2)
    print('42;testcluster')
""")
        fake.chmod(0o700)
        for name in ("squeue", "scontrol", "sinfo", "sbatch"):
            (fake.parent / name).symlink_to(fake)
        self.original_run = subprocess.run

        def local_ssh(args, **kwargs):
            env = dict(
                os.environ, PATH=str(self.binary.parent) + ":" + os.environ["PATH"]
            )
            return self.original_run(["bash", "-s"], env=env, **kwargs)

        self.ssh = patch.object(
            execution_transport_ssh.subprocess, "run", side_effect=local_ssh
        )
        self.ssh.start()
        self.addCleanup(self.ssh.stop)
        roots = patch.dict(
            monitoring_scheduler.CLUSTER_ROOTS, ranvier=str(self.root / "remote")
        )
        roots.start()
        self.addCleanup(roots.stop)
        (self.root / "remote").mkdir()

    def test_shell_transfer_checks_submit_monitor_and_hashes(self):
        # Generate before intercepting subprocess.run because bash -n is also tested elsewhere.
        plan = self.plan()
        root = Path(plan["remote"])
        self.assertEqual(
            execution_transport_dispatch.remote(plan, "reserve")["state"], "reserved"
        )
        shutil.copytree(self.source, root / "data", dirs_exist_ok=True)
        for name in ("plan.json", "job.sh"):
            shutil.copyfile(Path(plan["output"]) / name, root / name)
        self.assertEqual(
            execution_transport_dispatch.remote(plan, "verify")["state"], "staged"
        )
        self.assertEqual(
            execution_transport_dispatch.remote(plan, "submit")["job_id"], "42"
        )
        self.assertTrue((root / "submission.intent").exists())
        self.assertEqual(
            execution_transport_observation.observe(plan)["state"], "unknown"
        )
        result = self.original_run(
            ["bash", str(root / "job.sh")],
            env=dict(os.environ, SLURM_CPUS_PER_TASK="8"),
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(
            execution_transport_observation.observe(plan)["state"], "complete"
        )
        observed = execution_transport_dispatch.remote(plan, "retrieve")
        self.assertEqual(observed["snapshot"], worker.snapshot(root))
        # A changed checkpoint is detected even though the job still has a completion marker.
        (root / "data/prod.cpt").write_text("changed")
        with self.assertRaisesRegex(ValueError, "Remote command failed"):
            execution_transport_observation.observe(plan)

    def test_interrupted_submission_intent_never_dispatches_twice(self):
        plan = self.plan()
        root = Path(plan["remote"])
        execution_transport_dispatch.remote(plan, "reserve")
        shutil.copytree(self.source, root / "data", dirs_exist_ok=True)
        for name in ("plan.json", "job.sh"):
            shutil.copyfile(Path(plan["output"]) / name, root / name)
        with patch.dict(os.environ, FAKE_SUBMIT_FAIL="1"):
            with self.assertRaises(ValueError):
                execution_transport_dispatch.remote(plan, "submit")
        self.assertTrue((root / "submission.intent").exists())
        self.assertFalse((root / "submission.id").exists())
        with self.assertRaises(ValueError):
            execution_transport_dispatch.remote(plan, "submit")
        self.assertFalse((root / "submission.id").exists())

    def test_conflicting_or_symlinked_staging_is_not_repaired(self):
        plan = self.plan()
        root = Path(plan["remote"])
        execution_transport_dispatch.remote(plan, "reserve")
        (root / "data/topol.top").write_text("conflict")
        with self.assertRaisesRegex(ValueError, "Transfer conflict"):
            execution_transport_dispatch.remote(plan, "reserve")
        (root / "data/topol.top").unlink()
        (root / "data/link").symlink_to(self.source / "topol.top")
        with self.assertRaises(ValueError):
            execution_transport_files.snapshot(plan, "data")
