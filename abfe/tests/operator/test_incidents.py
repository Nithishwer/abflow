import json
import sqlite3

import abfe.analysis.incidents as analysis_incidents
import abfe.operator.incidents as operator_incidents
import abfe.operator.records as operator_records
import abfe.operator.select as operator_select
import abfe.registry.values as registry_values

from .fixtures import OperatorFixture


class IncidentsTests(OperatorFixture):
    def test_acknowledge_and_resolve_do_not_authorize_resume(self):
        analysis_incidents.incident(
            self.registry, self.run_id, "unexpected", "original failure"
        )
        scope_id = self.scope()
        proof = self.root / "review.txt"
        proof.write_text("user inspected the preserved evidence")
        operator_incidents.incident_event(
            self.registry, self.run_id, "unexpected", "acknowledge", "seen"
        )
        self.assertTrue(self.row()["incidents"])
        operator_incidents.incident_event(
            self.registry,
            self.run_id,
            "unexpected",
            "resolve",
            "fixed manually",
            "user approved this fix",
            [proof],
        )
        self.assertEqual(self.apply(scope_id)["state"], "waiting")
        operator_incidents.incident_event(
            self.registry,
            self.run_id,
            "unexpected",
            "resume",
            "resume this run",
            "explicit resume approval",
            [proof],
        )
        self.assertFalse(self.row()["incidents"])
        self.assertEqual(
            self.row()["incident_history"][0]["message"], "original failure"
        )
        self.assertEqual(
            operator_select.overview(self.registry, scope_id)["runs"][0]["next_action"],
            "plan",
        )
        operator_records.incident(
            self.registry, self.run_id, "unexpected", "new failure"
        )
        self.assertTrue(self.row()["incidents"])
        self.assertEqual(self.history()[-1]["operation"], "incident-reopened")

    def test_resolution_requires_authorization_and_unchanged_evidence(self):
        analysis_incidents.incident(
            self.registry, self.run_id, "unexpected", "original failure"
        )
        with self.assertRaisesRegex(ValueError, "authorization and evidence"):
            operator_incidents.incident_event(
                self.registry, self.run_id, "unexpected", "resolve", "fixed"
            )
        proof = self.root / "proof"
        proof.write_text("reviewed")
        operator_incidents.incident_event(
            self.registry,
            self.run_id,
            "unexpected",
            "resolve",
            "fixed manually",
            "approval",
            [proof],
        )
        proof.write_text("replaced")
        with self.assertRaisesRegex(ValueError, "evidence changed"):
            operator_incidents.incident_event(
                self.registry,
                self.run_id,
                "unexpected",
                "resume",
                "resume",
                "approval",
                [proof],
            )
        self.assertTrue(self.row()["incidents"])

    def test_blocked_run_does_not_stop_unaffected_work(self):
        other_id = "test/L1/vanilla/2"
        other_manifest = self.root / "other.json"
        other_manifest.write_text(json.dumps(dict(self.config, run_id=other_id)))
        with sqlite3.connect(self.registry) as db:
            spec = json.loads(
                db.execute("SELECT spec FROM runs WHERE id=?", (other_id,)).fetchone()[
                    0
                ]
            )
            spec["path"] = str(self.source)
            db.execute(
                "UPDATE runs SET spec=? WHERE id=?",
                (registry_values.encode(spec), other_id),
            )
        analysis_incidents.incident(
            self.registry, self.run_id, "unexpected", "needs review"
        )
        scope_id = self.scope(
            extra=dict(
                run_id=other_id, operations=["plan"], manifest=str(other_manifest)
            )
        )
        result = self.apply(scope_id)
        self.assertEqual(result["run_id"], other_id)
        self.assertEqual(result["operation"], "plan")
        self.assertTrue(self.row()["incidents"])
