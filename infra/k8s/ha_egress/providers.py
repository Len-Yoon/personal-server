"""Trusted, typed provider adapters. Credentials never enter app requests/results.

Adapters are instantiated by the broker's trusted policy, never from request
URLs. Transport/network ownership and credential migration remain prerequisites.
"""
import http.client
import hashlib
import json
import re
import ssl
import socket
import threading
import time

from ha_runtime.effects import ProviderAck


class ProviderUnavailable(RuntimeError):
    def __init__(self):
        super().__init__("effect_provider_unavailable")


def remaining(timeout, deadline):
    budget = min(timeout, deadline - time.monotonic())
    if not 0 < budget <= 10:
        raise ProviderUnavailable()
    return budget


def post_json(host, path, payload, *, timeout, deadline):
    """HTTPS only, fixed host/path from the trusted adapter, redirects disabled."""
    connection = None
    timer = None
    try:
        context = ssl.create_default_context()
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        body = json.dumps(payload, allow_nan=False, separators=(",", ":")).encode()
        if len(body) > 65536:
            raise ProviderUnavailable()
        connection = http.client.HTTPSConnection(host, timeout=remaining(timeout, deadline), context=context)
        connection.connect()
        # DNS/TLS can consume time before the first effectful send. Recheck now.
        wire = connection.sock
        wire.settimeout(remaining(timeout, deadline))
        def expire():
            try:
                wire.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        timer = threading.Timer(remaining(timeout, deadline), expire)
        timer.daemon = True
        timer.start()
        remaining(timeout, deadline)
        connection.request("POST", path, body, {"Content-Type": "application/json"})
        response = connection.getresponse()
        remaining(timeout, deadline)
        if response.status != 200:
            raise ProviderUnavailable()
        chunks, size = [], 0
        reader = getattr(response, 'read1', response.read)
        while True:
            # HTTP/1.0/Connection:close can close the saved socket on the last body byte.
            if response.isclosed() is True:
                remaining(timeout, deadline)
                if type(response.length) is int and response.length > 0:
                    raise ProviderUnavailable()
                break
            wire.settimeout(remaining(timeout, deadline))
            chunk = reader(min(8192, 65537-size))
            remaining(timeout, deadline)
            if not chunk:
                if type(response.length) is int and response.length > 0:
                    raise ProviderUnavailable()
                break
            chunks.append(chunk); size += len(chunk)
            if size > 65536:
                raise ProviderUnavailable()
        raw = b''.join(chunks)
        if len(raw) > 65536:
            raise ProviderUnavailable()
        from .client import _pairs
        result = json.loads(raw, object_pairs_hook=_pairs,
                            parse_constant=lambda _: (_ for _ in ()).throw(ProviderUnavailable()))
        if type(result) is not dict:
            raise ProviderUnavailable()
        return result
    except ProviderUnavailable:
        raise
    except Exception:
        raise ProviderUnavailable() from None
    finally:
        if timer is not None:
            timer.cancel()
        if connection is not None:
            connection.close()


class TelegramSender:
    """One configured bot/chat, one operation, one attempt, acknowledged ID only."""
    def __init__(self, token, chat_id):
        if (type(token) is not str or not re.fullmatch(r"[0-9]{5,20}:[A-Za-z0-9_-]{20,100}", token)
                or type(chat_id) is not str or not re.fullmatch(r"-?[1-9][0-9]{0,19}", chat_id)):
            raise ProviderUnavailable()
        self._token = token
        self._chat_id = chat_id

    def __repr__(self):
        return "TelegramSender(<private>)"

    def __call__(self, payload, *, timeout, deadline):
        if (type(payload) is not dict or set(payload) != {"text"}
                or type(payload["text"]) is not str or not 0 < len(payload["text"]) <= 4096):
            raise ProviderUnavailable()
        result = post_json("api.telegram.org", "/bot" + self._token + "/sendMessage",
                           {"chat_id": self._chat_id, "text": payload["text"], "disable_web_page_preview": True},
                           timeout=timeout, deadline=deadline)
        message = result.get("result")
        if (result.get("ok") is not True or type(message) is not dict
                or type(message.get("message_id")) is not int or message["message_id"] <= 0
                or type(message.get("chat")) is not dict
                or type(message["chat"].get("id")) is not int
                or str(message["chat"]["id"]) != self._chat_id):
            raise ProviderUnavailable()
        # Do not expose the raw provider body (which may include private fields).
        return ProviderAck("telegram-message:" + str(message["message_id"]),
                           {"message_id": message["message_id"]})


class TelegramPoller(TelegramSender):
    def __call__(self, payload, *, timeout, deadline):
        if (type(payload) is not dict or set(payload) != {'offset'}
                or (payload['offset'] is not None and (type(payload['offset']) is not int
                                                     or not 0 <= payload['offset'] < 2**63))):
            raise ProviderUnavailable()
        body = {'timeout':0, 'limit':3, 'allowed_updates':['message']}
        if payload['offset'] is not None:
            body['offset'] = payload['offset']
        response = post_json('api.telegram.org','/bot' + self._token + '/getUpdates',body,
                             timeout=timeout,deadline=deadline)
        if response.get('ok') is not True or type(response.get('result')) is not list or len(response['result']) > 3:
            raise ProviderUnavailable()
        updates = []
        for item in response['result']:
            if type(item) is not dict or type(item.get('update_id')) is not int or not 0 <= item['update_id'] < 2**63:
                raise ProviderUnavailable()
            message = item.get('message')
            message = message if type(message) is dict else {}
            chat = message.get('chat')
            chat = chat if type(chat) is dict else {}
            chat_id = str(chat['id']) if type(chat.get('id')) is int else ''
            text = message.get('text')
            accepted = chat_id == self._chat_id
            updates.append({'update_id':item['update_id'],'chat_id':chat_id if accepted else '',
                            'text':text if accepted and type(text) is str and len(text) <= 4096 else None})
        reference = hashlib.sha256(json.dumps([payload['offset'],[x['update_id'] for x in updates]]).encode()).hexdigest()
        return ProviderAck('telegram-poll:' + reference, {'updates':updates})
