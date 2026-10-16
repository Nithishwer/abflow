"""Telemetry logic uses synthetic snapshots, never cluster compute jobs."""
from datetime import datetime, timezone
import unittest
from abfe.cluster_monitor import expand_nodes, parse_limits, parse_ownership, utilization, job_counts, sample_slot
from abfe.cluster_view import chart


class MonitorTests(unittest.TestCase):
    def test_paid_qos_counts_only_matching_jobs_across_accounts(self):
        record={'scheduler': {'state':'observed','jobs':[
            dict(state='RUNNING',qos='standard',account='a'),dict(state='PENDING',qos='standard',account='b'),
            dict(state='RUNNING',qos='lowpriority'),dict(state='CONFIGURING',qos='standard')]}}
        self.assertEqual(job_counts(record,'standard'),dict(running=1,queued=2,other=0,submitted=3))
        self.assertIsNone(job_counts({'scheduler':{'state':'unavailable'}}))

    def test_live_limits_require_numeric_standard_values(self):
        self.assertEqual(parse_limits('standard|16|64|\nlowpriority|16|16|')['standard'],dict(running=16,submitted=64))
        for value in ('standard||64|','lowpriority|16|16|','standard|16|unlimited|'):
            with self.subTest(value=value),self.assertRaises(ValueError):parse_limits(value)

    def test_hostlist_expansion(self):
        self.assertEqual(expand_nodes('ran-[01-03,05],scn01'),['ran-01','ran-02','ran-03','ran-05','scn01'])
        for value in ('ran-[09-01]','ran-[01-999999]','bad;command','ran-[a-c]'):
            with self.subTest(value=value),self.assertRaises(ValueError):expand_nodes(value)

    def test_actual_node_gpu_allocations_and_no_other_user_paths_retained(self):
        result=parse_ownership('JobId=42 UserId=alice(1001) JobState=RUNNING Partition=g-ran Nodes=ran-[01-02] CPU_IDs=0-7 Mem=0 GRES=gpu:A40:2(IDX:0-1) WorkDir=/private/alice Command=/private/script')
        self.assertEqual([a['gpus'] for a in result['assignments']],[2,2])
        self.assertNotIn('/private',str(result))
        self.assertEqual(parse_ownership('No jobs in the system')['assignments'],[])
        with self.assertRaises(ValueError):parse_ownership('malformed')

    def test_attribution_preserves_unknown_and_total_capacity(self):
        record={'capacity':{'state':'observed','totals':dict(used=3,available=2,unavailable=3,total=8),
                'routine_nodes':[dict(name='n1',used=3)]},'ownership':{'state':'observed','assignments':[dict(node='n1',user='alice',gpus=2)]}}
        values=utilization(record)
        self.assertEqual(values,dict(Available=2,Unavailable=3,Unattributed=1,alice=2))
        self.assertEqual(sum(values.values()),8)
        record['ownership']['assignments'][0]['gpus']=4
        self.assertEqual(utilization(record),dict(Available=2,Unavailable=3,Unattributed=3))
        record['ownership']['state']='unavailable'
        self.assertEqual(utilization(record)['Unattributed'],3)

    def test_five_minute_slots_do_not_invent_past_samples(self):
        self.assertEqual(sample_slot(datetime.fromtimestamp(1234,timezone.utc)),1200)
        self.assertIn('Waiting for the first measured sample',chart([], 'jobs','archer',64))
        record={'checked_at':'2026-10-16T15:00:00+00:00','scheduler':{'state':'observed','jobs':[]}}
        plot=chart([dict(slot=1200,record=record)],'jobs','archer',64)
        self.assertIn('1 measured samples',plot)
        self.assertIn('Running: 0',plot)
        self.assertIn('Queued: 0',plot)
        self.assertNotIn('<rect',plot,'Zero jobs should not create a nonzero filled bar')

    def test_chart_escapes_user_labels(self):
        record={'checked_at':'now','capacity':{'state':'observed','totals':dict(used=1,available=0,unavailable=0,total=1),
        'routine_nodes':[dict(name='n1',used=1)]},'ownership':{'state':'observed','assignments':[dict(node='n1',user='<script>',gpus=1)]}}
        plot=chart([dict(slot=1200,record=record)],'gpus','ranvier')
        self.assertNotIn('<script>',plot)
        self.assertIn('&lt;script&gt;',plot)


if __name__=='__main__':unittest.main()
