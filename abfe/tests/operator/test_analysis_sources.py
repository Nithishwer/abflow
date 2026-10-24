import json
import shutil
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

import abfe.analysis.estimate as analysis_estimate
import abfe.analysis.provenance as analysis_provenance
import abfe.analysis.records as analysis_records
import abfe.analysis.run as analysis_run
import abfe.execution.files as files
import abfe.execution.journal as execution_journal
import abfe.operator.advance as operator_advance
import abfe.operator.plan as operator_plan
import abfe.registry.read as registry_read
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

    def test_new_source_supersedes_old_analysis_without_repairing_or_deleting_it(self):
        with patch.object(
            analysis_estimate, "run_analysis", side_effect=self.fake_backend
        ):
            previous = analysis_run.analyse(self.registry, self.complex_id, 20)
            scope_id, target, original_spec = self.retrieved_scope()
            self.assertEqual(
                operator_advance.step(self.registry, scope_id, True)["operation"],
                "map-source",
            )
            row = analysis_records.selected(self.registry, self.complex_id)[2]
            self.assertFalse(row["incidents"])
            self.assertEqual(row["analysis_record"]["state"], "superseded")
            self.assertEqual(row["analysis_record"]["supersedes"], previous["id"])
            self.assertEqual(
                operator_advance.step(self.registry, scope_id, True)["operation"],
                "analyse",
            )
            self.assertEqual(
                operator_advance.step(self.registry, scope_id, True)["result"][
                    "action"
                ],
                "stop_converged",
            )
        self.assertEqual(
            analysis_records.selected(self.registry, self.complex_id)[2]["spec"][
                "path"
            ],
            str(target / "data"),
        )
        analysis_provenance.verify(previous, original_spec, rehash=True)
        with closing(registry_read.readonly(self.registry)) as db:
            self.assertEqual(
                db.execute(
                    "SELECT count(*) FROM analyses WHERE run_id=?", (self.complex_id,)
                ).fetchone()[0],
                3,
            )

    def test_changed_mapped_snapshot_blocks_analysis_before_estimator(self):
        scope_id, target, _ = self.retrieved_scope()
        operator_advance.step(self.registry, scope_id, True)
        dhdl = target / "data/coul.00/prod/dhdl.xvg"
        dhdl.write_text(dhdl.read_text().replace("1000.0000 0", "1000.0000 1"))
        with patch.object(
            analysis_estimate,
            "run_analysis",
            side_effect=AssertionError("no estimator"),
        ):
            with self.assertRaisesRegex(ValueError, "Retrieved files changed"):
                operator_advance.step(self.registry, scope_id, True)
        self.assertTrue(
            analysis_records.selected(self.registry, self.complex_id)[2]["incidents"]
        )

    def test_local_source_keeps_its_immutable_receipt_after_later_monitoring(self):
        scope_id, _, _ = self.retrieved_scope()
        operator_advance.step(self.registry, scope_id, True)
        current = execution_journal.load(self.registry, "synthetic-retrieval")
        execution_journal.save(
            self.registry,
            current["plan"],
            "complete",
            observation=dict(state="complete", jobs=[]),
        )
        scope_id = (
            self.scope()
        )  # Explicit local analysis, independent of remote monitoring.
        with patch.object(
            analysis_estimate, "run_analysis", side_effect=self.fake_backend
        ):
            self.assertEqual(
                operator_advance.step(self.registry, scope_id, True)["operation"],
                "analyse",
            )
