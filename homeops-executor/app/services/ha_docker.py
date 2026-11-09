"""Pinned broker operations selected only after the Executor's own approval gate."""
import os


def call(service, action):
    if 'HA_PG_CONFIG' not in os.environ:
        return None
    from ha_egress.client import configuration, route
    if configuration('homeops-executor') is None:
        return None
    if service not in {'portal-web','system-agent','crawler-worker','youtube-memo','book-memo','car-care-worker','homeops-executor'}:
        raise ValueError('service_not_allowed')
    if action not in {'restart', 'inspect'}:
        raise OSError('docker_broker_unavailable')
    result = route('homeops-executor', 'executor.docker.' + action + '.' + service, {})
    if (type(result) is not dict or set(result) != {'service','status','health','started_at'}
            or result['service'] != service or type(result['status']) is not str
            or result['status'] not in {'created','running','paused','restarting','removing','exited','dead'}
            or type(result['health']) is not str or result['health'] not in {'none','starting','healthy','unhealthy'}
            or type(result['started_at']) is not str or len(result['started_at']) > 40
            or (action == 'restart' and result['status'] != 'running')):
        raise OSError('docker_broker_unavailable')
    # Absent metrics are unknown; never manufacture successful resource readings.
    snapshot = {key:result[key] for key in ('status','health','started_at')}
    snapshot.update(exit_code=None,cpu_percent=None,memory_percent=None)
    if action == 'restart':
        return {'service':service,'status':result['status'],'container':snapshot}
    return {'service':service,'container':snapshot,'logs':[]}
