from contextlib import contextmanager
import copy
import json
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'infra/k8s'))
from app.services.hyundai import HyundaiClient
from app.services.ha_hyundai import BrokerHyundaiClient


class BrokerHyundaiTests(unittest.TestCase):
    def test_enforced_factory_has_no_provider_secret_and_uses_only_gateway(self):
        with patch.dict(os.environ, {'HA_PG_CONFIG': '/fixture/config'}, clear=True), \
                patch('ha_egress.client.configuration', return_value={'fixture': True}), \
                patch('app.services.hyundai.urlopen') as raw:
            proxy = HyundaiClient.from_environment()
            self.assertIsInstance(proxy, BrokerHyundaiClient)
            self.assertFalse(hasattr(proxy, '_client_secret'))
            with patch('ha_egress.client.route', return_value={'connected': True}) as route:
                self.assertIn('완료', proxy.complete_authorization('fixture-code', 'fixture-state'))
                first = route.call_args
                proxy.complete_authorization('fixture-code', 'fixture-state')
                self.assertEqual(first.kwargs['request_id'], route.call_args.kwargs['request_id'])
                self.assertNotIn('fixture-code', first.kwargs['request_id'])
            raw.assert_not_called()

    def test_private_backend_uses_canonical_token_logic_without_app_or_filesystem_state(self):
        class Private:
            def __init__(self):
                self.value = {}
            @contextmanager
            def state_transaction(self, service, key, default):
                self_case.assertEqual((service, key), ('car-care-worker', 'hyundai_tokens'))
                value = copy.deepcopy(self.value)
                yield value
                self.value = value
            def read_state(self, service, key, default):
                return copy.deepcopy(self.value)
        self_case = self
        backend = Private()
        client = HyundaiClient(state_backend=backend, token_store_path='/fixture/not-used')
        with patch('app.services.hyundai.runtime', side_effect=AssertionError('app state was used')), \
                patch.object(Path, 'write_text', side_effect=AssertionError('filesystem token was used')):
            client._save_refresh_token('synthetic-private-token')
            self.assertEqual(client._load_refresh_token(), 'synthetic-private-token')
        self.assertEqual(backend.value['refresh_token'], 'synthetic-private-token')

    def test_injected_transport_is_used_without_raw_network_fallback(self):
        response = MagicMock()
        response.__enter__.return_value.read.return_value = json.dumps({'fixture': True}).encode()
        transport = MagicMock(return_value=response)
        client = HyundaiClient(request_transport=transport)
        with patch('app.services.hyundai.urlopen', side_effect=AssertionError('raw network')):
            self.assertEqual(client._get_json('/fixture', 'synthetic-token'), {'fixture': True})
        transport.assert_called_once()

    def test_proxy_projects_snapshot_and_rejects_provider_secrets_in_reply(self):
        proxy = BrokerHyundaiClient()
        snapshot = {'observed_at':'2026-11-09T00:00:00+00:00','odometer_km':123,'dte_km':45,
                    'warnings':[],'unknown_warnings':[]}
        with patch('ha_egress.client.route', return_value={'status':'ok','snapshot':snapshot}):
            self.assertEqual(proxy.fetch_snapshot().snapshot.odometer_km, 123)
        with patch('ha_egress.client.route', return_value={'status':'ok','snapshot':snapshot,'access_token':'synthetic'}):
            with self.assertRaises(OSError):
                proxy.fetch_snapshot()
