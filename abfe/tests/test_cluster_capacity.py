"""Tiny synthetic Slurm snapshots; no SSH or simulation work."""
import unittest
from unittest.mock import patch
from abfe.cluster_capacity import parse_capacity, gpu_count


class CapacityTests(unittest.TestCase):
    def node(self, name='n1', state='MIXED', used=1, cpu=8):
        return f'NodeName={name} CPUTot=32 CPUAlloc={cpu} State={state} Partitions=g-ran,g-ranshort,gpu4-biggin Gres=gpu:A40:4(S:0-1) GresUsed=gpu:A40:{used}(IDX:N/A)'

    def parse(self, text):
        pools = {'ranvier': [('g-ran', 'A40 GPUs', 'Preferred', True), ('gpu4-biggin', 'A40 GPUs', 'Preferred', True), ('g-ranshort', 'A40 GPUs', 'Short', False)]}
        with patch('abfe.cluster_capacity.POOLS', pools):
            return parse_capacity(text, 'ranvier')

    def test_overlap_is_deduplicated_and_short_pool_is_separate(self):
        result = self.parse(self.node())
        self.assertEqual(result['totals'], dict(total=4, used=1, available=3, unavailable=0))
        self.assertEqual([p['total'] for p in result['pools']], [4, 4, 4])
        self.assertFalse(result['pools'][-1]['routine'])

    def test_blocked_nodes_keep_used_counts_but_never_offer_free_gpus(self):
        for state in ['MIXED+DRAIN', 'IDLE+RESERVED', 'IDLE+PLANNED', 'IDLE+POWERED_DOWN', 'DOWN+NOT_RESPONDING', 'UNKNOWN']:
            with self.subTest(state=state):
                totals = self.parse(self.node(state=state))['totals']
                self.assertEqual(totals, dict(total=4, used=1, available=0, unavailable=3))

    def test_cpu_exhaustion_blocks_remaining_gpu_capacity(self):
        self.assertEqual(self.parse(self.node(cpu=32))['totals']['available'], 0)

    def test_drained_gpu_devices_are_not_available(self):
        self.assertEqual(self.parse(self.node() + ' GresDrain=gpu:A40:2')['totals'],
                         dict(total=4, used=1, available=1, unavailable=2))

    def test_effective_cpu_exhaustion_blocks_gpu_capacity(self):
        self.assertEqual(self.parse(self.node(cpu=8) + ' CPUEfctv=8')['totals']['available'], 0)

    def test_invalid_allocation_and_missing_gpu_usage_fail_closed(self):
        for text in [self.node(used=5), self.node(cpu=33), self.node().replace('GresUsed=', 'Unknown='), self.node()+'\n'+self.node(), '', 'banner']:
            with self.subTest(text=text), self.assertRaises((ValueError, KeyError)):
                self.parse(text)

    def test_missing_partition_is_not_zero_capacity(self):
        with self.assertRaises(ValueError):
            self.parse(self.node().replace('Partitions=g-ran,g-ranshort,gpu4-biggin', 'Partitions=other'))

    def test_gpu_parser_counts_devices_not_socket_or_index_numbers(self):
        self.assertEqual(gpu_count('gpu:A40:4(S:0-1),gpu:L40S:2(S:0)', 'A40'), 4)
        self.assertEqual(gpu_count('gpu:L40S:2(IDX:0-1)', 'L40S'), 2)

    def test_archer_counts_nodes_instead_of_hardware_threads(self):
        text = '\n'.join([f'NodeName=n{i} CPUTot=256 CPUAlloc={cpu} State={state} Partitions=standard' for i, cpu, state in [(1,256,'ALLOCATED'),(2,0,'IDLE'),(3,0,'IDLE+RESERVED'),(4,1,'MIXED')]])
        totals = parse_capacity(text, 'archer')['totals']
        self.assertEqual(totals, dict(total=4, used=2, available=1, unavailable=1))


if __name__ == '__main__':
    unittest.main()
