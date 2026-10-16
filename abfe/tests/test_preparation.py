"""Preparation mapping, real MDP generation and preservation at failure boundaries."""
from contextlib import redirect_stdout, redirect_stderr
import io
import json
import os
import shutil
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from abfe import campaign, preparation as prep


class PreparationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.registry = self.root / 'registry.sqlite'
        self.source = self.root / 'campaign'
        self.source.mkdir()
        manifest = self.root / 'campaign.json'
        manifest.write_text(json.dumps(dict(schema_version=1, name='test', source_root=str(self.source), datasets=[
            dict(id='test', label='test', directory='set', expected_ligands=1, ligands=['L1'],
                 complex_windows=prep.COMPLEX_WINDOWS, solvent_windows=prep.SOLVENT_WINDOWS)])))
        campaign.initialize(self.registry, manifest)
        self.output = self.root / 'staged'
        inputs = self.root / 'inputs'
        inputs.mkdir()
        self.ff = inputs / 'amber99sb-star-ildn-mut.ff'
        self.ff.mkdir()
        for name in ('forcefield.itp', 'tip3p.itp', 'ions.itp'):
            (self.ff / name).write_text('; reviewed synthetic fixture\n')
        self.lipid = inputs / 'lipid21'
        self.lipid.mkdir()
        for name in prep.LIPID_FILES:
            (self.lipid / name).write_text('; synthetic lipid fixture\n')
        self.protein = inputs / 'protein'
        self.protein.mkdir()
        topology = '#include "amber99sb-star-ildn-mut.ff/forcefield.itp"\n'
        (self.protein / 'topol.top').write_text(topology)
        (self.protein / 'protein.gro').write_text('synthetic protein coordinates')
        for name in ('L1.sdf', 'sage.offxml', 'waters.gro', 'waterbox.gro'):
            (inputs / name).write_text('synthetic fixture only\n')
        (inputs / 'solvent.top').write_text(topology)
        (inputs / 'restraint.py').write_text('# synthetic fixture script\n')
        charmm = inputs / 'charmm_gmx'
        charmm.mkdir()
        (charmm / 'step5_input.gro').write_text('membrane input\n')
        (charmm / 'topol.top').write_text('POPC 100\n')
        repeats = {}
        for r in (1, 2, 3):
            repeats[str(r)] = dict(
                                  vanilla=str(self.output / 'L1' / f'vanilla_rep_{r}'),
                                  topology=str(self.output / 'L1' / f'vanilla_rep_{r}' / 'topol.top'))
        self.config = dict(schema_version=1, compound='test/L1', repeats=repeats,
                           protocol=dict(reviewed_by='fixture author', reference='test preparation',
                                         chemical_state='test molecule', waters_and_ions='test reference',
                                         charge_treatment='ABFlow co-annihilation', expected_charge=0,
                                         water_model='TIP3P', salt_molar=0.15, temperature_K=298.15,
                                         protein_forcefield=dict(name='amber99sb-star-ildn-mut.ff', version='fixture', path=str(self.ff)),
                                         ligand_forcefield=dict(name='Sage', version='fixture', path=str(inputs / 'sage.offxml')),
                                         lipid_forcefield=dict(name='Lipid21', version='fixture', path=str(self.lipid))),
                           inputs=dict(sdf=str(inputs / 'L1.sdf'), protein=str(self.protein), charmm_folder=str(charmm),
                                       restraint_script=str(inputs / 'restraint.py'),
                                       crystal_water_gro=str(inputs / 'waters.gro'), solvent_box=str(inputs / 'waterbox.gro'),
                                       solvent_topology=str(inputs / 'solvent.top'), solvate_water_count=10, crystal_water_count=2))
        self.manifest = self.root / 'preparation.json'
        self.write_config()

    def write_config(self):
        self.manifest.write_text(json.dumps(self.config))

    def plan(self):
        return prep.prepare(self.registry, 'test/L1', self.manifest, self.output, 'vanilla', True)

    def fake_vanilla(self, plan):
        for r in (1, 2, 3):
            path = self.output / 'L1' / f'vanilla_rep_{r}'
            path.mkdir(parents=True)
            for name in ('system_solv_ions.gro', 'topol.top', 'index.ndx'):
                (path / name).write_text('synthetic output')
            (path / 'prod.mdp').write_text('dt=0.002\nnsteps=50000000\nref-t=298.15 298.15\n')
        prep.validate_outputs(plan)

    def run_staging(self, generator=None):
        with patch.object(prep, 'generate', side_effect=generator or self.fake_vanilla), \
             patch.object(prep.subprocess, 'run') as command:
            command.return_value.stdout = 'test gmx'
            command.return_value.stderr = ''
            return prep.prepare(self.registry, 'test/L1', self.manifest, self.output, 'vanilla')

    def test_plan_has_exact_mapping_and_writes_nothing(self):
        before = self.registry.read_bytes()
        plan = self.plan()
        self.assertEqual(plan['config']['protocol']['protein_forcefield']['name'], 'amber99sb-star-ildn-mut.ff')
        self.assertEqual(len(plan['mappings']), 3)
        self.assertEqual(plan['mappings'][2]['complex'], str(self.output / 'L1/abfe_van3_hrex_r1/complex'))
        self.assertEqual(plan['solvent'], str(self.output / 'L1/abfe_van1_hrex_r1/ligand'))
        self.assertEqual(self.registry.read_bytes(), before)
        self.assertFalse(self.output.exists())
        self.assertFalse(Path(str(self.registry) + '.preparation.lock').exists())

    def test_preparation_tracks_equilibration_without_source_identity_labels(self):
        plan = self.plan()
        self.assertEqual([item['equilibration_repeat'] for item in plan['mappings']], ['van1', 'van2', 'van3'])
        for item in plan['mappings']:
            self.assertEqual(set(item), {'repeat', 'equilibration_repeat', 'vanilla', 'topology', 'complex'})
            self.assertNotIn('embedding', item)
            self.assertNotIn('embedding_id', item)
        membrane = self.config['inputs']['charmm_folder']
        self.assertEqual(len([item for item in plan['sources'] if item['path'] == membrane]), 1)

    def test_alchemical_preparation_needs_no_membrane_source_or_identity(self):
        self.make_alchemical_inputs()
        self.config['inputs'].pop('charmm_folder')
        self.write_config()
        plan = prep.prepare(self.registry, 'test/L1', self.manifest, self.output, 'alchemical', True)
        self.assertEqual(len(plan['mappings']), 3)

    def test_vanilla_generation_uses_membrane_input_for_each_repeat(self):
        from rdkit import Chem
        from unittest.mock import Mock
        sdf = Path(self.config['inputs']['sdf'])
        with Chem.SDWriter(str(sdf)) as writer:
            writer.write(Chem.MolFromSmiles('C'))
        calls = []
        def setup_factory(**kwargs):
            calls.append(kwargs)
            def setup_simulation(compound_name, repeat, output_path):
                path = Path(output_path)
                path.mkdir(parents=True)
                for name in ('system_solv_ions.gro', 'topol.top', 'index.ndx'):
                    (path / name).write_text('synthetic fixture output')
                (path / 'prod.mdp').write_text('dt=0.002\nnsteps=50000000\nref-t=298.15 298.15\n')
                template = Path(prep.__file__).parent / 'utils/data/mdps/mdps_vanilla_memprot/nvt.mdp'
                shutil.copyfile(template, path / 'nvt.mdp')
            return Mock(setup_simulation=setup_simulation)
        with patch('abfe.setup_vanilla_simulations.SimulationSetup', side_effect=setup_factory), \
             patch.object(prep, 'compile_input'):
            prep.generate(self.plan())
        self.assertEqual(len({call['charmm_folder'] for call in calls}), 1)



    def test_rejects_protocol_mismatch_before_generation(self):
        (self.protein / 'topol.top').write_text('#include "amber99sb-ildn.ff/forcefield.itp"')
        with self.assertRaisesRegex(ValueError, 'mismatch'):
            self.plan()
        self.assertFalse(self.output.exists())

    def test_rejects_normal_ildn_manifest_name_and_directory(self):
        self.config['protocol']['protein_forcefield']['name'] = 'AMBER99SB-ILDN'
        self.write_config()
        with self.assertRaisesRegex(ValueError, 'Expected protein_forcefield: amber99sb-star-ildn-mut.ff'):
            self.plan()
        self.config['protocol']['protein_forcefield']['name'] = 'amber99sb-star-ildn-mut.ff'
        self.config['protocol']['protein_forcefield']['path'] = str(self.ff.parent / 'amber99sb-ildn.ff')
        self.write_config()
        with self.assertRaisesRegex(ValueError, 'Supply reviewed amber99sb-star-ildn-mut.ff'):
            self.plan()

    def test_alchemical_repeat_rejects_normal_ildn_topology(self):
        self.make_alchemical_inputs()
        topology = Path(self.config['repeats']['2']['topology'])
        topology.write_text('#include "amber99sb-ildn.ff/forcefield.itp"\n')
        with self.assertRaisesRegex(ValueError, 'Vanilla protein force-field mismatch'):
            prep.prepare(self.registry, 'test/L1', self.manifest, self.output, 'alchemical', True)

    def test_solvent_default_template_copies_the_required_forcefield(self):
        from abfe.utils.ligand_only_setup import LigandOnlySetup
        from abfe.utils.utils import working_directory
        self.make_alchemical_inputs()
        helper = LigandOnlySetup('L1', self.root / 'solvent-test',
                                 vanilla_path=self.config['repeats']['1']['vanilla'],
                                 forcefield_path=self.ff, write_job_scripts=False)
        with working_directory(self.root):
            helper._make_dirs()
            helper._copy_files()
            helper._replace_lig_name()
        text = (helper.lig_dir / 'lig.top').read_text()
        for name in ('forcefield.itp', 'tip3p.itp', 'ions.itp'):
            self.assertIn('amber99sb-star-ildn-mut.ff/' + name, text)
        self.assertNotIn('amber99sb-ildn.ff/', text)
        self.assertEqual(prep.snapshot(helper.lig_dir / self.ff.name)['files'], prep.snapshot(self.ff)['files'])

    def test_solvent_helper_rejects_other_forcefield_directory(self):
        from abfe.utils.ligand_only_setup import LigandOnlySetup
        helper = LigandOnlySetup('L1', self.root / 'solvent-test',
                                 forcefield_path=self.ff.parent / 'amber99sb-ildn.ff')
        with self.assertRaisesRegex(ValueError, 'amber99sb-star-ildn-mut.ff'):
            helper._copy_files()
        self.assertFalse(helper.lig_dir.exists())

    def test_rejects_relative_and_overlapping_destinations(self):
        with self.assertRaisesRegex(ValueError, 'absolute'):
            prep.prepare(self.registry, 'test/L1', self.manifest, Path('relative'), 'vanilla', True)
        self.output = self.source / 'set/L1'
        for r in (1, 2, 3):
            self.config['repeats'][str(r)]['vanilla'] = str(self.output / 'L1' / f'vanilla_rep_{r}')
        self.write_config()
        with self.assertRaisesRegex(ValueError, 'outside'):
            self.plan()

    def test_identical_rerun_reuses_verified_outputs(self):
        first = self.run_staging()
        before = prep.snapshot(self.output)
        with patch.object(prep, 'generate', side_effect=AssertionError('must not regenerate')):
            second = prep.prepare(self.registry, 'test/L1', self.manifest, self.output, 'vanilla')
        self.assertEqual(first, second)
        self.assertEqual(before, prep.snapshot(self.output))
        rows = campaign.read_registry(self.registry)[1]
        self.assertTrue(all(row['preparation']['state'] == 'prepared' for row in rows))
        self.assertIn('Staged preparation', campaign.report_html(*campaign.read_registry(self.registry)))

    def test_changed_output_pauses_and_preserves_evidence(self):
        self.run_staging()
        changed = self.output / 'L1/vanilla_rep_2/topol.top'
        changed.write_text('changed')
        with self.assertRaisesRegex(ValueError, 'outputs changed'):
            self.run_staging()
        self.assertEqual(changed.read_text(), 'changed')
        self.assertTrue(all(any(i['code'] == 'preparation' for i in row['incidents'])
                            for row in campaign.read_registry(self.registry)[1]))
        with self.assertRaisesRegex(ValueError, 'explicit resolution'):
            self.plan()

    def test_failed_setup_restores_cwd_and_blocks_retry(self):
        before = Path.cwd()
        def fail(plan):
            os.chdir(self.output)
            (self.output / 'partial').write_text('retain')
            raise SystemExit('restraint failure')
        with self.assertRaisesRegex(ValueError, 'restraint failure'):
            self.run_staging(fail)
        self.assertEqual(Path.cwd(), before)
        self.assertEqual((self.output / 'partial').read_text(), 'retain')
        self.assertFalse((self.output / 'receipt.json').exists())
        self.assertEqual(campaign.read_registry(self.registry)[1][0]['preparation']['state'], 'failed')
        with self.assertRaisesRegex(ValueError, 'Open preparation incident'):
            self.plan()

    def test_input_change_during_generation_prevents_publication(self):
        def change(plan):
            self.fake_vanilla(plan)
            (self.protein / 'topol.top').write_text('changed during setup')
        with self.assertRaisesRegex(ValueError, 'inputs changed during'):
            self.run_staging(change)
        self.assertFalse((self.output / 'receipt.json').exists())

    def test_invalid_preflight_records_incident_without_creating_staging(self):
        (self.protein / 'topol.top').write_text('unreviewed force field')
        with self.assertRaisesRegex(ValueError, 'force-field mismatch'):
            self.run_staging()
        self.assertFalse(self.output.exists())
        self.assertTrue(all(row['preparation']['state'] == 'failed'
                            and any(i['code'] == 'preparation' for i in row['incidents'])
                            for row in campaign.read_registry(self.registry)[1]))

    def test_changed_inputs_cannot_replace_existing_staging(self):
        self.run_staging()
        before = prep.snapshot(self.output)
        (self.protein / 'protein.gro').write_text('different coordinates')
        with self.assertRaisesRegex(ValueError, 'different inputs'):
            self.run_staging()
        self.assertEqual(before, prep.snapshot(self.output))

    def test_cli_plan(self):
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            campaign.main(['--registry', str(self.registry), 'prepare', 'test/L1', '--manifest', str(self.manifest),
                           '--stage', 'vanilla', '--output', str(self.output), '--plan'])
        self.assertEqual(json.loads(stdout.getvalue())['compound'], 'test/L1')

    def test_existing_abfe_setup_creates_three_not_nine(self):
        from abfe.setup_abfe_simulations import ABFESetup
        base = self.root / 'legacy'
        for r in (1, 2, 3):
            (base / 'L1' / f'vanilla_rep_{r}').mkdir(parents=True)
        extra = base / 'L1/abfe_van1_hrex_r2'
        extra.mkdir()
        (extra / 'historical').write_text('preserve')
        with patch.object(ABFESetup, '_setup_abfe_folder') as setup:
            ABFESetup(base, ['L1'], 3).setup()
        self.assertEqual([call.args[1] for call in setup.call_args_list],
                         ['van1_hrex_r1', 'van2_hrex_r1', 'van3_hrex_r1'])
        self.assertEqual((extra / 'historical').read_text(), 'preserve')

    def test_real_templates_generate_44_complex_and_32_solvent_windows(self):
        from abfe.utils.abfe_helpers_hrex import create_directories_with_MDP_pertuball_smaller_dumps
        from abfe.utils.ligand_only_setup import LigandOnlySetup
        from abfe.utils.utils import working_directory
        complex_path = self.root / 'complex'
        complex_path.mkdir()
        with working_directory(complex_path):
            for phase in prep.COMPLEX_WINDOWS:
                create_directories_with_MDP_pertuball_smaller_dumps(phase, 'Protein_unk_PA_PC_OL', 'Water_and_ions')
        prep.validate_windows(complex_path, prep.COMPLEX_WINDOWS, 298.15)
        lig = LigandOnlySetup('L1', self.root / 'abfe', forcefield_path=self.ff, write_job_scripts=False)
        lig.lig_dir.mkdir(parents=True)
        lig._write_mdps()
        prep.validate_windows(lig.lig_dir, prep.SOLVENT_WINDOWS, 298.15)
        self.assertFalse((lig.lig_dir / 'rest.00').exists())
        self.assertEqual(len(list(complex_path.glob('*/prod/*.mdp'))), 44)
        self.assertEqual(len(list(lig.lig_dir.glob('*/prod/*.mdp'))), 32)

    def make_alchemical_inputs(self):
        from rdkit import Chem
        sdf = Path(self.config['inputs']['sdf'])
        with Chem.SDWriter(str(sdf)) as writer:
            writer.write(Chem.MolFromSmiles('C'))
        for r in (1, 2, 3):
            path = self.root / 'inputs' / f'vanilla{r}'
            path.mkdir()
            for name in ('prod.tpr', 'npt.gro', 'index.ndx'):
                (path / name).write_text('synthetic test fixture')
            (path / 'prod.xtc').write_text(f'synthetic trajectory {r}')
            (path / 'prod.mdp').write_text('dt=0.002\nnsteps=50000000\nref-t=298.15 298.15\n')
            shutil.copyfile(self.protein / 'topol.top', path / 'topol.top')
            shutil.copytree(self.ff, path / self.ff.name)
            (path / 'toppar').mkdir()
            for name in ('L1.itp', 'L1_atomtypes.itp', 'L1_posre.itp'):
                (path / 'toppar' / name).write_text('; shared ligand fixture\n')
            (path / 'smirnoff').mkdir()
            shutil.copyfile(sdf, path / 'smirnoff/L1.sdf')
            (path / 'smirnoff/L1_mod.gro').write_text('synthetic ligand coordinates')
            self.config['repeats'][str(r)].update(vanilla=str(path), topology=str(path / 'topol.top'))
        self.write_config()

    def test_alchemical_staging_uses_each_trajectory_and_one_solvent(self):
        from abfe.utils.ligand_only_setup import LigandOnlySetup
        self.make_alchemical_inputs()
        seen = []
        def restraints(vanilla_folder_name, abfe_path, **kwargs):
            seen.append(str(vanilla_folder_name))
            path = abfe_path / 'boresch'
            path.mkdir()
            for name in ('ClosestRestraintFrame.gro', 'BoreschRestraint.top'):
                (path / name).write_text('synthetic restraint fixture')
            (path / 'resgen_output.txt').write_text('dG_off: -3.0, dG_on: 3.0\n')
        def complex_fep(*args, **kwargs):
            for phase in prep.COMPLEX_WINDOWS:
                Path(f'complex_{phase}.top').write_text('synthetic topology')
                Path(f'complex_{phase}.gro').write_text('synthetic structure')
        def index(*args):
            for name in ('index.ndx', 'index_rest.ndx', 'index_coul.ndx', 'index_vdw.ndx'):
                Path(name).write_text('synthetic index')
        def solvent_fep(self):
            for phase in prep.SOLVENT_WINDOWS:
                (self.lig_dir / f'lig_{phase}.top').write_text('synthetic topology')
                (self.lig_dir / f'lig_{phase}.gro').write_text('synthetic coordinates')
        with patch('MDAnalysis.Universe') as universe, \
             patch('abfe.setup_abfe_simulations.generate_boresch_restraints', side_effect=restraints), \
             patch('abfe.setup_abfe_simulations.create_fep_system', side_effect=complex_fep), \
             patch('abfe.setup_abfe_simulations.create_index', side_effect=index), \
             patch.object(LigandOnlySetup, '_run_gromacs_prep'), \
             patch.object(LigandOnlySetup, '_create_fep_system', solvent_fep), \
             patch.object(LigandOnlySetup, '_make_index', side_effect=index), \
             patch.object(prep.subprocess, 'run') as command:
            universe.return_value.trajectory.__enter__.return_value.__getitem__.return_value.time = 100000
            from types import SimpleNamespace
            def compiler(args, **kwargs):
                self.assertNotIn('mdrun', args)
                if '-o' in args:
                    Path(args[args.index('-o') + 1]).write_bytes(b'synthetic compile result')
                return SimpleNamespace(stdout='test gmx', stderr='')
            command.side_effect = compiler
            result = prep.prepare(self.registry, 'test/L1', self.manifest, self.output, 'alchemical')
        self.assertEqual(result['state'], 'prepared')
        self.assertEqual(seen, [self.config['repeats'][str(r)]['vanilla'] for r in (1, 2, 3)])
        self.assertEqual(len(list((self.output / 'L1').glob('*/complex/*/prod/*.mdp'))), 132)
        self.assertEqual(len(list((self.output / 'L1').glob('*/ligand/*/prod/*.mdp'))), 32)
        self.assertEqual(len(list((self.output / 'L1').glob('*/ligand'))), 1)
        self.assertFalse(list((self.output / 'L1').rglob('*.sh')))

    def test_equilibration_repeats_cannot_reuse_one_vanilla_trajectory(self):
        self.make_alchemical_inputs()
        first = Path(self.config['repeats']['1']['vanilla']) / 'prod.xtc'
        second = Path(self.config['repeats']['2']['vanilla']) / 'prod.xtc'
        shutil.copyfile(first, second)
        with self.assertRaisesRegex(ValueError, 'Identical vanilla trajectories'):
            prep.prepare(self.registry, 'test/L1', self.manifest, self.output, 'alchemical', True)



    def test_shared_solvent_rejects_mismatched_parameters(self):
        self.make_alchemical_inputs()
        Path(self.config['repeats']['2']['vanilla'], 'toppar/L1.itp').write_text('different parameters')
        with self.assertRaisesRegex(ValueError, 'parameters differ'):
            prep.prepare(self.registry, 'test/L1', self.manifest, self.output, 'alchemical', True)

    def test_preparation_lock_prevents_concurrent_generation(self):
        import fcntl
        with Path(str(self.registry) + '.preparation.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with patch.object(prep, 'generate', side_effect=AssertionError('must not run')):
                with self.assertRaises(BlockingIOError):
                    prep.prepare(self.registry, 'test/L1', self.manifest, self.output, 'vanilla')
        self.assertFalse(self.output.exists())


if __name__ == '__main__':
    unittest.main()
