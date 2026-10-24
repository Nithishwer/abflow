from datetime import datetime, timedelta, timezone

import abfe.console.runs as console_runs
import abfe.console.state as console_state
import abfe.registry.read as registry_read
import abfe.registry.values as registry_values
from abfe.monitoring.jobs import matching_jobs

from .fixtures import CampaignFixture


class SchedulerTests(CampaignFixture):
    def test_monitoring_matches_keep_queue_order_and_window_directory_rules(self):
        at = datetime(2026, 10, 23, tzinfo=timezone.utc)
        remote = "/home/reub0138/set/run"
        row = dict(
            spec=dict(path="/local/set/run", source_root="/local"), observation=None
        )
        jobs = [
            dict(job_id=str(i), state="RUNNING", work_dir=directory)
            for i, directory in enumerate(
                (remote + "/coul.00/prod", remote, remote + "-other")
            )
        ]
        connection = dict(
            state="connected",
            checked_at=at.isoformat(),
            scheduler=dict(state="observed", jobs=jobs),
        )
        metadata = dict(connections=dict(ranvier=connection))
        self.assertEqual(
            matching_jobs(row, metadata, at=at),
            [dict(job, cluster="ranvier") for job in jobs[:2]],
        )
        connection["checked_at"] = (at - timedelta(minutes=6)).isoformat()
        self.assertEqual(
            matching_jobs(row, metadata, at=at),
            [dict(job, cluster="ranvier") for job in jobs[:2]],
        )

    def test_monitoring_managed_precedence_and_recorded_fallback(self):
        at = datetime(2026, 10, 23, tzinfo=timezone.utc)
        older = (at - timedelta(minutes=1)).isoformat()
        remote = "/home/reub0138/managed/action"
        job = dict(job_id="42", state="RUNNING", work_dir=remote)
        execution = dict(
            state="active",
            plan=dict(remote=remote, config=dict(cluster="ranvier")),
            observation=dict(at=older, jobs=[job]),
        )
        row = dict(spec=dict(path=None), observation={}, executions=[execution])
        connection = dict(
            state="connected",
            checked_at=at.isoformat(),
            scheduler=dict(state="observed", jobs=[]),
        )
        metadata = dict(connections=dict(ranvier=connection))
        self.assertEqual(matching_jobs(row, metadata, at=at), [])
        connection["checked_at"] = older
        execution["observation"]["at"] = at.isoformat()
        self.assertEqual(
            matching_jobs(row, metadata, at=at),
            [dict(job, cluster="ranvier", observed_at=at.isoformat())],
        )
        row["executions"] = []
        row["observation"]["scheduler"] = dict(
            job_id=7, cluster="soma", observed_at=at.isoformat()
        )
        self.assertEqual(
            matching_jobs(row, metadata, at=at),
            [
                dict(
                    job_id="7",
                    cluster="soma",
                    observed_at=at.isoformat(),
                    state="unknown",
                )
            ],
        )

    def test_location_requires_evidence_and_local_completion_requires_retrieval(self):
        self.initialize()
        metadata, rows = registry_read.read_registry(self.registry)
        row = rows[0]
        self.assertEqual(
            console_state.presentation(row, metadata)["location"], "Unknown"
        )
        row["observation"] = {
            "completion": "validated",
            "transfer": {"state": "unknown"},
        }
        self.assertEqual(
            console_state.presentation(row, metadata)["location"], "Unknown"
        )
        row["observation"]["transfer"]["state"] = "verified_local"
        self.assertEqual(
            console_state.presentation(row, metadata)["location"], "absolut"
        )
        self.assertEqual(console_state.presentation(row, metadata)["remarks"], "OK")

    def test_recorded_exact_work_directory_matches_supply_job_id_and_location(self):
        self.initialize()
        metadata, rows = registry_read.read_registry(self.registry)
        row = self.complex_row()
        metadata["connections"] = {
            "ranvier": {
                "state": "connected",
                "checked_at": registry_values.now(),
                "scheduler": {
                    "state": "observed",
                    "jobs": [
                        {
                            "job_id": "1234",
                            "state": "RUNNING",
                            "work_dir": "/home/reub0138/set/L1/abfe_van1_hrex_r1/complex",
                        },
                        {
                            "job_id": "9999",
                            "state": "RUNNING",
                            "work_dir": "/home/reub0138/set/L1/abfe_van1_hrex_r1/complex-other",
                        },
                    ],
                },
            }
        }
        view = console_state.presentation(row, metadata)
        self.assertEqual(
            (view["job_id"], view["location"], view["remarks"]),
            ("1234", "ranvier", "OK"),
        )
        metadata["connections"]["ranvier"]["checked_at"] = (
            datetime.now(timezone.utc) - timedelta(minutes=6)
        ).isoformat()
        view = console_state.presentation(row, metadata)
        self.assertEqual((view["job_id"], view["location"]), ("1234", "ranvier"))

    def test_array_job_work_directories_can_match_a_window(self):
        self.initialize()
        metadata, rows = registry_read.read_registry(self.registry)
        row = self.complex_row()
        metadata["connections"] = {
            "soma": {
                "state": "connected",
                "checked_at": registry_values.now(),
                "scheduler": {
                    "state": "observed",
                    "jobs": [
                        {
                            "job_id": "12_3",
                            "state": "PENDING",
                            "work_dir": "/home/reub0138/set/L1/abfe_van1_hrex_r1/complex/coul.00/prod",
                        }
                    ],
                },
            }
        }
        view = console_state.presentation(row, metadata)
        self.assertEqual(
            (view["job_id"], view["location"], view["remarks"], view["stage"]),
            ("12_3", "soma", "Waiting", "queued"),
        )

    def test_fresh_recorded_scheduler_identity_is_displayed(self):
        self.initialize()
        metadata, rows = registry_read.read_registry(self.registry)
        row = self.complex_row()
        row["observation"] = {
            "stage": "production_observed",
            "scheduler": {
                "state": "RUNNING",
                "job_id": "123",
                "cluster": "archer",
                "observed_at": registry_values.now(),
            },
        }
        view = console_state.presentation(row, metadata)
        self.assertEqual(
            (view["job_id"], view["location"], view["remarks"]), ("123", "archer", "OK")
        )

    def test_live_jobs_conflict_with_claimed_local_completion(self):
        self.initialize()
        metadata, rows = registry_read.read_registry(self.registry)
        row = self.complex_row()
        row["observation"] = {
            "completion": "validated",
            "transfer": {"state": "verified_local"},
            "scheduler": {
                "state": "RUNNING",
                "job_id": "123",
                "cluster": "archer",
                "observed_at": registry_values.now(),
            },
        }
        view = console_state.presentation(row, metadata)
        self.assertEqual(view["location"], "archer")
        self.assertEqual(view["remarks"], "Attention required")
        self.assertNotEqual(view["stage"], "complete")

    def test_remarks_do_not_hide_open_incidents_or_unverified_convergence(self):
        self.initialize()
        metadata, rows = registry_read.read_registry(self.registry)
        row = self.complex_row()
        row["incidents"] = [{"code": "problem"}]
        row["observation"] = {
            "stage": "production_observed",
            "analysis": {"state": "legacy_unverified", "convergence": "converged"},
        }
        view = console_state.presentation(row, metadata)
        self.assertEqual(view["remarks"], "Attention required")
        self.assertEqual(view["convergence"], "unverified")
        row["incidents"] = []
        row["observation"]["analysis"] = {}
        self.assertEqual(
            console_state.presentation(row, metadata)["remarks"], "Unknown"
        )

    def test_newer_queue_observation_overrides_older_manual_monitor(self):
        self.initialize()
        metadata, rows = registry_read.read_registry(self.registry)
        row = next(r for r in rows if r["spec"]["id"] == "test/L1/complex/1")
        remote = "/home/reub0138/abflow/test/L1/complex/1/action"
        older = (datetime.now(timezone.utc) - timedelta(minutes=11)).isoformat()
        checked = registry_values.now()
        row["executions"] = [
            dict(
                state="active",
                plan=dict(
                    id="action",
                    remote=remote,
                    output=str(self.work / "action"),
                    fingerprint="fixture",
                    config=dict(
                        cluster="ranvier",
                        workload="complex-equilibration",
                        mode="initial",
                    ),
                ),
                observation=dict(
                    at=older, jobs=[dict(job_id="42", state="PENDING", work_dir=remote)]
                ),
            )
        ]
        metadata["connections"] = dict(
            ranvier=dict(
                state="connected",
                checked_at=checked,
                scheduler=dict(
                    state="observed",
                    jobs=[
                        dict(
                            job_id="42", state="RUNNING", work_dir=remote + "/coul.00"
                        ),
                        dict(job_id="99", state="RUNNING", work_dir=remote + "-other"),
                    ],
                ),
            )
        )
        view = console_state.presentation(row, metadata)
        self.assertEqual(
            (
                view["job_id"],
                view["location"],
                view["scheduler_state"],
                view["remarks"],
            ),
            ("42", "ranvier", "Running", "OK"),
        )
        self.assertEqual(view["jobs"][0]["observed_at"], checked)
        self.assertEqual(view["convergence"], "unverified")
        report = console_runs.report_html(metadata, rows)
        self.assertIn("Sort by Scheduler, ascending", report)
        self.assertIn('<td class="scheduler">Running</td>', report)
        self.assertNotIn("ageJobEvidence", report)
        metadata["connections"]["ranvier"]["checked_at"] = older
        self.assertEqual(
            console_state.presentation(row, metadata)["scheduler_state"], "Pending"
        )

    def test_latest_queue_observation_overrides_older_pending_execution(self):
        self.initialize()
        metadata, rows = registry_read.read_registry(self.registry)
        row = next(r for r in rows if r["spec"]["id"] == "test/L1/complex/1")
        remote = "/home/reub0138/abflow/test/L1/complex/1/action"
        older = (datetime.now(timezone.utc) - timedelta(minutes=2)).isoformat()
        newer = registry_values.now()
        execution = dict(
            state="active",
            plan=dict(remote=remote, config=dict(cluster="ranvier")),
            observation=dict(
                at=older, jobs=[dict(job_id="42", state="PENDING", work_dir=remote)]
            ),
        )
        row["executions"] = [execution]
        metadata["connections"] = dict(
            ranvier=dict(
                state="connected",
                checked_at=newer,
                scheduler=dict(state="observed", jobs=[]),
            )
        )
        self.assertEqual(
            console_state.presentation(row, metadata)["scheduler_state"], "Unknown"
        )
        # Submission followed by a manual observation can be newer than the cached cluster sample.
        execution["observation"]["at"] = newer
        metadata["connections"]["ranvier"]["checked_at"] = older
        view = console_state.presentation(row, metadata)
        self.assertEqual(
            (view["scheduler_state"], view["stage"]), ("Pending", "queued")
        )
        self.assertEqual(view["jobs"][0]["observed_at"], newer)
        metadata["connections"]["ranvier"]["checked_at"] = newer
        self.assertEqual(
            console_state.presentation(row, metadata)["scheduler_state"], "Pending"
        )
