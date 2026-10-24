import json
import math
import re
from importlib.util import find_spec
from pathlib import Path

import abfe.preparation.inputs as preparation_inputs
import abfe.preparation.validate as preparation_validate
import abfe.registry.manifest as registry_manifest
import abfe.registry.read as registry_read

STAGES = ("enmin", "nvt", "npt_b", "npt_pr", "prod")


PROTEIN_FORCEFIELD = "amber99sb-star-ildn-mut.ff"


COMPLEX_WINDOWS = {"rest": 12, "coul": 11, "vdw": 21}


SOLVENT_WINDOWS = {"coul": 11, "vdw": 21}


LIPID_FILES = ("forcefield_popc.itp", "POPC.itp", "popc_posres.itp")


CODE_FILES = (
    "mdp.py",
    "preparation/__init__.py",
    "preparation/alchemical_system.py",
    "preparation/bespokefit.py",
    "preparation/build.py",
    "preparation/compile.py",
    "preparation/complex.py",
    "preparation/complex_topology.py",
    "preparation/coordinates.py",
    "preparation/directory.py",
    "preparation/extract_membrane.py",
    "preparation/gromacs.py",
    "preparation/inputs.py",
    "preparation/ligand_topology.py",
    "preparation/manifest.py",
    "preparation/membrane.py",
    "preparation/parameterize.py",
    "preparation/position_restraints.py",
    "preparation/receipts.py",
    "preparation/restraints.py",
    "preparation/run.py",
    "preparation/simulation_inputs.py",
    "preparation/smirnoff.py",
    "preparation/solvate.py",
    "preparation/solvent.py",
    "preparation/submission.py",
    "preparation/topology_counts.py",
    "preparation/validate.py",
    "preparation/vanilla.py",
    "preparation/vanilla_jobs.py",
    "preparation/water.py",
    "preparation/windows.py",
    "utils/water_deletor.pl",
)


