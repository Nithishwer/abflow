"""Deterministic scheduler telemetry, containing no simulation actions."""
from datetime import datetime, timezone
import re

INTERVAL_SECONDS = 300
RUNNING = {'RUNNING', 'COMPLETING', 'SUSPENDED', 'SIGNALING', 'STAGE_OUT'}
QUEUED = {'PENDING', 'CONFIGURING', 'REQUEUED', 'REQUEUE_FED', 'REQUEUE_HOLD', 'RESV_DEL_HOLD', 'RESIZING'}


def job_counts(record, qos=None):
    scheduler = record.get('scheduler', {})
    if scheduler.get('state') != 'observed':
        return None
    jobs = [j for j in scheduler.get('jobs', []) if qos is None or j.get('qos') == qos]
    running = sum(j['state'] in RUNNING for j in jobs)
    queued = sum(j['state'] in QUEUED for j in jobs)
    return dict(running=running, queued=queued, other=len(jobs)-running-queued, submitted=len(jobs))


def parse_limits(text):
    limits = {}
    for line in text.splitlines():
        if not line.strip():
            continue
        name, running, submitted, *_ = line.strip().split('|')
        if name in ('standard', 'lowpriority'):
            if not running.isdigit() or not submitted.isdigit():
                raise ValueError('Missing numeric QoS limits')
            limits[name] = dict(running=int(running), submitted=int(submitted))
    if 'standard' not in limits:
        raise ValueError('Missing standard QoS limits')
    return limits


def expand_nodes(expression):
    """Expand Slurm's bounded hostlist notation without a shell or remote lookup."""
    parts = re.split(r',(?=[^\]]*(?:\[|$))', expression)
    result = []
    for part in parts:
        match = re.search(r'\[([^\]]+)\]', part)
        if match:
            for group in match[1].split(','):
                if re.fullmatch(r'\d+-\d+', group):
                    low, high = group.split('-')
                    if int(high) < int(low) or int(high)-int(low) > 10000:
                        raise ValueError('Invalid host range')
                    values = [str(i).zfill(len(low)) for i in range(int(low), int(high)+1)]
                elif group.isdigit():
                    values = [group]
                else:
                    raise ValueError('Unrecognized host range')
                for value in values:
                    result.extend(expand_nodes(part[:match.start()]+value+part[match.end():]))
        elif re.fullmatch(r'[A-Za-z0-9_.-]+', part):
            result.append(part)
        else:
            raise ValueError('Unrecognized node name')
        if len(result) > 10000:
            raise ValueError('Hostlist too large')
    return result


def parse_ownership(text):
    jobs, assignments = [], []
    if text.strip() == 'No jobs in the system':
        return dict(state='observed', jobs=[], assignments=[])
    for line in text.splitlines():
        if not line.strip():
            continue
        fields = dict(re.findall(r'(\w+)=(\S*)', line))
        if not line.startswith('JobId=') or not all(key in fields for key in ('JobId', 'UserId', 'JobState', 'Partition')):
            raise ValueError('Unrecognized job ownership output')
        user = fields['UserId'].split('(')[0]
        job = dict(job_id=fields['JobId'], user=user, state=fields['JobState'], partition=fields['Partition'])
        jobs.append(job)
        # Retain only ownership/resource fields, not other users' paths or commands.
        for block in re.finditer(r'\bNodes=(\S+)\s+CPU_IDs=.*?\bGRES=(\S*)', line):
            gres = block[2]
            count = sum(int(n) for n in re.findall(r'(?:^|,)gpu(?::[^:,()]+)?:(\d+)(?:\([^)]*\))?(?=,|$)', gres))
            for node in expand_nodes(block[1]):
                assignments.append(dict(job_id=job['job_id'], user=user, node=node, gpus=count))
    return dict(state='observed', jobs=jobs, assignments=assignments)


def utilization(record):
    capacity = record.get('capacity', {})
    if capacity.get('state') != 'observed':
        return None
    totals = capacity['totals']
    values = {'Available': totals['available'], 'Unavailable': totals['unavailable']}
    ownership = record.get('ownership', {})
    assignments = ownership.get('assignments', []) if ownership.get('state') == 'observed' else []
    nodes = capacity.get('routine_nodes', [])
    if not nodes:
        values['Unattributed'] = totals['used']
        return values
    for node in nodes:
        owners = {}
        for item in assignments:
            if item['node'] == node['name']:
                owners[item['user']] = owners.get(item['user'], 0) + item['gpus']
        attributed = sum(owners.values())
        if attributed > node['used']:
            # Queries are sequential: a changing allocation is a gap in attribution.
            owners, attributed = {}, 0
        for user, count in owners.items():
            values[user] = values.get(user, 0) + count
        values['Unattributed'] = values.get('Unattributed', 0) + node['used']-attributed
    return values


def sample_slot(timestamp=None):
    return int((timestamp or datetime.now(timezone.utc)).timestamp()) // INTERVAL_SECONDS * INTERVAL_SECONDS
