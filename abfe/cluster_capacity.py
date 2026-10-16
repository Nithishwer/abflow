"""Read-only Slurm capacity snapshots; counts are allocations, not device load."""
import re


# Each partition is displayed separately; routine totals deduplicate its nodes.
POOLS = {
    'archer': [('standard', 'CPU nodes', 'HREX production', True)],
    'ranvier': [('g-ran', 'A40 GPUs', 'Preferred', True),
                ('gpu4-biggin', 'A40 GPUs', 'Preferred', True),
                ('gpuL40S-biggin', 'L40S GPUs', 'Eligible', True),
                ('g-ranshort', 'A40 GPUs', 'Short wall time; not routine', False),
                ('g-ranultrashort', 'A40 GPUs', 'Very short wall time; not routine', False),
                ('g-kha', 'A40 GPUs', 'Explicit approval required; minimal conditional use', False),
                ('gpu-khalid', 'A40 GPUs', 'Minimal conditional use; no approval workaround', False)],
    'soma': [('gpu3-A40', 'A40 GPUs', 'Eligible', True)],
}


def gpu_count(value, model):
    matches = re.findall(r'(?:^|,)gpu:' + re.escape(model) + r':(\d+)(?:\([^)]*\))?(?=,|$)', value)
    if not matches:
        raise ValueError('Missing or unrecognized ' + model + ' GPU count')
    return sum(map(int, matches))


def parse_capacity(text, alias):
    nodes = {}
    for line in text.splitlines():
        if not line.strip():
            continue
        if not line.startswith('NodeName='):
            raise ValueError('Unrecognized Slurm node output')
        fields = dict(re.findall(r'(\w+)=(\S*)', line))
        name = fields['NodeName']
        if name in nodes:
            raise ValueError('Duplicate node in capacity output')
        nodes[name] = fields
    if not nodes:
        raise ValueError('Empty Slurm node output')
    pools, routine = [], {}
    for partition, unit, policy, included in POOLS[alias]:
        selected = {}
        for name, node in nodes.items():
            if partition not in node.get('Partitions', '').split(','):
                continue
            states = set(node.get('State', '').split('+'))
            # Exclude drained, reserved, planned, powered-down and unknown nodes.
            ready = states <= {'IDLE', 'MIXED'} and bool(states)
            cpu_total, cpu_used = int(node['CPUTot']), int(node['CPUAlloc'])
            if not 0 <= cpu_used <= cpu_total:
                raise ValueError('Inconsistent CPU allocation on ' + name)
            if alias == 'archer':
                total, used = 1, int(cpu_used > 0)
            else:
                model = 'L40S' if unit == 'L40S GPUs' else 'A40'
                total = gpu_count(node.get('Gres', ''), model)
                used = gpu_count(node.get('GresUsed', ''), model)
                ready = ready and cpu_used < int(node.get('CPUEfctv', cpu_total))
            if not 0 <= used <= total:
                raise ValueError('Inconsistent allocation on ' + name)
            drained = 0
            if alias != 'archer' and node.get('GresDrain', 'N/A') not in ('N/A', '(null)', ''):
                drained = gpu_count(node['GresDrain'], model)
                if drained > total:
                    raise ValueError('Inconsistent drained GPU count on ' + name)
            free = max(0, total - used - drained) if ready else 0
            selected[name] = {'name': name, 'total': total, 'used': used, 'available': free, 'unavailable': total-used-free}
        if not selected:
            raise ValueError('Configured partition is missing: ' + partition)
        values = {key: sum(n[key] for n in selected.values()) for key in ('total', 'used', 'available', 'unavailable')}
        pools.append(dict(values, partition=partition, unit=unit, policy=policy, nodes=len(selected), routine=included))
        if included:
            routine.update(selected)
    totals = {key: sum(n[key] for n in routine.values()) for key in ('total', 'used', 'available', 'unavailable')}
    return dict(state='observed', pools=pools, totals=totals, routine_nodes=list(routine.values()) if alias != 'archer' else [], unit='CPU nodes' if alias == 'archer' else 'eligible GPUs')
