import json
import shutil
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

import abfe.execution.files as worker
import abfe.execution.plan as execution_plan
import abfe.registry.manifest as registry_manifest
import abfe.registry.values as registry_values


class ExecutionFixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.registry = self.root / "registry.sqlite"
        source = self.root / "campaign"
        source.mkdir()
        self.campaign_source = source
        manifest = self.root / "campaign.json"
        manifest.write_text(
            json.dumps(
                dict(
                    schema_version=1,
                    name="test",
                    source_root=str(source),
                    datasets=[
                        dict(
                            id="test",
                            label="test",
                            directory="set",
                            expected_ligands=1,
                            ligands=["L1"],
                            complex_windows=dict(rest=12, coul=11, vdw=21),
                            solvent_windows=dict(coul=11, vdw=21),
                        )
                    ],
                )
            )
        )
        registry_manifest.initialize(self.registry, manifest)
        self.source = self.root / "prepared"
        self.source.mkdir()
        for name in ("minim.mdp", "nvt.mdp", "npt.mdp", "prod.mdp"):
            (self.source / name).write_text(
                "integrator="
                + ("steep" if name == "minim.mdp" else "md")
                + "\ndt=0.002\nnsteps="
                + ("50000000" if name == "prod.mdp" else "500")
                + "\n"
            )
        for name in ("system_solv_ions.gro", "topol.top", "index.ndx"):
            (self.source / name).write_text("synthetic input\n")
        self.run_id = "test/L1/vanilla/1"
        self.config = dict(
            schema_version=1,
            run_id=self.run_id,
            source=str(self.source),
            relative_run=".",
            workload="vanilla",
            mode="initial",
            cluster="ranvier",
            partition="g-ran",
            gpu="A40",
            walltime_hours=24,
            output_reserve_bytes=1048576,
            gromacs="/opt/gromacs/bin/gmx",
            gromacs_version="2023.1",
            modules=[],
        )
        self.manifest = self.root / "execution.json"
        self.register_source()

    def register_source(self):
        with closing(sqlite3.connect(self.registry)) as db, db:
            spec = json.loads(
                db.execute(
                    "SELECT spec FROM runs WHERE id=?", (self.run_id,)
                ).fetchone()[0]
            )
            spec["path"] = str(self.source)
            db.execute(
                "UPDATE runs SET spec=? WHERE id=?",
                (registry_values.encode(spec), self.run_id),
            )

    def plan(self):
        self.manifest.write_text(json.dumps(self.config))
        return execution_plan.make_plan(self.registry, self.run_id, self.manifest)

    def windows(self, leg="complex"):
        self.run_id = "test/L1/" + ("complex/1" if leg == "complex" else "solvent")
        self.config.update(
            run_id=self.run_id,
            workload="complex-equilibration" if leg == "complex" else "solvent",
            array_concurrency=2,
        )
        shutil.rmtree(self.source)
        self.source.mkdir()
        counts = (
            dict(rest=12, coul=11, vdw=21)
            if leg == "complex"
            else dict(coul=11, vdw=21)
        )
        count, state = sum(counts.values()), 0
        prefix = "complex" if leg == "complex" else "lig"
        for phase, amount in counts.items():
            for name in (
                f"{prefix}_{phase}.top",
                f"{prefix}_{phase}.gro",
                f"index_{phase}.ndx" if leg == "complex" else "index.ndx",
            ):
                (self.source / name).write_text("synthetic input\n")
            for i in range(amount):
                for stage in ("enmin", "nvt", "npt_b", "npt_pr", "prod"):
                    path = self.source / f"{phase}.{i:02d}" / stage
                    path.mkdir(parents=True)
                    (path / f"{phase}.{stage}.{i:02d}.mdp").write_text(
                        "integrator="
                        + ("steep" if stage == "enmin" else "md")
                        + "\ndt=0.002\nnsteps="
                        + ("10000000" if stage == "prod" else "500")
                        + f"\nref-t=298.15\ninit-lambda-state={state}\nfep-lambdas="
                        + " ".join(str(j / (count - 1)) for j in range(count))
                        + "\n"
                    )
                state += 1
        (self.source / "plumed.dat").write_text("")
        self.register_source()

    def bundle(self, plan):
        root = self.root / "remote" / plan["id"]
        root.mkdir(parents=True)
        for name in ("job.sh", "plan.json"):
            shutil.copyfile(Path(plan["output"]) / name, root / name)
        shutil.copytree(self.source, root / "data")
        return root

    def equilibrium_receipts(self):
        for path in self.source.glob("*/npt_pr"):
            outputs = {}
            for name in ("confout.gro", "state.cpt"):
                (path / name).write_text("synthetic equilibrated output")
                outputs[name] = worker.sha(path / name)
            (path / "abflow-step.json").write_text(
                json.dumps(dict(state="complete", outputs=outputs))
            )

    def production_receipts(self):
        for path in self.source.glob("*/prod"):
            for name in ("state.cpt", "topol.tpr"):
                (path / name).write_text("synthetic managed restart")
            (path / "abflow-step.json").write_text("{}")


FAKE_GROMACS = r"""#!/usr/bin/env python3
import os
from pathlib import Path
import re
import sys
args = sys.argv[1:]
import json
if os.environ.get('FAKE_COMMAND_LOG'):
    with open(os.environ['FAKE_COMMAND_LOG'], 'a') as log: log.write(json.dumps(args)+'\n')
if os.environ.get('FAKE_FAIL') == args[0]:
    print('fake failure detail',file=sys.stderr)
    raise SystemExit(9)
def option(key, default=None):
    return args[args.index(key)+1] if key in args else default
def write(path, text):
    Path(path).write_text(text)
def target(text):
    values = dict(re.findall(r'(?m)^\s*([\w-]+)\s*=\s*([^\n;]+)', text))
    return float(values['dt'])*int(values['nsteps'])
if args == ['--version']:
    print('GROMACS version: 2023.1')
elif args[0] == 'grompp':
    write(option('-o'), Path(option('-f')).read_text())
elif args[0] == 'trjconv':
    write(option('-o'), Path(option('-f')).read_text())
elif args[0] == 'dump':
    if '-cp' in args:
        print('t = ' + Path(option('-cp')).read_text())
    elif '-om' in args:
        write(option('-om'), Path(option('-s')).read_text())
elif args[0] == 'convert-tpr':
    text = Path(option('-s')).read_text()
    dt = float(re.search(r'(?m)^\s*dt\s*=\s*([^\n]+)', text)[1])
    text = re.sub(r'(?m)^\s*nsteps\s*=.*$', 'nsteps=' + str(round(float(option('-until'))/dt)), text)
    write(option('-o'), text)
elif args[0] == 'mdrun':
    if '-multidir' in args:
        start = args.index('-multidir')+1
        paths = []
        while start < len(args) and not args[start].startswith('-'):
            paths.append(Path(args[start]))
            start += 1
    else:
        paths = [Path.cwd()]
    for path in paths:
        prefix = option('-deffnm')
        tpr = path / (option('-s', 'topol.tpr'))
        text = tpr.read_text()
        final_time = min(target(text), float(os.environ.get('FAKE_GMX_STOP_PS', '1e20')))
        write(path / ((prefix or 'confout')+'.gro'), 'coordinates')
        write(path / ((prefix or 'state')+'.cpt'), str(final_time))
        write(path / ((prefix or 'md')+'.log'), 'Finished mdrun\n')
        write(path / 'dhdl.xvg', '@ xaxis label "Time (ps)"\n0 0\n' + str(final_time) + ' 1\n')
else:
    raise SystemExit('unsupported fake engine arguments: ' + repr(args))
"""
