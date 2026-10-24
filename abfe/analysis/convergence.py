import csv
from pathlib import Path

import abfe.analysis.policy as analysis_policy
import abfe.analysis.values as analysis_values


def half_difference(path):
    with Path(path).open(newline="") as stream:
        reader = csv.DictReader(stream)
        if not {"Forward", "Backward", "data_fraction"} <= set(reader.fieldnames or []):
            raise ValueError("Missing convergence columns")
        halves = [
            row for row in reader if analysis_values.finite(row["data_fraction"]) == 0.5
        ]
    if len(halves) != 1:
        raise ValueError("Expected exactly one half-fraction row")
    return analysis_values.finite(
        abs(
            analysis_values.finite(halves[0]["Forward"])
            - analysis_values.finite(halves[0]["Backward"])
        )
    )


def decide(cutoff_ps, target_ps, difference=None, blocked=None):
    p = analysis_policy.protocol()
    if blocked:
        return dict(action="blocked", reason=str(blocked), next_target_ns=None)
    cutoff_ps, target_ps = (
        analysis_values.finite(cutoff_ps),
        analysis_values.finite(target_ps),
    )
    if not 0 < cutoff_ps <= target_ps <= p["maximum_ps"]:
        raise ValueError("Invalid cutoff/target or production beyond the cap")
    if cutoff_ps < p["minimum_ps"]:
        return dict(
            action="continue_target",
            reason="Below the 16 ns analysis minimum",
            next_target_ns=target_ps / 1000,
        )
    if difference is None:
        raise ValueError("An eligible decision requires a valid half-difference")
    difference = analysis_values.finite(difference)
    if difference < 0:
        raise ValueError("An eligible decision requires a valid half-difference")
    if difference < p["half_difference_kcal_mol"]:
        return dict(
            action="stop_converged",
            reason="Absolute MBAR half-difference <0.5 kcal/mol",
            next_target_ns=None,
        )
    if cutoff_ps == p["maximum_ps"]:
        return dict(
            action="stop_at_limit",
            reason="Unconverged at 50 ns; needs attention",
            next_target_ns=None,
        )
    next_target = min(cutoff_ps + p["increment_ps"], p["maximum_ps"])
    if next_target <= target_ps:
        return dict(
            action="blocked",
            reason="The cutoff +5 ns target does not extend the configured target; review continuation versus reanalysis",
            next_target_ns=next_target / 1000,
        )
    return dict(
        action="propose_extension",
        reason="Eligible unconverged analysis; analysed cutoff +5 ns",
        next_target_ns=next_target / 1000,
    )
