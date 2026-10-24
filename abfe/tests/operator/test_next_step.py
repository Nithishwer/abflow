import io
import json
import sqlite3
from contextlib import closing, redirect_stderr, redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import abfe.commands.arguments as arguments
import abfe.commands.main as main
import abfe.execution.dispatch as dispatch
import abfe.execution.files as files
import abfe.execution.journal as execution_journal
import abfe.analysis.inputs as analysis_inputs
import abfe.analysis.fingerprints as fingerprints
import abfe.monitoring.inventory as inventory
import abfe.monitoring.inspect as inspect
import abfe.operator.advance as advance
import abfe.operator.next_evidence as evidence
import abfe.operator.next_execute as execute
import abfe.operator.next_plan as plan
import abfe.operator.next_select as select
import abfe.operator.records as records
import abfe.operator.sources as sources
import abfe.operator.view as view
import abfe.registry.read as registry_read
import abfe.registry.values as values
from abfe.tests.analysis.fixtures import AnalysisFixture

from .fixtures import OperatorFixture


def connected():
    return {
        cluster: dict(
            state="connected",
            checked_at=values.now(),
            scheduler=dict(state="observed", jobs=[]),
        )
        for cluster in ("ranvier", "soma", "archer")
    }


class NextStepSupport:
    def observations(self):
        with closing(sqlite3.connect(self.registry)) as db, db:
            db.execute(
                "INSERT OR REPLACE INTO metadata VALUES ('connections', ?)",
                (values.encode(connected()),),
            )
            for (raw,) in db.execute("SELECT spec FROM runs").fetchall():
                spec = json.loads(raw)
                observed = inspect.inspect_run(spec)
                observed["issues"] = []
                db.execute(
                    "UPDATE runs SET observation=?, observed_at=? WHERE id=?",
                    (values.encode(observed), values.now(), spec["id"]),
                )

    def collect(self, registry):
        metadata, rows = registry_read.read_registry(registry)
        return metadata, rows, dict(locations={"local": {"state": "checked"}})

    def saved(self):
        with patch.object(evidence, "collect", side_effect=self.collect):
            scope = plan.propose(self.registry)
        result = dict(plan_id=plan.save(self.registry, scope))
        view.publish(self.registry)
        return result

    def proposal(self, result):
        return records.load(self.registry, result["plan_id"])

    def entry(self, result, run_id):
        return records.entry_for(self.proposal(result), run_id)

    def preview_unchanged(self, operation):
        root = self.registry.parent
        before = files.snapshot(root)
        connect = sqlite3.connect

        def readonly_connection(database, *args, **kwargs):
            self.assertIn("mode=ro", str(database))
            return connect(database, *args, **kwargs)

        with (
            patch.object(sqlite3, "connect", side_effect=readonly_connection),
            patch.object(
                execute, "execute", side_effect=AssertionError("No execution")
            ),
            patch.object(view, "publish", side_effect=AssertionError("No publication")),
        ):
            result = execute.campaign(self.registry, dry_run=True)
        self.assertEqual(result["actions"].get(operation), 1)
        self.assertEqual(before, files.snapshot(root))

    def apply_next(self, result, run_id, operation):
        with patch(
            "abfe.monitoring.scheduler.check_connections", return_value=connected()
        ):
            return execute.execute(self.registry, result["plan_id"], run_id, operation)


