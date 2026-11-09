"""Portal preserves typed incident approval and Executor supports a verifier only."""
import hashlib
import os
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from tests._test_support import prepare_service_import
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'infra/k8s'))


class PortalBrokerTest(unittest.TestCase):
    def setUp(self):
        prepare_service_import('portal-web')
        from app.services import homeops
        self.homeops=homeops

    def test_incident_approval_forwarded_without_raw_network_or_app_secret(self):
        client=self.homeops.ExecutorClient();client.secret=''
        result={'value':{'status':'running'},'managed_services':['book-memo']}
        with patch.dict(os.environ,{'HA_PG_CONFIG':'fixture','HA_HOMEOPS_TARGET_NODE':'A'}), \
             patch('ha_egress.client.configuration',return_value={'signed':True}), \
             patch('ha_egress.client.route',return_value=result) as route, \
             patch.object(self.homeops,'runtime',return_value=SimpleNamespace(node_id=lambda:'A')), \
             patch.object(self.homeops,'urlopen',side_effect=AssertionError('raw HTTP forbidden')):
            self.assertEqual(client.restart('incident-1','approval-1','book-memo'),result['value'])
        route.assert_called_once_with('portal-web','portal.executor.restart',
            {'incident_id':'incident-1','approval_token':'approval-1','service':'book-memo'},request_id='incident-1')

    def test_stale_target_and_broker_failure_never_fall_back(self):
        client=self.homeops.ExecutorClient();client.secret='legacy-secret'
        for target in ('B','A'):
            with patch.dict(os.environ,{'HA_PG_CONFIG':'fixture','HA_HOMEOPS_TARGET_NODE':target}), \
                 patch('ha_egress.client.configuration',return_value={'signed':True}), \
                 patch('ha_egress.client.route',side_effect=OSError('broker unavailable')), \
                 patch.object(self.homeops,'runtime',return_value=SimpleNamespace(node_id=lambda:'A')), \
                 patch.object(self.homeops,'urlopen',side_effect=AssertionError('raw HTTP forbidden')):
                with self.assertRaises(OSError):client.all_diagnostics()


class ExecutorVerifierTest(unittest.TestCase):
    def setUp(self):
        prepare_service_import('homeops-executor')
        from app import main
        self.main=main

    def test_digest_only_accepts_matching_header_and_rejects_plaintext_fallback(self):
        secret='fixture-not-production';digest=hashlib.sha256(secret.encode()).hexdigest()
        with patch.dict(os.environ,{'HOMEOPS_EXECUTOR_SHARED_SECRET_SHA256':digest,
              'HOMEOPS_EXECUTOR_SHARED_SECRET':'legacy-other'}):
            self.assertTrue(self.main._auth_configured())
            self.main._require_shared_secret(secret)
            for provided in ('','legacy-other','wrong'):
                with self.assertRaises(self.main.HTTPException):self.main._require_shared_secret(provided)

    def test_enforced_without_digest_never_uses_old_shared_secret(self):
        with patch.dict(os.environ,{'HA_PG_CONFIG':'fixture','HOMEOPS_EXECUTOR_SHARED_SECRET_SHA256':'',
              'HOMEOPS_EXECUTOR_SHARED_SECRET':'legacy-secret'}), \
             patch('ha_egress.client.configuration',return_value={'signed':True}):
            self.assertFalse(self.main._auth_configured())
            with self.assertRaises(self.main.HTTPException):self.main._require_shared_secret('legacy-secret')
