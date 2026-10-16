"""Local cluster snapshots and measured history, rendered without chart dependencies."""
from datetime import datetime, timezone
import html
from .cluster_monitor import INTERVAL_SECONDS, job_counts, utilization

PALETTE = ['#ff0059', '#ffd166', '#9500ff', '#ff8c00', '#b4e600', '#ffa8d9', '#cbb7ff', '#d8e6b4']
COLORS = {'Available': '#0fffdb', 'Unavailable': '#535b66', 'Unattributed': '#8a9199', 'Running': '#ff0059', 'Queued': '#ffd166', 'Other': '#ff8c00'}


def escape(value):
    return html.escape(str(value), quote=True)


def time_label(value):
    return datetime.fromtimestamp(value, timezone.utc).strftime('%H:%M')


def chart(samples, kind, alias, ceiling=None):
    points = []
    for sample in samples:
        record = sample['record']
        values = job_counts(record, 'standard' if alias == 'archer' else None) if kind == 'jobs' else utilization(record)
        if kind == 'jobs' and values is not None:
            values = {'Running': values['running'], 'Queued': values['queued'], 'Other': values['other']}
        points.append((sample['slot'], values, record.get('checked_at', '')))
    measured = [(t, v, checked) for t, v, checked in points if v is not None]
    if not measured:
        return '<div class="chart-empty">Waiting for the first measured sample</div>'
    end = max(t for t, _, _ in points) + INTERVAL_SECONDS
    start = max(end-86400, min(t for t, _, _ in points)-INTERVAL_SECONDS)
    start = min(start, end-3600)
    keys = sorted({key for _, values, _ in measured for key in values if values[key] and key not in ('Available', 'Unavailable', 'Unattributed')})
    keys += [key for key in ('Unattributed', 'Available', 'Unavailable') if any(values.get(key, 0) for _, values, _ in measured)]
    if kind == 'jobs':
        keys = ['Running', 'Queued'] + (['Other'] if any(v.get('Other') for _, v, _ in measured) else [])
    colors = {key: COLORS.get(key, PALETTE[i % len(PALETTE)]) for i, key in enumerate(keys)}
    maximum = max(ceiling or 1, *(sum(values.values()) for _, values, _ in measured))
    left, top, width, height = 34, 10, 650, 100
    x = lambda t: left + width*(t-start)/(end-start)
    y = lambda n: top + height-height*n/maximum
    svg = [f'<svg class="history-chart" viewBox="0 0 700 140" role="img" aria-label="{escape(alias)} {escape(kind)} history in UTC">']
    for amount in (0, maximum):
        svg.append(f'<path d="M{left} {y(amount)}H{left+width}" class="grid-line"/><text x="{left-6}" y="{y(amount)+4}" text-anchor="end">{amount}</text>')
    if ceiling:
        svg.append(f'<path d="M{left} {y(ceiling)}H{left+width}" class="limit-line"/>')
    for slot, values, checked in points:
        if values is None:
            continue
        offset = 0
        tooltip = f'{checked} · ' + ', '.join(f'{key}: {values.get(key, 0)}' for key in keys)
        bar_width = width*INTERVAL_SECONDS/(end-start)
        for key in keys:
            count = values.get(key, 0)
            if count:
                svg.append(f'<rect x="{x(slot):.2f}" y="{y(offset+count):.2f}" width="{bar_width:.2f}" height="{height*count/maximum:.2f}" fill="{colors[key]}"><title>{escape(tooltip)}</title></rect>')
            offset += count
        # Keep genuine zero samples visible without inventing use.
        if not offset:
            svg.append(f'<path d="M{x(slot):.2f} {y(0)}h{bar_width:.2f}" stroke="#0fffdb" stroke-width="3"><title>{escape(tooltip)}</title></path>')
    for stamp, anchor in ((start, 'start'), (end, 'end')):
        svg.append(f'<text x="{x(stamp):.2f}" y="133" text-anchor="{anchor}">{time_label(stamp)} UTC</text>')
    svg.append('</svg>')
    legend = ''.join(f'<span class="legend-item"><i class="dot" style="--color:{colors[key]}" aria-hidden="true"></i>{escape(key)}</span>' for key in keys)
    return ''.join(svg)+f'<div class="legend chart-legend">{legend}</div><p class="chart-caption">{len(measured)} measured samples · every 5 min · gaps stay empty</p>'


