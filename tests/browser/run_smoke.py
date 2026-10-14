#!/usr/bin/env python3
"""Run explicit isolated Chromium smoke: login, upload, trash, search retry.

Setup: pip install -r tests/browser/requirements.browser.txt
       python -m playwright install chromium
Run:   python tests/browser/run_smoke.py
No URL option exists: the runner owns a loopback server and disposable state.
"""
import argparse
from contextlib import closing
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import traceback
from urllib.parse import urlsplit
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[2]


def smoke(page, base, scratch, expect):
    page.goto(base + '/files')
    expect(page.locator('#file-manager-password')).to_be_visible()
    page.locator('#file-manager-password').fill('wrong-smoke-password')
    page.get_by_role('button', name='파일함 들어가기').click()
    expect(page.locator('.auth-error')).to_contain_text('올바르지')
    page.locator('#file-manager-password').fill('smoke-access-only')
    page.get_by_role('button', name='파일함 들어가기').click()
    expect(page.locator('#file-upload-input')).to_be_attached()
    page.locator('#file-upload-input').set_input_files({
        'name': 'smoke-upload.txt', 'mimeType': 'text/plain', 'buffer': b'isolated browser fixture',
    })
    tile = page.locator('[data-item-path="smoke-upload.txt"]')
    expect(tile).to_be_visible()
    assert (scratch / 'files/smoke-upload.txt').read_bytes() == b'isolated browser fixture', 'uploaded file content mismatch'
    tile.click()
    def answer(dialog):
        if dialog.type == 'prompt':
            dialog.accept('smoke-delete-only')
        elif dialog.type == 'confirm':
            dialog.accept()
        else:
            dialog.dismiss()
    page.on('dialog', answer)
    page.locator('#delete-selection').click()
    expect(tile).to_have_count(0)
    assert not (scratch / 'files/smoke-upload.txt').exists(), 'deleted tile disappeared but file remains'
    entries = list((scratch / 'files/.trash').glob('*/metadata.json'))
    assert len(entries) == 1, f'expected one trash entry, found {len(entries)}'
    assert json.loads(entries[0].read_text())['path'] == 'smoke-upload.txt', 'trash metadata path mismatch'
    assert (entries[0].parent / 'item').read_bytes() == b'isolated browser fixture', 'trashed file content mismatch'
    page.goto(base + '/?q=fixture&source=all')
    news = page.locator('[data-search-source="news"]')
    expect(news.get_by_role('status')).to_have_text('현재 응답 없음')
    expect(page.locator('[data-search-source="books"]')).to_contain_text('격리 검색 복구 성공')
    news.locator('[data-search-retry]').click()
    expect(news).to_contain_text('격리 검색 복구 성공')
    expect(news.locator('[data-search-retry]')).to_have_count(0)


def argument_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--artifacts', type=Path, help='save synthetic failure screenshot and HTML here')
    return parser


def main(argv=None):
    args = argument_parser().parse_args(argv)
    try:
        from playwright.sync_api import sync_playwright, expect
    except ImportError:
        print('portal_browser_smoke=UNAVAILABLE dependency=playwright')
        return 2
    with tempfile.TemporaryDirectory(prefix='portal-browser-smoke-') as directory, closing(socket.socket()) as listener:
        scratch = Path(directory).resolve()
        listener.bind(('127.0.0.1', 0))
        listener.listen(128)
        listener.set_inheritable(True)
        port = listener.getsockname()[1]
        base = f'http://127.0.0.1:{port}'
        environment = {key: os.environ[key] for key in ('PATH', 'SYSTEMROOT', 'TMPDIR') if key in os.environ}
        environment['PYTHONDONTWRITEBYTECODE'] = '1'
        process = subprocess.Popen([sys.executable, str(Path(__file__).with_name('isolated_server.py')),
                                    '--root', str(scratch), '--fd', str(listener.fileno())],
                                   pass_fds=(listener.fileno(),), cwd=scratch, env=environment,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            for _ in range(100):
                if process.poll() is not None:
                    raise RuntimeError('isolated server failed')
                try:
                    with urlopen(base + '/health', timeout=0.3) as response:
                        if response.status == 200:
                            break
                except OSError:
                    time.sleep(0.05)
            else:
                raise RuntimeError('isolated server readiness timeout')
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=True,
                    executable_path=os.environ.get('PLAYWRIGHT_CHROMIUM_EXECUTABLE'))
                try:
                    context = browser.new_context(service_workers='block')
                    # Browser-origin requests can reach only this owned server.
                    context.route('**/*', lambda route: route.continue_()
                                  if (urlsplit(route.request.url).scheme == 'http' and
                                      urlsplit(route.request.url).netloc == f'127.0.0.1:{port}') else route.abort())
                    page = context.new_page()
                    try:
                        smoke(page, base, scratch, expect)
                    except Exception:
                        if args.artifacts is not None:
                            args.artifacts.mkdir(parents=True, exist_ok=True)
                            page.screenshot(path=str(args.artifacts / 'failure.png'), full_page=True)
                            (args.artifacts / 'failure.html').write_text(page.content(), encoding='utf-8')
                            (args.artifacts / 'failure.txt').write_text(traceback.format_exc(), encoding='utf-8')
                        raise
                finally:
                    browser.close()
            print('portal_browser_smoke=PASS login=PASS upload=PASS trash=PASS search_retry=PASS isolated=true')
            return 0
        except Exception as error:
            print(f'portal_browser_smoke=FAIL type={type(error).__name__} reason={error}')
            traceback.print_exc()
            return 1
        finally:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)


if __name__ == '__main__':
    raise SystemExit(main())
