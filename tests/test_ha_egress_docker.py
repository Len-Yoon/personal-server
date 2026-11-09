"""Pinned Docker restart protocol; doubles never touch a real Docker daemon."""
import io
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'infra/k8s'))
from ha_egress import docker_provider as provider
from ha_runtime.effects import ProviderAck

UnixConnection = provider._UnixConnection
CID = 'a' * 64
IMAGE = 'sha256:' + 'b' * 64
OLD = '2026-11-09T01:01:01.123456789Z'
NEW = '2026-11-09T01:01:02.123456789Z'


def inspect(started=OLD):
    return {'Id': CID, 'Image': IMAGE,
            'Config': {'Labels': {'com.docker.compose.project': 'owned-fixture',
                                 'com.docker.compose.service': 'book-memo'},
                       'Env': ['DO_NOT_RETURN=private']},
            'State': {'Running': True, 'Status': 'running', 'Paused': False,
                      'Restarting': False, 'StartedAt': started,
                      'Health': {'Status': 'starting', 'Log': [{'Output': 'private-log'}]}}}


def response(value=None, status=200, raw=None):
    result = MagicMock(status=status)
    data = raw if raw is not None else (b'' if value is None else json.dumps(value).encode())
    result.read1.side_effect = io.BytesIO(data).read1
    return result


