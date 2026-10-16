"""Explicit one-compound preparation in protected staging directories."""

from contextlib import closing, contextmanager, redirect_stdout, redirect_stderr
from decimal import Decimal
import fcntl
import hashlib
from importlib import metadata as distributions
from importlib.util import find_spec
import json
import math
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import sys
import traceback
import uuid

from .analysis import incident, settings
from .campaign import check_destination, encode, now, read_registry

STAGES = ("enmin", "nvt", "npt_b", "npt_pr", "prod")
COMPLEX_WINDOWS = {"rest": 12, "coul": 11, "vdw": 21}
SOLVENT_WINDOWS = {"coul": 11, "vdw": 21}
LIPID_FILES = ("forcefield_popc.itp", "POPC.itp", "popc_posres.itp")
CODE_FILES = (
    "preparation.py",
    "setup_abfe_simulations.py",
    "setup_vanilla_simulations.py",
    "utils/abfe_helpers_hrex.py",
    "utils/ligand_only_setup.py",
    "utils/gromacs_helpers_single_chain.py",
    "utils/smirnoff_param_func.py",
    "utils/utils.py",
    "utils/extract_membrane.py",
    "utils/add_alchemical_ion_v3_pertuball.py",
    "utils/water_deletor.pl",
)


def digest(value):
    return hashlib.sha256(encode(value).encode()).hexdigest()


def snapshot(path):
    path = Path(path)
    if path.is_symlink():
        raise ValueError(
            f"Symlink input/output requires explicit reconciliation: {path}"
        )
    if not path.exists():
        raise ValueError(f"Missing preparation input/output: {path}")
    result = {}
    for item in sorted(path.rglob("*")) if path.is_dir() else [path]:
        if "__pycache__" in item.parts:
            continue
        if item.is_symlink():
            raise ValueError(
                f"Symlink input/output requires explicit reconciliation: {item}"
            )
        if item.is_dir():
            result[str(item.relative_to(path)) + "/"] = None
        elif item.is_file():
            sha = hashlib.sha256()
            before = item.stat()
            with item.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    sha.update(chunk)
            after = item.stat()
            if (before.st_size, before.st_mtime_ns) != (
                after.st_size,
                after.st_mtime_ns,
            ):
                raise ValueError(f"Input changed while hashing: {item}")
            result[str(item.relative_to(path)) if path.is_dir() else item.name] = (
                sha.hexdigest()
            )
        else:
            raise ValueError(f"Unsupported preparation file: {item}")
    if not result:
        raise ValueError(f"Empty preparation input/output: {path}")
    return {"path": str(path), "files": result}


def absolute(value):
    path = Path(value)
    if not path.is_absolute():
        raise ValueError(f"Preparation paths must be absolute: {value}")
    return path


def nonempty(value, label):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"Missing {label}")


