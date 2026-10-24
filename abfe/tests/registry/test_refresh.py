import io
import sqlite3
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

import abfe
import abfe.commands.arguments as commands_arguments
import abfe.commands.main as commands_main
import abfe.console.runs as console_runs
import abfe.console.state as console_state
import abfe.monitoring.update as monitoring_update
import abfe.registry.read as registry_read
import abfe.registry.values as registry_values

from .fixtures import CampaignFixture


class RefreshTests(CampaignFixture):
    @patch("abfe.monitoring.scheduler.probe_cluster")
    def test_sampler_rejects_publishing_into_simulations(self, probe):
        self.initialize()
        with self.assertRaises(ValueError):
            monitoring_update.refresh(self.registry, self.source, remote=True)
        probe.assert_not_called()

    def test_supported_cli_is_small_and_old_commands_are_retired(self):
        commands = next(
            action
            for action in commands_arguments.parser()._actions
            if hasattr(action, "choices") and isinstance(action.choices, dict)
        )
        self.assertEqual(
            set(commands.choices),
            {
                "init",
                "refresh",
                "prepare",
                "next-step",
            },
        )
        self.assertFalse((Path(abfe.__file__).parent / "campaign.py").exists())
        for command in (
            "analyse",
            "batch",
            "incident",
            "dry-run",
            "status",
            "scan",
            "report",
            "show",
            "operator-step",
            "cluster-action",
            "sample-clusters",
        ):
            with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as exc:
                commands_main.main(["--registry", str(self.registry), command])
            self.assertEqual(exc.exception.code, 2)

    def test_local_refresh_never_queries_clusters(self):
        self.initialize()
        with (
            patch(
                "abfe.monitoring.scheduler.subprocess.run",
                side_effect=AssertionError("No SSH"),
            ),
            patch(
                "abfe.execution.transport.dispatch.remote",
                side_effect=AssertionError("No execution"),
            ),
        ):
            with redirect_stdout(io.StringIO()):
                commands_main.main(["--registry", str(self.registry), "refresh"])
        self.assertEqual(
            (self.work / "campaign.html").read_bytes(),
            (self.work / "preview.html").read_bytes(),
        )
        with sqlite3.connect(self.registry) as db:
            self.assertFalse(
                db.execute(
                    "SELECT 1 FROM sqlite_master WHERE name='cluster_samples'"
                ).fetchone()
            )

    def test_remote_refresh_reconciles_uncertain_submission_without_dispatch(self):
        self.initialize()
        plan = self.managed_execution()
        connection = {
            "state": "connected",
            "checked_at": registry_values.now(),
            "scheduler": {"state": "observed", "jobs": []},
        }
        with (
            patch("abfe.monitoring.scheduler.probe_cluster", return_value=connection),
            patch("abfe.execution.journal.records", return_value=[plan]),
            patch(
                "abfe.execution.dispatch.action", return_value={"state": "complete"}
            ) as action,
        ):
            result = monitoring_update.refresh(self.registry, remote=True)
        action.assert_called_once_with(self.registry, "uncertain", "monitor")
        self.assertEqual(result["state"], "refreshed")
        self.assertEqual(
            result["monitored"], [{"action_id": "uncertain", "state": "complete"}]
        )

    def test_remote_refresh_unreachable_cluster_stays_unknown_and_publishes(self):
        self.initialize()
        record = self.managed_execution()
        connection = {
            "state": "unreachable",
            "checked_at": registry_values.now(),
            "scheduler": {"state": "unknown", "jobs": []},
        }
        with (
            patch("abfe.monitoring.scheduler.probe_cluster", return_value=connection),
            patch("abfe.execution.journal.records", return_value=[record]),
            patch("abfe.execution.dispatch.action") as action,
        ):
            with (
                redirect_stdout(io.StringIO()),
                redirect_stderr(io.StringIO()),
                self.assertRaises(SystemExit) as exc,
            ):
                commands_main.main(
                    ["--registry", str(self.registry), "refresh", "--remote"]
                )
        self.assertEqual(exc.exception.code, 1)
        action.assert_not_called()
        self.assertTrue((self.work / "preview.html").is_file())
        self.assertTrue(
            all(
                console_state.presentation(row, metadata)["scheduler_state"]
                == "Unknown"
                for metadata, rows in [registry_read.read_registry(self.registry)]
                for row in rows
            )
        )

    def test_remote_refresh_continues_other_monitors_after_failure(self):
        self.initialize()
        records = [self.managed_execution(str(n), "active") for n in (1, 2)]
        connection = {
            "state": "connected",
            "checked_at": registry_values.now(),
            "scheduler": {"state": "observed", "jobs": []},
        }
        with (
            patch("abfe.monitoring.scheduler.probe_cluster", return_value=connection),
            patch("abfe.execution.journal.records", return_value=records),
            patch(
                "abfe.execution.dispatch.action",
                side_effect=[ValueError("Changed input"), {"state": "complete"}],
            ) as action,
        ):
            result = monitoring_update.refresh(self.registry, remote=True)
        self.assertEqual(action.call_count, 2)
        self.assertEqual(result["state"], "incomplete")
        self.assertEqual(result["monitored"], [{"action_id": "2", "state": "complete"}])
        self.assertTrue((self.work / "preview.html").is_file())

    def test_historical_telemetry_is_preserved_but_not_loaded_or_rendered(self):
        self.initialize()
        with sqlite3.connect(self.registry) as db:
            db.execute(
                "CREATE TABLE cluster_samples(slot INTEGER, cluster TEXT, record TEXT)"
            )
            db.execute(
                "INSERT INTO cluster_samples VALUES (1, 'ranvier', 'historical evidence')"
            )
        before = self.registry.read_bytes()
        metadata, rows = registry_read.read_registry(self.registry)
        report = console_runs.report_html(metadata, rows)
        self.assertNotIn("cluster_history", metadata)
        self.assertNotIn("view-clusters", report)
        self.assertNotIn("Cluster SSH connections", report)
        self.assertEqual(self.registry.read_bytes(), before)
