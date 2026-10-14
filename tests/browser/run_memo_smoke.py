#!/usr/bin/env python3
"""Actual Book/YouTube Chromium smoke against owned loopback and scratch state."""
import argparse
from contextlib import closing
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
from urllib.parse import urlsplit
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[2]
SERVICES = ('book-memo', 'youtube-memo')


def is_owned_request(url, base):
    parsed, owned = urlsplit(url), urlsplit(base)
    return (parsed.scheme == owned.scheme == 'http' and parsed.netloc == owned.netloc
            and parsed.hostname == '127.0.0.1' and parsed.username is None and parsed.password is None)


def assert_layout(page):
    dimensions = page.evaluate('''() => ({width: innerWidth,
        document: document.documentElement.scrollWidth, body: document.body.scrollWidth})''')
    assert max(dimensions['document'], dimensions['body']) <= dimensions['width'] + 1, dimensions
    for selector in ['#import-file', '#import-preview']:
        box = page.locator(selector).bounding_box()
        assert box and box['width'] > 0 and box['height'] > 0, selector
        assert box['x'] >= -1 and box['x'] + box['width'] <= dimensions['width'] + 1, (selector, box)


def smoke(page, base, scratch, expect):
    page.goto(base + '/import')
    expect(page.locator('input[name="password"]')).to_be_visible()
    assert urlsplit(page.url).path == '/auth/login'
    page.locator('input[name="password"]').fill('memo-smoke-only')
    page.locator('button[type="submit"]').click()
    expect(page.get_by_role('button', name='쓰기 로그아웃', exact=True)).to_be_visible()
    page.goto(base + '/import')
    expect(page.locator('#import-file')).to_be_visible()
    fixture = (scratch / 'import-fixture.json').read_bytes()
    commit = page.locator('#import-commit')
    for width in (1280, 375):
        page.set_viewport_size({'width': width, 'height': 900})
        page.goto(base + '/import')
        expect(commit).to_be_hidden()
        expect(commit).to_be_disabled()
        assert commit.evaluate('(element) => element.hidden')
        assert commit.evaluate('(element) => element.getBoundingClientRect().height') == 0
        assert_layout(page)
        page.locator('#import-preview').click()
        expect(page.locator('#import-status')).to_contain_text('JSON 파일을 선택')
        page.locator('#import-file').set_input_files({'name': 'fixture.json', 'mimeType': 'application/json', 'buffer': fixture})
        page.locator('#import-preview').click()
        expect(page.locator('#import-status')).to_contain_text('아직 저장하지 않았습니다')
        expect(commit).to_be_visible()
        expect(commit).to_be_enabled()
        assert_layout(page)
        page.locator('#import-file').set_input_files({'name': 'invalid.json', 'mimeType': 'application/json', 'buffer': b'{invalid'})
        expect(commit).to_be_hidden()
        expect(commit).to_be_disabled()
        assert commit.evaluate('(element) => element.getBoundingClientRect().height') == 0
        page.locator('#import-preview').click()
        expect(page.locator('#import-status')).not_to_have_text('')
        expect(commit).to_be_hidden()
        page.locator('#import-file').set_input_files({'name': 'again.json', 'mimeType': 'application/json', 'buffer': fixture})
        expect(page.locator('#import-status')).to_have_text('')
        page.locator('#import-preview').click()
        expect(commit).to_be_visible()
        expect(commit).to_be_enabled()
    # Commit changes only the disposable DB, then hide the stale confirmation.
    commit.click()
    expect(page.locator('#import-status')).to_contain_text('가져오기 완료: 1개 저장')
    expect(commit).to_be_hidden()
    expect(commit).to_be_disabled()
    detail_path = (scratch / 'detail-path.txt').read_text(encoding='utf-8')
    page.goto(base + detail_path)
    trash = page.locator('#memo-trash')
    expect(trash).to_contain_text('총 1개')
    button = page.locator('#memo-trash-purge button')
    expect(button).to_be_disabled()
    page.locator('.memo-trash-select').check()
    expect(button).to_be_enabled()
    def cancel(dialog):
        assert dialog.type == 'confirm' and '영구 삭제' in dialog.message and '복구할 수 없습니다' in dialog.message
        dialog.dismiss()
    page.once('dialog', cancel)
    button.click()
    page.reload()
    expect(trash).to_contain_text('총 1개')
    expect(page.get_by_role('button', name='메모 복구', exact=True)).to_be_visible()


def argument_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--service', choices=SERVICES, action='append')
    parser.add_argument('--artifacts', type=Path, help='save synthetic failure screenshot and HTML here')
    return parser


def run_service(service, artifacts=None):
    try:
        from playwright.sync_api import sync_playwright, expect
    except ImportError:
        print(f'{service}_browser_smoke=UNAVAILABLE dependency=playwright')
        return 2
    local_python = ROOT / '.venv' / service / 'bin/python'
    server_python = str(local_python) if local_python.exists() else sys.executable
    with tempfile.TemporaryDirectory(prefix='memo-browser-smoke-') as directory, closing(socket.socket()) as listener:
        scratch = Path(directory).resolve()
        listener.bind(('127.0.0.1', 0))
        listener.listen(128)
        listener.set_inheritable(True)
        base = f'http://127.0.0.1:{listener.getsockname()[1]}'
        environment = {key: os.environ[key] for key in ('PATH', 'SYSTEMROOT', 'TMPDIR') if key in os.environ}
        environment['PYTHONDONTWRITEBYTECODE'] = '1'
        server_log = (scratch / 'server.log').open('w+')
        process = subprocess.Popen([server_python, str(Path(__file__).with_name('isolated_memo_server.py')),
                                    '--service', service, '--root', str(scratch), '--fd', str(listener.fileno())],
                                   pass_fds=(listener.fileno(),), cwd=scratch, env=environment,
                                   stdout=server_log, stderr=server_log)
        try:
            for _ in range(100):
                if process.poll() is not None:
                    server_log.flush()
                    raise RuntimeError('isolated server failed: ' + (scratch / 'server.log').read_text()[-4000:])
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
                    context.route('**/*', lambda route: route.continue_()
                                  if is_owned_request(route.request.url, base) else route.abort())
                    page = context.new_page()
                    try:
                        smoke(page, base, scratch, expect)
                    except Exception:
                        if artifacts is not None:
                            destination = artifacts / service
                            destination.mkdir(parents=True, exist_ok=True)
                            page.screenshot(path=str(destination / 'failure.png'), full_page=True)
                            (destination / 'failure.html').write_text(page.content(), encoding='utf-8')
                        raise
                finally:
                    browser.close()
            print(f'{service}_browser_smoke=PASS login=PASS import=PASS reset=PASS hidden=PASS layout=PASS trash_cancel=PASS isolated=true')
            return 0
        except Exception as error:
            print(f'{service}_browser_smoke=FAIL type={type(error).__name__} reason={error}')
            return 1
        finally:
            process.terminate()
            server_log.close()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)


def main(argv=None):
    args = argument_parser().parse_args(argv)
    results = [run_service(service, args.artifacts) for service in (args.service or SERVICES)]
    return 1 if 1 in results else (2 if any(results) else 0)


if __name__ == '__main__':
    raise SystemExit(main())
