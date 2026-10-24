import json
import shutil
from contextlib import closing
from unittest.mock import patch

import abfe.analysis.records as analysis_records
import abfe.execution.dispatch as execution_dispatch
import abfe.execution.files as files
import abfe.execution.transfer as execution_transfer
import abfe.execution.transport.dispatch as execution_transport_dispatch
import abfe.operator.advance as operator_advance
import abfe.operator.plan as operator_plan
import abfe.operator.records as operator_records
import abfe.registry.read as registry_read
from abfe.tests.execution.fixtures import ExecutionFixture


class OperatorFixture(ExecutionFixture):
    def scope(self, operations=None, action=None, extra=None):
        entry = dict(
            run_id=self.run_id,
            operations=operations
            or ["plan", "stage", "submit", "monitor", "retrieve", "map-source"],
            retrieval_output=str(self.root / "retrieved"),
        )
        if action:
            entry["action_id"] = action["id"]
        else:
            self.manifest.write_text(json.dumps(self.config))
            entry["manifest"] = str(self.manifest)
        raw = dict(
            schema_version=1,
            authorization="Synthetic user authorization for this fixture only",
            max_submitted={"ranvier": 2},
            runs=[entry],
        )
        if extra:
            raw["runs"].append(extra)
        path = self.root / "scope.json"
        path.write_text(json.dumps(raw))
        return operator_plan.create_plan(self.registry, path)["scope_id"]

    def row(self):
        return analysis_records.selected(self.registry, self.run_id)[2]

    def history(self):
        with closing(registry_read.readonly(self.registry)) as db:
            return operator_records.events(db)

    def apply(self, scope_id, **kwargs):
        return operator_advance.step(self.registry, scope_id, apply=True, **kwargs)

    def interrupt_terminal(self, scope_id):
        original = operator_records.journal

        def journal(registry, record, state, **kwargs):
            if state == "succeeded":
                raise KeyboardInterrupt("operator stopped after underlying receipt")
            return original(registry, record, state, **kwargs)

        with patch.object(operator_records, "journal", side_effect=journal):
            with self.assertRaises(KeyboardInterrupt):
                self.apply(scope_id)

    def retrieved(self):
        plan = self.plan()
        root = self.bundle(plan)
        observed = dict(
            state="complete", jobs=[], application=[], snapshot=files.snapshot(root)
        )
        target = self.root / "retrieved"

        def copy(plan, source, destination, retrieval=False):
            shutil.copytree(root, destination, dirs_exist_ok=True)

        with (
            patch.object(execution_transport_dispatch, "remote", return_value=observed),
            patch.object(execution_transfer, "rsync", side_effect=copy),
        ):
            execution_dispatch.action(
                self.registry, plan["id"], "retrieve", True, target
            )
        return plan, target