def load_plan(registry, compound, manifest, output, stage):
    manifest, output = absolute(manifest), absolute(output).resolve()
    config = json.loads(manifest.read_text())
    if config["schema_version"] != 1 or config["compound"] != compound:
        raise ValueError("Preparation manifest version/compound mismatch")
    if stage not in ("vanilla", "alchemical"):
        raise ValueError("Unknown preparation stage")
    metadata, all_rows = read_registry(registry)
    rows = [
        row
        for row in all_rows
        if row["spec"]["id"].rsplit("/", 2)[0] == compound
        or row["spec"]["id"] == compound + "/solvent"
    ]
    expected_ids = {
        f"{compound}/{leg}/{r}" for leg in ("vanilla", "complex") for r in (1, 2, 3)
    } | {compound + "/solvent"}
    if {row["spec"]["id"] for row in rows} != expected_ids:
        raise ValueError(
            "Expected the compound's three vanilla, three complex and one solvent records"
        )
    if any(
        i["code"] == "preparation" and i["status"] == "open"
        for row in rows
        for i in row["incidents"]
    ):
        raise ValueError(
            "Open preparation incident; explicit resolution required before retry"
        )
    specs = [row["spec"] for row in rows]
    check_destination(output, metadata, [row["spec"] for row in all_rows])
    if output == Path(registry).resolve() or output in Path(registry).resolve().parents:
        raise ValueError("Staging cannot contain/overwrite the registry")
    ligand = next(s["ligand"] for s in specs)
    if not re.fullmatch(r"[A-Za-z0-9_-]+", ligand):
        raise ValueError("Unsafe ligand directory name")
    repeats = config["repeats"]
    if sorted(repeats) != ["1", "2", "3"]:
        raise ValueError("Exactly three explicit repeat mappings are required")
    sources = [manifest]
    protocol = config["protocol"]
    for key in (
        "reviewed_by",
        "reference",
        "chemical_state",
        "waters_and_ions",
        "charge_treatment",
    ):
        nonempty(protocol[key], key)
    for key, expected in (
        ("protein_forcefield", "AMBER99SB-ILDN"),
        ("ligand_forcefield", "Sage"),
        ("lipid_forcefield", "Lipid21"),
    ):
        if protocol[key]["name"] != expected:
            raise ValueError(f"Expected {key}: {expected}")
        nonempty(protocol[key]["version"], f"{key} version")
        sources.append(absolute(protocol[key]["path"]))
    ff = absolute(protocol["protein_forcefield"]["path"])
    if ff.name != "amber99sb-ildn.ff" or not (ff / "forcefield.itp").is_file():
        raise ValueError(
            "Supply reviewed standard amber99sb-ildn.ff; the legacy star/mut force field cannot be substituted"
        )
    for name in ("tip3p.itp", "ions.itp"):
        if not (ff / name).is_file():
            raise ValueError(f"Missing force-field file: {ff / name}")
    if protocol["water_model"] != "TIP3P":
        raise ValueError(
            "Existing preparation supports TIP3P; other water models require an explicit implementation change"
        )
    salt, temperature = float(protocol["salt_molar"]), float(protocol["temperature_K"])
    if (
        not math.isfinite(salt)
        or salt < 0
        or not math.isfinite(temperature)
        or temperature <= 0
    ):
        raise ValueError("Invalid salt concentration/temperature")
    if type(protocol["expected_charge"]) is not int:
        raise ValueError("expected_charge must be an integer")
    lipid = absolute(protocol["lipid_forcefield"]["path"])
    for name in LIPID_FILES:
        if not (lipid / name).is_file():
            raise ValueError(f"Missing reviewed Lipid21 file: {lipid / name}")
    inputs = config["inputs"]
    for key in (
        "sdf",
        "protein",
        "crystal_water_gro",
        "solvent_box",
        "solvent_topology",
    ):
        sources.append(absolute(inputs[key]))
    if not (absolute(inputs["protein"]) / "protein.gro").is_file():
        raise ValueError("Missing reference protein.gro")
    if stage == "vanilla":
        charmm = absolute(inputs["charmm_folder"])
        for name in ("step5_input.gro", "topol.top"):
            if not (charmm / name).is_file():
                raise ValueError(f"Missing membrane preparation input: {charmm / name}")
        sources.append(charmm)
    suffix = ligand.split("_")[-1]
    if absolute(inputs["sdf"]).name != suffix + ".sdf":
        raise ValueError(
            f"Existing helpers require the ligand SDF filename {suffix}.sdf"
        )
    for key in ("solvate_water_count", "crystal_water_count"):
        if type(inputs[key]) is not int or inputs[key] < 0:
            raise ValueError(f"Explicit nonnegative {key} required")
    bundled_ff = absolute(inputs["protein"]) / ff.name
    if bundled_ff.exists() and snapshot(bundled_ff)["files"] != snapshot(ff)["files"]:
        raise ValueError("Protein bundle contains different force-field parameters")
    for topology in (
        absolute(inputs["protein"]) / "topol.top",
        absolute(inputs["solvent_topology"]),
    ):
        text = topology.read_text()
        if "amber99sb-star" in text or "amber99sb-ildn.ff/forcefield.itp" not in text:
            raise ValueError(f"Protein force-field mismatch in {topology}")
    vanilla = {r: absolute(repeats[str(r)]["vanilla"]) for r in (1, 2, 3)}
    topologies = {r: absolute(repeats[str(r)]["topology"]) for r in (1, 2, 3)}
    if stage == "alchemical":
        if len({p.resolve() for p in vanilla.values()}) != 3:
            raise ValueError("Three distinct vanilla trajectories are required")
        sources += list(vanilla.values()) + list(topologies.values())
        for r, path in vanilla.items():
            for name in (
                "prod.xtc",
                "prod.tpr",
                "prod.mdp",
                "npt.gro",
                "index.ndx",
                "toppar",
            ):
                if not (path / name).exists():
                    raise ValueError(f"Missing vanilla restraint input: {path / name}")
            validate_mdp(path / "prod.mdp", 100000, temperature)
            top = topologies[r].read_text()
            if "amber99sb-star" in top or "amber99sb-ildn.ff/forcefield.itp" not in top:
                raise ValueError(f"Vanilla protein force-field mismatch: {path}")
            if snapshot(path / ff.name)["files"] != snapshot(ff)["files"]:
                raise ValueError(
                    f"Vanilla force-field parameters differ from review: {path}"
                )
            if (
                snapshot(path / "smirnoff" / (suffix + ".sdf"))["files"]
                != snapshot(inputs["sdf"])["files"]
            ):
                raise ValueError(
                    f"Vanilla ligand chemical input differs from review: {path}"
                )
    else:
        for r in (1, 2, 3):
            if (
                vanilla[r].resolve() != output / ligand / f"vanilla_rep_{r}"
                or topologies[r].resolve() != vanilla[r] / "topol.top"
            ):
                raise ValueError(
                    "Vanilla mappings must name this stage's exact output paths"
                )
    if stage == "alchemical":
        first = vanilla[1]
        if (
            len(
                {
                    snapshot(path / "prod.xtc")["files"]["prod.xtc"]
                    for path in vanilla.values()
                }
            )
            != 3
        ):
            raise ValueError("Identical vanilla trajectories require provenance review")
        for path in vanilla.values():
            for name in (suffix + ".itp", suffix + "_atomtypes.itp"):
                if (
                    snapshot(path / "toppar" / name)["files"]
                    != snapshot(first / "toppar" / name)["files"]
                ):
                    raise ValueError(
                        "Ligand parameters differ between repeats; shared solvent compatibility is unverified"
                    )
    package = Path(__file__).parent
    validate_mdp(
        package / "utils/data/mdps/mdps_vanilla_memprot/prod.mdp", 100000, temperature
    )
    for directory, phases in (
        ("mdps/super_mdp_files_pertuball_smaller_dumps", COMPLEX_WINDOWS),
        ("ligand_only/lig_super_mdp_files_pertuball", SOLVENT_WINDOWS),
    ):
        for phase in phases:
            validate_mdp(
                package / "utils/data" / directory / f"{phase}.prod.super.mdp",
                20000,
                temperature,
            )
    restraint = find_spec("MDRestraintsGenerator")
    if restraint is None:
        raise ValueError("MDRestraintsGenerator is not installed in this environment")
    restraint_root = Path(restraint.origin).parent
    sources += [restraint_root, absolute(inputs["restraint_script"])]
    sources += [package / name for name in CODE_FILES]
    sources += [
        package / "utils/data/mdps/mdps_vanilla_memprot",
        package / "utils/data/mdps/super_mdp_files_pertuball_smaller_dumps",
        package / "utils/data/ligand_only",
    ]
    for path in sources:
        resolved = path.resolve()
        if (
            resolved == output
            or resolved in output.parents
            or output in resolved.parents
        ):
            raise ValueError(f"Staging must be separate from every input: {path}")
    evidence = [snapshot(path) for path in dict.fromkeys(sources)]
    plan = dict(
        schema_version=1,
        compound=compound,
        stage=stage,
        output=str(output),
        ligand=ligand,
        config=config,
        sources=evidence,
        run_ids=sorted(expected_ids),
        mappings=[
            dict(
                repeat=r,
                equilibration_repeat=f"van{r}",
                vanilla=str(vanilla[r]),
                topology=str(topologies[r]),
                complex=str(output / ligand / f"abfe_van{r}_hrex_r1/complex"),
            )
            for r in (1, 2, 3)
        ],
        solvent=str(output / ligand / "abfe_van1_hrex_r1/ligand"),
    )
    plan["fingerprint"] = digest(plan)
    return plan


