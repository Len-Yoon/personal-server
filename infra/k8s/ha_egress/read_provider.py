"""TCB-only fixed public GETs, with numeric-IP dialing and hostname TLS checks.

No generic proxy, CONNECT, cookies, app credentials, arbitrary paths or methods.
Only the trusted broker constructor supplies the optional YES24 credential.
"""
import base64
import hashlib
import http.client
import ipaddress
import json
import math
import re
import socket
import ssl
import time
import threading
from urllib.parse import parse_qsl, quote, quote_plus, urljoin, urlsplit

from ha_runtime.effects import ProviderAck

MAX_BODY = 2 * 1024 * 1024
_REDIRECTS = {301,302,303,307,308}
_ENDPOINTS = {
    'book-memo':{'apis.yes24.com':{'/v1/goods/itemList','/v1/goods/content'},
                 'www.googleapis.com':{'/books/v1/volumes'},'openlibrary.org':{'/search.json'}},
    'youtube-memo':{'www.youtube.com':{'/oembed'}},
    'crawler-worker':{'news.google.com':{'/rss/search'},
                      'kr.investing.com':{'/rss/news_14.rss','/rss/news_95.rss','/rss/news_11.rss'}}}
_TRANSITION = tuple(ipaddress.ip_network(value) for value in
                    ('64:ff9b::/96','64:ff9b:1::/48','2001::/32','2002::/16'))


class PublicReadUnavailable(OSError):
    def __init__(self):
        super().__init__('public_read_provider_unavailable')


def _remaining(timeout, deadline):
    if (type(timeout) not in {int,float} or type(deadline) not in {int,float}
            or not math.isfinite(timeout) or not math.isfinite(deadline) or not 0 < timeout <= 10):
        raise PublicReadUnavailable()
    value = min(timeout,deadline-time.monotonic())
    if value <= 0:
        raise PublicReadUnavailable()
    return value


def _target(service, url):
    if (type(url) is not str or not 0 < len(url) <= 8192
            or any(ord(c)<=32 or ord(c)==127 for c in url)):
        raise PublicReadUnavailable()
    parsed = urlsplit(url)
    host = parsed.hostname
    if (parsed.scheme != 'https' or parsed.username is not None or parsed.password is not None
            or parsed.fragment or parsed.port not in {None,443} or host not in _ENDPOINTS[service]
            or parsed.netloc not in {host,host+':443'} or parsed.path not in _ENDPOINTS[service][host]):
        raise PublicReadUnavailable()
    items = parse_qsl(parsed.query,keep_blank_values=True,strict_parsing=True,max_num_fields=8)
    params = dict(items)
    if len(params) != len(items) or any(any(ord(c)<32 or ord(c)==127 for c in k+v) for k,v in items):
        raise PublicReadUnavailable()
    keys = set(params)
    query = params.get('query',params.get('q',''))
    if host == 'apis.yes24.com':
        if parsed.path.endswith('itemList'):
            valid = (keys == {'query','category','page','pageSize'} and 0 < len(query) <= 500
                     and params['category']=='BOOK' and params['page']=='1'
                     and re.fullmatch(r'[1-9][0-9]?',params['pageSize']) and int(params['pageSize']) <= 40)
        else:
            valid = (keys == {'searchType','query'} and
                     ((params['searchType']=='ItemId' and re.fullmatch(r'[0-9]{1,20}',query))
                      or (params['searchType']=='ISBN13' and re.fullmatch(r'[0-9]{13}',query))))
    elif host == 'www.googleapis.com':
        valid = (keys == {'q','maxResults','printType','langRestrict'} and 0 < len(query) <= 500
                 and params['printType']=='books' and params['langRestrict']=='ko'
                 and re.fullmatch(r'[1-9][0-9]?',params['maxResults']) and int(params['maxResults']) <= 40)
    elif host == 'openlibrary.org':
        valid = (keys == {'q','limit'} and 0 < len(query) <= 500
                 and re.fullmatch(r'[1-9][0-9]?',params['limit']) and int(params['limit']) <= 40)
    elif host == 'www.youtube.com':
        valid = (keys == {'url','format'} and params['format']=='json'
                 and re.fullmatch(r'https://www\.youtube\.com/watch\?v=[A-Za-z0-9_-]{11}',params['url']))
    elif host == 'news.google.com':
        valid = (keys == {'q','hl','gl','ceid'} and 0 < len(query) <= 1000
                 and params['hl']=='ko' and params['gl']=='KR' and params['ceid']=='KR:ko')
    else:
        valid = not keys
    if not valid:
        raise PublicReadUnavailable()
    return host,parsed.path+('?' + parsed.query if parsed.query else '')


