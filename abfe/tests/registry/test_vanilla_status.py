import json

import abfe.console.state as console_state
import abfe.console.tiles as console_tiles
import abfe.registry.read as registry_read
import abfe.registry.values as registry_values

from .fixtures import CampaignFixture


class TilesTests(CampaignFixture):
    def test_user_accepted_vanilla_completion_is_display_only_and_scoped(self):
        self.initialize()
        metadata, rows = registry_read.read_registry(self.registry)
        row = next(row for row in rows if row["spec"]["leg"] == "vanilla")
        row["observation"] = {
            "stage": "production_observed",
            "production_ns": 100,
            "target_ns": 100,
            "transfer": {"state": "unknown"},
        }
        original = json.dumps(row, sort_keys=True)
        metadata["accepted_vanilla_completions"] = {
            "paths": {row["spec"]["id"]: row["spec"]["path"]}
        }
        view = console_state.presentation(row, metadata)
        self.assertEqual(
            (
                console_tiles.compound_status(row, view),
                view["remarks"],
                view["stage_label"],
            ),
            ("complete", "OK", "Complete (user accepted)"),
        )
        self.assertEqual(view["location"], "Unknown")
        self.assertEqual(json.dumps(row, sort_keys=True), original)
        for changed in (
            dict(row, incidents=[{"code": "new_failure", "message": "Review"}]),
            dict(row, observation={"stage": "observation_error"}),
            dict(
                row,
                observation={
                    "stage": "production_observed",
                    "production_ns": 90,
                    "target_ns": 100,
                },
            ),
            dict(row, spec=dict(row["spec"], path="/different/source")),
            dict(row, spec=dict(row["spec"], leg="complex")),
            dict(
                row,
                observation=dict(
                    row["observation"],
                    scheduler={
                        "job_id": "123",
                        "cluster": "ranvier",
                        "state": "RUNNING",
                        "observed_at": registry_values.now(),
                    },
                ),
            ),
        ):
            self.assertNotEqual(
                console_tiles.compound_status(
                    changed, console_state.presentation(changed, metadata)
                ),
                "complete",
            )
        metadata.pop("accepted_vanilla_completions")
        self.assertEqual(
            console_tiles.compound_status(
                row, console_state.presentation(row, metadata)
            ),
            "attention",
        )

    def test_accepted_remote_vanilla_completion_requires_the_reviewed_execution(self):
        import copy

        self.initialize()
        metadata, rows = registry_read.read_registry(self.registry)
        row = next(r for r in rows if r["spec"]["leg"] == "vanilla")
        row["observation"] = {
            "stage": "setup_present",
            "target_ns": 100,
            "transfer": {"state": "unknown"},
        }
        execution = dict(
            action_id="reviewed-action",
            at="2026-10-18T19:00:00+00:00",
            state="failed",
            plan=dict(
                fingerprint="original-plan",
                remote="/home/user/abflow/reviewed",
                config={"cluster": "ranvier"},
            ),
            observation={"state": "failed"},
        )
        row["executions"] = [execution]
        proof = dict(
            registered_spec=copy.deepcopy(row["spec"]),
            action_id=execution["action_id"],
            execution_at=execution["at"],
            plan_fingerprint=execution["plan"]["fingerprint"],
            remote=execution["plan"]["remote"],
            production_ns=100,
            target_ns=100,
            authorization="User authorized completion reconciliation",
            evidence_sha256="a" * 64,
        )
        metadata["accepted_vanilla_completions"] = {
            "remote": {row["spec"]["id"]: proof}
        }
        original = json.dumps(row, sort_keys=True)
        view = console_state.presentation(row, metadata)
        self.assertEqual(
            (
                console_tiles.compound_status(row, view),
                view["remarks"],
                view["stage_label"],
            ),
            ("complete", "OK", "Complete (remote accepted)"),
        )
        self.assertEqual(view["location"], "Unknown")
        self.assertEqual(json.dumps(row, sort_keys=True), original)
        for field, value in (
            ("action_id", "different"),
            ("execution_at", "new observation"),
            ("plan_fingerprint", "different"),
            ("remote", "/different"),
            ("production_ns", 7.174),
            ("authorization", ""),
            ("evidence_sha256", ""),
        ):
            with self.subTest(field=field):
                changed = copy.deepcopy(metadata)
                changed["accepted_vanilla_completions"]["remote"][row["spec"]["id"]][
                    field
                ] = value
                self.assertNotEqual(
                    console_tiles.compound_status(
                        row, console_state.presentation(row, changed)
                    ),
                    "complete",
                )
        for changed in (
            dict(row, incidents=[{"code": "unexpected", "message": "Review"}]),
            dict(row, spec=dict(row["spec"], path="/different/source")),
            dict(row, spec=dict(row["spec"], leg="complex")),
            dict(row, executions=[]),
            dict(row, executions=[execution, dict(execution, action_id="new-action")]),
            dict(row, observation={"stage": "observation_error"}),
            dict(
                row,
                observation=dict(
                    row["observation"],
                    scheduler={
                        "job_id": "123",
                        "cluster": "ranvier",
                        "state": "RUNNING",
                        "observed_at": registry_values.now(),
                    },
                ),
            ),
        ):
            self.assertNotEqual(
                console_tiles.compound_status(
                    changed, console_state.presentation(changed, metadata)
                ),
                "complete",
            )

    def test_vanilla_and_shared_solvent_display_mapping(self):
        self.initialize()
        metadata, rows = registry_read.read_registry(self.registry)
        vanilla = next(row for row in rows if row["spec"]["leg"] == "vanilla")
        view = console_state.presentation(vanilla, metadata)
        self.assertEqual(
            (view["simulation_type"], view["leg"], view["repeat"], view["convergence"]),
            ("Vanilla", "Complex", 1, "N/A"),
        )
        solvent = next(row for row in rows if row["spec"]["leg"] == "solvent")
        view = console_state.presentation(solvent, metadata)
        self.assertEqual(
            (view["simulation_type"], view["leg"], view["repeat"]),
            ("Alchemical", "Solvent", 1),
        )
        self.assertIsNone(
            solvent["spec"]["repeat"],
            "Presentation must not duplicate the shared calculation",
        )
