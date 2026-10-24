import json
import shutil
import unittest
from pathlib import Path
from unittest.mock import patch

import abfe.analysis.estimate as analysis_estimate
import abfe.analysis.records as analysis_records
import abfe.execution.files as files
import abfe.execution.journal as execution_journal
import abfe.execution.plan as execution_plan
import abfe.operator.advance as operator_advance
import abfe.operator.permissions as operator_permissions
import abfe.operator.plan as operator_plan
import abfe.operator.records as operator_records
import abfe.registry.values as registry_values
from abfe.tests.analysis import fixtures as analysis_fixture


class OperatorAnalysisTests(unittest.TestCase):
    setUp = analysis_fixture.AnalysisFixture.setUp

    write_dhdl = analysis_fixture.AnalysisFixture.write_dhdl

    fake_backend = analysis_fixture.AnalysisFixture.fake_backend

    dhdl = analysis_fixture.AnalysisFixture.dhdl

    def scope(self):
        path = self.work / "scope.json"
        path.write_text(
            json.dumps(
                dict(
                    schema_version=1,
                    authorization="Synthetic analysis-only approval",
                    max_submitted={},
                    runs=[
                        dict(
                            run_id=self.complex_id,
                            operations=["analyse", "decide"],
                            cutoff_ns=20,
                        )
                    ],
                )
            )
        )
        return operator_plan.create_plan(self.registry, path)["scope_id"]

    def retrieved_scope(self):
        _, _, row = analysis_records.selected(self.registry, self.complex_id)
        original_spec = dict(row["spec"])
        directory = self.work / "execution-controls"
        directory.mkdir()
        config = dict(
            schema_version=1,
            run_id=self.complex_id,
            workload="complex-equilibration",
            mode="continue",
            source=original_spec["path"],
            relative_run=".",
            cluster="ranvier",
            partition="g-ran",
            gpu="A40",
            gromacs="/opt/gmx",
            gromacs_version="2023.1",
            walltime_hours=24,
            output_reserve_bytes=1024,
        )
        script = directory / "job.sh"
        script.write_text("true\n")
        plan = dict(
            id="synthetic-retrieval",
            run_id=self.complex_id,
            config=config,
            spec=original_spec,
            windows=["coul.00", "coul.01"],
            output=str(directory),
            remote="/home/reub0138/abflow/synthetic",
            executor="slurm-shell-v1",
            script_sha256=files.sha(script),
        )
        plan["fingerprint"] = files.digest(plan)
        (directory / "plan.json").write_text(json.dumps(plan))
        target = self.work / "retrieved"
        shutil.copytree(Path(original_spec["path"]), target / "data")
        for name in ("job.sh", "plan.json"):
            shutil.copyfile(directory / name, target / name)
        snapshot = files.snapshot(target)
        receipt = dict(
            action_id=plan["id"],
            run_id=self.complex_id,
            state="verified_local",
            snapshot=snapshot,
            output=str(target),
            source=plan["remote"],
            cluster="ranvier",
            application_state="complete",
            at=registry_values.now(),
        )
        files.write(target / "retrieval.json", receipt)
        files.write(
            target / ".retrieval-owner.json",
            dict(action_id=plan["id"], snapshot=snapshot),
        )
        execution_journal.save(
            self.registry,
            plan,
            "retrieved",
            transfer=receipt,
            observation=dict(state="complete", jobs=[]),
        )
        path = self.work / "retrieval-scope.json"
        path.write_text(
            json.dumps(
                dict(
                    schema_version=1,
                    authorization="Synthetic source mapping and analysis approval",
                    max_submitted={},
                    runs=[
                        dict(
                            run_id=self.complex_id,
                            action_id=plan["id"],
                            operations=["map-source", "analyse", "decide"],
                            cutoff_ns=20,
                        )
                    ],
                )
            )
        )
        return (
            operator_plan.create_plan(self.registry, path)["scope_id"],
            target,
            original_spec,
        )

    def test_analysis_then_decision_and_converged_stop(self):
        scope_id = self.scope()
        with patch.object(
            analysis_estimate, "run_analysis", side_effect=self.fake_backend
        ) as backend:
            first = operator_advance.step(self.registry, scope_id, True)
            second = operator_advance.step(self.registry, scope_id, True)
            third = operator_advance.step(self.registry, scope_id, True)
        self.assertEqual(first["operation"], "analyse")
        self.assertEqual(second["result"]["action"], "stop_converged")
        self.assertEqual(third["state"], "waiting")
        self.assertEqual(backend.call_count, 1)

    def test_valid_unconverged_proposes_extension_without_applying(self):
        scope_id = self.scope()
        with patch.object(
            analysis_estimate,
            "run_analysis",
            side_effect=lambda *args: self.fake_backend(*args, backward=2.0),
        ):
            operator_advance.step(self.registry, scope_id, True)
        result = operator_advance.step(self.registry, scope_id, True)
        self.assertEqual(result["result"]["action"], "propose_extension")
        self.assertEqual(result["result"]["next_target_ns"], 25)
        with patch.object(
            execution_plan,
            "make_plan",
            side_effect=AssertionError("no extension authority"),
        ):
            self.assertEqual(
                operator_advance.step(self.registry, scope_id, True)["state"], "waiting"
            )

    def test_stale_analysis_blocks_extension_and_reanalysis(self):
        scope_id = self.scope()
        with patch.object(
            analysis_estimate, "run_analysis", side_effect=self.fake_backend
        ):
            operator_advance.step(self.registry, scope_id, True)
        self.dhdl().write_text(self.dhdl().read_text() + "21000 0 0 0 0\n")
        with patch.object(
            analysis_estimate, "run_analysis", side_effect=AssertionError("no rerun")
        ):
            result = operator_advance.step(self.registry, scope_id, True)
        self.assertEqual(result["state"], "waiting")
        self.assertIn("stale_analysis", result["runs"][0]["blocked_reason"])

    def test_interrupted_analysis_blocks_without_estimators(self):
        scope_id = self.scope()
        original = operator_records.journal

        def stop(registry, record, state, **kwargs):
            if state == "succeeded":
                raise KeyboardInterrupt()
            return original(registry, record, state, **kwargs)

        with (
            patch.object(operator_records, "journal", side_effect=stop),
            patch.object(
                analysis_estimate, "run_analysis", side_effect=self.fake_backend
            ),
        ):
            with self.assertRaises(KeyboardInterrupt):
                operator_advance.step(self.registry, scope_id, True)
        with patch.object(
            analysis_estimate,
            "run_analysis",
            side_effect=AssertionError("no repeated analysis"),
        ):
            with self.assertRaisesRegex(ValueError, "explicit review"):
                operator_advance.step(self.registry, scope_id, True)

    def test_shared_authorization_check_rejects_unreviewed_cutoff(self):
        scope_id = self.scope()
        token = operator_records.CURRENT.set(
            dict(
                scope_id=scope_id,
                run_id=self.complex_id,
                id="test",
                operation="analyse",
            )
        )
        try:
            with self.assertRaisesRegex(ValueError, "cutoff differs"):
                operator_permissions.authorize(
                    self.registry, self.complex_id, "analyse", cutoff_ns=16
                )
        finally:
            operator_records.CURRENT.reset(token)

    def test_busy_analysis_waits_without_incident(self):
        import fcntl

        scope_id = self.scope()
        with (self.work / "analysis.lock").open("a") as stream:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            result = operator_advance.step(self.registry, scope_id, True)
        self.assertEqual(result["state"], "waiting")
        self.assertFalse(
            analysis_records.selected(self.registry, self.complex_id)[2]["incidents"]
        )
