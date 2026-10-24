import json
import math
import re
from contextlib import closing
from pathlib import Path

import abfe.execution.files as worker
import abfe.execution.journal as execution_journal
import abfe.execution.profile as execution_profile
import abfe.preparation.validate as preparation_validate
import abfe.registry.read as registry_read


def source_check(config, spec):
    source = Path(config["source"])
    if not source.is_absolute():
        raise ValueError("source must be absolute")
    run = source / execution_profile.relative(config["relative_run"])
    if not run.is_dir():
        raise ValueError("Missing prepared run directory")
    for topology in list(source.rglob("*.top")) + list(source.rglob("*.itp")):
        for reference in re.findall(
            r'^\s*#include\s*[<"]([^>"]+)[>"]', topology.read_text(), re.M
        ):
            included = (topology.parent / reference).resolve()
            if not included.is_file() or source.resolve() not in included.parents:
                raise ValueError(
                    "Bundle all topology includes inside source: "
                    + str(topology)
                    + ": "
                    + reference
                )
    windows = [
        f"{phase}.{i:02d}"
        for phase in ("rest", "coul", "vdw")
        for i in range(spec["windows"].get(phase, 0))
    ]
    if spec["leg"] == "vanilla":
        for name in (
            "minim.mdp",
            "nvt.mdp",
            "npt.mdp",
            "prod.mdp",
            "system_solv_ions.gro",
            "topol.top",
            "index.ndx",
        ):
            if not (run / name).is_file():
                raise ValueError("Missing vanilla input: " + name)
        if worker.duration(run / "prod.mdp") != 100000:
            raise ValueError("Vanilla target must remain 100 ns")
    else:
        first = run / windows[0] / "prod" / (windows[0].split(".")[0] + ".prod.00.mdp")
        temperature = float(worker.mdp(first)["ref-t"].split()[0])
        if not math.isfinite(temperature) or temperature <= 0:
            raise ValueError("Invalid production temperature")
        targets = [
            worker.duration(
                run / name / "prod" / "{}.prod.{}.mdp".format(*name.split("."))
            )
            for name in windows
        ]
        preparation_validate.validate_windows(
            run,
            {
                p: spec["windows"][p]
                for p in ("rest", "coul", "vdw")
                if p in spec["windows"]
            },
            temperature,
            targets[0],
        )
        if len(set(targets)) != 1 or targets[0] > 50000:
            raise ValueError("Inconsistent alchemical targets or target above cap")
        if config["mode"] == "initial" and targets[0] != 20000:
            raise ValueError("Initial alchemical target must be 20 ns")
        if (
            config["workload"] == "complex-production"
            and not (run / "plumed.dat").is_file()
        ):
            raise ValueError("Missing complex plumed.dat")
        for phase in spec["windows"]:
            prefix = "lig" if spec["leg"] == "solvent" else "complex"
            for name in (
                f"{prefix}_{phase}.top",
                f"{prefix}_{phase}.gro",
                "index.ndx" if prefix == "lig" else f"index_{phase}.ndx",
            ):
                if not (run / name).is_file():
                    raise ValueError("Missing alchemical input: " + name)
    from abfe.execution.jobs.validate import check_source

    check_source(config, spec, run, windows)
    return source, run, windows


def source_identity(registry, spec, source, run, snapshot):
    if spec.get("path") and run.resolve() == Path(spec["path"]).resolve():
        return dict(kind="registered_source", run_id=spec["id"])
    candidates = []
    with closing(registry_read.readonly(registry)) as db:
        if db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='preparations'"
        ).fetchone():
            for (text,) in db.execute("SELECT record FROM preparations"):
                record = json.loads(text)
                if (
                    record.get("state") != "prepared"
                    or spec["id"] not in record["plan"]["run_ids"]
                ):
                    continue
                prepared = record["plan"]
                if spec["leg"] == "vanilla":
                    expected = (
                        Path(prepared["output"])
                        / prepared["ligand"]
                        / ("vanilla_rep_" + str(spec["repeat"]))
                    )
                elif spec["leg"] == "solvent":
                    expected = Path(prepared["solvent"])
                else:
                    expected = Path(
                        next(
                            m["complex"]
                            for m in prepared["mappings"]
                            if m["repeat"] == spec["repeat"]
                        )
                    )
                if expected.resolve() == run.resolve():
                    candidates.append(
                        (
                            Path(record["outputs"]["path"]),
                            record["outputs"]["files"],
                            dict(
                                kind="preparation", receipt_sha256=worker.digest(record)
                            ),
                        )
                    )
        for record in execution_journal.records(db):
            if record["run_id"] != spec["id"] or record["state"] != "retrieved":
                continue
            transfer, previous = record["transfer"], record["plan"]
            base = Path(transfer["output"])
            if (
                base / "data" / previous["config"]["relative_run"]
            ).resolve() == run.resolve():
                candidates.append(
                    (
                        base,
                        transfer["snapshot"]["files"],
                        dict(kind="verified_retrieval", action_id=previous["id"]),
                    )
                )
    for base, files, identity in candidates:
        base = base.resolve()
        if source.resolve() != base and base not in source.resolve().parents:
            continue
        prefix = source.resolve().relative_to(base).as_posix()
        prefix = "" if prefix == "." else prefix + "/"
        expected = {
            name[len(prefix) :]: value
            for name, value in files.items()
            if name.startswith(prefix) and name != prefix
        }
        if expected == snapshot["files"]:
            return identity
    raise ValueError(
        "Source is not mapped to this run; use registered inputs, recorded preparation or verified retrieval"
    )
