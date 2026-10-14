"""Contracts for disposable memo browser smoke boundaries and failure reporting."""
import importlib.util
from pathlib import Path
from contextlib import redirect_stderr
import io
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


def runner():
    spec = importlib.util.spec_from_file_location('memo_browser_smoke', ROOT / 'tests/browser/run_memo_smoke.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class MemoBrowserSmokeContracts(unittest.TestCase):
    def test_only_exact_owned_http_origin_is_allowed(self):
        module = runner()
        base = 'http://127.0.0.1:12345'
        self.assertTrue(module.is_owned_request(base + '/static/js/import-library.js', base))
        for url in ['https://len.pe.kr', 'http://localhost:12345', 'http://127.0.0.1:54321',
                    'https://127.0.0.1:12345', 'http://127.0.0.1:12345.evil.invalid/',
                    'http://user@127.0.0.1:12345/', 'file:///tmp/file', 'data:text/plain,test']:
            self.assertFalse(module.is_owned_request(url, base), url)

    def test_cli_does_not_accept_external_url_or_unknown_service(self):
        module = runner()
        for args in [['--url', 'https://len.pe.kr'], ['--service', 'portal']]:
            with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                module.argument_parser().parse_args(args)

    def test_failure_in_any_service_is_nonzero_and_all_requested_services_run(self):
        module = runner()
        with patch.object(module, 'run_service', side_effect=[1, 0]) as run:
            self.assertEqual(module.main(['--service', 'book-memo', '--service', 'youtube-memo']), 1)
            self.assertEqual([call.args[0] for call in run.call_args_list], ['book-memo', 'youtube-memo'])
        with patch.object(module, 'run_service', return_value=2):
            self.assertEqual(module.main(['--service', 'book-memo']), 2)


class PortalBrowserSmokeContracts(unittest.TestCase):
    def test_portal_cli_accepts_artifacts_but_rejects_external_url(self):
        spec = importlib.util.spec_from_file_location('portal_browser_smoke', ROOT / 'tests/browser/run_smoke.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self.assertEqual(module.argument_parser().parse_args(['--artifacts', 'browser-artifacts/portal']).artifacts,
                         Path('browser-artifacts/portal'))
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            module.argument_parser().parse_args(['--url', 'https://len.pe.kr'])


    def test_portal_write_waits_for_matching_post_response_before_reading_result(self):
        from types import SimpleNamespace
        spec = importlib.util.spec_from_file_location('portal_browser_write', ROOT / 'tests/browser/run_smoke.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        events = []
        response = SimpleNamespace(url='http://127.0.0.1:12345/files/trash',
                                   request=SimpleNamespace(url='http://127.0.0.1:12345/files/trash', method='POST'),
                                   status=200, ok=True)
        class Pending:
            def __init__(self, name):
                self.name = name
            def __enter__(self):
                events.append('armed-' + self.name)
                return self
            def __exit__(self, *_):
                events.append('delivered-' + self.name)
            @property
            def value(self):
                if not events or events[-1] != 'delivered-response':
                    raise AssertionError('response read before delivery')
                return response
        class Page:
            def expect_response(self, predicate):
                self_test.assertTrue(predicate(response))
                self_test.assertFalse(predicate(SimpleNamespace(url=response.url, request=SimpleNamespace(method='GET'))))
                self_test.assertFalse(predicate(SimpleNamespace(url='https://len.pe.kr/files/trash', request=response.request)))
                return Pending('response')
            def expect_request_finished(self, *_):
                raise AssertionError('reload may abort requestfinished; do not await it')
        self_test = self
        result = module.completed_write(Page(), response.url, lambda: events.append('action'))
        self.assertEqual(result, 200)
        self.assertEqual(events, ['armed-response', 'action', 'delivered-response'])
        for response.ok, response.status in [(False, 500), (True, 207)]:
            with self.assertRaisesRegex(AssertionError, f'HTTP {response.status}'):
                module.completed_write(Page(), response.url, lambda: events.append('action'))