def load_plan(registry, compound, manifest, output, stage):
    manifest, output = (
        preparation_inputs.absolute(manifest),
        preparation_inputs.absolute(output).resolve(),
    )
    config = json.loads(manifest.read_text())
    if config["schema_version"] != 1 or config["compound"] != compound:
        raise ValueError("Preparation manifest version/compound mismatch")
    if stage not in ("vanilla", "alchemical"):
        raise ValueError("Unknown preparation stage")
    metadata, all_rows = registry_read.read_registry(registry)
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
    registry_manifest.check_destination(
        output, metadata, [row["spec"] for row in all_rows]
    )
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
        preparation_inputs.nonempty(protocol[key], key)
    for key, expected in (
        ("protein_forcefield", PROTEIN_FORCEFIELD),
        ("ligand_forcefield", "Sage"),
        ("lipid_forcefield", "Lipid21"),
    ):
        if protocol[key]["name"] != expected:
            raise ValueError(f"Expected {key}: {expected}")
        preparation_inputs.nonempty(protocol[key]["version"], f"{key} version")
        sources.append(preparation_inputs.absolute(protocol[key]["path"]))
    ff = preparation_inputs.absolute(protocol["protein_forcefield"]["path"])
    if ff.name != PROTEIN_FORCEFIELD or not (ff / "forcefield.itp").is_file():
        raise ValueError(
            f"Supply reviewed {PROTEIN_FORCEFIELD}; other protein force fields cannot be substituted"
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
    lipid = preparation_inputs.absolute(protocol["lipid_forcefield"]["path"])
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
        sources.append(preparation_inputs.absolute(inputs[key]))
    if not (preparation_inputs.absolute(inputs["protein"]) / "protein.gro").is_file():
        raise ValueError("Missing reference protein.gro")
    if stage == "vanilla":
        charmm = preparation_inputs.absolute(inputs["charmm_folder"])
        for name in ("step5_input.gro", "topol.top"):
            if not (charmm / name).is_file():
                raise ValueError(f"Missing membrane preparation input: {charmm / name}")
        sources.append(charmm)
    suffix = ligand.split("_")[-1]
    if preparation_inputs.absolute(inputs["sdf"]).name != suffix + ".sdf":
        raise ValueError(
            f"Existing helpers require the ligand SDF filename {suffix}.sdf"
        )
    for key in ("solvate_water_count", "crystal_water_count"):
        if type(inputs[key]) is not int or inputs[key] < 0:
            raise ValueError(f"Explicit nonnegative {key} required")
    bundled_ff = preparation_inputs.absolute(inputs["protein"]) / ff.name
    if (
        bundled_ff.exists()
        and preparation_inputs.snapshot(bundled_ff)["files"]
        != preparation_inputs.snapshot(ff)["files"]
    ):
        raise ValueError("Protein bundle contains different force-field parameters")
    for topology in (
        preparation_inputs.absolute(inputs["protein"]) / "topol.top",
        preparation_inputs.absolute(inputs["solvent_topology"]),
    ):
        text = topology.read_text()
        if (
            "amber99sb-ildn.ff/" in text
            or f"{PROTEIN_FORCEFIELD}/forcefield.itp" not in text
        ):
            raise ValueError(f"Protein force-field mismatch in {topology}")
    vanilla = {
        r: preparation_inputs.absolute(repeats[str(r)]["vanilla"]) for r in (1, 2, 3)
    }
    topologies = {
        r: preparation_inputs.absolute(repeats[str(r)]["topology"]) for r in (1, 2, 3)
    }
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
            preparation_validate.validate_mdp(path / "prod.mdp", 100000, temperature)
            top = topologies[r].read_text()
            if (
                "amber99sb-ildn.ff/" in top
                or f"{PROTEIN_FORCEFIELD}/forcefield.itp" not in top
            ):
                raise ValueError(f"Vanilla protein force-field mismatch: {path}")
            if (
                preparation_inputs.snapshot(path / ff.name)["files"]
                != preparation_inputs.snapshot(ff)["files"]
            ):
                raise ValueError(
                    f"Vanilla force-field parameters differ from review: {path}"
                )
            if (
                preparation_inputs.snapshot(path / "smirnoff" / (suffix + ".sdf"))[
                    "files"
                ]
                != preparation_inputs.snapshot(inputs["sdf"])["files"]
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
                    preparation_inputs.snapshot(path / "prod.xtc")["files"]["prod.xtc"]
                    for path in vanilla.values()
                }
            )
            != 3
        ):
            raise ValueError("Identical vanilla trajectories require provenance review")
        for path in vanilla.values():
            for name in (suffix + ".itp", suffix + "_atomtypes.itp"):
                if (
                    preparation_inputs.snapshot(path / "toppar" / name)["files"]
                    != preparation_inputs.snapshot(first / "toppar" / name)["files"]
                ):
                    raise ValueError(
                        "Ligand parameters differ between repeats; shared solvent compatibility is unverified"
                    )
    package = Path(__file__).parents[1]
    preparation_validate.validate_mdp(
        package / "utils/data/mdps/mdps_vanilla_memprot/prod.mdp", 100000, temperature
    )
    for directory, phases in (
        ("mdps/super_mdp_files_pertuball_smaller_dumps", COMPLEX_WINDOWS),
        ("ligand_only/lig_super_mdp_files_pertuball", SOLVENT_WINDOWS),
    ):
        for phase in phases:
            preparation_validate.validate_mdp(
                package / "utils/data" / directory / f"{phase}.prod.super.mdp",
                20000,
                temperature,
            )
    restraint = find_spec("MDRestraintsGenerator")
    if restraint is None:
        raise ValueError("MDRestraintsGenerator is not installed in this environment")
    restraint_root = Path(restraint.origin).parent
    sources += [restraint_root, preparation_inputs.absolute(inputs["restraint_script"])]
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
    evidence = [preparation_inputs.snapshot(path) for path in dict.fromkeys(sources)]
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
    plan["fingerprint"] = preparation_inputs.digest(plan)
    return plan
