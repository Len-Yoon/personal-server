"""Fixed Executor routing; the Executor retains incident/approval enforcement."""
import hashlib
import json
from urllib.parse import urlsplit
from urllib.request import Request

from ha_runtime.config import SERVICES
from ha_runtime.effects import ProviderAck
from .hyundai_provider import BoundRequestTransport
from .providers import ProviderUnavailable


def _diagnostic(value, allowed):
    if type(value) is not dict or type(value.get('service')) is not str or value['service'] not in allowed:
        raise ProviderUnavailable()
    if (set(value) == {'service','container','logs','error'}
            and value['container'] == {'status':'unknown','health':'unknown'}
            and value['logs'] == [] and type(value['error']) is str):
        return {'service':value['service'],'container':{'status':'unknown','health':'unknown',
            'started_at':'','exit_code':None,'cpu_percent':None,'memory_percent':None},'logs':[]}
    if set(value) != {'service','container','logs'}:
        raise ProviderUnavailable()
    container = value['container']
    required = {'status','health','started_at','exit_code','cpu_percent','memory_percent'}
    if (type(container) is not dict or set(container) != required
            or container['status'] not in {'created','running','paused','restarting','removing','exited','dead','unknown'}
            or container['health'] not in {'none','starting','healthy','unhealthy','unknown'}
            or type(container['started_at']) is not str or len(container['started_at']) > 40
            or any(container[key] is not None and type(container[key]) not in {int,float}
                   for key in ('exit_code','cpu_percent','memory_percent')) or value['logs'] != []):
        raise ProviderUnavailable()
    return {'service':value['service'],'container':dict(container),'logs':[]}


class ExecutorForwarder:
    def __init__(self, *, url, node, secret, context, server_sha256, services):
        parsed = urlsplit(url)
        if (parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password
                or parsed.path not in {'','/'} or parsed.query or parsed.fragment or node not in {'A','B'}
                or type(secret) is not str or not 32 <= len(secret) <= 4096
                or type(server_sha256) is not str or len(server_sha256) != 64
                or any(c not in '0123456789abcdef' for c in server_sha256)
                or not services or not set(services) <= SERVICES):
            raise ProviderUnavailable()
        self._url = url.rstrip('/')
        self._origin = (parsed.hostname,parsed.port or 443)
        self._node, self._secret = node,secret
        self._context,self._pin = context,server_sha256
        self.services = frozenset(services)

    def _request(self, path, payload, request_id, *, timeout, deadline):
        headers = {'Content-Type':'application/json','X-HomeOps-Executor-Secret':self._secret,
                   'X-HomeOps-Target-Node':self._node}
        if request_id:
            headers['X-HomeOps-Request-Id'] = request_id
        raw = None if payload is None else json.dumps(payload,allow_nan=False,separators=(',',':')).encode()
        request = Request(self._url + path, data=raw, headers=headers,
                          method='GET' if payload is None and not request_id else 'POST')
        with BoundRequestTransport({self._origin},deadline,context=self._context,server_sha256=self._pin)(request,timeout=timeout) as response:
            from .client import _pairs
            value = json.loads(response.read(),object_pairs_hook=_pairs,
                               parse_constant=lambda _: (_ for _ in ()).throw(ProviderUnavailable()))
            return value,response.headers

    def diagnose(self, payload, *, timeout, deadline):
        if type(payload) is not dict or set(payload) != {'service'}:
            raise ProviderUnavailable()
        service = payload['service']
        if service is not None and (type(service) is not str or service not in self.services):
            raise ProviderUnavailable()
        value,headers = self._request('/v1/diagnostics' + ('/' + service if service else ''),None,None,
                                      timeout=timeout,deadline=deadline)
        if service:
            value = _diagnostic(value,self.services)
            if value['service'] != service:
                raise ProviderUnavailable()
            managed = sorted(self.services)
        else:
            declared = headers.get_all('X-HomeOps-Managed-Services')
            if declared is None or len(declared) != 1:
                raise ProviderUnavailable()
            names = [name.strip() for name in declared[0].split(',')]
            managed = sorted(set(names))
            if (len(managed) != len(names) or not set(managed) <= self.services
                    or type(value) is not list or len(value) != len(managed)):
                raise ProviderUnavailable()
            value = [_diagnostic(item,self.services) for item in value]
            if sorted(item['service'] for item in value) != managed:
                raise ProviderUnavailable()
        return ProviderAck('executor-diagnostics', {'value':value,'managed_services':managed})

    def restart(self, payload, *, timeout, deadline):
        if (type(payload) is not dict or set(payload) != {'incident_id','approval_token','service'}
                or type(payload['service']) is not str or payload['service'] not in self.services
                or any(type(payload[key]) is not str or not 0 < len(payload[key]) <= 4096
                       for key in ('incident_id','approval_token'))):
            raise ProviderUnavailable()
        value,_ = self._request('/v1/restarts',dict(payload,action='restart_container',target_node=self._node),None,
                                timeout=timeout,deadline=deadline)
        if type(value) is not dict or set(value) != {'service','status','container'}:
            raise ProviderUnavailable()
        projected = _diagnostic({'service':value['service'],'container':value['container'],'logs':[]},self.services)
        if value['service'] != payload['service'] or value['status'] != 'running' or projected['container']['status'] != 'running':
            raise ProviderUnavailable()
        return ProviderAck('executor-restart:' + hashlib.sha256(payload['incident_id'].encode()).hexdigest(),
                           {'value':dict(projected, status='running'),'managed_services':sorted(self.services)})

    def restart_all(self, payload, *, timeout, deadline):
        if type(payload) is not dict or set(payload) != {'request_id'} or type(payload['request_id']) is not str or not 0 < len(payload['request_id']) <= 128:
            raise ProviderUnavailable()
        value,_ = self._request('/v1/restarts/all',None,payload['request_id'],timeout=timeout,deadline=deadline)
        if value != {'status':'accepted'}:
            raise ProviderUnavailable()
        return ProviderAck('executor-restart-all:' + hashlib.sha256(payload['request_id'].encode()).hexdigest(),
                           {'value':value,'managed_services':sorted(self.services)})
