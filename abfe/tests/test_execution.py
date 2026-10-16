"""Tiny execution fixtures exercise failure boundaries without real SSH or MD."""
from contextlib import closing, redirect_stdout
import io
import os
import json
from pathlib import Path
import shutil
import sqlite3
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from abfe import campaign, execution as ex, execution_files as worker, execution_transport as transport


class ExecutionFixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.registry = self.root / 'registry.sqlite'
        source = self.root / 'campaign'
        source.mkdir()
        self.campaign_source = source
        manifest = self.root / 'campaign.json'
        manifest.write_text(json.dumps(dict(schema_version=1, name='test', source_root=str(source), datasets=[
            dict(id='test', label='test', directory='set', expected_ligands=1, ligands=['L1'],
                 complex_windows=dict(rest=12, coul=11, vdw=21), solvent_windows=dict(coul=11, vdw=21))])))
        campaign.initialize(self.registry, manifest)
        self.source = self.root / 'prepared'
        self.source.mkdir()
        for name in ('minim.mdp', 'nvt.mdp', 'npt.mdp', 'prod.mdp'):
            (self.source / name).write_text('integrator=' + ('steep' if name == 'minim.mdp' else 'md') +
                '\ndt=0.002\nnsteps=' + ('50000000' if name == 'prod.mdp' else '500') + '\n')
        for name in ('system_solv_ions.gro', 'topol.top', 'index.ndx'):
            (self.source / name).write_text('synthetic input\n')
        self.run_id = 'test/L1/vanilla/1'
        self.config = dict(schema_version=1, run_id=self.run_id, source=str(self.source), relative_run='.',
            workload='vanilla', mode='initial', cluster='ranvier', partition='g-ran', gpu='A40',
            walltime_hours=24, output_reserve_bytes=1048576, gromacs='/opt/gromacs/bin/gmx', gromacs_version='2023.1', modules=[])
        self.manifest = self.root / 'execution.json'
        self.register_source()


    def register_source(self):
        with closing(sqlite3.connect(self.registry)) as db, db:
            spec = json.loads(db.execute('SELECT spec FROM runs WHERE id=?', (self.run_id,)).fetchone()[0])
            spec['path'] = str(self.source)
            db.execute('UPDATE runs SET spec=? WHERE id=?', (campaign.encode(spec), self.run_id))

    def plan(self):
        self.manifest.write_text(json.dumps(self.config))
        return ex.make_plan(self.registry, self.run_id, self.manifest)


    def windows(self, leg='complex'):
        self.run_id = 'test/L1/' + ('complex/1' if leg == 'complex' else 'solvent')
        self.config.update(run_id=self.run_id, workload='complex-equilibration' if leg == 'complex' else 'solvent', array_concurrency=2)
        shutil.rmtree(self.source)
        self.source.mkdir()
        counts = dict(rest=12, coul=11, vdw=21) if leg == 'complex' else dict(coul=11, vdw=21)
        count, state = sum(counts.values()), 0
        prefix = 'complex' if leg == 'complex' else 'lig'
        for phase, amount in counts.items():
            for name in (f'{prefix}_{phase}.top', f'{prefix}_{phase}.gro', f'index_{phase}.ndx' if leg == 'complex' else 'index.ndx'):
                (self.source / name).write_text('synthetic input\n')
            for i in range(amount):
                for stage in ('enmin', 'nvt', 'npt_b', 'npt_pr', 'prod'):
                    path = self.source / f'{phase}.{i:02d}' / stage
                    path.mkdir(parents=True)
                    (path / f'{phase}.{stage}.{i:02d}.mdp').write_text('integrator=' + ('steep' if stage == 'enmin' else 'md') +
                        '\ndt=0.002\nnsteps=' + ('10000000' if stage == 'prod' else '500') +
                        f'\nref-t=298.15\ninit-lambda-state={state}\nfep-lambdas=' + ' '.join(str(j/(count-1)) for j in range(count)) + '\n')
                state += 1
        (self.source / 'plumed.dat').write_text('')
        self.register_source()


    def bundle(self, plan):
        root = self.root / 'remote' / plan['id']
        root.mkdir(parents=True)
        for name in ('job.sh', 'plan.json'):
            shutil.copyfile(Path(plan['output']) / name, root / name)
        shutil.copytree(self.source, root / 'data')
        return root


    def equilibrium_receipts(self):
        for path in self.source.glob('*/npt_pr'):
            outputs = {}
            for name in ('confout.gro', 'state.cpt'):
                (path / name).write_text('synthetic equilibrated output')
                outputs[name] = worker.sha(path / name)
            (path / 'abflow-step.json').write_text(json.dumps(dict(state='complete', outputs=outputs)))


    def production_receipts(self):
        for path in self.source.glob('*/prod'):
            for name in ('state.cpt', 'topol.tpr'):
                (path / name).write_text('synthetic managed restart')
            (path / 'abflow-step.json').write_text('{}')