def validate_mdp(path, duration_ps, temperature):
    mdp = settings(path)
    if Decimal(mdp["dt"]) * Decimal(mdp["nsteps"]) != duration_ps:
        raise ValueError(f"Unexpected production target in {path}")
    temperatures = [float(value) for value in mdp["ref-t"].split()]
    if not temperatures or any(value != temperature for value in temperatures):
        raise ValueError(f"Temperature mismatch in {path}")
    return mdp




def validate_outputs(plan):
    output, ligand = Path(plan["output"]), plan["ligand"]
    temp = float(plan["config"]["protocol"]["temperature_K"])
    if plan["stage"] == "vanilla":
        for r in (1, 2, 3):
            path = output / ligand / f"vanilla_rep_{r}"
            for name in ("system_solv_ions.gro", "topol.top", "index.ndx"):
                if not (path / name).is_file() or not (path / name).stat().st_size:
                    raise ValueError(
                        f"Missing/empty prepared vanilla output: {path / name}"
                    )
            validate_mdp(path / "prod.mdp", 100000, temp)
        return
    expected = {f"abfe_van{r}_hrex_r1" for r in (1, 2, 3)}
    if {p.name for p in (output / ligand).iterdir() if p.is_dir()} != expected:
        raise ValueError("Expected exactly three complex preparation directories")
    if list((output / ligand).glob("*/ligand")) != [Path(plan["solvent"])]:
        raise ValueError("Expected exactly one shared solvent preparation")
    for r in (1, 2, 3):
        root = output / ligand / f"abfe_van{r}_hrex_r1"
        for name in (
            "ClosestRestraintFrame.gro",
            "BoreschRestraint.top",
            "resgen_output.txt",
        ):
            if (
                not (root / "boresch" / name).is_file()
                or not (root / "boresch" / name).stat().st_size
            ):
                raise ValueError(
                    f"Missing/empty restraint output: {root / 'boresch' / name}"
                )
        corrections = re.findall(
            r"^dG_off:\s*([^,\s]+)",
            (root / "boresch/resgen_output.txt").read_text(),
            re.M,
        )
        if len(corrections) != 1 or not math.isfinite(float(corrections[0])):
            raise ValueError("Missing/invalid MDRestraintsGenerator correction")
        validate_windows(root / "complex", COMPLEX_WINDOWS, temp)
    validate_windows(Path(plan["solvent"]), SOLVENT_WINDOWS, temp)


