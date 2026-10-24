import fcntl
import json
import os
import shutil
import subprocess
import sys
import traceback
from importlib import metadata as distributions
from pathlib import Path

import abfe.analysis.incidents as analysis_incidents
import abfe.preparation.build as preparation_build
import abfe.preparation.inputs as preparation_inputs
import abfe.preparation.manifest as preparation_manifest
import abfe.preparation.receipts as preparation_receipts
import abfe.registry.read as registry_read
import abfe.registry.values as registry_values


def prepare(registry, compound, manifest, output, stage, plan_only=False):
    """Plan or generate a fresh staged preparation; never adopt/repair source runs."""
    if plan_only:
        return preparation_manifest.load_plan(
            registry, compound, manifest, output, stage
        )
    registry = Path(registry).resolve()
    # Serialise preparation attempts, including incident checks and durable intent.
    with (registry.parent / (registry.name + ".preparation.lock")).open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        plan = None
        output = preparation_inputs.absolute(output).resolve()
        original_cwd = Path.cwd()
        try:
            plan = preparation_manifest.load_plan(
                registry, compound, manifest, output, stage
            )
            if output.exists():
                receipt = json.loads((output / "receipt.json").read_text())
                if (
                    receipt["state"] != "prepared"
                    or receipt["plan"]["fingerprint"] != plan["fingerprint"]
                ):
                    raise ValueError(
                        "Existing staging is incomplete or belongs to different inputs; preserving it"
                    )
                actual = preparation_inputs.snapshot(output / plan["ligand"])
                if actual != receipt["outputs"]:
                    raise ValueError(
                        "Prepared outputs changed; preserving them for review"
                    )
                if [
                    preparation_inputs.snapshot(item["path"])
                    for item in plan["sources"]
                ] != plan["sources"]:
                    raise ValueError(
                        "Inputs changed while verifying the staged preparation"
                    )
                return receipt
            output.mkdir(parents=True)
            record = dict(
                compound=compound,
                state="preparing",
                at=registry_values.now(),
                stage=stage,
                output=str(output),
                plan=plan,
            )
            (output / "intent.json").write_text(registry_values.encode(record))
            preparation_receipts.save_record(registry, record)
            version = subprocess.run(
                ["gmx", "--version"], capture_output=True, text=True, check=True
            )
            software = {
                "python": sys.version,
                "executable": sys.executable,
                "gmx": shutil.which("gmx"),
                "gmx_version": version.stdout + version.stderr,
                "gmx_binary": preparation_inputs.snapshot(
                    Path(shutil.which("gmx")).resolve()
                ),
                "environment": {
                    key: os.environ.get(key)
                    for key in (
                        "PATH",
                        "LD_LIBRARY_PATH",
                        "GMXLIB",
                        "GMXDATA",
                        "OMP_NUM_THREADS",
                    )
                },
            }
            for name in (
                "MDAnalysis",
                "MDRestraintsGenerator",
                "openff-toolkit",
                "openmmforcefields",
                "rdkit",
            ):
                try:
                    software[name] = distributions.version(name)
                except distributions.PackageNotFoundError:
                    software[name] = "unknown"
            with preparation_receipts.preparation_log(output / "preparation.log"):
                preparation_build.generate(plan)
            if [
                preparation_inputs.snapshot(item["path"]) for item in plan["sources"]
            ] != plan["sources"]:
                raise ValueError(
                    "Preparation inputs changed during setup; outputs require review"
                )
            record = dict(
                compound=compound,
                state="prepared",
                at=registry_values.now(),
                stage=stage,
                output=str(output),
                plan=plan,
                software=software,
                outputs=preparation_inputs.snapshot(output / plan["ligand"]),
            )
            with (output / "receipt.json").open("x") as stream:
                stream.write(registry_values.encode(record))
            preparation_receipts.save_record(registry, record)
            return record
        except (Exception, SystemExit) as exc:
            message = f"{stage} preparation paused: {exc}. Staging preserved at {output}; explicit resolution required."
            record = dict(
                compound=compound,
                state="failed",
                at=registry_values.now(),
                stage=stage,
                output=str(output),
                error=message,
                traceback=traceback.format_exc(),
            )
            preparation_receipts.save_record(registry, record)
            _, rows = registry_read.read_registry(registry)
            for row in rows:
                if row["spec"]["id"].startswith(compound + "/"):
                    analysis_incidents.incident(
                        registry, row["spec"]["id"], "preparation", message
                    )
            raise ValueError(message) from exc
        finally:
            os.chdir(original_cwd)