FAKE_GROMACS = r'''#!/usr/bin/env python3
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
'''


class ExecutionTests(ExecutionFixture):
    def test_plan_is_local_plain_gromacs_and_preserves_source(self):
        before=worker.snapshot(self.source)
        with patch.object(ex,'remote',side_effect=AssertionError('no SSH')):
            plan=self.plan()
            self.assertFalse(ex.action(self.registry,plan['id'],'stage')['apply'])
        text=(Path(plan['output'])/'job.sh').read_text()
        self.assertIn('--job-name=test-L1-vanilla-1',text)
        self.assertIn('"$GMX" grompp -f "prod.mdp"',text)
        self.assertIn('-t "npt.cpt"',text)
        self.assertIn('--gres=gpu:A40:1',text)
        for unwanted in ('python','worker.py','nvidia-smi','base64'):
            self.assertNotIn(unwanted,text)
        self.assertFalse((Path(plan['output'])/'worker.py').exists())
        self.assertEqual(before,worker.snapshot(self.source))
        subprocess.run(['bash','-n',str(Path(plan['output'])/'job.sh')],check=True)

    def test_modules_use_login_shell_and_names_are_safe(self):
        self.config['modules']=['GROMACS/2023.1-foss-2022a-CUDA-11.7.0']
        plan=self.plan()
        self.assertEqual(ex.script(plan).splitlines()[0],'#!/bin/bash -l')
        plan['run_id']='yuan/DOR_10/vanilla/1\n#SBATCH --exclusive'
        self.assertNotIn('\n#SBATCH --exclusive\n',ex.script(plan))

    def test_partitions_and_warning_allowances_are_explicit(self):
        for cluster,partition,gpu in [('soma','gpu4-2080','2080'),('ranvier','g-kha','A40'),
                ('ranvier','gpu-khalid','A40'),('ranvier','g-ran','L40S')]:
            self.config.update(cluster=cluster,partition=partition,gpu=gpu)
            with self.assertRaisesRegex(ValueError,'routine A40/L40S'): self.plan()
        self.config.update(cluster='ranvier',partition='g-ran',gpu='A40',maxwarn={'minim':2})
        with self.assertRaisesRegex(ValueError,'warning review'): self.plan()

    def test_changed_source_and_script_raise_incidents(self):
        plan=self.plan()
        (self.source/'topol.top').write_text('changed')
        with patch.object(ex,'remote',side_effect=AssertionError('must stop locally')):
            with self.assertRaisesRegex(ValueError,'Source snapshot changed'):
                ex.action(self.registry,plan['id'],'stage',True)
        (Path(plan['output'])/'job.sh').write_text('changed')
        with self.assertRaisesRegex(ValueError,'changed'):
            ex.action(self.registry,plan['id'],'submit')
        row=next(r for r in campaign.read_registry(self.registry)[1] if r['spec']['id']==self.run_id)
        self.assertTrue(row['incidents'])

    def test_transfer_and_submission_preserve_ids_and_never_duplicate(self):
        plan=self.plan()
        def remote(plan,op): return dict(state='submitted',job_id='42') if op=='submit' else dict(state='staged')
        with patch.object(ex,'remote',side_effect=remote) as call,patch.object(ex,'rsync') as copy:
            ex.action(self.registry,plan['id'],'stage',True)
            self.assertEqual(copy.call_count,3)  # data, plan and the shell script; no runner.
            result=ex.action(self.registry,plan['id'],'submit',True)
            self.assertEqual(result['submission']['job_id'],'42')
            with patch.object(ex,'action_monitor',return_value=dict(state='active')):
                ex.action(self.registry,plan['id'],'submit',True)
            self.assertEqual(sum(c.args[1]=='submit' for c in call.call_args_list),1)

    def test_lost_submission_response_blocks_retry(self):
        plan=self.plan()
        ex.save(self.registry,plan,'staged')
        with patch.object(ex,'remote',side_effect=subprocess.TimeoutExpired('ssh',1)):
            with self.assertRaises(ValueError): ex.action(self.registry,plan['id'],'submit',True)
        with patch.object(ex,'remote',side_effect=AssertionError('no retry')):
            with self.assertRaisesRegex(ValueError,'Open incidents'):
                ex.action(self.registry,plan['id'],'submit',True)

    def test_closed_failure_requires_resolution_evidence(self):
        plan=self.plan()
        for state in ('paused','resolved'):
            ex.save(self.registry,plan,state)
            with self.assertRaisesRegex(ValueError,'earlier execution'): self.plan()
        ex.save(self.registry,plan,'resolved',resolution=dict(authorization='User approved replacement',state='FAILED'))
        self.assertNotEqual(self.plan()['id'],plan['id'])
        ex.recheck(plan)

    def test_external_topology_and_unrecorded_restart_are_rejected(self):
        outside=self.root/'outside.itp'
        outside.write_text('parameters')
        (self.source/'topol.top').write_text('#include "../outside.itp"\n')
        with self.assertRaisesRegex(ValueError,'Bundle all topology'): self.plan()
        (self.source/'topol.top').write_text('parameters')
        (self.source/'prod.cpt').write_text('legacy checkpoint')
        with self.assertRaisesRegex(ValueError,'continuation'): self.plan()
        self.config['mode']='continue'
        with self.assertRaisesRegex(ValueError,'recorded stage'): self.plan()

    def test_retrieval_verifies_snapshot_and_preserves_local_conflicts(self):
        plan=self.plan()
        root=self.bundle(plan)
        status=dict(state='checkpointed',jobs=[],application=[],snapshot=worker.snapshot(root))
        target=self.root/'retrieved'
        def copying(plan,source,destination,retrieval=False): shutil.copytree(root,destination,dirs_exist_ok=True)
        with patch.object(ex,'remote',return_value=status),patch.object(ex,'rsync',side_effect=copying):
            self.assertEqual(ex.action(self.registry,plan['id'],'retrieve',True,target)['state'],'retrieved')
        (target/'data/topol.top').write_text('conflict')
        with patch.object(ex,'remote',return_value=status),patch.object(ex,'rsync',side_effect=AssertionError('no overwrite')):
            with self.assertRaisesRegex(ValueError,'Local retrieval conflict'):
                ex.action(self.registry,plan['id'],'retrieve',True,target)

    def test_ssh_uses_shell_and_preserves_error_stdout(self):
        plan=self.plan()
        result=subprocess.CompletedProcess([],1,stdout='NVML error on stdout',stderr='another diagnostic')
        with patch.object(transport.subprocess,'run',return_value=result) as call:
            with self.assertRaisesRegex(ValueError,'NVML error on stdout'):
                transport.execute(plan,'squeue --me')
        self.assertEqual(call.call_args.args[0][-1],'bash -s')
        self.assertNotIn('python',call.call_args.kwargs['input'])

    def test_remote_manifest_preserves_spaces_and_empty_directories(self):
        plan=self.plan()
        digest='f'*64
        output='D\tempty\0F\t42\t'+digest+'  ./file with space\0'
        with patch.object(transport,'execute',return_value=output):
            self.assertEqual(transport.snapshot(plan),dict(files={'empty/':None,'file with space':digest},bytes=42))
        plan['remote']='/work/e280/shared/unsafe'
        with self.assertRaisesRegex(ValueError,'approved root'): transport.execute(plan,'true')

    def test_remote_controls_and_data_conflicts_block_submission(self):
        plan=self.plan()
        with patch.object(transport,'execute'),patch.object(transport,'snapshot',return_value=dict(files={'unexpected':'x'})):
            with self.assertRaisesRegex(ValueError,'Transfer conflict'): transport.checks(plan,data=True)

    def test_gpu_and_archer_admission_limits_remain_checked(self):
        plan=self.plan()
        with patch.object(transport,'command',side_effect=['PartitionName=g-ran State=UP MaxTime=UNLIMITED','gpu:RTX2080:4']):
            with self.assertRaisesRegex(ValueError,'requested GPU'): transport.admission(plan,[])
        plan['config'].update(cluster='archer',partition='standard',qos='standard')
        with patch.object(transport,'command',side_effect=['PartitionName=standard State=UP MaxTime=1-00:00:00','standard|16|64|']):
            with self.assertRaisesRegex(ValueError,'ceiling'):
                transport.admission(plan,[dict(qos='standard',state='PENDING')]*64)
        with patch.object(transport,'command',side_effect=['Filesystem 1024-blocks Used Available Capacity Mounted\nx 1000 900 100 90% /work','1506 project','/work 100 0 200 - 1 0 0 -']):
            with self.assertRaisesRegex(ValueError,'Insufficient'): transport.quota(plan)

    def test_empty_queue_does_not_establish_completion(self):
        plan=self.plan()
        with patch.object(transport,'checks'),patch.object(transport,'jobs',return_value=[]),\
             patch.object(transport,'execute',side_effect=['','','']):
            self.assertEqual(transport.observe(plan)['state'],'unknown')
        with patch.object(ex,'remote',return_value=dict(state='unknown',jobs=[],application=[],submission=dict(job_id='42'))):
            self.assertEqual(ex.action(self.registry,plan['id'],'monitor')['state'],'unknown')
        row=next(r for r in campaign.read_registry(self.registry)[1] if r['spec']['id']==self.run_id)
        self.assertTrue(row['incidents'])

    def test_job_name_is_not_used_for_matching(self):
        plan=self.plan()
        job=dict(name='same',work_dir=plan['remote'])
        other=dict(name='same',work_dir=plan['remote']+'-other')
        self.assertEqual(transport.writers(plan,[job,other]),[job])


