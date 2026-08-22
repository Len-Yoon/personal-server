"""Loopback-only Hyundai OAuth callback endpoint."""

from __future__ import annotations

from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from typing import Protocol
from urllib.parse import parse_qs, urlparse


class HyundaiAuthorizationCompleter(Protocol):
    def complete_authorization(self, code: str, state: str) -> str: ...


class HyundaiOAuthCallbackServer:
    """Expose only the Hyundai OAuth callback and a local health endpoint."""

    def __init__(
        self,
        completer: HyundaiAuthorizationCompleter,
        host: str = "127.0.0.1",
        port: int = 8015,
    ) -> None:
        self._completer = completer
        self._server = ThreadingHTTPServer((host, port), self._handler_type())
        self._thread: Thread | None = None

    @property
    def port(self) -> int:
        return int(self._server.server_address[1])

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=2)
            self._thread = None

    def _handler_type(self) -> type[BaseHTTPRequestHandler]:
        completer = self._completer

        class CallbackHandler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                parsed = urlparse(self.path)
                if parsed.path == "/health":
                    self._respond(HTTPStatus.OK, "ok")
                    return
                if parsed.path == "/oauth/hyundai/data-redirect":
                    self._respond(HTTPStatus.OK, "현대 차량 데이터 제공 동의가 완료되었습니다.")
                    return
                if parsed.path != "/oauth/hyundai/callback":
                    self._respond(HTTPStatus.NOT_FOUND, "not found")
                    return
                query = parse_qs(parsed.query)
                code = _single_value(query, "code")
                state = _single_value(query, "state")
                if not code or not state:
                    self._respond(HTTPStatus.BAD_REQUEST, "잘못된 OAuth 콜백 요청입니다.")
                    return
                try:
                    result = completer.complete_authorization(code, state)
                except ValueError:
                    self._respond(HTTPStatus.BAD_REQUEST, "인증 요청이 만료되었거나 유효하지 않습니다.")
                    return
                except OSError:
                    self._respond(HTTPStatus.BAD_GATEWAY, "현대 인증 처리에 실패했습니다. 다시 시도하세요.")
                    return
                message = result if isinstance(result, str) else "현대 차량 연결이 완료되었습니다. Telegram으로 돌아가세요."
                self._respond(HTTPStatus.OK, message)

            def do_POST(self) -> None:  # noqa: N802
                if urlparse(self.path).path == "/oauth/hyundai/data-redirect":
                    self._respond(HTTPStatus.OK, "ok")
                    return
                self._respond(HTTPStatus.NOT_FOUND, "not found")

            def log_message(self, _format: str, *_args: object) -> None:
                """Do not log OAuth code or state from request paths."""

            def _respond(self, status: HTTPStatus, text: str) -> None:
                body = text.encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)

        return CallbackHandler


def _single_value(query: dict[str, list[str]], name: str) -> str | None:
    values = query.get(name)
    if values is None or len(values) != 1:
        return None
    value = values[0].strip()
    return value or None
