"""Real local mTLS transport; DB ledger and production network ACLs are separate."""
from datetime import datetime, timedelta, timezone
import http.client
import json
import os
from pathlib import Path
import ssl
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "infra/k8s"))
from ha_egress import client
from ha_egress.server import BrokerServer
from ha_runtime import config as runtime_config


class ActivationBindingTest(unittest.TestCase):
    def test_enforced_activation_requires_signed_effect_endpoint_and_plan_binding(self):
        effects = {"url": "https://fixture-broker:8443", "ca": "/fixture/ca", "certificate": "/fixture/cert",
                   "key": "/fixture/key", "server_sha256": "a" * 64, "plan_sha256": "b" * 64}
        payload = {"scope": "n100-approved", "cluster": "fixture", "system_id": "123", "node": "A",
                   "service": "portal-web", "primary_node": "A", "primary": {}, "partner": {},
                   "profile": "enforced-vm-v1", "enforcement_sha256": "c" * 64, "effects": effects}
        approval = {key: value for key, value in payload.items() if key != "scope"}
        approval.update(action="activate-ha", expires=(datetime.now(timezone.utc) + timedelta(hours=1)).isoformat().replace("+00:00", "Z"),
                        nodes={"A": "ha-a", "B": "ha-b"})
        authority = unittest.mock.MagicMock()
        authority.read_text.return_value = json.dumps(approval)
        with patch.dict(os.environ, {"APP_ENV": "production", "HA_PG_ACTIVATION": "/fixture/approval"}, clear=True), \
                patch.object(runtime_config, "_release", return_value={}), \
                patch.object(runtime_config, "_authority_file", return_value=authority), \
                patch.object(runtime_config, "_private") as private:
            self.assertEqual(runtime_config._approved(payload), approval["nodes"])
            private.assert_called_once_with("/fixture/key")
            missing = dict(payload)
            del missing["effects"]
            with self.assertRaisesRegex(runtime_config.DatabaseUnavailable, "activation_effects_binding_required"):
                runtime_config._approved(missing)
            changed = dict(payload, effects=dict(effects, plan_sha256="d" * 64))
            with self.assertRaisesRegex(runtime_config.DatabaseUnavailable, "activation_identity_mismatch"):
                runtime_config._approved(changed)
            installed_payload = dict(payload,installation_id='e'*64,generation=2)
            installed = {key:value for key,value in installed_payload.items() if key != 'scope'}
            installed.update(schema_version=3,action='activate-ha',authority_kind='installed-runtime',
                installed_at=(datetime.now(timezone.utc)-timedelta(days=30)).isoformat().replace('+00:00','Z'),
                nodes={'A':'ha-a','B':'ha-b'})
            authority.read_text.return_value = json.dumps(installed)
            self.assertEqual(runtime_config._approved(installed_payload),installed['nodes'])
            for changes in ({'expires':'2099-01-01T00:00:00Z'}, {'installation_id':'f'*64},
                            {'installed_at':(datetime.now(timezone.utc)+timedelta(days=1)).isoformat().replace('+00:00','Z')},
                            {'installed_at':'2026-01-01T00:00:00'}, {'generation':3}):
                authority.read_text.return_value = json.dumps(dict(installed,**changes))
                with self.assertRaises(runtime_config.DatabaseUnavailable):
                    runtime_config._approved(installed_payload)


class TransportTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.ca_key = ec.generate_private_key(ec.SECP256R1())
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "fixture-ca")])
        now = datetime.now(timezone.utc)
        self.ca = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
                   .public_key(self.ca_key.public_key()).serial_number(x509.random_serial_number())
                   .not_valid_before(now - timedelta(minutes=1)).not_valid_after(now + timedelta(hours=1))
                   .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
                   .sign(self.ca_key, hashes.SHA256()))
        (self.root / "ca.pem").write_bytes(self.ca.public_bytes(serialization.Encoding.PEM))
        self.server_cert = self.certificate("server", True)
        self.client_cert = self.certificate("client", False)
        self.certificate("unknown", False)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = ssl.TLSVersion.TLSv1_3
        context.verify_mode = ssl.CERT_REQUIRED
        context.load_verify_locations(cafile=self.root / "ca.pem")
        context.load_cert_chain(self.root / "server.pem", self.root / "server.key")
        self.calls = []
        self.identity = object()
        def dispatch(identity, body):
            self.calls.append((identity, body))
            return {"status": "completed", "result": {"accepted": True}}
        self.server = BrokerServer(("127.0.0.1", 0), context,
            {self.client_cert.fingerprint(hashes.SHA256()).hex(): self.identity}, dispatch, plan_sha256="a" * 64,
            large_read_operations={'book.http.get'})
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.close)
        self.config = {"url": "https://localhost:" + str(self.server.server_port),
                       "ca": str(self.root / "ca.pem"), "certificate": str(self.root / "client.pem"),
                       "key": str(self.root / "client.key"),
                       "server_sha256": self.server_cert.fingerprint(hashes.SHA256()).hex(), "plan_sha256": "a" * 64}
        # This suite isolates actual TLS and dispatch. Production file ownership
        # is checked by the existing authority helpers, not this local fixture.
        for name in ("_authority_file", "_private"):
            patched = patch("ha_runtime.config." + name, side_effect=Path)
            patched.start()
            self.addCleanup(patched.stop)

    def certificate(self, name, server):
        key = ec.generate_private_key(ec.SECP256R1())
        now = datetime.now(timezone.utc)
        builder = (x509.CertificateBuilder()
            .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)]))
            .issuer_name(self.ca.subject).public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(minutes=1)).not_valid_after(now + timedelta(hours=1))
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH if server
                                                else ExtendedKeyUsageOID.CLIENT_AUTH]), critical=True))
        if server:
            builder = builder.add_extension(x509.SubjectAlternativeName([x509.DNSName("localhost")]), critical=False)
        cert = builder.sign(self.ca_key, hashes.SHA256())
        (self.root / (name + ".pem")).write_bytes(cert.public_bytes(serialization.Encoding.PEM))
        path = self.root / (name + ".key")
        path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                          serialization.NoEncryption()))
        path.chmod(0o600)
        return cert

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)

    def raw(self, body, *, name="client", headers=None):
        context = ssl.create_default_context(cafile=self.root / "ca.pem")
        context.load_cert_chain(self.root / (name + ".pem"), self.root / (name + ".key"))
        connection = http.client.HTTPSConnection("localhost", self.server.server_port, context=context, timeout=3)
        self.addCleanup(connection.close)
        connection.request("POST", "/v1/dispatch", body,
                           headers or {"Content-Type": "application/json", "X-HA-Plan": "a" * 64})
        response = connection.getresponse()
        return response.status, response.read()

    def test_peer_certificate_selects_identity_and_body_cannot_override_it(self):
        value = client.dispatch(self.config, "notify", "fixture-request", {"text": "fixture"})
        self.assertEqual(value["status"], "completed")
        self.assertIs(self.calls[0][0], self.identity)
        forged = json.dumps({"operation": "notify", "request_id": "other", "payload": {},
                             "service": "forged", "node": "B", "generation": 99})
        self.assertEqual(self.raw(forged)[0], 503)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.raw(b'{"operation":"notify","request_id":"x","payload":{}}',
                                  headers={"Content-Type": "application/json", "X-HA-Plan": "b" * 64})[0], 503)
        self.assertEqual(len(self.calls), 1)

    def test_unknown_ca_valid_client_and_wrong_server_pin_never_dispatch(self):
        body = b'{"operation":"notify","request_id":"x","payload":{}}'
        self.assertEqual(self.raw(body, name="unknown")[0], 503)
        config = dict(self.config, server_sha256="0" * 64)
        with self.assertRaises(client.BrokerUnavailable):
            client.dispatch(config, "notify", "x", {})
        self.assertEqual(self.calls, [])

    def test_missing_client_certificate_and_server_hostname_mismatch_never_dispatch(self):
        context = ssl.create_default_context(cafile=self.root / "ca.pem")
        connection = http.client.HTTPSConnection("localhost", self.server.server_port, context=context, timeout=3)
        try:
            with self.assertRaises((OSError, http.client.HTTPException)):
                connection.request("POST", "/v1/dispatch", b"{}")
                connection.getresponse()
        finally:
            connection.close()
        with self.assertRaises(client.BrokerUnavailable):
            client.dispatch(dict(self.config, url=self.config["url"].replace("localhost", "127.0.0.1")),
                            "notify", "x", {})
        self.assertEqual(self.calls, [])

    def test_duplicate_json_oversize_and_nonfinite_never_dispatch(self):
        for body in (b'{"operation":"x","operation":"y","request_id":"x","payload":{}}',
                     b'{"operation":"x","request_id":"x","payload":{"x":NaN}}', b"x" * 65537):
            with self.subTest(size=len(body)):
                status, raw = self.raw(body)
                self.assertEqual(status, 503)
                self.assertEqual(json.loads(raw), {"error": "effect_broker_unavailable"})
        self.assertEqual(self.calls, [])

    def test_lost_response_after_dispatch_is_never_retried(self):
        def lose(identity, request):
            self.calls.append((identity, request))
            raise OSError("private-provider-value")
        self.server.dispatch = lose
        with self.assertRaisesRegex(client.BrokerUnavailable, "^effect_broker_unavailable$"):
            client.dispatch(self.config, "notify", "x", {})
        self.assertEqual(len(self.calls), 1)

    def test_large_wire_result_is_only_enabled_for_registered_public_reads(self):
        self.server.dispatch = lambda identity, request: {'result':{'body_b64':'x' * 70000}}
        value = client.dispatch(self.config,'book.http.get','fixture',{})
        self.assertEqual(len(value['result']['body_b64']),70000)
        with self.assertRaises(client.BrokerUnavailable):
            client.dispatch(self.config,'notify','fixture',{})

    def test_response_loss_after_request_send_has_one_transport_attempt(self):
        sent = threading.Event()
        original = self.server.dispatch
        def dispatch(identity, request):
            result = original(identity, request)
            sent.set()
            return result
        self.server.dispatch = dispatch
        with patch.object(http.client.HTTPSConnection, "getresponse", side_effect=ConnectionResetError("fixture")):
            with self.assertRaises(client.BrokerUnavailable):
                client.dispatch(self.config, "notify", "x", {})
        self.assertTrue(sent.wait(2))
        self.assertEqual(len(self.calls), 1)

    def test_http_plaintext_and_redirect_configuration_have_no_dispatch(self):
        connection = http.client.HTTPConnection("localhost", self.server.server_port, timeout=3)
        try:
            with self.assertRaises((OSError, http.client.HTTPException)):
                connection.request("POST", "/v1/dispatch", b"{}")
                connection.getresponse()
        finally:
            connection.close()
        for url in ("http://localhost", self.config["url"] + "/redirect", "https://user:pass@localhost"):
            with self.assertRaises(client.BrokerUnavailable):
                client.dispatch(dict(self.config, url=url), "notify", "x", {})
        self.assertEqual(self.calls, [])