class NativeScriptTests(ExecutionFixture):
    def fake_engine(self):
        binary=self.root/'bin/gmx'
        binary.parent.mkdir()
        binary.write_text(FAKE_GROMACS)
        binary.chmod(0o700)
        self.config['gromacs']=str(binary)
        return binary

    def launch(self,plan,root,task=None,**environment):
        plan=dict(plan,remote=str(root))
        script=ex.script(plan)
        env=dict(os.environ,SLURM_CPUS_PER_TASK='8',FAKE_COMMAND_LOG=str(root/'commands.jsonl'),**environment)
        if task is not None: env['SLURM_ARRAY_TASK_ID']=str(task)
        result=subprocess.run(['bash'],input=script,text=True,env=env,capture_output=True)
        if result.returncode: self.fail(result.stdout+'\n'+result.stderr)
        return script

    def test_native_vanilla_and_checkpoint_continuation(self):
        self.fake_engine()
        plan=self.plan()
        root=self.bundle(plan)
        self.launch(plan,root,FAKE_GMX_STOP_PS='16000')
        self.assertEqual((root/'status/0.state').read_text().strip(),'checkpointed')
        tpr=worker.sha(root/'data/prod.tpr')
        self.config.update(source=str(root/'data'),mode='continue')
        self.source=root/'data'
        self.register_source()
        continued=self.plan()
        continued_root=self.bundle(continued)
        self.launch(continued,continued_root)
        self.assertEqual((continued_root/'status/0.state').read_text().strip(),'complete')
        self.assertEqual(tpr,worker.sha(continued_root/'data/prod.tpr'))
        commands=[json.loads(x) for folder in (root,continued_root) for x in (folder/'commands.jsonl').read_text().splitlines()]
        self.assertTrue(any('-cpi' in c and 'prod.cpt' in c and '-append' in c for c in commands))
        self.assertTrue(any(c[0]=='grompp' and '-t' in c and 'npt.cpt' in c for c in commands))
        (self.source/'prod.cpt').write_text('tampered')
        with self.assertRaisesRegex(ValueError,'checkpoint evidence'): self.plan()

    def test_native_failure_is_visible_and_stops_before_md(self):
        self.fake_engine()
        plan=self.plan()
        root=self.bundle(plan)
        result=subprocess.run(['bash'],input=ex.script(dict(plan,remote=str(root))),text=True,capture_output=True,
            env=dict(os.environ,SLURM_CPUS_PER_TASK='8',FAKE_FAIL='grompp'))
        self.assertEqual(result.returncode,9)
        self.assertIn('fake failure detail',result.stderr)
        self.assertEqual((root/'status/0.state').read_text().strip(),'failed 9')
        self.assertFalse((root/'data/em.gro').exists())

    def test_an_allocation_is_never_silently_retried(self):
        self.fake_engine()
        plan=self.plan()
        root=self.bundle(plan)
        script=self.launch(plan,root)
        before=worker.snapshot(root/'data')
        repeated=subprocess.run(['bash'],input=script,text=True,capture_output=True,
            env=dict(os.environ,SLURM_CPUS_PER_TASK='8'))
        self.assertNotEqual(repeated.returncode,0)
        self.assertEqual(before,worker.snapshot(root/'data'))
        self.assertIn('#SBATCH --no-requeue',script)

    def test_solvent_windows_are_independent(self):
        self.windows('solvent')
        self.fake_engine()
        plan=self.plan()
        root=self.bundle(plan)
        script=self.launch(plan,root,task=0)
        self.assertNotIn('-hrex',script)
        self.assertTrue((root/'data/coul.00/prod/abflow-centered.gro').exists())
        self.assertFalse((root/'data/coul.01/prod/topol.tpr').exists())

    def test_gpu_equilibration_then_production_only_hrex(self):
        self.windows()
        binary=self.fake_engine()
        plan=self.plan()
        root=self.bundle(plan)
        # Generate and run each independent window exactly as the Slurm array would.
        for task in range(44): self.launch(plan,root,task=task)
        self.assertFalse(list((root/'data').glob('*/prod/topol.tpr')))
        self.config.update(source=str(root/'data'),workload='complex-production',cluster='archer',
            partition='standard',qos='standard',account='e280-biggin')
        self.source=root/'data'
        self.register_source()
        # The production generator is tested with the fake binary; policy validation still rejects
        # external ARCHER executables in the controller tests.
        c=dict(self.config,gromacs=str(binary))
        cpu_root=self.root/'cpu-initial'
        cpu_root.mkdir()
        shutil.copytree(root/'data',cpu_root/'data')
        production=dict(plan,config=c,remote=str(cpu_root))
        srun=binary.parent/'srun'
        srun.write_text('#!/bin/bash\nshift\nexec "$@"\n')
        srun.chmod(0o700)
        self.launch(production,cpu_root,PATH=str(binary.parent)+':'+os.environ['PATH'])
        self.assertEqual(len(list((cpu_root/'data').glob('*/prod/topol.tpr'))),44)
        self.assertEqual((cpu_root/'status/0.state').read_text().strip(),'complete')
        commands=[json.loads(x) for x in (cpu_root/'commands.jsonl').read_text().splitlines()]
        hrex=[c for c in commands if '-hrex' in c]
        self.assertEqual(len(hrex),1)
        self.assertNotIn('-cpi',hrex[0])
        production['config']=dict(c,mode='continue',source=str(cpu_root/'data'))
        continued_root=self.root/'cpu-continue'
        continued_root.mkdir()
        shutil.copytree(cpu_root/'data',continued_root/'data')
        self.launch(production,continued_root,PATH=str(binary.parent)+':'+os.environ['PATH'])
        commands=[json.loads(x) for x in (continued_root/'commands.jsonl').read_text().splitlines()]
        self.assertIn('-cpi',[c for c in commands if '-hrex' in c][-1])
    def test_extension_requires_analysis_and_preserves_all_previous_tprs(self):
        self.windows('solvent')
        binary=self.fake_engine()
        plan=self.plan()
        root=self.bundle(plan)
        run=root/'data'
        originals={}
        for window in plan['windows']:
            phase,number=window.split('.')
            directory=run/window/'prod'
            mdp=directory/f'{phase}.prod.{number}.mdp'
            (directory/'topol.tpr').write_text(mdp.read_text())
            (directory/'state.cpt').write_text('20000')
            (directory/'confout.gro').write_text('coordinates')
            (directory/'abflow-state.time').write_text('20000\n')
            (directory/'abflow-state.done').touch()
            def hashes(names):
                return ''.join(worker.sha(directory/name)+'  '+name+'\n' for name in names)
            (directory/'abflow-state.inputs.sha256').write_text(hashes(['topol.tpr',mdp.name,'../../lig_'+phase+'.top','../../index.ndx']))
            (directory/'abflow-state.outputs.sha256').write_text(hashes(['topol.tpr','state.cpt','confout.gro','abflow-state.time']))
            originals[window]=worker.sha(directory/'topol.tpr')
        (run/'abflow-gromacs-ranvier.sha256').write_text(worker.sha(binary)+'  '+str(binary)+'\n')
        self.source=run
        self.config.update(source=str(run),mode='extend')
        self.register_source()
        with patch.object(ex,'current_decision',return_value=dict(action='stop_converged',reason='converged')):
            with self.assertRaisesRegex(ValueError,'No fresh eligible'): self.plan()
        with patch.object(ex,'current_decision',return_value=dict(action='propose_extension',reason='extend',next_target_ns=25,analysis_id='verified')):
            extension=self.plan()
        failed_root=self.root/'failed-extension'
        failed_root.mkdir()
        shutil.copytree(run,failed_root/'data')
        failed_script=ex.script(dict(extension,remote=str(failed_root)))
        result=subprocess.run(['bash'],input=failed_script,text=True,capture_output=True,
            env=dict(os.environ,SLURM_CPUS_PER_TASK='8',SLURM_ARRAY_TASK_ID='0',FAKE_FAIL='convert-tpr'))
        self.assertNotEqual(result.returncode,0)
        for window in extension['windows']:
            self.assertEqual(worker.sha(failed_root/'data'/window/'prod/topol.tpr'),originals[window])
        # Another array task must stop rather than repair an interrupted extension.
        result=subprocess.run(['bash'],input=failed_script,text=True,capture_output=True,
            env=dict(os.environ,SLURM_CPUS_PER_TASK='8',SLURM_ARRAY_TASK_ID='1'))
        self.assertNotEqual(result.returncode,0)
        self.launch(extension,root,task=0)
        for window in extension['windows']:
            directory=run/window/'prod'
            self.assertEqual(worker.sha(directory/('topol.tpr.before-'+extension['id'])),originals[window])
            self.assertEqual(worker.duration(directory/'topol.tpr'),25000)
        self.assertTrue((root/'extended').exists())