def validate_windows(root, windows, temperature):
    actual = {
        p.name
        for p in root.iterdir()
        if p.is_dir() and re.fullmatch(r"(rest|coul|vdw)\.\d+", p.name)
    }
    expected = {
        f"{phase}.{i:02d}" for phase, count in windows.items() for i in range(count)
    }
    if actual != expected:
        raise ValueError(f"Unexpected/missing windows: {root}")
    state, reference = 0, None
    for phase, count in windows.items():
        for i in range(count):
            for stage in STAGES:
                path = (
                    root / f"{phase}.{i:02d}" / stage / f"{phase}.{stage}.{i:02d}.mdp"
                )
                mdp = settings(path)
                if int(mdp["init-lambda-state"]) != state:
                    raise ValueError(f"Wrong lambda state in {path}")
                vectors = {
                    key: value.split()
                    for key, value in mdp.items()
                    if key.endswith("-lambdas")
                }
                if not vectors or any(
                    len(v) != sum(windows.values()) for v in vectors.values()
                ):
                    raise ValueError(f"Wrong lambda map length in {path}")
                if reference is None:
                    reference = vectors
                if vectors != reference:
                    raise ValueError(f"Inconsistent lambda maps in {path}")
                if stage == "prod":
                    validate_mdp(path, 20000, temperature)
            state += 1


