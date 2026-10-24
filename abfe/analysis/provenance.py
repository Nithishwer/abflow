import re
from pathlib import Path

import abfe.analysis.fingerprints as analysis_fingerprints
import abfe.analysis.policy as analysis_policy
import abfe.analysis.values as analysis_values

from . import identity


def verify(record, spec, rehash=False):
    """A report's cheap metadata check is never permission to execute a decision."""
    evidence = record["evidence"]
    if (
        analysis_values.digest(spec) != evidence["spec_sha256"]
        or analysis_values.digest(analysis_policy.protocol())
        != evidence["protocol_sha256"]
    ):
        raise ValueError("Run mapping or analysis protocol changed")
    if not identity.matches(record["software"]["analysis_code_sha256"]):
        raise ValueError(
            "Analysis implementation changed; explicit reanalysis/review required"
        )
    expected = {w["name"] for w in evidence["windows"]}
    observed = {
        path.name
        for path in Path(spec["path"]).iterdir()
        if path.is_dir() and re.fullmatch(r"[a-z]+\.\d+", path.name)
    }
    if observed != expected:
        raise ValueError("Window inventory changed")
    for window in evidence["windows"]:
        if (
            str(
                (Path(spec["path"]) / window["name"] / "prod/dhdl.xvg").resolve(
                    strict=True
                )
            )
            != window["path"]
        ):
            raise ValueError("Window source mapping changed")
    items = (
        evidence["inputs"]
        + evidence["windows"]
        + record.get("artifacts", [])
        + record["software"].get("source_files", [])
    )
    for item in items:
        if str(Path(item["source_path"]).resolve(strict=True)) != item["path"]:
            raise ValueError("Source symlink mapping changed")
        if analysis_fingerprints.stamp(item["path"]) != item["stamp"]:
            raise ValueError(f"Input/output snapshot changed: {item['path']}")
        if (
            rehash
            and analysis_fingerprints.fingerprint(item["path"])["sha256"]
            != item["sha256"]
        ):
            raise ValueError(f"Input/output content changed: {item['path']}")
    if any(
        analysis_fingerprints.stamp(item["path"]) != item["stamp"] for item in items
    ):
        raise ValueError("Inputs changed during integrity verification")