def _public(address):
    value = ipaddress.ip_address(address)
    if (not value.is_global or value.is_private or value.is_loopback or value.is_link_local
            or value.is_multicast or value.is_reserved or value.is_unspecified
            or (value.version==6 and (value.ipv4_mapped is not None or any(value in net for net in _TRANSITION)))):
        raise PublicReadUnavailable()
    return value


def _resolve(host):
    rows = socket.getaddrinfo(host,443,type=socket.SOCK_STREAM,proto=socket.IPPROTO_TCP)
    if not rows or len(rows)>64:
        raise PublicReadUnavailable()
    candidates = []
    for family,kind,protocol,_,address in rows:
        if (family not in {socket.AF_INET,socket.AF_INET6} or kind != socket.SOCK_STREAM
                or protocol != socket.IPPROTO_TCP or len(address) not in {2,4}
                or address[1] != 443 or (len(address)==4 and (address[2] or address[3]))):
            raise PublicReadUnavailable()
        ip = _public(address[0])
        if family != (socket.AF_INET if ip.version==4 else socket.AF_INET6):
            raise PublicReadUnavailable()
        candidates.append((family,str(ip)))
    return candidates[0]  # No retry, second resolution or alternative-IP fallback.


class _PinnedHTTPS(http.client.HTTPSConnection):
    def __init__(self,host,address,timeout,deadline):
        # Construct directly: create_default_context can honor SSLKEYLOGFILE.
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.load_default_certs()
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        super().__init__(host,443,context=context,timeout=_remaining(timeout,deadline))
        self._address = address
        self._budget = (timeout,deadline)

    def connect(self):
        family,ip = self._address
        raw = socket.socket(family,socket.SOCK_STREAM,socket.IPPROTO_TCP)
        self.sock = raw
        raw.settimeout(_remaining(*self._budget))
        raw.connect((ip,443) if family==socket.AF_INET else (ip,443,0,0))
        if _public(raw.getpeername()[0]) != ipaddress.ip_address(ip):
            raise PublicReadUnavailable()
        raw.settimeout(_remaining(*self._budget))
        self.sock = self._context.wrap_socket(raw,server_hostname=self.host)
        _remaining(*self._budget)


def _fetch(host,path,headers,*,timeout,deadline):
    connection = None
    timer = None
    transport = [None]
    def interrupt():
        sock = transport[0] or (connection.sock if connection is not None else None)
        if sock is not None:
            try:sock.shutdown(socket.SHUT_RDWR)
            except Exception:pass
    try:
        address = _resolve(host)
        _remaining(timeout,deadline)
        connection = _PinnedHTTPS(host,address,timeout,deadline)
        timer = threading.Timer(_remaining(timeout,deadline),interrupt)
        timer.daemon = True
        timer.start()
        connection.connect()
        connection.sock.settimeout(_remaining(timeout,deadline))
        connection.request('GET',path,headers=headers)
        sock = connection.sock
        transport[0] = sock
        response = connection.getresponse()
        _remaining(timeout,deadline)
        if response.status in _REDIRECTS:
            location = response.getheader('Location')
            if type(location) is not str or len(location)>8192:
                raise PublicReadUnavailable()
            return response.status,{},b'',location
        if response.status not in {200,404} or response.getheader('Content-Encoding','identity').lower() != 'identity':
            raise PublicReadUnavailable()
        content_type = response.getheader('Content-Type','application/octet-stream')
        if (type(content_type) is not str or len(content_type)>512
                or any(ord(c)<32 or ord(c)==127 for c in content_type)):
            raise PublicReadUnavailable()
        length = response.getheader('Content-Length')
        if length is not None and (not length.isascii() or not length.isdigit() or int(length)>MAX_BODY):
            raise PublicReadUnavailable()
        chunks = []
        size = 0
        while True:
            sock.settimeout(_remaining(timeout,deadline))
            chunk = response.read1(min(65536,MAX_BODY+1-size))
            _remaining(timeout,deadline)
            size += len(chunk)
            if size>MAX_BODY:
                raise PublicReadUnavailable()
            if not chunk:
                break
            chunks.append(chunk)
        return response.status,{'Content-Type':content_type},b''.join(chunks),None
    finally:
        if timer is not None:
            timer.cancel()
            timer.join()
        if connection is not None:
            try:connection.close()
            except Exception:pass


