"""Fingerprint analysis sources and recognize this one reviewed layout migration."""

import hashlib
import json
from pathlib import Path

PREVIOUS_CODE_SHA256 = (
    "838bb61d9886ad9853aa2ba6facf053a239b242c8d4d4bbddbb752025dd0a0db"
)


def modules():
    root = Path(__file__).parent
    result = {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.glob("*.py"))
    }

    result.update(
        {
            name: hashlib.sha256((root.parent / name).read_bytes()).hexdigest()
            for name in ("mdp.py", "monitoring/jobs.py")
        }
    )
    return result


def current():
    root = Path(__file__).parent
    evidence = dict(
        modules(),
        migration=hashlib.sha256(
            (root / "source-migration.json").read_bytes()
        ).hexdigest(),
    )
    return hashlib.sha256(json.dumps(evidence, sort_keys=True).encode()).hexdigest()


def matches(recorded):
    if recorded == current():
        return True
    migration = json.loads(
        (Path(__file__).parent / "source-migration.json").read_text()
    )
    return (
        recorded == PREVIOUS_CODE_SHA256
        and migration["previous_sha256"] == PREVIOUS_CODE_SHA256
        and migration["modules"] == modules()
    )
