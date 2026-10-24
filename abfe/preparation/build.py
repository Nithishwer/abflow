from pathlib import Path

import abfe.preparation.compile as preparation_compile
import abfe.preparation.manifest as preparation_manifest
import abfe.preparation.validate as preparation_validate


def generate(plan):
    """Reuse the existing scientific routines; no MD or scheduler commands are run."""
    from abfe.preparation.complex import ABFESetup
    from abfe.preparation.directory import working_directory
    from abfe.preparation.solvent import LigandOnlySetup
    from abfe.preparation.vanilla import SimulationSetup

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
    preparation_validate.validate_outputs(plan)
    # Compile each distinct topology once; all window state maps are checked above.
    if plan["stage"] == "vanilla":
        for item in plan["mappings"]:
            root = Path(item["vanilla"])
            preparation_compile.compile_input(
                root, "minim.mdp", "topol.top", "system_solv_ions.gro", "index.ndx"
            )
    else:
        for item in plan["mappings"]:
            root = Path(item["complex"])
            for phase in preparation_manifest.COMPLEX_WINDOWS:
                preparation_compile.compile_input(
                    root,
                    f"{phase}.00/enmin/{phase}.enmin.00.mdp",
                    f"complex_{phase}.top",
                    f"complex_{phase}.gro",
                    f"index_{phase}.ndx",
                    phase,
                )
        root = Path(plan["solvent"])
        for phase in preparation_manifest.SOLVENT_WINDOWS:
            preparation_compile.compile_input(
                root,
                f"{phase}.00/enmin/{phase}.enmin.00.mdp",
                f"lig_{phase}.top",
                f"lig_{phase}.gro",
                "index.ndx",
                phase,
            )