class DockerRestartTests(unittest.TestCase):
    def setUp(self):
        self.adapter = provider.DockerRestart('/private/tmp/fixture.sock', CID, IMAGE, 'owned-fixture', 'book-memo')
        self.connection = MagicMock()
        self.connection.getresponse.side_effect = [response(inspect()), response(status=204), response(inspect(NEW))]
        self.factory = patch.object(provider, '_UnixConnection', return_value=self.connection)
        self.clock = patch.object(provider.time, 'monotonic', return_value=10.)
        self.factory.start(); self.clock.start()
        self.addCleanup(self.factory.stop); self.addCleanup(self.clock.stop)

    def call(self, payload=None):
        return self.adapter({} if payload is None else payload, timeout=5, deadline=15)

    def test_exact_id_get_restart_get_and_private_projection(self):
        result = self.call()
        self.assertIs(type(result), ProviderAck)
        self.assertRegex(result.reference, '^docker-restart:[0-9a-f]{64}$')
        self.assertEqual(result.result, {'service': 'book-memo', 'status': 'running',
                                       'started_at': NEW, 'health': 'starting'})
        self.assertEqual([(call.args[0], call.args[1]) for call in self.connection.request.call_args_list], [
            ('GET', '/v1.44/containers/' + CID + '/json'),
            ('POST', '/v1.44/containers/' + CID + '/restart?t=4'),
            ('GET', '/v1.44/containers/' + CID + '/json')])
        self.assertNotIn('private', str(result))
        self.connection.close.assert_called_once()

    def test_invalid_constructor_cannot_bind_names_urls_or_protected_services(self):
        baseline = ['/private/tmp/fixture.sock', CID, IMAGE, 'owned-fixture', 'book-memo']
        for index, bad in [(0, 'tcp://host'), (0, '/tmp/../docker.sock'), (0, '/tmp/x\0'),
                           (1, 'book-memo'), (1, CID[:12]), (1, CID.upper()),
                           (2, 'postgres:17'), (3, '../other'), (4, 'postgres'),
                           (4, 'caddy'), (4, 'egress-broker'), (4, [])]:
            values = baseline.copy(); values[index] = bad
            with self.subTest(index=index, bad=bad), self.assertRaises(provider.DockerUnavailable):
                provider.DockerRestart(*values)

    def test_payload_cannot_change_target_action_or_credentials(self):
        for payload in ({'container_id': CID}, {'service': 'portal-web'}, {'url': 'x'}, {'timeout': 1}, []):
            with self.assertRaisesRegex(OSError, '^effect_docker_unavailable$'):
                self.call(payload)
        self.connection.connect.assert_not_called()

    def test_inspect_mismatch_or_missing_required_state_prevents_post(self):
        cases = []
        for field, value in [('Id', 'c' * 64), ('Image', 'sha256:' + 'c' * 64), ('Config', None)]:
            item = inspect(); item[field] = value; cases.append(item)
        for field, value in [('Running', False), ('Status', 'exited'), ('Paused', True),
                             ('Restarting', True), ('StartedAt', '0001-01-01T00:00:00Z'),
                             ('StartedAt', '2026-02-30T00:00:00Z'), ('Health', {'Status': 'secret'})]:
            item = inspect(); item['State'][field] = value; cases.append(item)
        for key in ('com.docker.compose.project', 'com.docker.compose.service'):
            item = inspect(); item['Config']['Labels'][key] = 'other'; cases.append(item)
        for item in cases:
            with self.subTest(item=item):
                self.connection.reset_mock()
                self.connection.getresponse.side_effect = [response(item)]
                with self.assertRaisesRegex(OSError, '^effect_docker_unavailable$'):
                    self.call()
                self.assertEqual(self.connection.request.call_count, 1)

    def test_no_change_wrong_identity_or_stopped_after_restart_never_acknowledges(self):
        cases = [inspect(), inspect(NEW)]
        cases[1]['Id'] = 'c' * 64
        stopped = inspect(NEW); stopped['State']['Running'] = False; cases.append(stopped)
        for item in cases:
            self.connection.reset_mock()
            self.connection.getresponse.side_effect = [response(inspect()), response(status=204), response(item)]
            with self.assertRaises(provider.DockerUnavailable):
                self.call()
            self.assertEqual(self.connection.request.call_count, 3)

    def test_non_204_redirect_and_transport_errors_never_retry(self):
        for failure in [response(status=200), response(status=302), response(status=500),
                        OSError('raw socket path/private')]:
            self.connection.reset_mock()
            self.connection.getresponse.side_effect = [response(inspect()), failure]
            with self.assertRaisesRegex(OSError, '^effect_docker_unavailable$'):
                self.call()
            self.assertEqual(self.connection.request.call_count, 2)

    def test_deadline_after_connect_prevents_first_send(self):
        self.connection.connect.side_effect = lambda: setattr(self.connection, 'connected', True)
        with patch.object(provider.time, 'monotonic', side_effect=[10., 16.]):
            with self.assertRaises(provider.DockerUnavailable):
                self.call()
        self.connection.request.assert_not_called()
        self.connection.close.assert_called_once()

    def test_deadline_exhaustion_before_post_or_during_body_prevents_effect(self):
        for at in ('before-post', 'body'):
            self.connection.reset_mock()
            first = response(inspect())
            now = [10.]
            if at == 'body':
                original = first.read1.side_effect
                def drip(size):
                    now[0] = 16.
                    return original(size)
                first.read1.side_effect = drip
            else:
                original = self.adapter._inspect
                def late(value):
                    result = original(value); now[0] = 16.; return result
            self.connection.getresponse.side_effect = [first]
            with patch.object(provider.time, 'monotonic', side_effect=lambda: now[0]):
                if at == 'before-post':
                    with patch.object(provider.DockerRestart, '_inspect', side_effect=late):
                        with self.assertRaises(provider.DockerUnavailable): self.call()
                else:
                    with self.assertRaises(provider.DockerUnavailable): self.call()
            self.assertEqual(self.connection.request.call_count, 1)

    def test_post_response_deadline_is_uncertain_without_followup_or_retry(self):
        now = [10.]
        post = response(status=204)
        def responses():
            yield response(inspect())
            now[0] = 16.
            yield post
        self.connection.getresponse.side_effect = responses()
        with patch.object(provider.time, 'monotonic', side_effect=lambda: now[0]):
            with self.assertRaises(provider.DockerUnavailable): self.call()
        self.assertEqual(self.connection.request.call_count, 2)

    def test_invalid_oversized_duplicate_json_and_closed_connection_fail_closed(self):
        duplicate = b'{"Id":"x","Id":"y"}'
        for raw in (duplicate, b'NaN', b'[]', b'not-json', b'x' * 1048577):
            self.connection.reset_mock()
            self.connection.getresponse.side_effect = [response(raw=raw)]
            with self.assertRaises(provider.DockerUnavailable): self.call()
            self.assertEqual(self.connection.request.call_count, 1)
        self.connection.reset_mock(); self.connection.sock = None
        with self.assertRaises(provider.DockerUnavailable): self.call()
        self.connection.request.assert_not_called()

    def test_nonfinite_or_short_budget_cannot_dispatch(self):
        for timeout, deadline in ((True, 15), (float('nan'), 15), (11, 15), (5, float('inf')), (0, 15), (5, 9)):
            with self.assertRaises(provider.DockerUnavailable):
                self.adapter({}, timeout=timeout, deadline=deadline)
        self.connection.request.assert_not_called()

    def test_unix_connection_connects_only_pinned_path_without_tcp(self):
        with patch.object(provider.socket, 'socket') as factory:
            connection = UnixConnection('/private/tmp/pinned.sock', 2)
            connection.connect()
        factory.assert_called_once_with(provider.socket.AF_UNIX, provider.socket.SOCK_STREAM)
        factory.return_value.connect.assert_called_once_with('/private/tmp/pinned.sock')
        factory.return_value.settimeout.assert_called_once_with(2)


    def test_inspect_is_one_read_only_exact_target_projection_even_when_stopped(self):
        adapter=provider.DockerInspect('/private/tmp/fixture.sock',CID,IMAGE,'owned-fixture','book-memo')
        stopped=inspect('0001-01-01T00:00:00Z')
        stopped['State'].update(Running=False,Status='created')
        self.connection.getresponse.side_effect=[response(stopped)]
        result=adapter({},timeout=5,deadline=15)
        self.assertEqual(result.result,{'service':'book-memo','status':'created','started_at':'0001-01-01T00:00:00Z','health':'starting'})
        self.assertRegex(result.reference,'^docker-inspect:[0-9a-f]{64}$')
        self.connection.request.assert_called_once_with('GET','/v1.44/containers/'+CID+'/json',body=b'',headers={'Content-Length':'0'})
        self.assertNotIn('private',str(result))

    def test_inspect_rejects_target_change_invalid_body_or_broker_control_input(self):
        adapter=provider.DockerInspect('/private/tmp/fixture.sock',CID,IMAGE,'owned-fixture','book-memo')
        wrong=inspect();wrong['Image']='sha256:'+'c'*64
        self.connection.getresponse.side_effect=[response(wrong)]
        with self.assertRaises(provider.DockerUnavailable):adapter({},timeout=5,deadline=15)
        self.assertEqual(self.connection.request.call_count,1)
        self.connection.reset_mock()
        inconsistent=inspect();inconsistent['State']['Running']=False
        self.connection.getresponse.side_effect=[response(inconsistent)]
        with self.assertRaises(provider.DockerUnavailable):adapter({},timeout=5,deadline=15)
        self.connection.reset_mock()
        with self.assertRaises(provider.DockerUnavailable):adapter({'logs':True},timeout=5,deadline=15)
        self.connection.request.assert_not_called()


if __name__ == '__main__':
    unittest.main()
