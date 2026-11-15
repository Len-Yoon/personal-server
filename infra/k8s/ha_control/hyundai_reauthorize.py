"""Run only the existing legacy Hyundai callback for an interactive recovery."""

import argparse
import os
import sys
from threading import Event, Lock, local


AUTHORIZATION_SECONDS = 600
CALLBACK_DRAIN_SECONDS = 20
LEGACY_REDIRECT_URI = "https://car.len.pe.kr/oauth/hyundai/callback"


class RecoveryAuthorization:
    def __init__(self, client):
        self.client = client
        self.completed = Event()
        self.finished = Event()
        self._gate = Lock()
        self._accepting = True
        self._active = False
        self._request = local()

    def complete_authorization(self, code, state):
        with self._gate:
            if not self._accepting or self._active:
                raise ValueError("authorization window closed or busy")
            self._active = True
        self._request.active = True
        try:
            message = self.client.complete_authorization(code, state)
        except ValueError:
            raise ValueError("authorization state invalid") from None
        except Exception:
            self.close_window()
            raise OSError("authorization callback failed") from None
        self._request.authorized = True
        self.close_window()
        return message

    def response_finished(self):
        if getattr(self._request, "active", False):
            with self._gate:
                self._active = False
                if getattr(self._request, "authorized", False):
                    self.completed.set()
                if not self._accepting:
                    self.finished.set()
            self._request.active = False
            self._request.authorized = False

    def close_window(self):
        with self._gate:
            self._accepting = False
            if not self._active:
                self.finished.set()


def callback_server(server_type, recovery, host):
    class RecoveryServer(server_type):
        def _handler_type(self):
            base_handler = super()._handler_type()

            class RecoveryHandler(base_handler):
                timeout = 5

                def do_GET(self):
                    try:
                        super().do_GET()
                    finally:
                        recovery.response_finished()

            return RecoveryHandler

    return RecoveryServer(recovery, host=host)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--container-interface", action="store_true",
        help="listen on the existing loopback-published container port",
    )
    args = parser.parse_args()
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        parser.error("an interactive terminal is required")
    if "HA_PG_CONFIG" in os.environ:
        parser.error("the legacy callback cannot use the HA writer")
    if os.environ.get("HYUNDAI_TOKEN_STORE_PATH") != "/data/oauth/hyundai-token.json":
        parser.error("the dedicated legacy OAuth volume is required")
    if not all(os.environ.get(key, "").strip() for key in ("HYUNDAI_CLIENT_ID", "HYUNDAI_CLIENT_SECRET")):
        parser.error("the existing Hyundai OAuth configuration is required")
    if os.environ.get("HYUNDAI_REDIRECT_URI") != LEGACY_REDIRECT_URI:
        parser.error("the enrolled production callback is required")

    # Import the installed Car Care client and callback only. Importing the
    # worker entry point would also bring in its Telegram and database paths.
    from app.services.hyundai import DEFAULT_API_BASE_URL, DEFAULT_OAUTH_BASE_URL, HyundaiClient
    from app.services.oauth_callback import HyundaiOAuthCallbackServer

    if (os.environ.get("HYUNDAI_OAUTH_BASE_URL") or DEFAULT_OAUTH_BASE_URL) != DEFAULT_OAUTH_BASE_URL:
        parser.error("the enrolled Hyundai OAuth endpoint is required")
    if (os.environ.get("HYUNDAI_API_BASE_URL") or DEFAULT_API_BASE_URL) != DEFAULT_API_BASE_URL:
        parser.error("the enrolled Hyundai API endpoint is required")

    client = HyundaiClient.from_environment()
    recovery = RecoveryAuthorization(client)
    host = "0.0.0.0" if args.container_interface else "127.0.0.1"
    server = callback_server(HyundaiOAuthCallbackServer, recovery, host)
    server.start()
    try:
        print("이 터미널에 표시된 링크를 열어 현대차 인증을 완료하세요.", flush=True)
        print(client.begin_authorization(), flush=True)
        recovery.finished.wait(AUTHORIZATION_SECONDS)
    finally:
        recovery.close_window()
        recovery.finished.wait(CALLBACK_DRAIN_SECONDS)
        server.stop()
    if recovery.completed.is_set():
        print("현대차 인증과 차량 데이터 제공 동의를 확인했습니다.", flush=True)
        return 0
    print("완료 콜백을 확인하지 못했습니다. 토큰이 이미 바뀌었을 수 있습니다.", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
