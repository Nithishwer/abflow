"""Small complete data streams exercise decisions, integrity and solvent dependencies."""
from contextlib import redirect_stdout, redirect_stderr
import io
import json
import math
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from abfe import analysis, campaign


class AnalysisTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.work = Path(self.temp.name)
        self.source = self.work / 'source'
        self.source.mkdir()
        self.registry = self.work / 'registry.sqlite'
        self.manifest = self.work / 'manifest.json'
        self.manifest.write_text(json.dumps(dict(schema_version=1, name='test', source_root=str(self.source), datasets=[
            dict(id='test', label='Test', expected_ligands=1, directory='set', ligands=['L1'],
                 complex_windows={'coul': 2}, solvent_windows={'coul': 2})])))
        campaign.initialize(self.registry, self.manifest)
        self.complex_id = 'test/L1/complex/1'
        self.solvent_id = 'test/L1/solvent'
        for leg in ('complex', 'ligand'):
            root = self.source / 'set/L1/abfe_van1_hrex_r1' / leg
            for i in range(2):
                prod = root / f'coul.{i:02d}/prod'
                prod.mkdir(parents=True)
                (prod / f'coul.prod.{i:02d}.mdp').write_text(
                    f'dt = 0.002\nnsteps = 10000000\nnstdhdl = 500000\nref-t = 298.15\n'
                    f'init-lambda-state = {i}\ncoul-lambdas = 0 1\n')
                (prod / 'topol.tpr').write_bytes(b'test identity only')
                self.write_dhdl(prod / 'dhdl.xvg', i)
        self.correction = self.source / 'set/L1/abfe_van1_hrex_r1/boresch/resgen_output.txt'
        self.correction.parent.mkdir()
        self.correction.write_text('dG_off: -3.0, dG_on: 3.0\n')

    def write_dhdl(self, path, state, end=20000, step=1000):
        text = ('@ xaxis label "Time (ps)"\n'
                f'@ subtitle "T = 298.15 (K) state {state}: (coul-lambda) = ({state:.4f})"\n'
                '@ s0 legend "Total Energy (kJ/mol)"\n'
                '@ s1 legend "dH/dl coul-lambda"\n'
                '@ s2 legend "Delta H to (0.0000)"\n'
                '@ s3 legend "Delta H to (1.0000)"\n')
        path.write_text(text + ''.join(f'{t:.4f} 0 0 0 0\n' for t in range(0, end+1, step)))

    def spec(self, run_id=None):
        return next(r['spec'] for r in campaign.read_registry(self.registry)[1] if r['spec']['id'] == (run_id or self.complex_id))

    def dhdl(self, window=0, leg='complex'):
        return self.source / f'set/L1/abfe_van1_hrex_r1/{leg}/coul.{window:02d}/prod/dhdl.xvg'

    def fake_backend(self, evidence, output, forward=1., backward=1.1):
        (output / 'ABFE_convergence.csv').write_text(f'Forward,Backward,data_fraction\n{forward},{backward},0.5\n')
        (output / 'ABFE_summary.csv').write_text('synthetic backend results\n')
        return dict(estimate_kcal_mol=10., uncertainty_kcal_mol=2., retained_samples=[20, 20])

    def analyse(self, run_id=None, cutoff=20, backend=None):
        with patch('abfe.analysis.run_analysis', side_effect=backend or self.fake_backend):
            return analysis.analyse(self.registry, run_id or self.complex_id, cutoff)

    def test_decision_boundaries_and_increment_from_cutoff(self):
        self.assertEqual(analysis.decide(15999,20000)['action'], 'continue_target')
        for cutoff, target in ((16000,21),(21000,26),(46000,50),(49000,50)):
            result=analysis.decide(cutoff,cutoff,.5)
            self.assertEqual(result['next_target_ns'],target)
        self.assertEqual(analysis.decide(16000,30000,.5)['action'],'blocked')
        self.assertEqual(analysis.decide(16000,30000,.5)['next_target_ns'],21)
        self.assertEqual(analysis.decide(16000,20000,.499999)['action'],'stop_converged')
        self.assertEqual(analysis.decide(50000,50000,.5)['action'],'stop_at_limit')
        self.assertEqual(analysis.decide(50000,50000,.1)['action'],'stop_converged')
        for cutoff,target,difference in ((16000,20000,None),(16000,20000,float('nan')),(16000,20000,-1),(51000,51000,.1)):
            with self.assertRaises(ValueError):analysis.decide(cutoff,target,difference)

    def test_complete_stream_identity_and_exact_cutoff(self):
        value=analysis.validate_inputs(self.spec(),16000)
        self.assertEqual(len(value['windows']),2)
        self.assertEqual(value['windows'][0]['samples_through_cutoff'],17)
        self.assertEqual(value['windows'][0]['endpoint_ps'],20000)
        self.assertEqual(value['temperature_K'],298.15)
        self.assertEqual(value['protocol']['discard_ps'],10)
        with self.assertRaisesRegex(ValueError,'exact requested cutoff'):
            analysis.validate_inputs(self.spec(),16500)

    def test_missing_and_extra_windows_fail(self):
        self.dhdl(1).parent.parent.rename(self.dhdl(1).parent.parent.with_name('coul.02'))
        with self.assertRaisesRegex(ValueError,'Window inventory mismatch'):
            analysis.validate_inputs(self.spec(),16000)

    def test_bad_interior_samples_are_not_hidden_by_good_endpoint(self):
        original=self.dhdl().read_text()
        for bad in ('nan','broken',''):
            with self.subTest(bad=bad):
                self.dhdl().write_text(original.replace('1000.0000 0 0 0 0',f'1000.0000 {bad} 0 0 0'))
                with self.assertRaises(ValueError):analysis.validate_inputs(self.spec(),16000)

    def test_gaps_duplicate_samples_and_partial_lines_fail(self):
        original=self.dhdl().read_text()
        for text in (original.replace('1000.0000 0 0 0 0\n',''),original.replace('1000.0000','0.0000'),original[:-1]):
            self.dhdl().write_text(text)
            with self.assertRaises(ValueError):analysis.validate_inputs(self.spec(),16000)

    def test_units_schedule_state_temperature_and_endpoint_columns_fail(self):
        original=self.dhdl().read_text()
        for text in (original.replace('Time (ps)','Time (ns)'),original.replace('state 0:','state 1:'),
                     original.replace('= (0.0000)','= (1.0000)'),original.replace('298.15','300'),
                     original.replace('to (1.0000)','to (0.5000)')):
            self.dhdl().write_text(text)
            with self.assertRaises(ValueError):analysis.validate_inputs(self.spec(),16000)

    def test_missing_tpr_and_inconsistent_targets_fail(self):
        tpr=self.dhdl().parent/'topol.tpr'
        tpr.unlink()
        with self.assertRaises(FileNotFoundError):analysis.validate_inputs(self.spec(),16000)
        tpr.write_bytes(b'restored test fixture')
        mdp=self.dhdl(1).parent/'coul.prod.01.mdp'
        mdp.write_text(mdp.read_text().replace('10000000','15000000'))
        with self.assertRaisesRegex(ValueError,'inconsistent targets'):
            analysis.validate_inputs(self.spec(),16000)

    def test_existing_data_above_cap_are_preserved_and_rejected(self):
        self.write_dhdl(self.dhdl(),0,end=51000)
        before=self.dhdl().read_bytes()
        with self.assertRaisesRegex(ValueError,'exceed the 50 ns cap'):
            analysis.validate_inputs(self.spec(),16000)
        self.assertEqual(self.dhdl().read_bytes(),before)

    def test_half_fraction_errors_and_absolute_difference(self):
        path=self.work/'halves.csv'
        for text in ('Forward,Backward,data_fraction\n0,1,0.1\n','Forward,Backward,data_fraction\n0,1,.5\n1,2,.5\n',
                     'Forward,Backward,data_fraction\n0,nan,.5\n','other\n1\n'):
            path.write_text(text)
            with self.assertRaises((ValueError,KeyError)):analysis.half_difference(path)
        path.write_text('Forward,Backward,data_fraction\n0,3,.5\n')
        self.assertEqual(analysis.half_difference(path),3)

    def test_plan_and_below_minimum_never_run_estimator(self):
        with patch('abfe.analysis.run_analysis') as backend:
            planned=analysis.analyse(self.registry,self.complex_id,16,plan=True)
            small=analysis.analyse(self.registry,self.complex_id,15)
            backend.assert_not_called()
        self.assertEqual(planned['decision']['action'],'analyse')
        self.assertEqual(small['decision']['action'],'continue_target')
        self.assertEqual(analysis.current_decision(self.registry,self.complex_id)['action'],'continue_target')

    def test_retired_checks_do_not_block_analysis_and_other_incidents_still_do(self):
        analysis.incident(self.registry,self.complex_id,'embedding_provenance','Embedding source unknown')
        with sqlite3.connect(self.registry) as db:
            db.execute("UPDATE incidents SET status='retired' WHERE code='embedding_provenance'")
        planned=analysis.analyse(self.registry,self.complex_id,20,plan=True)
        self.assertEqual(planned['decision']['action'],'analyse')
        record=self.analyse()
        self.assertEqual(record['state'],'validated')
        self.assertFalse(record['open_incidents'])
        row=next(r for r in campaign.read_registry(self.registry)[1] if r['spec']['id']==self.complex_id)
        self.assertFalse(row['incidents'])
        self.assertEqual(row['incident_history'][0]['status'],'retired')
        self.assertEqual(analysis.current_decision(self.registry,self.complex_id)['action'],'stop_converged')
        analysis.incident(self.registry,self.complex_id,'unexpected_data_issue','Needs review')
        with patch('abfe.analysis.run_analysis') as backend:
            result=analysis.analyse(self.registry,self.complex_id,20)
            self.assertEqual(result['action'],'blocked')
            self.assertIn('unexpected_data_issue',result['reason'])
            backend.assert_not_called()

    def test_no_copies_no_source_writes_and_immutable_analysis(self):
        before={str(p):p.read_bytes() for p in self.source.rglob('*') if p.is_file()}
        record=self.analyse()
        self.assertEqual(before,{str(p):p.read_bytes() for p in self.source.rglob('*') if p.is_file()})
        self.assertFalse(list(Path(record['output']).glob('*.xvg')))
        self.assertEqual(analysis.current_decision(self.registry,self.complex_id)['action'],'stop_converged')
        with sqlite3.connect(self.registry) as db:
            for statement in ('UPDATE analyses SET record=record','DELETE FROM analyses'):
                with self.assertRaisesRegex(sqlite3.IntegrityError,'immutable'):db.execute(statement)

    def test_new_plan_does_not_replace_validated_analysis(self):
        record=self.analyse()
        analysis.analyse(self.registry,self.complex_id,16,plan=True)
        self.assertEqual(next(r for r in campaign.read_registry(self.registry)[1] if r['spec']['id']==self.complex_id)['analysis_record']['id'],record['id'])

    def test_input_or_output_change_blocks_decision_and_console(self):
        record=self.analyse()
        self.dhdl().write_text(self.dhdl().read_text().replace('1000.0000 0','1000.0000 1'))
        self.assertEqual(analysis.current_decision(self.registry,self.complex_id)['action'],'blocked')
        metadata,rows=campaign.read_registry(self.registry)
        row=next(r for r in rows if r['spec']['id']==self.complex_id)
        self.assertEqual(row['observation']['analysis']['state'],'stale')
        self.assertEqual(campaign.presentation(row,metadata)['remarks'],'Attention required')
        self.assertIn('Analysis and decision',campaign.report_html(metadata,rows))

    def test_estimator_failure_is_retained_and_incident_blocks_retry(self):
        def broken(*args):raise RuntimeError('scientific failure')
        with self.assertRaisesRegex(ValueError,'incident recorded'):self.analyse(backend=broken)
        metadata,rows=campaign.read_registry(self.registry)
        row=next(r for r in rows if r['spec']['id']==self.complex_id)
        self.assertEqual(row['analysis_record']['state'],'failed')
        self.assertEqual(len(row['incidents']),1)
        with patch('abfe.analysis.run_analysis') as backend:
            self.assertEqual(analysis.analyse(self.registry,self.complex_id,20)['action'],'blocked')
            backend.assert_not_called()

    def test_change_during_analysis_is_an_incident_not_a_decision(self):
        def changing(evidence, output):
            result = self.fake_backend(evidence, output)
            self.dhdl().write_text(self.dhdl().read_text().replace('1000.0000 0','1000.0000 1'))
            return result
        with self.assertRaisesRegex(ValueError,'snapshot changed'):
            self.analyse(backend=changing)
        self.assertEqual(analysis.current_decision(self.registry,self.complex_id)['action'],'blocked')

    def test_malformed_backend_results_cannot_authorize_extension(self):
        def invalid(evidence,output):
            result=self.fake_backend(evidence,output)
            (output/'ABFE_convergence.csv').write_text('Forward,Backward,data_fraction\n0,1,.5\n0,1,.5\n')
            return result
        with self.assertRaisesRegex(ValueError,'exactly one'):
            self.analyse(backend=invalid)
        self.assertEqual(analysis.current_decision(self.registry,self.complex_id)['action'],'blocked')

    def test_output_tampering_and_protocol_change_block_decisions(self):
        record=self.analyse()
        out=Path(record['output'])/'ABFE_summary.csv'
        out.write_text('changed result\n')
        self.assertEqual(analysis.current_decision(self.registry,self.complex_id)['action'],'blocked')
        p=dict(analysis.protocol(),increment_ps=10000)
        with patch('abfe.analysis.protocol',return_value=p),self.assertRaisesRegex(ValueError,'protocol changed'):
            analysis.verify(record,self.spec())

    def test_scan_preserves_records_and_keeps_incidents_open(self):
        record=self.analyse()
        analysis.incident(self.registry,self.complex_id,'restraint_provenance','Needs explicit review')
        campaign.scan(self.registry)
        _,rows=campaign.read_registry(self.registry)
        row=next(r for r in rows if r['spec']['id']==self.complex_id)
        self.assertEqual(row['analysis_record']['id'],record['id'])
        self.assertEqual(row['observation']['analysis']['analysed_ns'],20)
        self.assertTrue(row['incidents'])
        self.assertEqual(analysis.current_decision(self.registry,self.complex_id)['action'],'blocked')

    def review(self,c,s):
        identity=dict(ligand_chemical_state='reviewed state',ligand_parameters='sha256:parameters',force_fields='historical protocol',
                      solvent_conditions='reviewed conditions',temperature_K=298.15,charge_treatment='reviewed treatment')
        path=self.work/'compatibility.json'
        path.write_text(json.dumps(dict(schema_version=1,reviewed_by='test reviewer',complex_analysis_id=c['id'],solvent_analysis_id=s['id'],
                                       complex=identity,solvent=dict(identity),evidence=[analysis.fingerprint(self.correction)])))
        return path

    def test_shared_solvent_result_uncertainty_and_replacement_invalidation(self):
        c=self.analyse()
        s=self.analyse(self.solvent_id)
        bound=analysis.binding(self.registry,self.complex_id,self.review(c,s))
        self.assertEqual(bound['result']['estimate_kcal_mol'],-3)
        self.assertEqual(bound['result']['shared_solvent_variance'],4)
        self.assertAlmostEqual(bound['result']['uncertainty_kcal_mol'],math.sqrt(8))
        self.analyse(self.solvent_id)
        row=next(r for r in campaign.read_registry(self.registry)[1] if r['spec']['id']==self.complex_id)
        self.assertEqual(row['binding_result']['state'],'outdated')
        self.assertTrue((Path(bound['output'])/'record.json').exists())

    def test_incompatible_or_unknown_solvent_review_cannot_pass(self):
        c=self.analyse();s=self.analyse(self.solvent_id)
        path=self.review(c,s)
        content=json.loads(path.read_text());content['solvent']['charge_treatment']='different'
        path.write_text(json.dumps(content))
        with self.assertRaisesRegex(ValueError,'incompatible'):analysis.check_compatibility(path,c,s)
        content['solvent']=dict(content['complex']);content['solvent']['ligand_parameters']='unknown'
        path.write_text(json.dumps(content))
        with self.assertRaisesRegex(ValueError,'incompatible'):analysis.check_compatibility(path,c,s)

    def historical_files(self):
        root=Path(self.spec()['path']).parent/'analysis'
        directory=root/'complex'
        directory.mkdir(parents=True,exist_ok=True)
        (directory/'ABFE_summary.csv').write_text(',,MBAR,MBAR_Error\nStages,TOTAL,10.0,2.0\n')
        (directory/'ABFE_convergence.csv').write_text(',Forward,Backward,data_fraction\n0,0,3,.5\n')
        (root/'abfe_results.csv').write_text('state,MBAR,MBAR_Error\ntotal,-3,4\n')
        return root

    def test_historical_preview_is_readonly_and_import_is_idempotent(self):
        root=self.historical_files()
        before=self.registry.read_bytes()
        source={str(p):p.read_bytes() for p in root.rglob('*.csv')}
        preview=analysis.import_analysis(self.registry,self.complex_id,plan=True)
        self.assertEqual(self.registry.read_bytes(),before)
        self.assertFalse((self.work/'analyses').exists())
        self.assertEqual(preview['analysed_ns'],None)
        self.assertEqual(preview['files'][1]['table']['reported_absolute_half_difference'],3)
        first=analysis.import_analysis(self.registry,self.complex_id)
        second=analysis.import_analysis(self.registry,self.complex_id)
        self.assertEqual(first['id'],second['id'])
        self.assertEqual(second['action'],'already_imported')
        with sqlite3.connect(self.registry) as db:
            self.assertEqual(db.execute('select count(*) from analyses').fetchone()[0],1)
        self.assertEqual(source,{str(p):p.read_bytes() for p in root.rglob('*.csv')})
        metadata,rows=campaign.read_registry(self.registry)
        row=next(r for r in rows if r['spec']['id']==self.complex_id)
        self.assertEqual(row['observation']['analysis']['state'],'historical_imported')
        self.assertEqual(campaign.presentation(row,metadata)['convergence'],'unverified')
        self.assertIn('Historical analysis imported',campaign.report_html(metadata,rows))
        self.assertEqual(analysis.current_decision(self.registry,self.complex_id)['action'],'blocked')

    def test_historical_duration_amendment_is_displayed_without_convergence(self):
        self.historical_files()
        original=analysis.import_analysis(self.registry,self.complex_id)
        amended=dict(original, id='historical-duration-test', analysed_ns=20, supersedes=original['id'],
                     duration_assignment=dict(rule='same_calendar_date'))
        amended.pop('action',None)
        output=self.work/'amended'
        output.mkdir()
        amended['output']=str(output)
        analysis.persist(self.registry,amended,output)
        metadata,rows=campaign.read_registry(self.registry)
        row=next(r for r in rows if r['spec']['id']==self.complex_id)
        self.assertEqual(row['observation']['analysis']['analysed_ns'],20)
        self.assertEqual(campaign.presentation(row,metadata)['convergence'],'unverified')
        self.assertIn('Analysed cutoff: 20 ns',campaign.report_html(metadata,rows))
        self.assertIn('same_calendar_date',campaign.report_html(metadata,rows))
        self.assertEqual(analysis.current_decision(self.registry,self.complex_id)['action'],'blocked')
        with sqlite3.connect(self.registry) as db:
            self.assertEqual(db.execute('select count(*) from analyses').fetchone()[0],2)

    def test_historical_convergence_labels_do_not_authorize_actions(self):
        self.historical_files()
        original=analysis.import_analysis(self.registry,self.complex_id)
        for index,state in enumerate(('historically_converged','historically_unconverged')):
            amended=dict(original,id=f'historical-convergence-{index}',analysed_ns=20,convergence=state)
            amended.pop('action',None)
            output=self.work/f'amended-{index}'
            output.mkdir()
            amended['output']=str(output)
            analysis.persist(self.registry,amended,output)
            metadata,rows=campaign.read_registry(self.registry)
            row=next(r for r in rows if r['spec']['id']==self.complex_id)
            label='converged (historical)' if index==0 else 'unconverged (historical)'
            self.assertEqual(campaign.presentation(row,metadata)['convergence'],label)
            self.assertEqual(campaign.presentation(row,metadata)['stage'],'analysis')
            self.assertIn(label,campaign.report_html(metadata,rows))
            self.assertEqual(campaign.summarize_runs(metadata,rows)['datasets'][0]['converged'],0)
            self.assertEqual(analysis.current_decision(self.registry,self.complex_id)['action'],'blocked')

    def test_historical_import_preserves_validated_analysis_and_open_incidents(self):
        managed=self.analyse()
        self.historical_files()
        analysis.incident(self.registry,self.complex_id,'restraint_provenance','Existing unresolved provenance')
        analysis.import_analysis(self.registry,self.complex_id)
        row=next(r for r in campaign.read_registry(self.registry)[1] if r['spec']['id']==self.complex_id)
        self.assertEqual(row['analysis_record']['id'],managed['id'])
        self.assertEqual(row['observation']['analysis']['state'],'validated')
        self.assertEqual(row['incidents'][0]['status'],'open')
        self.assertEqual(row['historical_analysis']['kind'],'historical')

    def test_historical_import_preserves_linked_solvent_origin(self):
        root=self.historical_files()
        old=self.source/'legacy-v1/analysis/ligand'
        old.mkdir(parents=True)
        (old/'ABFE_summary.csv').write_text(',,MBAR,MBAR_Error\nStages,TOTAL,5,1\n')
        (root/'ligand').symlink_to(old,target_is_directory=True)
        record=analysis.import_analysis(self.registry,self.complex_id)
        reference=record['shared_solvent']
        self.assertEqual(reference['path'],str(old))
        self.assertEqual(reference['compatibility'],'unverified')
        self.assertEqual(reference['files'][0]['path'],str(old/'ABFE_summary.csv'))
        solvent=analysis.import_analysis(self.registry,self.solvent_id)
        self.assertEqual(solvent['files'][0]['identity']['path'],reference['files'][0]['path'])

    def test_bad_historical_results_block_and_record_incident(self):
        root=self.historical_files()
        path=root/'complex/ABFE_convergence.csv'
        path.write_text('Forward,Backward,data_fraction\n0,1,.5\n0,1,.5\n')
        before=self.registry.read_bytes()
        with self.assertRaisesRegex(ValueError,'Duplicate convergence'):
            analysis.import_analysis(self.registry,self.complex_id,plan=True)
        self.assertEqual(self.registry.read_bytes(),before)
        with self.assertRaisesRegex(ValueError,'incident recorded'):
            analysis.import_analysis(self.registry,self.complex_id)
        with sqlite3.connect(self.registry) as db:
            self.assertEqual(db.execute("select status from incidents where code='analysis_import'").fetchone()[0],'open')
        self.historical_files()
        with self.assertRaisesRegex(ValueError,'Resolve the previous'):
            analysis.import_analysis(self.registry,self.complex_id)

    def test_historical_csv_rejects_missing_nonfinite_and_conflicting_results(self):
        path=self.work/'ABFE_summary.csv'
        for text in ('other\n1\n','state,MBAR,MBAR_Error\ntotal,nan,1\n',
                     'state,MBAR,MBAR_Error\ntotal,1,-1\n','state,MBAR,MBAR_Error\ntotal,1,1\ntotal,2,1\n'):
            path.write_text(text)
            with self.assertRaises(ValueError):analysis.historical_csv(path)

    def test_historical_cli_preview(self):
        self.historical_files()
        before=self.registry.read_bytes()
        out=io.StringIO()
        with redirect_stdout(out):
            campaign.main(['--registry',str(self.registry),'import-analysis',self.complex_id,'--plan'])
        self.assertEqual(json.loads(out.getvalue())['action'],'preview')
        self.assertEqual(self.registry.read_bytes(),before)

    @unittest.skipUnless(os.environ.get('ABFLOW_SCIENTIFIC_TEST') == '1', 'set ABFLOW_SCIENTIFIC_TEST=1 for the alchemlyb integration test')
    def test_scientific_backend_reads_in_place_and_honours_cutoff(self):
        import numpy as np
        rng=np.random.default_rng(20261016)
        for state in range(2):
            path=self.dhdl(state)
            mdp=path.parent/f'coul.prod.{state:02d}.mdp'
            mdp.write_text(mdp.read_text().replace('nstdhdl = 500000','nstdhdl = 50000')+'vdw-lambdas = 0 0\n')
            text=('@ xaxis label "Time (ps)"\n'
                  f'@ subtitle "T = 298.15 (K) state {state}: (coul-lambda, vdw-lambda) = ({state:.4f}, 0.0000)"\n'
                  '@ s0 legend "Total Energy (kJ/mol)"\n'
                  f'@ s1 legend "dH/d\\xl\\f{{}} coul-lambda = {state:.4f}"\n'
                  '@ s2 legend "dH/d\\xl\\f{} vdw-lambda = 0.0000"\n'
                  '@ s3 legend "\\xD\\f{}H \\xl\\f{} to (0.0000, 0.0000)"\n'
                  '@ s4 legend "\\xD\\f{}H \\xl\\f{} to (1.0000, 0.0000)"\n')
            rt=0.00831446261815324*298.15
            for t,x in zip(range(0,20001,100),rng.normal(state,1,201)):
                u=.5*(x-state)**2
                text+=f'{t:.4f} {u*rt:.10f} {(state-x)*rt:.10f} 0 {(.5*x*x-u)*rt:.10f} {(.5*(x-1)**2-u)*rt:.10f}\n'
            path.write_text(text)
        before=[self.dhdl(i).read_bytes() for i in range(2)]
        record=analysis.analyse(self.registry,self.complex_id,16)
        self.assertEqual(record['state'],'validated')
        self.assertEqual(record['evidence']['cutoff_ps'],16000)
        self.assertTrue(all(w['endpoint_ps']==20000 for w in record['evidence']['windows']))
        self.assertTrue(all(0<n<=160 for n in record['result']['retained_samples']))
        self.assertLess(abs(record['result']['estimate_kcal_mol']),.2)
        self.assertEqual(record['decision']['action'],'stop_converged')
        self.assertEqual([self.dhdl(i).read_bytes() for i in range(2)],before)
        self.assertFalse(list(Path(record['output']).glob('*.xvg')))
        self.assertTrue(record['software']['source_files'])

    def test_cli_plan_and_decision_do_not_submit_jobs(self):
        out=io.StringIO()
        with redirect_stdout(out):campaign.main(['--registry',str(self.registry),'analyse',self.complex_id,'--cutoff-ns','16','--plan'])
        self.assertEqual(json.loads(out.getvalue())['decision']['action'],'analyse')
        with redirect_stdout(io.StringIO()),redirect_stderr(io.StringIO()),self.assertRaises(SystemExit) as exit_:
            campaign.main(['--registry',str(self.registry),'decide',self.complex_id])
        self.assertEqual(exit_.exception.code,1)


if __name__=='__main__':unittest.main()