def generate(plan):
    """Reuse the existing scientific routines; no MD or scheduler commands are run."""
    from .setup_vanilla_simulations import SimulationSetup
    from .setup_abfe_simulations import ABFESetup
    from .utils.ligand_only_setup import LigandOnlySetup
    from .utils.utils import working_directory

    config, output = plan["config"], Path(plan["output"])
    protocol, inputs, ligand = config["protocol"], config["inputs"], plan["ligand"]
    ff = protocol["protein_forcefield"]["path"]
    # Charge is reviewed explicitly; never answer a scientific prompt automatically.
    from rdkit import Chem

    molecules = Chem.SDMolSupplier(inputs["sdf"])
    if len(molecules) != 1:
        raise ValueError("Expected exactly one ligand molecule in the reviewed SDF")
    molecule = molecules[0]
    if (
        molecule is None
        or Chem.GetFormalCharge(molecule) != protocol["expected_charge"]
    ):
        raise ValueError(
            "Ligand chemical state/charge differs from the reviewed manifest"
        )
    if plan["stage"] == "vanilla":
        for item in plan["mappings"]:
            setup = SimulationSetup(
                base_path=output,
                charmm_folder=inputs["charmm_folder"],
                protein_folder=inputs["protein"],
                sdf_folder=Path(inputs["sdf"]).parent,
                crystal_water_gro=inputs["crystal_water_gro"],
                solvate_water_count=inputs["solvate_water_count"],
                crystal_water_count=inputs["crystal_water_count"],
                forcefield_path=ff,
                sage_offxml=protocol["ligand_forcefield"]["path"],
                lipid_dir=protocol["lipid_forcefield"]["path"],
                solvent_box=inputs["solvent_box"],
                salt_molar=protocol["salt_molar"],
                write_job_scripts=False,
            )
            setup.setup_simulation(
                Path(inputs["sdf"]).stem, item["repeat"], output_path=item["vanilla"]
            )
    else:
        import MDAnalysis as mda

        for item in plan["mappings"]:
            path = Path(item["vanilla"])
            with mda.Universe(
                str(path / "prod.tpr"), str(path / "prod.xtc")
            ).trajectory as trajectory:
                if trajectory[-1].time < 100000:
                    raise ValueError(
                        f"Vanilla trajectory has not reached 100 ns: {path}"
                    )
        ABFESetup(
            output,
            [ligand],
            3,
            vanilla_paths={
                item["repeat"]: item["vanilla"] for item in plan["mappings"]
            },
            forcefield_path=ff,
            expected_charge=protocol["expected_charge"],
            restraint_script=inputs["restraint_script"],
            topology_paths={
                item["repeat"]: item["topology"] for item in plan["mappings"]
            },
            write_job_scripts=False,
        ).setup()
        with working_directory(output):
            LigandOnlySetup(
                ligand,
                output / ligand / "abfe_van1_hrex_r1",
                vanilla_path=config["repeats"]["1"]["vanilla"],
                forcefield_path=ff,
                topology_template=inputs["solvent_topology"],
                solvent_box=inputs["solvent_box"],
                salt_molar=protocol["salt_molar"],
                expected_charge=protocol["expected_charge"],
                write_job_scripts=False,
            ).setup()
    validate_outputs(plan)
    # Compile each distinct topology once; all window state maps are checked above.
    if plan["stage"] == "vanilla":
        for item in plan["mappings"]:
            root = Path(item["vanilla"])
            compile_input(
                root, "minim.mdp", "topol.top", "system_solv_ions.gro", "index.ndx"
            )
    else:
        for item in plan["mappings"]:
            root = Path(item["complex"])
            for phase in COMPLEX_WINDOWS:
                compile_input(
                    root,
                    f"{phase}.00/enmin/{phase}.enmin.00.mdp",
                    f"complex_{phase}.top",
                    f"complex_{phase}.gro",
                    f"index_{phase}.ndx",
                    phase,
                )
        root = Path(plan["solvent"])
        for phase in SOLVENT_WINDOWS:
            compile_input(
                root,
                f"{phase}.00/enmin/{phase}.enmin.00.mdp",
                f"lig_{phase}.top",
                f"lig_{phase}.gro",
                "index.ndx",
                phase,
            )


def compile_input(root, mdp, topology, structure, index, suffix="vanilla"):
    output = root / f"preparation-{suffix}.tpr"
    subprocess.run(
        [
            "gmx",
            "grompp",
            "-f",
            str(root / mdp),
            "-c",
            str(root / structure),
            "-r",
            str(root / structure),
            "-p",
            str(root / topology),
            "-n",
            str(root / index),
            "-o",
            str(output),
        ],
        cwd=root,
        check=True,
    )
    if not output.is_file() or not output.stat().st_size:
        raise ValueError(f"GROMACS did not validate the prepared inputs: {root}")