class TransportIntegrationTests(ExecutionFixture):
    def setUp(self):
        super().setUp()
        self.binary=self.root/'bin/gmx'
        self.binary.parent.mkdir()
        self.binary.write_text(FAKE_GROMACS)
        self.binary.chmod(0o700)
        self.config['gromacs']=str(self.binary)
        fake=self.binary.parent/'slurm'
        fake.write_text('''#!/usr/bin/env python3
import os,sys
from pathlib import Path
name=Path(sys.argv[0]).name
if name=='scontrol':
    print('PartitionName=g-ran State=UP MaxTime=UNLIMITED' if sys.argv[2]=='partition' else 'JobId=42 JobState=COMPLETED ExitCode=0:0')
elif name=='sinfo': print('gpu:A40:4')
elif name=='sbatch' and '--test-only' not in sys.argv:
    if os.environ.get('FAKE_SUBMIT_FAIL'): raise SystemExit(2)
    print('42;testcluster')
''')
        fake.chmod(0o700)
        for name in ('squeue','scontrol','sinfo','sbatch'):
            (fake.parent/name).symlink_to(fake)
        self.original_run=subprocess.run
        def local_ssh(args,**kwargs):
            env=dict(os.environ,PATH=str(self.binary.parent)+':'+os.environ['PATH'])
            return self.original_run(['bash','-s'],env=env,**kwargs)
        self.ssh=patch.object(transport.subprocess,'run',side_effect=local_ssh)
        self.ssh.start()
        self.addCleanup(self.ssh.stop)
        roots=patch.dict(transport.CLUSTER_ROOTS,ranvier=str(self.root/'remote'))
        roots.start()
        self.addCleanup(roots.stop)
        (self.root/'remote').mkdir()

    def test_shell_transfer_checks_submit_monitor_and_hashes(self):
        # Generate before intercepting subprocess.run because bash -n is also tested elsewhere.
        plan=self.plan()
        root=Path(plan['remote'])
        self.assertEqual(transport.remote(plan,'reserve')['state'],'reserved')
        shutil.copytree(self.source,root/'data',dirs_exist_ok=True)
        for name in ('plan.json','job.sh'): shutil.copyfile(Path(plan['output'])/name,root/name)
        self.assertEqual(transport.remote(plan,'verify')['state'],'staged')
        self.assertEqual(transport.remote(plan,'submit')['job_id'],'42')
        self.assertTrue((root/'submission.intent').exists())
        self.assertEqual(transport.observe(plan)['state'],'unknown')
        result=self.original_run(['bash',str(root/'job.sh')],env=dict(os.environ,SLURM_CPUS_PER_TASK='8'),capture_output=True,text=True)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        self.assertEqual(transport.observe(plan)['state'],'complete')
        observed=transport.remote(plan,'retrieve')
        self.assertEqual(observed['snapshot'],worker.snapshot(root))
        # A changed checkpoint is detected even though the job still has a completion marker.
        (root/'data/prod.cpt').write_text('changed')
        with self.assertRaisesRegex(ValueError,'Remote command failed'): transport.observe(plan)

    def test_interrupted_submission_intent_never_dispatches_twice(self):
        plan=self.plan()
        root=Path(plan['remote'])
        transport.remote(plan,'reserve')
        shutil.copytree(self.source,root/'data',dirs_exist_ok=True)
        for name in ('plan.json','job.sh'): shutil.copyfile(Path(plan['output'])/name,root/name)
        with patch.dict(os.environ,FAKE_SUBMIT_FAIL='1'):
            with self.assertRaises(ValueError): transport.remote(plan,'submit')
        self.assertTrue((root/'submission.intent').exists())
        self.assertFalse((root/'submission.id').exists())
        with self.assertRaises(ValueError): transport.remote(plan,'submit')
        self.assertFalse((root/'submission.id').exists())

    def test_conflicting_or_symlinked_staging_is_not_repaired(self):
        plan=self.plan()
        root=Path(plan['remote'])
        transport.remote(plan,'reserve')
        (root/'data/topol.top').write_text('conflict')
        with self.assertRaisesRegex(ValueError,'Transfer conflict'): transport.remote(plan,'reserve')
        (root/'data/topol.top').unlink()
        (root/'data/link').symlink_to(self.source/'topol.top')
        with self.assertRaises(ValueError): transport.snapshot(plan,'data')
