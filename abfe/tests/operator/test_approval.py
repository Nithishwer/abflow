import json
from pathlib import Path
from unittest.mock import patch

import abfe.analysis.run as analysis_run
import abfe.execution.dispatch as execution_dispatch
import abfe.execution.files as files
import abfe.execution.journal as execution_journal
import abfe.execution.transfer as execution_transfer
import abfe.execution.transport.dispatch as execution_transport_dispatch
import abfe.operator.advance as operator_advance
import abfe.operator.plan as operator_plan

from .fixtures import OperatorFixture


class ApprovalTests(OperatorFixture):
    def test_scope_and_preview_never_operate_and_are_reusable(self):
        before = files.snapshot(self.source)
        with patch.object(
            execution_transport_dispatch,
            "remote",
            side_effect=AssertionError("no remote operation"),
        ):
            scope_id = self.scope()
            self.assertEqual(
                scope_id,
                operator_plan.create_plan(self.registry, self.root / "scope.json")[
                    "scope_id"
                ],
            )
            self.assertEqual(
                operator_advance.step(self.registry, scope_id)["runs"][0][
                    "next_action"
                ],
                "plan",
            )
            self.assertFalse(self.history())
        self.assertEqual(before, files.snapshot(self.source))
        html = (self.root / "preview.html").read_text()
        self.assertIn("Operator next action and blocked reason", html)
        self.assertIn(scope_id, html)
        self.assertEqual(
            (self.root / "campaign.html").read_bytes(),
            (self.root / "preview.html").read_bytes(),
        )

    def test_changed_reviewed_source_blocks_before_remote_or_plan(self):
        scope_id = self.scope()
        (self.source / "topol.top").write_text("unexpected replacement")
        with patch.object(
            execution_transport_dispatch,
            "remote",
            side_effect=AssertionError("no remote"),
        ):
            with self.assertRaisesRegex(ValueError, "manifest/source changed"):
                self.apply(scope_id)
        self.assertFalse(self.row().get("executions"))
        self.assertTrue(self.row()["incidents"])
        self.assertIn(
            "manifest/source changed", (self.root / "preview.html").read_text()
        )

    def test_scoped_execution_cannot_bypass_dispatcher(self):
        scope_id = self.scope()
        action_id = self.apply(scope_id)["result"]["action_id"]
        with patch.object(
            execution_transport_dispatch, "remote", side_effect=AssertionError("no SSH")
        ):
            with self.assertRaisesRegex(ValueError, "operator dispatcher"):
                execution_dispatch.action(self.registry, action_id, "stage", True)

    def test_new_explicit_scope_can_authorize_an_existing_scoped_action(self):
        initial_scope = self.scope(operations=["plan"])
        action_id = self.apply(initial_scope)["result"]["action_id"]
        plan = execution_journal.load(self.registry, action_id)["plan"]
        new_scope = self.scope(action=plan, operations=["stage"])
        with (
            patch.object(
                execution_transport_dispatch,
                "remote",
                return_value=dict(state="staged"),
            ),
            patch.object(execution_transfer, "rsync"),
        ):
            result = self.apply(new_scope)
        self.assertEqual(result["operation"], "stage")
        self.assertEqual(result["result"]["action_id"], action_id)
        self.assertEqual(len(self.row()["executions"]), 1)

    def test_scope_validation_and_tampering(self):
        scope_id = self.scope()
        path = self.root / "operator-scopes" / scope_id / "scope.json"
        scope = json.loads(path.read_text())
        scope["authorization"] = "changed"
        path.write_text(json.dumps(scope))
        with self.assertRaisesRegex(ValueError, "scope changed"):
            self.apply(scope_id)

    def test_explicit_continuation_permission_is_required(self):
        self.config["mode"] = "continue"
        (self.source / "abflow-gromacs-ranvier.sha256").write_text(
            "recorded executable"
        )
        (self.source / "abflow-test.inputs.sha256").write_text(
            files.sha(self.source / "topol.top") + "  topol.top\n"
        )
        with self.assertRaisesRegex(ValueError, "Explicitly permit continue"):
            self.scope()
        scope_id = self.scope(
            operations=["plan", "continue", "stage", "submit", "monitor"]
        )
        self.assertEqual(self.apply(scope_id)["operation"], "plan")

    def test_extension_requires_permission_and_rechecks_decision_before_staging(self):
        self.windows("solvent")
        self.config["mode"] = "extend"
        (self.source / "abflow-gromacs-ranvier.sha256").write_text(
            "recorded executable"
        )
        (self.source / "abflow-test.inputs.sha256").write_text(
            files.sha(self.source / "lig_coul.top") + "  lig_coul.top\n"
        )
        for prod in self.source.glob("*/prod"):
            (prod / "state.cpt").write_text("checkpoint fixture")
            (prod / "topol.tpr").write_text("TPR fixture")
            (prod / "abflow-state.outputs.sha256").write_text(
                files.sha(prod / "state.cpt") + "  state.cpt\n"
            )
        with self.assertRaisesRegex(ValueError, "Explicitly permit extend"):
            self.scope()
        scope_id = self.scope(
            operations=["plan", "extend", "stage", "submit", "monitor"]
        )
        decision = dict(
            action="propose_extension",
            analysis_id="validated-fixture",
            next_target_ns=25,
            reason="valid unconverged analysis",
        )
        with patch.object(analysis_run, "current_decision", return_value=decision):
            action_id = self.apply(scope_id)["result"]["action_id"]
        text = (
            Path(execution_journal.load(self.registry, action_id)["plan"]["output"])
            / "job.sh"
        ).read_text()
        self.assertIn("-until 25000", text)
        with (
            patch.object(
                analysis_run,
                "current_decision",
                return_value=dict(action="blocked", reason="stale analysis"),
            ),
            patch.object(execution_transport_dispatch, "remote") as remote,
        ):
            with self.assertRaisesRegex(ValueError, "Analysis decision changed"):
                self.apply(scope_id)
            remote.assert_not_called()
