import subprocess
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import abfe.monitoring.scheduler as monitoring_scheduler
import abfe.registry.read as registry_read
import abfe.registry.values as registry_values

from .fixtures import CampaignFixture


class SchedulerTests(CampaignFixture):
    def test_connection_status_handles_old_failed_and_unchecked_probes(self):
        self.assertEqual(monitoring_scheduler.connection_state({}), "unknown")
        self.assertEqual(
            monitoring_scheduler.connection_state({"checked_at": "invalid"}), "unknown"
        )
        self.assertEqual(
            monitoring_scheduler.connection_state(
                {"state": "unreachable", "checked_at": registry_values.now()}
            ),
            "unreachable",
        )
        for offset in (-365, 1):
            checked = datetime.now(timezone.utc) + timedelta(days=offset)
            with self.subTest(checked=checked):
                self.assertEqual(
                    monitoring_scheduler.connection_state(
                        {"state": "connected", "checked_at": checked.isoformat()}
                    ),
                    "connected",
                )

    @patch("abfe.monitoring.scheduler.subprocess.run")
    def test_probe_uses_bounded_noninteractive_ssh_and_parses_own_jobs(self, run):
        run.side_effect = [
            subprocess.CompletedProcess([], 0, "ranvier\n", ""),
            subprocess.CompletedProcess(
                [],
                0,
                "1234|RUNNING|/home/reub0138/set/L1|normal|biggin|ran-01|reub0138\n",
                "",
            ),
            subprocess.CompletedProcess([], 1, "", "capacity unavailable"),
            subprocess.CompletedProcess([], 1, "", "ownership unavailable"),
        ]
        result = monitoring_scheduler.probe_cluster("ranvier")
        self.assertEqual(result["state"], "connected")
        self.assertEqual(result["scheduler"]["jobs"][0]["job_id"], "1234")
        self.assertTrue(
            all(call.kwargs["timeout"] == 15 for call in run.call_args_list)
        )
        self.assertIn("BatchMode=yes", run.call_args_list[0].args[0])
        self.assertTrue("--me" in run.call_args_list[1].args[0][-1])
        self.assertEqual(run.call_count, 2)
        self.assertNotIn("capacity", result)
        self.assertNotIn("ownership", result)

    @patch("abfe.monitoring.scheduler.subprocess.run")
    def test_scheduler_failure_does_not_mean_ssh_disconnected(self, run):
        run.side_effect = [
            subprocess.CompletedProcess([], 0, "soma\n", ""),
            subprocess.CompletedProcess([], 1, "", "scheduler unavailable"),
            subprocess.CompletedProcess([], 1, "", "capacity unavailable"),
            subprocess.CompletedProcess([], 1, "", "ownership unavailable"),
        ]
        result = monitoring_scheduler.probe_cluster("soma")
        self.assertEqual(result["state"], "connected")
        self.assertEqual(result["scheduler"]["state"], "unavailable")

    @patch("abfe.monitoring.scheduler.subprocess.run")
    def test_probe_timeout_is_not_success(self, run):
        run.side_effect = subprocess.TimeoutExpired("ssh", 15)
        self.assertEqual(
            monitoring_scheduler.probe_cluster("archer")["state"], "unreachable"
        )

    @patch("abfe.monitoring.scheduler.probe_cluster")
    def test_connection_check_persists_health_without_touching_run_history(self, probe):
        self.initialize()
        probe.return_value = {
            "state": "connected",
            "checked_at": registry_values.now(),
            "scheduler": {"state": "observed", "jobs": []},
        }
        monitoring_scheduler.check_connections(self.registry)
        metadata, rows = registry_read.read_registry(self.registry)
        self.assertEqual(set(metadata["connections"]), {"archer", "ranvier", "soma"})
        self.assertTrue(all(not row["history"] for row in rows))
