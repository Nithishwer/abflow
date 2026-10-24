import json
import sqlite3
from pathlib import Path
from unittest.mock import patch

import abfe.analysis.incidents as analysis_incidents
import abfe.analysis.policy as analysis_policy
import abfe.analysis.provenance as analysis_provenance
import abfe.analysis.records as analysis_records
import abfe.analysis.run as analysis_run
import abfe.analysis.values as analysis_values
import abfe.console.runs as console_runs
import abfe.console.state as console_state
import abfe.console.tiles as console_tiles
import abfe.monitoring.inventory as monitoring_inventory
import abfe.registry.read as registry_read
from abfe.analysis import identity

from .fixtures import AnalysisFixture


class FreshnessTests(AnalysisFixture):
    def test_input_or_output_change_blocks_decision_and_console(self):
        record = self.analyse()
        self.dhdl().write_text(
            self.dhdl().read_text().replace("1000.0000 0", "1000.0000 1")
        )
        self.assertEqual(
            analysis_run.current_decision(self.registry, self.complex_id)["action"],
            "blocked",
        )
        metadata, rows = registry_read.read_registry(self.registry)
        row = next(r for r in rows if r["spec"]["id"] == self.complex_id)
        self.assertEqual(row["observation"]["analysis"]["state"], "stale")
        self.assertEqual(
            console_state.presentation(row, metadata)["remarks"], "Attention required"
        )
        self.assertIn("Analysis and decision", console_runs.report_html(metadata, rows))

    def test_output_tampering_and_protocol_change_block_decisions(self):
        record = self.analyse()
        out = Path(record["output"]) / "ABFE_summary.csv"
        out.write_text("changed result\n")
        self.assertEqual(
            analysis_run.current_decision(self.registry, self.complex_id)["action"],
            "blocked",
        )
        p = dict(analysis_policy.protocol(), increment_ps=10000)
        with (
            patch("abfe.analysis.policy.protocol", return_value=p),
            self.assertRaisesRegex(ValueError, "protocol changed"),
        ):
            analysis_provenance.verify(record, self.spec())

    def test_scan_preserves_records_and_keeps_incidents_open(self):
        record = self.analyse()
        analysis_incidents.incident(
            self.registry,
            self.complex_id,
            "restraint_provenance",
            "Needs explicit review",
        )
        monitoring_inventory.scan(self.registry)
        _, rows = registry_read.read_registry(self.registry)
        row = next(r for r in rows if r["spec"]["id"] == self.complex_id)
        self.assertEqual(row["analysis_record"]["id"], record["id"])
        self.assertEqual(row["observation"]["analysis"]["analysed_ns"], 20)
        self.assertTrue(row["incidents"])
        self.assertEqual(
            analysis_run.current_decision(self.registry, self.complex_id)["action"],
            "blocked",
        )

    def test_user_review_accepts_only_recorded_code_mismatch_without_revalidating_analysis(
        self,
    ):
        record = self.analyse()
        altered = dict(
            record,
            software=dict(record["software"], analysis_code_sha256="previous-code"),
        )
        altered["id"] = "reviewed-old-code"
        output = self.work / "old-code"
        output.mkdir()
        altered["output"] = str(output)
        analysis_records.persist(self.registry, altered, output)
        proof = dict(
            authorization="User accepts existing converged result",
            analysis_id=altered["id"],
            spec_sha256=analysis_values.digest(self.spec()),
            analysis_code_sha256=identity.current(),
        )
        with sqlite3.connect(self.registry) as db:
            db.execute(
                "INSERT INTO metadata VALUES (?,?)",
                ("accepted_analysis_reviews", json.dumps({self.complex_id: proof})),
            )
        metadata, rows = registry_read.read_registry(self.registry)
        row = next(r for r in rows if r["spec"]["id"] == self.complex_id)
        self.assertEqual(row["observation"]["analysis"]["state"], "stale")
        view = console_state.presentation(row, metadata)
        self.assertEqual(
            (
                view["stage_label"],
                view["convergence"],
                console_tiles.compound_status(row, view),
            ),
            ("Complete (user accepted)", "converged (user accepted)", "complete"),
        )
        self.assertEqual(
            analysis_run.current_decision(self.registry, self.complex_id)["action"],
            "blocked",
        )
        row["incidents"].append(
            dict(code="other-failure", message="Review", status="open")
        )
        self.assertEqual(
            console_tiles.compound_status(
                row, console_state.presentation(row, metadata)
            ),
            "attention",
        )
        for key, value in [
            ("authorization", ""),
            ("analysis_id", "other"),
            ("analysis_code_sha256", "other"),
            ("spec_sha256", "other"),
        ]:
            with sqlite3.connect(self.registry) as db:
                db.execute(
                    "UPDATE metadata SET value=? WHERE key=?",
                    (
                        json.dumps({self.complex_id: dict(proof, **{key: value})}),
                        "accepted_analysis_reviews",
                    ),
                )
            row = next(
                r
                for r in registry_read.read_registry(self.registry)[1]
                if r["spec"]["id"] == self.complex_id
            )
            self.assertNotIn("accepted_analysis_review", row)
        with sqlite3.connect(self.registry) as db:
            db.execute(
                "UPDATE metadata SET value=? WHERE key=?",
                (json.dumps({self.complex_id: proof}), "accepted_analysis_reviews"),
            )
        Path(altered["evidence"]["windows"][0]["source_path"]).write_text(
            "Changed source"
        )
        row = next(
            r
            for r in registry_read.read_registry(self.registry)[1]
            if r["spec"]["id"] == self.complex_id
        )
        self.assertNotIn("accepted_analysis_review", row)