class PublicGet:
    def __init__(self,service,*,yes24_key=None):
        if (type(service) is not str or service not in _ENDPOINTS
                or (yes24_key is not None and (service!='book-memo' or type(yes24_key) is not str
                    or not 0 < len(yes24_key) <= 512 or any(ord(c)<33 or ord(c)>126 for c in yes24_key)))):
            raise PublicReadUnavailable()
        self.service = service
        self._yes24_key = yes24_key

    def __repr__(self):
        return 'PublicGet(<pinned>)'

    def __call__(self,payload,*,timeout,deadline):
        try:
            if type(payload) is not dict or set(payload) != {'url','headers'}:
                raise PublicReadUnavailable()
            headers = payload['headers']
            if (type(headers) is not dict or len(headers)>2
                    or any(type(k) is not str or k.lower() not in {'accept','user-agent'}
                           or type(v) is not str or len(v)>512 or any(ord(c)<32 or ord(c)==127 for c in v)
                           for k,v in headers.items()) or len({k.lower() for k in headers}) != len(headers)):
                raise PublicReadUnavailable()
            current = payload['url']
            for hop in range(3):
                host,path = _target(self.service,current)
                request_headers = {key.lower():value for key,value in headers.items()}
                request_headers.update({'accept-encoding':'identity'})
                credential = self._yes24_key if host=='apis.yes24.com' else None
                if host=='apis.yes24.com':
                    if credential is None:raise PublicReadUnavailable()
                    request_headers['X-Api-Key'] = credential
                _remaining(timeout,deadline)
                status,projected,body,location = _fetch(host,path,request_headers,timeout=timeout,deadline=deadline)
                _remaining(timeout,deadline)
                if status in _REDIRECTS:
                    if credential is not None or hop==2:raise PublicReadUnavailable()
                    current = urljoin(current,location)
                    continue
                if credential is not None:
                    # YES24 is parsed JSON; do not return upstream metadata
                    # that could reflect the broker-only credential.
                    projected = {'Content-Type':'application/json'}
                    variants = {credential,quote(credential,safe=''),quote_plus(credential),
                                json.dumps(credential,ensure_ascii=True)[1:-1]}
                    if any(value.encode() in body for value in variants):raise PublicReadUnavailable()
                    data = json.loads(body)
                    if type(data) is not dict:raise PublicReadUnavailable()
                    # Inspect decoded strings too: JSON unicode escapes must
                    # not turn an echoed API key into an app-readable secret.
                    pending = [data]
                    while pending:
                        value = pending.pop()
                        if type(value) is str and any(secret in value for secret in variants):
                            raise PublicReadUnavailable()
                        if type(value) is dict:
                            pending.extend(value.keys());pending.extend(value.values())
                        elif type(value) is list:
                            pending.extend(value)
                    if status!=200 or data.get('success') is not True or data.get('errorCode') is not None:
                        code = data.get('errorCode')
                        if status!=404 or code not in {'SEARCH_001','GOODS_001','GOODS_002'}:
                            raise PublicReadUnavailable()
                        body = json.dumps({'success':False,'errorCode':code,'data':{}}).encode()
                if len(body)>MAX_BODY:raise PublicReadUnavailable()
                result = {'status':status,'headers':projected,'body_b64':base64.b64encode(body).decode('ascii')}
                reference = hashlib.sha256(json.dumps(result,sort_keys=True,separators=(',',':')).encode()).hexdigest()
                _remaining(timeout,deadline)
                return ProviderAck('public-get:'+reference,result)
            raise PublicReadUnavailable()
        except Exception:
            raise PublicReadUnavailable() from None
