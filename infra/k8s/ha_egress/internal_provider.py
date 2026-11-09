"""Portal-only fixed internal GET routes; no arbitrary URL, headers or methods."""
import hashlib
import json
from urllib.parse import urlencode,urlsplit
from urllib.request import Request
from ha_runtime.effects import ProviderAck
from .hyundai_provider import BoundRequestTransport
from .providers import ProviderUnavailable

TARGETS={'news':'crawler-worker','youtube':'youtube-memo','books':'book-memo','system':'system-agent'}


class InternalGet:
    def __init__(self, endpoints, *, context, node):
        if type(endpoints) is not dict or set(endpoints)!=set(TARGETS):raise ProviderUnavailable()
        if node not in {'A','B'}:raise ProviderUnavailable()
        self._endpoints={};self._context=context;self.node=node
        for name,value in endpoints.items():
            if type(value) is not dict or set(value)!={'url','server_sha256'}:raise ProviderUnavailable()
            parsed=urlsplit(value['url']);pin=value['server_sha256']
            if (parsed.scheme!='https' or parsed.hostname!=TARGETS[name]+'.ha.internal' or parsed.port!=10443
                    or parsed.path not in {'','/'} or parsed.username or parsed.password or parsed.query or parsed.fragment
                    or type(pin) is not str or len(pin)!=64 or any(c not in '0123456789abcdef' for c in pin)):
                raise ProviderUnavailable()
            self._endpoints[name]=(value['url'].rstrip('/'),parsed.hostname,pin)

    def __call__(self,payload,*,timeout,deadline):
        if type(payload) is not dict or set(payload)!={'target','path','query'}:raise ProviderUnavailable()
        target,path,query=(payload[name] for name in ('target','path','query'))
        if type(target) is not str or target not in TARGETS or type(path) is not str or type(query) is not dict:raise ProviderUnavailable()
        if path=='/api/search' and target in {'news','youtube','books'}:
            if (set(query)!={'q','limit'} or type(query['q']) is not str or not 0<len(query['q'])<=512
                    or type(query['limit']) is not int or not 1<=query['limit']<=20):raise ProviderUnavailable()
        elif path=='/health' or (target=='system' and path in {'/metrics','/recovery-events'}):
            if query:raise ProviderUnavailable()
        else:raise ProviderUnavailable()
        base,host,pin=self._endpoints[target]
        url=base+path+('?' + urlencode(query) if query else '')
        request=Request(url,headers={'Accept':'application/json','X-HA-Target-Node':self.node},method='GET')
        with BoundRequestTransport({(host,10443)},deadline,context=self._context,server_sha256=pin)(request,timeout=timeout) as response:
            from .client import _pairs
            raw=response.read()
            if len(raw)>49152:raise ProviderUnavailable()
            value=json.loads(raw,object_pairs_hook=_pairs,parse_constant=lambda _: (_ for _ in ()).throw(ProviderUnavailable()))
        if type(value) not in {dict,list}:raise ProviderUnavailable()
        # Canonical size enforced by Broker too; no request headers or provider secrets are returned.
        return ProviderAck('internal-read:'+hashlib.sha256(raw).hexdigest(),{'value':value})