def cluster_cards(metadata, aliases, display_time, connection_state):
    cards = []
    history = metadata.get('cluster_history', [])
    for alias in aliases:
        record = metadata.get('connections', {}).get(alias, {})
        checked = record.get('checked_at', '')
        state = connection_state(record)
        capacity = record.get('capacity', {})
        limits = record.get('limits', {})
        standard = limits.get('qos', {}).get('standard', {}) if limits.get('state') == 'observed' else {}
        counts = job_counts(record, 'standard' if alias == 'archer' else None)
        running_limit, submitted_limit = standard.get('running'), standard.get('submitted')
        values = counts or {}
        running = values.get('running', '?')
        queued = values.get('queued', '?')
        submitted = values.get('submitted', '?')
        metrics = '<div class="job-metrics">'
        for value, label in ((f'{running} / {running_limit}' if running_limit is not None else running, 'Your running jobs'),
                             (queued, 'Your queued jobs'),
                             (f'{submitted} / {submitted_limit}' if submitted_limit is not None else submitted, 'Your submitted jobs')):
            metrics += f'<div><strong>{escape(value)}</strong><span>{label}</span></div>'
        metrics += '</div>'
        if counts and submitted_limit is not None:
            metrics += f'<p class="headroom"><strong>{max(0, submitted_limit-counts["submitted"])}</strong> more jobs may be submitted</p>'
        else:
            metrics += '<p class="subtle">Submission ceiling: not reported</p>'
        if alias == 'archer':
            metrics += '<p class="subtle">Paid standard QoS · <a href="https://docs.archer2.ac.uk/user-guide/scheduler/#quality-of-service-qos">Limits</a></p>'
            all_counts = job_counts(record)
            if all_counts and counts:
                metrics += f'<p class="subtle">Other QoS jobs: {all_counts["submitted"]-counts["submitted"]}</p>'
        if capacity.get('state') == 'observed' and alias != 'archer':
            totals = capacity['totals']
            metrics += f'<div class="gpu-snapshot"><strong>{totals["used"]} / {totals["total"]}</strong> eligible GPUs allocated<br><span>{totals["available"]} available · {totals["unavailable"]} unavailable</span></div>'
        samples = [sample for sample in history if sample['cluster'] == alias]
        job_plot = chart(samples, 'jobs', alias, submitted_limit)
        resource_plot = '' if alias == 'archer' else '<h3>Eligible GPUs · by user</h3>'+chart(samples, 'gpus', alias)
        node_rows = []
        ownership = record.get('ownership', {})
        assignments = ownership.get('assignments', [])
        for node in capacity.get('routine_nodes', []):
            owners = ', '.join(f'{item["user"]} · {item["gpus"]} GPU · job {item["job_id"]}' for item in assignments if item['node'] == node['name'] and item['gpus'])
            node_rows.append(f'<tr><td>{escape(node["name"])}</td><td>{escape(owners or ("Unattributed" if node["used"] else "—"))}</td><td>{node["used"]}</td><td>{node["available"]}</td><td>{node["unavailable"]}</td></tr>')
        details = ''
        if node_rows:
            details = '<details class="node-details"><summary>Current node assignments</summary><div class="table"><table><thead><tr><th>Node</th><th>Users / allocations</th><th>Allocated</th><th>Available</th><th>Unavailable</th></tr></thead><tbody>'+''.join(node_rows)+'</tbody></table></div></details>'
        pool_rows = ''.join('<tr>'+''.join(f'<td>{escape(pool[key])}</td>' for key in ('partition','used','available','total','policy'))+'</tr>' for pool in capacity.get('pools', []) if alias != 'archer')
        if pool_rows:
            details += '<details class="node-details"><summary>Partition snapshot</summary><div class="table"><table><thead><tr><th>Partition</th><th>Allocated</th><th>Available</th><th>Total</th><th>Policy</th></tr></thead><tbody>'+pool_rows+'</tbody></table></div></details>'
        errors = [part.get('error') for part in (record, capacity, limits, ownership, record.get('scheduler', {})) if part.get('error')]
        error_html = ''.join(f'<p class="attention">{escape(error)}</p>' for error in errors)
        cards.append(f'<article class="cluster-card" data-capacity-checked="{escape(checked)}"><div class="topline"><h2>{alias}</h2><span class="snapshot-state">{escape(state)}</span></div>'
                     f'<p class="subtle">Checked: {escape(display_time(checked))}</p><div class="cluster-layout"><div class="cluster-snapshot">{metrics}{error_html}</div>'
                     f'<div class="cluster-history"><h3>Your jobs · {"standard QoS" if alias == "archer" else "all partitions"}</h3>{job_plot}{resource_plot}</div></div>{details}</article>')
    return ''.join(cards)
