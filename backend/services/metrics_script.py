"""Remote Linux metrics, measured over an interval rather than since boot."""
import shlex

METRICS_CODE = '''import json, os, time
def cpu_sample():
    with open('/proc/stat') as stream:
        values = [int(value) for value in stream.readline().split()[1:]]
    # guest/guest_nice are already included in user/nice.
    return sum(values[:8]), values[3] + values[4]
before = cpu_sample()
time.sleep(0.2)
after = cpu_sample()
delta_total, delta_idle = after[0] - before[0], after[1] - before[1]
if delta_total <= 0:
    raise ValueError('CPU sample interval was invalid')
with open('/proc/meminfo') as stream:
    memory = {line.split(':', 1)[0]: int(line.split()[1]) for line in stream}
if 'MemAvailable' not in memory or memory.get('MemTotal', 0) <= 0:
    raise ValueError('Memory metrics are unavailable')
with open('/proc/loadavg') as stream:
    load1 = float(stream.read().split()[0])
disk = os.statvfs('/')
print(json.dumps({'cpu_pct': round(max(0, min(100, 100 * (1 - delta_idle / delta_total))), 1),
    'mem_pct': round(100 * (1 - memory['MemAvailable'] / memory['MemTotal']), 1),
    'load1': load1, 'disk_pct': round(100 * (1 - disk.f_bavail / disk.f_blocks), 1)}))
'''
METRICS_SCRIPT = "python3 -c " + shlex.quote(METRICS_CODE)