class NextStepAnalysisTests(NextStepSupport, AnalysisFixture):
    def test_shared_helpers_are_part_of_frozen_code_identity(self):
        recorded = plan.code_identity()
        original_sha = files.sha
        for name in ("mdp.py", "monitoring/jobs.py"):
            shared = Path(plan.__file__).parents[1] / name
            with (
                self.subTest(name=name),
                patch.object(
                    files,
                    "sha",
                    side_effect=lambda path: (
                        "changed" if path == shared else original_sha(path)
                    ),
                ),
            ):
                self.assertNotEqual(recorded, plan.code_identity())

    def setUp(self):
        super().setUp()
        self.observations()

    def test_old_queue_observations_allow_local_analysis_and_vanilla_completion(self):
        metadata, rows = registry_read.read_registry(self.registry)
        metadata = dict(metadata, selection_at=values.now())
        for connection in metadata["connections"].values():
            connection["checked_at"] = "2000-01-01T00:00:00+00:00"
        complex_row = next(row for row in rows if row["spec"]["id"] == self.complex_id)
        entry = select.select(complex_row, metadata, {}, [], rows)
        self.assertEqual(entry["operation"], "analyse-dhdl")
        vanilla = next(row for row in rows if row["spec"]["leg"] == "vanilla")
        vanilla["observation"] = dict(production_ns=100)
        entry = select.select(vanilla, metadata, {}, [], rows)
        self.assertEqual(entry["state"], "complete")

    def test_missing_or_failed_queue_observations_still_block(self):
        metadata, rows = registry_read.read_registry(self.registry)
        row = next(row for row in rows if row["spec"]["leg"] == "vanilla")
        row["observation"] = dict(production_ns=100)
        for failure in ("missing", "unreachable", "scheduler_unavailable"):
            metadata["connections"] = connected()
            if failure == "missing":
                del metadata["connections"]["soma"]
            elif failure == "unreachable":
                metadata["connections"]["soma"]["state"] = "unreachable"
            else:
                metadata["connections"]["soma"]["scheduler"]["state"] = "unavailable"
            with self.subTest(failure=failure):
                entry = select.select(row, metadata, {}, [], rows)
                self.assertEqual(entry["state"], "blocked")
                self.assertEqual(entry["reason"], "Own-job evidence unavailable: soma")

    def test_default_registry_override_and_missing_registry_never_created(self):
        parser = arguments.parser()
        self.assertEqual(
            parser.parse_args(["next-step", "--dry-run"]).registry,
            arguments.DEFAULT_REGISTRY,
        )
        self.assertEqual(
            parser.parse_args(
                ["--registry", str(self.registry), "next-step", "--dry-run"]
            ).registry,
            self.registry,
        )
        missing = self.work / "missing.sqlite"
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            main.main(["--registry", str(missing), "next-step", "--dry-run"])
        self.assertFalse(missing.exists())

    def test_next_step_rejects_incomplete_or_mixed_modes_before_any_work(self):
        invalid = (
            ["analyse-dhdl"],
            ["analyse-dhdl", "--plan-id", "saved"],
            ["analyse-dhdl", "--run-id", self.complex_id],
            ["--plan-id", "saved", "--run-id", self.complex_id],
            ["--dry-run", "analyse-dhdl"],
            ["--dry-run", "--plan-id", "saved"],
            ["--dry-run", "--run-id", self.complex_id],
        )
        with (
            patch.object(execute, "campaign") as planning,
            patch.object(execute, "execute") as execution,
        ):
            for options in invalid:
                with (
                    self.subTest(options=options),
                    redirect_stderr(io.StringIO()),
                    self.assertRaises(SystemExit) as error,
                ):
                    main.main(["--registry", str(self.registry), "next-step", *options])
                self.assertEqual(error.exception.code, 2)
        planning.assert_not_called()
        execution.assert_not_called()

    def test_dry_run_is_readonly_and_has_the_same_selection_as_execution(self):
        before = files.snapshot(self.work)
        original_connect = sqlite3.connect

        def readonly_connection(database, *args, **kwargs):
            self.assertIn("mode=ro", str(database))
            return original_connect(database, *args, **kwargs)

        with (
            patch.object(sqlite3, "connect", side_effect=readonly_connection),
            patch.object(plan, "save", side_effect=AssertionError("No registration")),
            patch.object(view, "publish", side_effect=AssertionError("No publication")),
            patch.object(
                execute, "execute", side_effect=AssertionError("No execution")
            ),
            patch(
                "abfe.monitoring.update.refresh",
                side_effect=AssertionError("No refresh"),
            ),
            redirect_stdout(io.StringIO()) as output,
        ):
            main.main(["--registry", str(self.registry), "next-step", "--dry-run"])
        preview = json.loads(output.getvalue())
        self.assertEqual(preview["state"], "preview")
        self.assertEqual(preview["actions"], {"analyse-dhdl": 2})
        self.assertEqual(before, files.snapshot(self.work))
        with (
            patch(
                "abfe.monitoring.scheduler.check_connections", return_value=connected()
            ),
            patch("abfe.analysis.estimate.run_analysis", side_effect=self.fake_backend),
            redirect_stdout(io.StringIO()) as output,
        ):
            with self.assertRaises(SystemExit) as exit_status:
                main.main(["--registry", str(self.registry), "next-step"])
            self.assertEqual(exit_status.exception.code, 1)
        performed = json.loads(output.getvalue())
        self.assertEqual(performed["actions"], preview["actions"])
        self.assertEqual(performed["counts"], preview["counts"])
        self.assertEqual(
            {item["run_id"] for item in performed["outcomes"]},
            {self.complex_id, self.solvent_id},
        )
        self.assertEqual(performed["outcomes"][0]["state"], "succeeded")
        self.assertFalse((self.work / "next-steps").exists())

    def test_next_step_executes_saved_operation_without_dry_run(self):
        proposal = self.saved()
        with (
            patch(
                "abfe.monitoring.scheduler.check_connections", return_value=connected()
            ),
            patch("abfe.analysis.estimate.run_analysis", side_effect=self.fake_backend),
            redirect_stdout(io.StringIO()) as output,
        ):
            main.main(
                [
                    "--registry",
                    str(self.registry),
                    "next-step",
                    "analyse-dhdl",
                    "--plan-id",
                    proposal["plan_id"],
                    "--run-id",
                    self.complex_id,
                ]
            )
        self.assertEqual(json.loads(output.getvalue())["state"], "succeeded")

    def test_dry_run_invalid_evidence_reports_blocked_without_registering_incident(
        self,
    ):
        missing = Path(self.spec()["path"]) / "coul.00/prod/topol.tpr"
        missing.unlink()
        before = files.snapshot(self.work)
        result = execute.campaign(self.registry, dry_run=True)
        self.assertGreater(result["counts"]["blocked"], 0)
        self.assertEqual(before, files.snapshot(self.work))
        with patch.object(execute, "execute", return_value=dict(state="succeeded")):
            executed = execute.campaign(self.registry)
        self.assertEqual(executed["state"], "incomplete")
        _, _, row = advance.analysis_records.selected(self.registry, self.complex_id)
        self.assertIn("next-step-evidence", [item["code"] for item in row["incidents"]])

    def test_dry_run_reads_endpoints_without_full_validation_or_hashing(self):
        with (
            patch.object(
                analysis_inputs,
                "validate_inputs",
                side_effect=AssertionError("No full validation during planning"),
            ),
            patch.object(
                fingerprints,
                "fingerprint",
                side_effect=AssertionError("No full file hashes during planning"),
            ),
        ):
            result = self.saved()
        self.assertEqual(
            self.entry(result, self.complex_id)["analysis_evidence"]["cutoff_ps"], 20000
        )
        self.assertEqual(
            self.entry(result, self.solvent_id)["operation"], "analyse-dhdl"
        )

    def test_converged_analysis_is_not_rehashed_during_planning(self):
        self.analyse()
        with patch.object(
            fingerprints, "fingerprint", side_effect=AssertionError("No rehash")
        ):
            result = self.saved()
        self.assertEqual(self.entry(result, self.complex_id)["state"], "complete")

    def test_collect_reads_existing_refresh_without_writing_or_querying_clusters(self):
        before = files.snapshot(self.work)
        with patch("subprocess.run", side_effect=AssertionError("No remote refresh")):
            metadata, rows, coverage = evidence.collect(self.registry)
        self.assertEqual(len(rows), 7)
        self.assertIn("selection_at", metadata)
        self.assertEqual(coverage["connections"], metadata["connections"])
        self.assertEqual(before, files.snapshot(self.work))

    def test_shared_authorization_preserves_unchanged_input_symlink_identity(self):
        original = self.work / "input.sdf"
        original.write_text("reviewed input")
        alias = self.work / "input-link.sdf"
        alias.symlink_to(original)
        with closing(sqlite3.connect(self.registry)) as db, db:
            spec = self.spec()
            spec["input_files"] = [str(alias)]
            db.execute(
                "UPDATE runs SET spec=? WHERE id=?",
                (values.encode(spec), self.complex_id),
            )
        result = self.saved()
        with patch(
            "abfe.analysis.estimate.run_analysis", side_effect=self.fake_backend
        ):
            self.assertEqual(
                self.apply_next(result, self.complex_id, "analyse-dhdl")["state"],
                "succeeded",
            )

    def test_partial_refresh_leaves_unrelated_observations_and_campaign_time_alone(
        self,
    ):
        metadata, rows = registry_read.read_registry(self.registry)
        unrelated = next(
            row for row in rows if row["spec"]["id"] == "test/L1/complex/2"
        )
        with patch.object(inspect, "inspect_run", wraps=inspect.inspect_run) as observe:
            count = inventory.scan(self.registry, run_ids=[self.complex_id])
        self.assertEqual(count, 3)
        self.assertCountEqual(
            [call.args[0]["id"] for call in observe.call_args_list],
            [self.complex_id, self.solvent_id, "test/L1/vanilla/1"],
        )
        after_metadata, after_rows = registry_read.read_registry(self.registry)
        after = next(
            row for row in after_rows if row["spec"]["id"] == unrelated["spec"]["id"]
        )
        self.assertEqual(
            (after["observation"], after["observed_at"], after["history"]),
            (unrelated["observation"], unrelated["observed_at"], unrelated["history"]),
        )
        self.assertEqual(after_metadata["scanned_at"], metadata["scanned_at"])

    def test_shared_solvent_refresh_includes_its_three_dependent_repeats(self):
        with patch.object(inspect, "inspect_run", wraps=inspect.inspect_run) as observe:
            inventory.scan(self.registry, run_ids=[self.solvent_id])
        self.assertCountEqual(
            [call.args[0]["id"] for call in observe.call_args_list],
            [self.solvent_id] + [f"test/L1/complex/{repeat}" for repeat in (1, 2, 3)],
        )

    def test_wet_command_requests_only_affected_run_refresh(self):
        result = self.saved()
        with (
            patch("abfe.analysis.estimate.run_analysis", side_effect=self.fake_backend),
            patch.object(inventory, "scan", wraps=inventory.scan) as scan,
        ):
            self.apply_next(result, self.complex_id, "analyse-dhdl")
        scan.assert_called_once_with(self.registry, run_ids=[self.complex_id])

    def test_console_failure_before_execution_is_waiting_and_can_be_retried(self):
        result = self.saved()
        publish = view.publish
        with (
            patch(
                "abfe.analysis.estimate.run_analysis", side_effect=self.fake_backend
            ) as backend,
            patch.object(
                view, "publish", side_effect=[OSError("report unavailable"), None]
            ),
        ):
            with self.assertRaises(OSError):
                self.apply_next(result, self.complex_id, "analyse-dhdl")
        backend.assert_not_called()
        row = next(
            row
            for row in registry_read.read_registry(self.registry)[1]
            if row["spec"]["id"] == self.complex_id
        )
        self.assertEqual(row["next_step"]["outcome"], "waiting")
        self.assertFalse(row["incidents"])
        with (
            patch("abfe.analysis.estimate.run_analysis", side_effect=self.fake_backend),
            patch.object(view, "publish", wraps=publish),
        ):
            self.assertEqual(
                self.apply_next(result, self.complex_id, "analyse-dhdl")["state"],
                "succeeded",
            )

    def test_same_evidence_has_same_selected_entries(self):
        first, second = plan.propose(self.registry), plan.propose(self.registry)
        self.assertEqual(first["runs"], second["runs"])
        self.assertEqual(plan.summarize(first), plan.summarize(second))

    def test_analysis_records_result_updates_console_and_cannot_repeat(self):
        result = self.saved()
        with patch(
            "abfe.analysis.estimate.run_analysis", side_effect=self.fake_backend
        ) as backend:
            first = self.apply_next(result, self.complex_id, "analyse-dhdl")
            second = self.apply_next(result, self.complex_id, "analyse-dhdl")
        self.assertEqual(first["state"], "succeeded")
        self.assertEqual(second["state"], "already_completed")
        self.assertEqual(backend.call_count, 1)
        metadata, rows = registry_read.read_registry(self.registry)
        row = next(row for row in rows if row["spec"]["id"] == self.complex_id)
        self.assertEqual(row["next_step"]["outcome"], "succeeded")
        self.assertEqual(row["analysis_record"]["decision"]["action"], "stop_converged")
        self.assertEqual(
            len(row["analysis_record"]["evidence"]["windows"][0]["sha256"]), 64
        )
        self.assertIn("succeeded", (self.work / "campaign.html").read_text())

    def test_converged_local_run_is_left_alone(self):
        self.analyse()
        source = files.snapshot(self.source)
        result = self.saved()
        entry = self.entry(result, self.complex_id)
        self.assertEqual((entry["operation"], entry["state"]), (None, "complete"))
        self.assertEqual(source, files.snapshot(self.source))
        self.assertFalse((self.work / "next-steps").exists())

    def test_changed_inputs_block_saved_analysis_before_estimator(self):
        result = self.saved()
        with self.dhdl().open("a") as stream:
            stream.write("21000 0 0 0 0\n")
        with patch("abfe.analysis.estimate.run_analysis") as backend:
            with self.assertRaises(ValueError):
                self.apply_next(result, self.complex_id, "analyse-dhdl")
        backend.assert_not_called()
        row = next(
            row
            for row in registry_read.read_registry(self.registry)[1]
            if row["spec"]["id"] == self.complex_id
        )
        self.assertEqual(row["next_step"]["outcome"], "failed")
        self.assertTrue(row["incidents"])

    def test_wrong_operation_or_changed_plan_is_rejected(self):
        result = self.saved()
        with self.assertRaisesRegex(ValueError, "frozen operation"):
            self.apply_next(result, self.complex_id, "submit-jobs")
        with closing(sqlite3.connect(self.registry)) as db, db:
            scope = self.proposal(result)
            scope["runs"][0]["reason"] = "changed"
            db.execute(
                "UPDATE operator_scopes SET record=? WHERE id=?",
                (values.encode(scope), result["plan_id"]),
            )
        with self.assertRaisesRegex(ValueError, "fingerprint"):
            self.apply_next(result, self.complex_id, "analyse-dhdl")

    def test_analysis_does_not_extend_in_same_round(self):
        result = self.saved()
        with (
            patch(
                "abfe.analysis.estimate.run_analysis",
                side_effect=lambda evidence, output: self.fake_backend(
                    evidence, output, forward=1, backward=2
                ),
            ),
            patch.object(dispatch, "action") as action,
        ):
            self.apply_next(result, self.complex_id, "analyse-dhdl")
        action.assert_not_called()
        row = next(
            row
            for row in registry_read.read_registry(self.registry)[1]
            if row["spec"]["id"] == self.complex_id
        )
        self.assertEqual(row["analysis_record"]["decision"]["next_target_ns"], 25)

    def test_common_cutoff_uses_all_windows_and_exact_sample_grid(self):
        self.write_dhdl(self.dhdl(1), 1, end=18000)
        self.assertEqual(select.production_evidence(self.spec())["cutoff_ps"], 18000)
        self.write_dhdl(self.dhdl(1), 1, end=17000)
        self.assertEqual(select.production_evidence(self.spec())["cutoff_ps"], 17000)

    def test_common_cutoff_still_requires_an_actual_sample(self):
        self.write_dhdl(self.dhdl(1), 1, end=18000)
        self.dhdl().write_text(
            self.dhdl().read_text().replace("18000.0000 0 0 0 0\n", "")
        )
        with self.assertRaisesRegex(ValueError, "exact common cutoff"):
            select.production_evidence(self.spec())

    def test_tpr_metadata_change_blocks_saved_analysis(self):
        result = self.saved()
        self.dhdl().with_name("topol.tpr").write_bytes(b"changed input")
        with patch("abfe.analysis.estimate.run_analysis") as backend:
            with self.assertRaisesRegex(ValueError, "Analysis inputs changed"):
                self.apply_next(result, self.complex_id, "analyse-dhdl")
        backend.assert_not_called()

    def test_unknown_queue_or_open_incident_blocks_selection(self):
        metadata, rows, _ = self.collect(self.registry)
        metadata.update(registry=str(self.registry), registry_parent=str(self.work))
        row = next(row for row in rows if row["spec"]["id"] == self.complex_id)
        metadata["connections"]["archer"]["state"] = "unreachable"
        self.assertIn(
            "unavailable", select.select(row, metadata, {}, [], rows)["reason"]
        )
        row["incidents"] = [dict(code="scientific-review")]
        self.assertIn(
            "scientific-review", select.select(row, metadata, {}, [], rows)["reason"]
        )

    def test_selector_uses_snapshot_time_even_when_wall_clock_has_advanced(self):
        metadata, rows, _ = self.collect(self.registry)
        metadata.update(registry=str(self.registry), registry_parent=str(self.work))
        row = next(row for row in rows if row["spec"]["id"] == self.complex_id)
        first = select.select(row, metadata, {}, [], rows)
        with patch("abfe.monitoring.scheduler.datetime") as clock:
            clock.fromisoformat.side_effect = datetime.fromisoformat
            clock.now.return_value = datetime(2040, 1, 1, tzinfo=timezone.utc)
            second = select.select(row, metadata, {}, [], rows)
        self.assertEqual(first, second)

    def binding_proposal(self):
        complex_record, solvent_record = self.analyse(), self.analyse(self.solvent_id)
        compatibility = self.review(complex_record, solvent_record)
        with closing(sqlite3.connect(self.registry)) as db, db:
            db.execute(
                "INSERT INTO metadata VALUES ('next_step_inputs', ?)",
                (
                    values.encode(
                        {self.complex_id: {"compatibility": str(compatibility)}}
                    ),
                ),
            )
        return self.saved(), compatibility

    def test_current_converged_legs_compile_binding_once_from_recorded_review(self):
        result, compatibility = self.binding_proposal()
        self.assertEqual(
            self.entry(result, self.complex_id)["operation"], "calculate-affinity"
        )
        self.assertEqual(
            self.apply_next(result, self.complex_id, "calculate-affinity")["state"],
            "succeeded",
        )
        self.assertEqual(
            self.apply_next(result, self.complex_id, "calculate-affinity")["state"],
            "already_completed",
        )

    def test_changed_binding_review_blocks_compilation(self):
        result, compatibility = self.binding_proposal()
        compatibility.write_text(compatibility.read_text() + "\n")
        with self.assertRaisesRegex(ValueError, "Binding evidence changed"):
            self.apply_next(result, self.complex_id, "calculate-affinity")

    def test_next_dry_run_blocks_prior_interrupted_analysis(self):
        first = self.saved()
        journal = records.journal

        def interrupt(registry, context, state, **kwargs):
            if state == "succeeded":
                raise KeyboardInterrupt()
            return journal(registry, context, state, **kwargs)

        with (
            patch("abfe.analysis.estimate.run_analysis", side_effect=self.fake_backend),
            patch.object(records, "journal", side_effect=interrupt),
        ):
            with self.assertRaises(KeyboardInterrupt):
                self.apply_next(first, self.complex_id, "analyse-dhdl")
        before = files.snapshot(self.work)
        preview = execute.campaign(self.registry, dry_run=True)
        self.assertNotIn("reconcile", preview["actions"])
        self.assertEqual(before, files.snapshot(self.work))
        second = self.saved()
        entry = self.entry(second, self.complex_id)
        self.assertEqual((entry["operation"], entry["state"]), (None, "blocked"))
        self.assertIn("explicit review", entry["reason"])
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            arguments.parser().parse_args(["next-step", "reconcile"])

    def test_preparation_refuses_to_regenerate_existing_sibling_runs(self):
        metadata, rows, _ = self.collect(self.registry)
        metadata.update(registry=str(self.registry), registry_parent=str(self.work))
        row = next(row for row in rows if row["spec"]["id"] == "test/L1/complex/2")
        reviewed = dict(
            preparation=dict(
                compound="test/L1",
                manifest="/reviewed",
                output=str(self.work / "new-preparation"),
                stage="alchemical",
            )
        )
        prepared = dict(
            run_ids=[self.complex_id, row["spec"]["id"]],
            stage="alchemical",
            output=str(self.work / "new-preparation"),
        )
        with patch("abfe.preparation.manifest.load_plan", return_value=prepared):
            result = select.select(row, metadata, reviewed, [], rows)
        self.assertEqual(result["state"], "blocked")
        self.assertIn("existing work", result["reason"])

    def test_below_minimum_plans_continuation_and_eligible_failure_plans_extension(
        self,
    ):
        for number in (0, 1):
            self.write_dhdl(self.dhdl(number), number, end=15000)
        self.observations()
        metadata, rows, _ = self.collect(self.registry)
        metadata.update(registry=str(self.registry), registry_parent=str(self.work))
        row = next(row for row in rows if row["spec"]["id"] == self.complex_id)
        with patch.object(select, "configuration", return_value=({}, {}, {})) as config:
            result = select.select(row, metadata, {}, [], rows)
        self.assertEqual(result["operation"], "plan-submission")
        self.assertEqual(config.call_args.args[2], "continue")

        for number in (0, 1):
            self.write_dhdl(self.dhdl(number), number)
        self.analyse(
            backend=lambda evidence, output: self.fake_backend(
                evidence, output, forward=1, backward=1.5
            )
        )
        self.observations()
        metadata, rows, _ = self.collect(self.registry)
        metadata.update(registry=str(self.registry), registry_parent=str(self.work))
        row = next(row for row in rows if row["spec"]["id"] == self.complex_id)
        with patch.object(select, "configuration", return_value=({}, {}, {})) as config:
            result = select.select(row, metadata, {}, [], rows)
        self.assertEqual(config.call_args.args[2], "extend")
        self.assertEqual(result["decision"]["next_target_ns"], 25)

    def test_console_failure_after_success_does_not_repeat_analysis(self):
        result = self.saved()
        publish = view.publish
        calls = []

        def once(registry):
            calls.append(registry)
            if len(calls) == 2:
                raise OSError("report disk unavailable")
            return publish(registry)

        with (
            patch(
                "abfe.analysis.estimate.run_analysis", side_effect=self.fake_backend
            ) as backend,
            patch.object(view, "publish", side_effect=once),
        ):
            with self.assertRaisesRegex(ValueError, "Console/evidence"):
                self.apply_next(result, self.complex_id, "analyse-dhdl")
            self.assertEqual(
                self.apply_next(result, self.complex_id, "analyse-dhdl")["state"],
                "already_completed",
            )
        self.assertEqual(backend.call_count, 1)

    def test_interrupted_analysis_blocks_without_rerunning(self):
        result = self.saved()
        journal = records.journal

        def interrupt(registry, context, state, **kwargs):
            if state == "succeeded":
                raise KeyboardInterrupt()
            return journal(registry, context, state, **kwargs)

        with (
            patch(
                "abfe.analysis.estimate.run_analysis", side_effect=self.fake_backend
            ) as backend,
            patch.object(records, "journal", side_effect=interrupt),
        ):
            with self.assertRaises(KeyboardInterrupt):
                self.apply_next(result, self.complex_id, "analyse-dhdl")
        with patch(
            "abfe.analysis.estimate.run_analysis",
            side_effect=AssertionError("No rerun"),
        ):
            with self.assertRaisesRegex(ValueError, "explicit review"):
                self.apply_next(result, self.complex_id, "analyse-dhdl")
        self.assertEqual(backend.call_count, 1)

    def test_campaign_continues_independent_actions_after_failure(self):
        def perform(registry, scope_id, run_id, operation):
            if run_id == self.complex_id:
                raise ValueError("Synthetic failure")
            return dict(state="succeeded")

        with patch.object(execute, "execute", side_effect=perform) as performing:
            result = execute.campaign(self.registry)
        self.assertEqual(result["state"], "incomplete")
        self.assertEqual(performing.call_count, 2)
        self.assertEqual(
            [item["state"] for item in result["outcomes"]], ["failed", "succeeded"]
        )


