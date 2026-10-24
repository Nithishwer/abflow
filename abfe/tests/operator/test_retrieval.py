import json
import shutil
from contextlib import closing
from unittest.mock import patch

import abfe.execution.files as files
import abfe.execution.journal as execution_journal
import abfe.execution.transfer as execution_transfer
import abfe.execution.transport.dispatch as execution_transport_dispatch
import abfe.registry.read as registry_read

from .fixtures import OperatorFixture


class RetrievalTests(OperatorFixture):
    def test_mapping_preserves_original_spec_files_and_history(self):
        before = files.snapshot(self.source)
        plan, target = self.retrieved()
        scope_id = self.scope(action=plan)
        original_spec = dict(self.row()["spec"])
        result = self.apply(scope_id)
        self.assertEqual(result["operation"], "map-source")
        self.assertEqual(
            self.row()["spec"], dict(original_spec, path=str(target / "data"))
        )
        self.assertEqual(before, files.snapshot(self.source))
        with closing(registry_read.readonly(self.registry)) as db:
            mapped = json.loads(
                db.execute(
                    "SELECT value FROM metadata WHERE key=?",
                    ("retrieved_source_mapping:" + plan["id"],),
                ).fetchone()[0]
            )
        self.assertEqual(mapped["previous_spec"], original_spec)
        self.assertEqual(len(self.row()["history"]), 1)
        self.assertEqual(self.apply(scope_id)["state"], "waiting")

    def test_mapping_rejects_modified_retrieval(self):
        plan, target = self.retrieved()
        scope_id = self.scope(action=plan)
        (target / "data/topol.top").write_text("corrupt transfer")
        original = dict(self.row()["spec"])
        with self.assertRaisesRegex(ValueError, "Retrieved files changed"):
            self.apply(scope_id)
        self.assertEqual(self.row()["spec"], original)
        self.assertTrue(self.row()["incidents"])

    def test_interrupted_retrieval_requires_resolution_before_resuming(self):
        plan = self.plan()
        execution_journal.save(self.registry, plan, "complete")
        scope_id = self.scope(action=plan)
        with (
            patch.object(
                execution_transport_dispatch,
                "remote",
                return_value=dict(
                    state="complete", jobs=[], snapshot=dict(files={}, bytes=0)
                ),
            ),
            patch.object(
                execution_transfer,
                "rsync",
                side_effect=ValueError("interrupted transfer"),
            ),
        ):
            with self.assertRaisesRegex(ValueError, "interrupted transfer"):
                self.apply(scope_id)
        with patch.object(
            execution_transfer, "rsync", side_effect=AssertionError("no silent retry")
        ):
            self.assertEqual(self.apply(scope_id)["state"], "waiting")

    def test_retrieval_and_mapping_interruption_blocks_without_repeating(self):
        plan = self.plan()
        root = self.bundle(plan)
        observed = dict(
            state="complete", jobs=[], application=[], snapshot=files.snapshot(root)
        )
        execution_journal.save(self.registry, plan, "complete")
        scope_id = self.scope(action=plan)

        def copy(plan, source, destination, retrieval=False):
            shutil.copytree(root, destination, dirs_exist_ok=True)

        with (
            patch.object(execution_transport_dispatch, "remote", return_value=observed),
            patch.object(execution_transfer, "rsync", side_effect=copy),
        ):
            self.interrupt_terminal(scope_id)
        with patch.object(
            execution_transfer,
            "rsync",
            side_effect=AssertionError("no repeated retrieval"),
        ):
            with self.assertRaisesRegex(ValueError, "explicit review"):
                self.apply(scope_id)
        self.assertNotEqual(self.row()["spec"]["path"], str(self.source))
        self.assertEqual(len(self.row()["history"]), 1)
