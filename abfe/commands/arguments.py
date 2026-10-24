"""Run deterministic ABFE operations and inspect campaign evidence."""

import argparse
from pathlib import Path

DEFAULT_REGISTRY = Path(
    "/biggin/b211/reub0138/Documents/ChatGPT/Membrane-protein-FEP/.abflow/campaign.sqlite"
)


def parser():
    root = argparse.ArgumentParser(description=__doc__)
    root.add_argument("--registry", default=DEFAULT_REGISTRY, type=Path)
    commands = root.add_subparsers(dest="command", required=True)

    init = commands.add_parser("init", help="Register an explicit campaign manifest")
    init.add_argument("--manifest", required=True, type=Path, help="Campaign YAML file")
    init.add_argument("--source-root", type=Path)

    refresh = commands.add_parser(
        "refresh", help="Scan local files and publish reports"
    )
    refresh.add_argument(
        "--remote",
        action="store_true",
        help="Also inspect own queues and active campaign jobs",
    )
    refresh.add_argument("--publish-dir", type=Path)

    next_step = commands.add_parser(
        "next-step",
        help="Execute next campaign actions, or preview them with --dry-run",
    )
    from abfe.operator.next_select import OPERATIONS

    next_step.add_argument("operation", nargs="?", choices=OPERATIONS)
    next_step.add_argument(
        "--dry-run",
        action="store_true",
        help="Show the same next actions without saving or executing anything",
    )
    next_step.add_argument("--plan-id")
    next_step.add_argument("--run-id")

    prepare = commands.add_parser(
        "prepare", help="Prepare one compound in protected local staging"
    )
    prepare.add_argument("compound")
    prepare.add_argument("--manifest", required=True, type=Path)
    prepare.add_argument("--output", required=True, type=Path)
    prepare.add_argument("--stage", required=True, choices=("vanilla", "alchemical"))
    prepare.add_argument("--plan", action="store_true")

    return root
