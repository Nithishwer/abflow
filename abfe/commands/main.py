import json
import sqlite3

import abfe.commands.arguments as commands_arguments
import abfe.monitoring.update as monitoring_update
import abfe.registry.manifest as registry_manifest


def main(argv=None):
    root = commands_arguments.parser()
    args = root.parse_args(argv)
    if args.command == "next-step":
        if args.dry_run:
            if args.operation or args.plan_id is not None or args.run_id is not None:
                root.error(
                    "next-step --dry-run cannot take an operation, --plan-id or --run-id"
                )
        elif any(
            (args.operation, args.plan_id is not None, args.run_id is not None)
        ) and not (args.operation and args.plan_id and args.run_id):
            root.error(
                "A saved operation requires an operation, --plan-id and --run-id"
            )
    try:
        if args.command != "init" and not args.registry.is_file():
            raise ValueError("Campaign registry does not exist: " + str(args.registry))
        if args.command == "next-step":
            from abfe.operator.next_execute import campaign, execute

            result = (
                execute(args.registry, args.plan_id, args.run_id, args.operation)
                if args.operation
                else campaign(args.registry, dry_run=args.dry_run)
            )
            print(json.dumps(result, indent=2, allow_nan=False))
            if result["state"] in ("waiting", "failed", "blocked", "incomplete"):
                root.exit(1, "Some work has not completed; inspect the console.\n")
            return
        if args.command == "init":
            count = registry_manifest.initialize(
                args.registry, args.manifest, args.source_root
            )
            print(f"Registered {count} runs; no source files changed.")
            return
        if args.command == "refresh":
            result = monitoring_update.refresh(
                args.registry, args.publish_dir, args.remote
            )
        elif args.command == "prepare":
            from abfe.preparation.run import prepare

            result = prepare(
                args.registry,
                args.compound,
                args.manifest,
                args.output,
                args.stage,
                args.plan,
            )
        print(json.dumps(result, indent=2, allow_nan=False))
        if args.command == "refresh" and result.get("state") == "incomplete":
            root.exit(1, "Operation needs attention; inspect the evidence above.\n")
    except (OSError, ValueError, KeyError, TypeError, sqlite3.Error) as exc:
        root.exit(2, f"abflow: {exc}\n")


if __name__ == "__main__":
    main()
