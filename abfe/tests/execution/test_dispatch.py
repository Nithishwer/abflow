import os
import shutil
import subprocess
from unittest.mock import patch

import abfe.execution.dispatch as execution_dispatch
import abfe.execution.files as worker
import abfe.execution.jobs.generate as execution_jobs_generate
import abfe.execution.journal as execution_journal
import abfe.execution.monitor as execution_monitor
import abfe.execution.plan as execution_plan
import abfe.execution.transfer as execution_transfer
import abfe.execution.transport.dispatch as execution_transport_dispatch
import abfe.execution.transport.files as execution_transport_files
import abfe.execution.transport.observation as execution_transport_observation
import abfe.execution.transport.queue as execution_transport_queue
import abfe.execution.transport.ssh as execution_transport_ssh
import abfe.registry.read as registry_read

from .fixtures import ExecutionFixture


class ExecutionTests(ExecutionFixture):
    def test_modules_use_login_shell_and_names_are_safe(self):
        self.config["modules"] = ["GROMACS/2023.1-foss-2022a-CUDA-11.7.0"]
        plan = self.plan()
        self.assertEqual(
            execution_jobs_generate.script(plan).splitlines()[0], "#!/bin/bash -l"
        )
        plan["run_id"] = "yuan/DOR_10/vanilla/1\n#SBATCH --exclusive"
        self.assertNotIn(
            "\n#SBATCH --exclusive\n", execution_jobs_generate.script(plan)
        )

    def test_successful_login_shell_logout_does_not_overwrite_completion(self):
        plan = self.plan()
        home = self.root / "home"
        home.mkdir()
        (home / ".bash_logout").write_text('if [ "$SHLVL" = 1 ]; then false; fi\n')
        for outcome, body, expected, code in (
            ("complete", "finish complete", "complete", 0),
            ("checkpointed", "finish checkpointed", "checkpointed", 0),
            ("failure", "false\nfinish complete", "failed 1", 1),
        ):
            with self.subTest(outcome=outcome):
                remote = self.root / outcome
                text = execution_jobs_generate.script(dict(plan, remote=str(remote)))
                prefix = text[
                    text.index("set -euo pipefail") : text.index('printf "running')
                ]
                job = self.root / (outcome + ".sh")
                job.write_text(prefix + body + "\n")
                result = subprocess.run(
                    ["bash", "--noprofile", "--login", str(job)],
                    env=dict(os.environ, HOME=str(home), SHLVL="0"),
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(result.returncode, code, result.stderr)
                self.assertEqual(
                    (remote / "status/0.state").read_text().strip(), expected
                )

    def test_transfer_and_submission_preserve_ids_and_never_duplicate(self):
        plan = self.plan()

        def remote(plan, op):
            return (
                dict(state="submitted", job_id="42")
                if op == "submit"
                else dict(state="staged")
            )

        with (
            patch.object(
                execution_transport_dispatch, "remote", side_effect=remote
            ) as call,
            patch.object(execution_transfer, "rsync") as copy,
        ):
            execution_dispatch.action(self.registry, plan["id"], "stage", True)
            self.assertEqual(
                copy.call_count, 3
            )  # data, plan and the shell script; no runner.
            result = execution_dispatch.action(
                self.registry, plan["id"], "submit", True
            )
            self.assertEqual(result["submission"]["job_id"], "42")
            with patch.object(
                execution_monitor, "action_monitor", return_value=dict(state="active")
            ):
                execution_dispatch.action(self.registry, plan["id"], "submit", True)
            self.assertEqual(sum(c.args[1] == "submit" for c in call.call_args_list), 1)

    def test_lost_submission_response_blocks_retry(self):
        plan = self.plan()
        execution_journal.save(self.registry, plan, "staged")
        with patch.object(
            execution_transport_dispatch,
            "remote",
            side_effect=subprocess.TimeoutExpired("ssh", 1),
        ):
            with self.assertRaises(ValueError):
                execution_dispatch.action(self.registry, plan["id"], "submit", True)
        with patch.object(
            execution_transport_dispatch,
            "remote",
            side_effect=AssertionError("no retry"),
        ):
            with self.assertRaisesRegex(ValueError, "Open incidents"):
                execution_dispatch.action(self.registry, plan["id"], "submit", True)

    def test_closed_failure_requires_resolution_evidence(self):
        plan = self.plan()
        for state in ("paused", "resolved"):
            execution_journal.save(self.registry, plan, state)
            with self.assertRaisesRegex(ValueError, "earlier execution"):
                self.plan()
        execution_journal.save(
            self.registry,
            plan,
            "resolved",
            resolution=dict(authorization="User approved replacement", state="FAILED"),
        )
        self.assertNotEqual(self.plan()["id"], plan["id"])
        execution_plan.recheck(plan)

    def test_external_topology_and_unrecorded_restart_are_rejected(self):
        outside = self.root / "outside.itp"
        outside.write_text("parameters")
        (self.source / "topol.top").write_text('#include "../outside.itp"\n')
        with self.assertRaisesRegex(ValueError, "Bundle all topology"):
            self.plan()
        (self.source / "topol.top").write_text("parameters")
        (self.source / "prod.cpt").write_text("legacy checkpoint")
        with self.assertRaisesRegex(ValueError, "continuation"):
            self.plan()
        self.config["mode"] = "continue"
        with self.assertRaisesRegex(ValueError, "recorded stage"):
            self.plan()

    def test_retrieval_verifies_snapshot_and_preserves_local_conflicts(self):
        plan = self.plan()
        root = self.bundle(plan)
        status = dict(
            state="checkpointed",
            jobs=[],
            application=[],
            snapshot=worker.snapshot(root),
        )
        target = self.root / "retrieved"

        def copying(plan, source, destination, retrieval=False):
            shutil.copytree(root, destination, dirs_exist_ok=True)

        with (
            patch.object(execution_transport_dispatch, "remote", return_value=status),
            patch.object(execution_transfer, "rsync", side_effect=copying),
        ):
            self.assertEqual(
                execution_dispatch.action(
                    self.registry, plan["id"], "retrieve", True, target
                )["state"],
                "retrieved",
            )
        (target / "data/topol.top").write_text("conflict")
        with (
            patch.object(execution_transport_dispatch, "remote", return_value=status),
            patch.object(
                execution_transfer, "rsync", side_effect=AssertionError("no overwrite")
            ),
        ):
            with self.assertRaisesRegex(ValueError, "Local retrieval conflict"):
                execution_dispatch.action(
                    self.registry, plan["id"], "retrieve", True, target
                )

    def test_ssh_uses_shell_and_preserves_error_stdout(self):
        plan = self.plan()
        result = subprocess.CompletedProcess(
            [], 1, stdout="NVML error on stdout", stderr="another diagnostic"
        )
        with patch.object(
            execution_transport_ssh.subprocess, "run", return_value=result
        ) as call:
            with self.assertRaisesRegex(ValueError, "NVML error on stdout"):
                execution_transport_ssh.execute(plan, "squeue --me")
        self.assertEqual(call.call_args.args[0][-1], "bash -s")
        self.assertNotIn("python", call.call_args.kwargs["input"])

    def test_remote_controls_and_data_conflicts_block_submission(self):
        plan = self.plan()
        with (
            patch.object(execution_transport_ssh, "execute"),
            patch.object(
                execution_transport_files,
                "snapshot",
                return_value=dict(files={"unexpected": "x"}),
            ),
        ):
            with self.assertRaisesRegex(ValueError, "Transfer conflict"):
                execution_transport_files.checks(plan, data=True)

    def test_empty_queue_does_not_establish_completion(self):
        plan = self.plan()
        with (
            patch.object(execution_transport_files, "checks"),
            patch.object(execution_transport_queue, "jobs", return_value=[]),
            patch.object(execution_transport_ssh, "execute", side_effect=["", "", ""]),
        ):
            self.assertEqual(
                execution_transport_observation.observe(plan)["state"], "unknown"
            )
        with patch.object(
            execution_transport_dispatch,
            "remote",
            return_value=dict(
                state="unknown", jobs=[], application=[], submission=dict(job_id="42")
            ),
        ):
            self.assertEqual(
                execution_dispatch.action(self.registry, plan["id"], "monitor")[
                    "state"
                ],
                "unknown",
            )
        row = next(
            r
            for r in registry_read.read_registry(self.registry)[1]
            if r["spec"]["id"] == self.run_id
        )
        self.assertTrue(row["incidents"])

    def test_job_name_is_not_used_for_matching(self):
        plan = self.plan()
        job = dict(name="same", work_dir=plan["remote"])
        other = dict(name="same", work_dir=plan["remote"] + "-other")
        self.assertEqual(execution_transport_queue.writers(plan, [job, other]), [job])