@contextmanager
def preparation_log(path):
    # Capture child-process output as well as Python diagnostics, restoring both on failure.
    sys.stdout.flush()
    sys.stderr.flush()
    saved = [os.dup(1), os.dup(2)]
    try:
        with path.open("x") as stream:
            os.dup2(stream.fileno(), 1)
            os.dup2(stream.fileno(), 2)
            with redirect_stdout(stream), redirect_stderr(stream):
                yield
    finally:
        for fd, original in zip((1, 2), saved):
            os.dup2(original, fd)
            os.close(original)


def save_record(registry, record):
    with closing(sqlite3.connect(registry)) as db, db:
        db.execute(
            "CREATE TABLE IF NOT EXISTS preparations (id TEXT PRIMARY KEY, compound TEXT NOT NULL, record TEXT NOT NULL)"
        )
        db.execute(
            "INSERT INTO preparations VALUES (?,?,?)",
            (str(uuid.uuid4()), record["compound"], encode(record)),
        )


def prepare(registry, compound, manifest, output, stage, plan_only=False):
    """Plan or generate a fresh staged preparation; never adopt/repair source runs."""
    if plan_only:
        return load_plan(registry, compound, manifest, output, stage)
    registry = Path(registry).resolve()
    # Serialise preparation attempts, including incident checks and durable intent.
    with (registry.parent / (registry.name + ".preparation.lock")).open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        plan = None
        output = absolute(output).resolve()
        original_cwd = Path.cwd()
        try:
            plan = load_plan(registry, compound, manifest, output, stage)
            if output.exists():
                receipt = json.loads((output / "receipt.json").read_text())
                if (
                    receipt["state"] != "prepared"
                    or receipt["plan"]["fingerprint"] != plan["fingerprint"]
                ):
                    raise ValueError(
                        "Existing staging is incomplete or belongs to different inputs; preserving it"
                    )
                actual = snapshot(output / plan["ligand"])
                if actual != receipt["outputs"]:
                    raise ValueError(
                        "Prepared outputs changed; preserving them for review"
                    )
                if [snapshot(item["path"]) for item in plan["sources"]] != plan[
                    "sources"
                ]:
                    raise ValueError(
                        "Inputs changed while verifying the staged preparation"
                    )
                return receipt
            output.mkdir(parents=True)
            record = dict(
                compound=compound,
                state="preparing",
                at=now(),
                stage=stage,
                output=str(output),
                plan=plan,
            )
            (output / "intent.json").write_text(encode(record))
            save_record(registry, record)
            version = subprocess.run(
                ["gmx", "--version"], capture_output=True, text=True, check=True
            )
            software = {
                "python": sys.version,
                "executable": sys.executable,
                "gmx": shutil.which("gmx"),
                "gmx_version": version.stdout + version.stderr,
                "gmx_binary": snapshot(Path(shutil.which("gmx")).resolve()),
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
            with preparation_log(output / "preparation.log"):
                generate(plan)
            if [snapshot(item["path"]) for item in plan["sources"]] != plan["sources"]:
                raise ValueError(
                    "Preparation inputs changed during setup; outputs require review"
                )
            record = dict(
                compound=compound,
                state="prepared",
                at=now(),
                stage=stage,
                output=str(output),
                plan=plan,
                software=software,
                outputs=snapshot(output / plan["ligand"]),
            )
            with (output / "receipt.json").open("x") as stream:
                stream.write(encode(record))
            save_record(registry, record)
            return record
        except (Exception, SystemExit) as exc:
            message = f"{stage} preparation paused: {exc}. Staging preserved at {output}; explicit resolution required."
            record = dict(
                compound=compound,
                state="failed",
                at=now(),
                stage=stage,
                output=str(output),
                error=message,
                traceback=traceback.format_exc(),
            )
            save_record(registry, record)
            _, rows = read_registry(registry)
            for row in rows:
                if row["spec"]["id"].startswith(compound + "/"):
                    incident(registry, row["spec"]["id"], "preparation", message)
            raise ValueError(message) from exc
        finally:
            os.chdir(original_cwd)