class NextStepExecutionTests(NextStepSupport, OperatorFixture):
    def setUp(self):
        super().setUp()
        self.scope()
        self.observations()

    def test_plan_upload_submit_are_separate_rounds_and_submit_is_idempotent(self):
        initial = self.saved()
        self.assertEqual(
            self.entry(initial, self.run_id)["operation"], "plan-submission"
        )
        self.apply_next(initial, self.run_id, "plan-submission")
        upload = self.saved()
        self.assertEqual(self.entry(upload, self.run_id)["operation"], "upload-jobs")
        with (
            patch(
                "abfe.execution.transport.dispatch.remote",
                return_value=dict(state="staged"),
            ),
            patch("abfe.execution.transfer.rsync"),
        ):
            self.apply_next(upload, self.run_id, "upload-jobs")
        submit = self.saved()
        self.assertEqual(self.entry(submit, self.run_id)["operation"], "submit-jobs")
        with patch(
            "abfe.execution.transport.dispatch.remote",
            return_value=dict(state="submitted", job_id="123"),
        ) as remote:
            self.apply_next(submit, self.run_id, "submit-jobs")
            self.assertEqual(
                self.apply_next(submit, self.run_id, "submit-jobs")["state"],
                "already_completed",
            )
        self.assertEqual(remote.call_count, 1)

    def test_plan_upload_and_submit_previews_do_not_register_or_execute(self):
        self.preview_unchanged("plan-submission")
        initial = self.saved()
        self.apply_next(initial, self.run_id, "plan-submission")
        self.preview_unchanged("upload-jobs")
        upload = self.saved()
        with (
            patch(
                "abfe.execution.transport.dispatch.remote",
                return_value=dict(state="staged"),
            ),
            patch("abfe.execution.transfer.rsync"),
        ):
            self.apply_next(upload, self.run_id, "upload-jobs")
        self.preview_unchanged("submit-jobs")

    def test_proposal_cannot_be_executed_through_dynamic_batch_dispatch(self):
        result = self.saved()
        with self.assertRaisesRegex(ValueError, "explicit next-step"):
            advance.step(self.registry, result["plan_id"], apply=True)

    def test_named_next_step_cannot_execute_a_dynamic_legacy_scope(self):
        legacy = self.scope()
        with patch.object(advance, "perform") as perform:
            with self.assertRaisesRegex(ValueError, "next-step proposal"):
                execute.execute(self.registry, legacy, self.run_id, "analyse-dhdl")
        perform.assert_not_called()

    def test_latest_proposal_render_keeps_history_and_batch_views(self):
        legacy = [item["scope_id"] for item in self.row().get("operator", [])]
        legacy += [self.scope(operations=["plan"]), self.scope()]
        first, latest = self.saved(), self.saved()
        event = dict(
            id="previous",
            scope_id=first["plan_id"],
            run_id=self.run_id,
            operation="plan",
        )
        records.journal(self.registry, event, "waiting")
        with patch.object(plan, "command", wraps=plan.command) as command:
            row = self.row()
        command.assert_called_once()
        self.assertEqual(row["next_step"]["plan_id"], latest["plan_id"])
        self.assertEqual([item["scope_id"] for item in row["operator"]], legacy)
        self.assertEqual(row["operator_history"][0]["scope_id"], first["plan_id"])
        self.assertEqual(self.entry(first, self.run_id)["operation"], "plan-submission")
        records.journal(
            self.registry,
            dict(event, id="latest", scope_id=latest["plan_id"]),
            "waiting",
        )
        with closing(registry_read.readonly(self.registry)) as db:
            view.attach(db, {}, [row])
        self.assertEqual(row["next_step"]["outcome"], "waiting")
        self.assertEqual([item["scope_id"] for item in row["operator"]], legacy)

    def submission_proposal(self):
        initial = self.saved()
        self.apply_next(initial, self.run_id, "plan-submission")
        upload = self.saved()
        with (
            patch(
                "abfe.execution.transport.dispatch.remote",
                return_value=dict(state="staged"),
            ),
            patch("abfe.execution.transfer.rsync"),
        ):
            self.apply_next(upload, self.run_id, "upload-jobs")
        return self.saved()

    def test_admission_wait_is_recorded_without_incident(self):
        submit = self.submission_proposal()
        from abfe.execution.transport.limits import AdmissionWait

        with patch(
            "abfe.execution.transport.dispatch.remote",
            side_effect=AdmissionWait("Operator submitted-job ceiling reached"),
        ):
            outcome = self.apply_next(submit, self.run_id, "submit-jobs")
        self.assertEqual(outcome["state"], "waiting")
        self.assertFalse(self.row()["incidents"])

    def test_queue_match_is_waiting_and_has_no_command(self):
        metadata, rows, _ = self.collect(self.registry)
        metadata.update(registry=str(self.registry), registry_parent=str(self.root))
        row = next(row for row in rows if row["spec"]["id"] == self.run_id)
        execution = self.plan()
        row["executions"] = [
            dict(action_id=execution["id"], plan=execution, state="active")
        ]
        metadata["connections"]["ranvier"]["scheduler"]["jobs"] = [
            dict(job_id="123", state="RUNNING", work_dir=execution["remote"])
        ]
        result = select.select(row, metadata, {}, [], rows)
        self.assertEqual((result["state"], result["operation"]), ("waiting", None))

    def download_proposal(self):
        import shutil

        execution = self.plan()
        root = self.bundle(execution)
        execution_journal.save(self.registry, execution, "complete")
        remote = dict(
            state="complete", jobs=[], application=[], snapshot=files.snapshot(root)
        )

        def copy(plan_, source, destination, retrieval=False):
            shutil.copytree(root, destination, dirs_exist_ok=True)

        with patch("abfe.execution.transport.dispatch.remote", return_value=remote):
            download = self.saved()
        return execution, remote, copy, download

    def test_download_registers_verified_source_in_the_same_round(self):
        execution, remote, copy, download = self.download_proposal()
        original = files.snapshot(self.source)
        with (
            patch("abfe.execution.transport.dispatch.remote", return_value=remote),
            patch("abfe.execution.transfer.rsync", side_effect=copy) as transfer,
        ):
            self.preview_unchanged("download-jobs")
            self.assertEqual(
                self.entry(download, self.run_id)["operation"], "download-jobs"
            )
            self.apply_next(download, self.run_id, "download-jobs")
            history = self.row()["history"]
            self.assertEqual(
                self.apply_next(download, self.run_id, "download-jobs")["state"],
                "already_completed",
            )
        transfer.assert_called_once()
        self.assertNotEqual(Path(self.row()["spec"]["path"]), self.source)
        self.assertEqual(original, files.snapshot(self.source))
        self.assertEqual(self.row()["history"], history)
        self.assertNotIn("map-source", select.OPERATIONS)
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            arguments.parser().parse_args(["next-step", "map-source"])

    def test_existing_verified_download_finishes_without_remote_or_transfer(self):
        execution, target = self.retrieved()
        with (
            patch(
                "abfe.execution.transport.dispatch.remote",
                side_effect=AssertionError("No remote read"),
            ),
            patch(
                "abfe.execution.transfer.rsync",
                side_effect=AssertionError("No transfer"),
            ),
        ):
            self.preview_unchanged("download-jobs")
            download = self.saved()
            self.apply_next(download, self.run_id, "download-jobs")
            history = self.row()["history"]
            self.assertEqual(
                self.apply_next(download, self.run_id, "download-jobs")["state"],
                "already_completed",
            )
        self.assertEqual(Path(self.row()["spec"]["path"]), target / "data")
        self.assertEqual(self.row()["history"], history)

    def test_interrupted_download_requires_review_without_repeating_transfer(self):
        execution, remote, copy, download = self.download_proposal()
        with (
            patch("abfe.execution.transport.dispatch.remote", return_value=remote),
            patch("abfe.execution.transfer.rsync", side_effect=copy),
            patch.object(sources, "map_source", side_effect=KeyboardInterrupt()),
        ):
            with self.assertRaises(KeyboardInterrupt):
                self.apply_next(download, self.run_id, "download-jobs")
        self.assertEqual(Path(self.row()["spec"]["path"]), self.source)
        self.assertEqual(
            execution_journal.load(self.registry, execution["id"])["state"], "retrieved"
        )
        with (
            patch(
                "abfe.execution.transport.dispatch.remote",
                side_effect=AssertionError("No remote read"),
            ),
            patch(
                "abfe.execution.transfer.rsync",
                side_effect=AssertionError("No transfer"),
            ),
        ):
            with self.assertRaisesRegex(ValueError, "explicit review"):
                self.apply_next(download, self.run_id, "download-jobs")
        self.assertEqual(Path(self.row()["spec"]["path"]), self.source)
        self.assertEqual(
            execution_journal.load(self.registry, execution["id"])["state"], "retrieved"
        )

    def test_next_round_blocks_interrupted_download(self):
        execution, remote, copy, download = self.download_proposal()
        with (
            patch("abfe.execution.transport.dispatch.remote", return_value=remote),
            patch("abfe.execution.transfer.rsync", side_effect=copy),
            patch.object(sources, "map_source", side_effect=KeyboardInterrupt()),
        ):
            with self.assertRaises(KeyboardInterrupt):
                self.apply_next(download, self.run_id, "download-jobs")
        with (
            patch(
                "abfe.execution.transport.dispatch.remote",
                side_effect=AssertionError("No remote read"),
            ),
            patch(
                "abfe.execution.transfer.rsync",
                side_effect=AssertionError("No transfer"),
            ),
        ):
            following = self.saved()
            entry = self.entry(following, self.run_id)
            self.assertEqual((entry["operation"], entry["state"]), (None, "blocked"))
            self.assertIn("explicit review", entry["reason"])
        self.assertEqual(Path(self.row()["spec"]["path"]), self.source)

    def test_mapping_failure_pauses_and_authorized_recovery_reuses_download(self):
        from abfe.operator.incidents import incident_event

        execution, remote, copy, download = self.download_proposal()
        with (
            patch("abfe.execution.transport.dispatch.remote", return_value=remote),
            patch("abfe.execution.transfer.rsync", side_effect=copy),
            patch.object(
                sources, "map_source", side_effect=ValueError("Registration failed")
            ),
        ):
            with self.assertRaisesRegex(ValueError, "Registration failed"):
                self.apply_next(download, self.run_id, "download-jobs")
        current = execution_journal.load(self.registry, execution["id"])
        self.assertEqual(current["state"], "retrieved")
        self.assertEqual(Path(self.row()["spec"]["path"]), self.source)
        self.assertEqual(self.entry(self.saved(), self.run_id)["state"], "blocked")
        for incident in self.row()["incidents"]:
            incident_event(
                self.registry,
                self.run_id,
                incident["code"],
                "resolve",
                "Synthetic registration failure resolved",
                authorization="Synthetic explicit repair/resumption permission",
                evidence=[Path(current["transfer"]["output"]) / "retrieval.json"],
                allow_resume=True,
            )
        with (
            patch(
                "abfe.execution.transport.dispatch.remote",
                side_effect=AssertionError("No remote read"),
            ),
            patch(
                "abfe.execution.transfer.rsync",
                side_effect=AssertionError("No transfer"),
            ),
        ):
            recovered = self.saved()
            self.assertEqual(
                self.entry(recovered, self.run_id)["operation"], "download-jobs"
            )
            self.apply_next(recovered, self.run_id, "download-jobs")
        self.assertNotEqual(Path(self.row()["spec"]["path"]), self.source)

    def test_modified_existing_download_blocks_registration(self):
        execution, target = self.retrieved()
        download = self.saved()
        (target / "data/topol.top").write_text("modified after verification")
        with patch(
            "abfe.execution.transfer.rsync", side_effect=AssertionError("No transfer")
        ):
            with self.assertRaisesRegex(ValueError, "Retrieved files changed"):
                self.apply_next(download, self.run_id, "download-jobs")
        self.assertEqual(Path(self.row()["spec"]["path"]), self.source)
        self.assertTrue(self.row()["incidents"])

    def test_new_execution_after_dry_run_blocks_duplicate_plan_generation(self):
        result = self.saved()
        self.plan()
        with self.assertRaisesRegex(ValueError, "Execution inventory changed"):
            self.apply_next(result, self.run_id, "plan-submission")

    def test_interrupted_submission_blocks_without_remote_actions(self):
        submit = self.submission_proposal()
        journal = records.journal

        def interrupt(registry, context, state, **kwargs):
            if state == "succeeded":
                raise KeyboardInterrupt()
            return journal(registry, context, state, **kwargs)

        with (
            patch.object(records, "journal", side_effect=interrupt),
            patch(
                "abfe.execution.transport.dispatch.remote",
                return_value=dict(state="submitted", job_id="123"),
            ) as remote,
        ):
            with self.assertRaises(KeyboardInterrupt):
                self.apply_next(submit, self.run_id, "submit-jobs")
        self.assertEqual(remote.call_count, 1)
        with patch(
            "abfe.execution.transport.dispatch.remote",
            return_value=dict(
                state="active", jobs=[], at=values.now(), submission=dict(job_id="123")
            ),
        ) as monitor:
            with self.assertRaisesRegex(ValueError, "explicit review"):
                self.apply_next(submit, self.run_id, "submit-jobs")
        monitor.assert_not_called()

    def test_preparation_deduplication_keeps_all_runs_visible(self):
        metadata, rows, coverage = self.collect(self.registry)
        affected = [
            row["spec"]["id"] for row in rows if row["spec"]["leg"] == "vanilla"
        ]

        def shared(row, *args):
            run_id = row["spec"]["id"]
            return dict(
                run_id=run_id,
                spec=row["spec"],
                operation="prepare-input" if run_id in affected else None,
                state="ready" if run_id in affected else "blocked",
                reason="fixture",
                affected_runs=affected if run_id in affected else [run_id],
            )

        with (
            patch.object(evidence, "collect", return_value=(metadata, rows, coverage)),
            patch.object(select, "select", side_effect=shared),
            patch.object(plan, "freeze"),
        ):
            entries = plan.propose(self.registry)["runs"]
        self.assertEqual(len(entries), len(rows))
        self.assertEqual(
            sum(entry["operation"] == "prepare-input" for entry in entries), 1
        )
