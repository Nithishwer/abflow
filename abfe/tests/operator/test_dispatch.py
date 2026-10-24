import subprocess
from unittest.mock import patch

import abfe.execution.journal as execution_journal
import abfe.execution.plan as execution_plan
import abfe.execution.transfer as execution_transfer
import abfe.execution.transport.dispatch as execution_transport_dispatch
import abfe.execution.transport.limits as execution_transport_limits
import abfe.operator.records as operator_records
import abfe.operator.select as operator_select
import abfe.registry.values as registry_values

from .fixtures import OperatorFixture


class DispatchTests(OperatorFixture):
    def test_plan_stage_submit_monitor_never_resubmits(self):
        scope_id = self.scope()
        planned = self.apply(scope_id)
        self.assertEqual(planned["operation"], "plan")
        action_id = planned["result"]["action_id"]

        def remote(plan, operation):
            if operation == "submit":
                return dict(job_id="42", state="submitted")
            if operation == "monitor":
                return dict(
                    state="active",
                    jobs=[dict(job_id="42", state="RUNNING")],
                    at=registry_values.now(),
                    submission=dict(job_id="42"),
                )
            return dict(state="staged")

        with (
            patch.object(
                execution_transport_dispatch, "remote", side_effect=remote
            ) as call,
            patch.object(execution_transfer, "rsync"),
        ):
            self.assertEqual(self.apply(scope_id)["operation"], "stage")
            self.assertEqual(self.apply(scope_id)["operation"], "submit")
            self.assertEqual(self.apply(scope_id)["operation"], "monitor")
            self.assertEqual(self.apply(scope_id)["state"], "waiting")
        self.assertEqual(sum(c.args[1] == "submit" for c in call.call_args_list), 1)
        self.assertEqual(
            execution_journal.load(self.registry, action_id)["submission"]["job_id"],
            "42",
        )

    def test_interrupted_plan_blocks_without_new_action(self):
        scope_id = self.scope()
        self.interrupt_terminal(scope_id)
        action_id = self.row()["executions"][0]["action_id"]
        with self.assertRaisesRegex(ValueError, "explicit review"):
            self.apply(scope_id)
        self.assertEqual(self.row()["executions"][0]["action_id"], action_id)
        self.assertEqual(len(self.row()["executions"]), 1)

    def test_interrupted_stage_blocks_without_copying(self):
        scope_id = self.scope()
        self.apply(scope_id)
        with (
            patch.object(
                execution_transport_dispatch,
                "remote",
                return_value=dict(state="staged"),
            ),
            patch.object(execution_transfer, "rsync"),
        ):
            self.interrupt_terminal(scope_id)
        with patch.object(
            execution_transfer, "rsync", side_effect=AssertionError("no recopy")
        ):
            with self.assertRaisesRegex(ValueError, "explicit review"):
                self.apply(scope_id)
        self.assertEqual(
            operator_select.overview(self.registry, scope_id)["runs"][0]["next_action"],
            None,
        )

    def test_interrupted_submit_blocks_without_remote_actions(self):
        plan = self.plan()
        execution_journal.save(self.registry, plan, "staged")
        scope_id = self.scope(action=plan)
        with patch.object(
            execution_transport_dispatch,
            "remote",
            return_value=dict(job_id="42", state="submitted"),
        ) as submit:
            self.interrupt_terminal(scope_id)
            self.assertEqual(submit.call_count, 1)
        observed = dict(
            state="active",
            jobs=[dict(job_id="42", state="RUNNING")],
            submission=dict(job_id="42"),
            at=registry_values.now(),
        )
        with patch.object(
            execution_transport_dispatch, "remote", return_value=observed
        ) as monitor:
            with self.assertRaisesRegex(ValueError, "explicit review"):
                self.apply(scope_id)
        monitor.assert_not_called()

    def test_lost_submission_response_pauses_and_never_retries(self):
        plan = self.plan()
        execution_journal.save(self.registry, plan, "staged")
        scope_id = self.scope(action=plan)
        with patch.object(
            execution_transport_dispatch,
            "remote",
            side_effect=subprocess.TimeoutExpired("ssh", 1),
        ):
            with self.assertRaises(ValueError):
                self.apply(scope_id)
        with patch.object(
            execution_transport_dispatch,
            "remote",
            side_effect=AssertionError("no resubmission"),
        ):
            self.assertEqual(self.apply(scope_id)["state"], "waiting")
        self.assertTrue(self.row()["incidents"])
        observed = dict(
            state="active",
            jobs=[dict(job_id="42", state="RUNNING")],
            submission=dict(job_id="42"),
            at=registry_values.now(),
        )
        with patch.object(
            execution_transport_dispatch, "remote", return_value=observed
        ):
            self.assertEqual(
                self.apply(scope_id, run_id=self.run_id, monitor=True)["operation"],
                "monitor",
            )
        self.assertTrue(self.row()["incidents"])

    def test_overlapping_operator_does_not_create_an_incident(self):
        scope_id = self.scope()
        with operator_records.lock(self.registry):
            with self.assertRaises(BlockingIOError):
                self.apply(scope_id)
        self.assertFalse(self.row()["incidents"])

    def test_resource_limit_counts_all_submitted_array_tasks(self):
        plan = self.plan()
        token = operator_records.CURRENT.set(dict(max_submitted={"ranvier": 2}))
        try:
            with self.assertRaisesRegex(ValueError, "ceiling reached"):
                execution_transport_limits.admission(plan, [{}, {}])
            plan["config"]["workload"] = "solvent"
            plan["windows"] = ["window"] * 32
            with self.assertRaisesRegex(ValueError, "ceiling reached"):
                execution_transport_limits.admission(plan, [])
        finally:
            operator_records.CURRENT.reset(token)

    def test_full_queue_waits_without_incident_or_submission_retry(self):
        plan = self.plan()
        execution_journal.save(self.registry, plan, "staged")
        scope_id = self.scope(action=plan)
        with patch.object(
            execution_transport_dispatch,
            "remote",
            side_effect=execution_transport_limits.AdmissionWait(
                "Operator submitted-job ceiling reached"
            ),
        ) as remote:
            result = self.apply(scope_id)
            self.assertEqual(result["state"], "waiting")
            self.assertEqual(self.apply(scope_id)["state"], "waiting")
        self.assertEqual(remote.call_count, 1)
        self.assertFalse(self.row()["incidents"])
        self.assertEqual(
            execution_journal.load(self.registry, plan["id"])["state"], "staged"
        )

    def test_unfinished_analysis_is_not_silently_restarted(self):
        scope_id = self.scope()
        marker = dict(
            id="interrupted",
            scope_id=scope_id,
            run_id=self.run_id,
            operation="plan",
            action_id=None,
            previous_analysis_id=None,
            max_submitted={"ranvier": 2},
        )
        operator_records.journal(self.registry, marker, "intent")
        with patch.object(
            execution_plan, "make_plan", side_effect=AssertionError("no replay")
        ):
            with self.assertRaisesRegex(ValueError, "explicit review"):
                self.apply(scope_id)
        self.assertTrue(self.row()["incidents"])
