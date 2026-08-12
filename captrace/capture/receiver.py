"""方案A：接收 Fiddler CustomRules 推送的本地 HTTP 采集服务。

FiddlerScript 在 OnDoneResponse 中把完成的 session POST 到
http://127.0.0.1:9090/ingest 即可实现毫秒级实时捕获。
"""
from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable

from captrace.config.models import CaptureSource, Session


def _make_handler(on_session: Callable[[Session], None]):
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            if self.path.rstrip("/") != "/ingest":
                self.send_response(404)
                self.end_headers()
                return
            length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(length) if length else b"{}"
            try:
                payload = json.loads(raw.decode("utf-8"))
                sess = Session(
                    id=payload.get("id", ""),
                    method=payload.get("method", "GET"),
                    url=payload.get("url", ""),
                    request_body=payload.get("request_body"),
                    response_body=payload.get("response_body"),
                    status_code=payload.get("status_code"),
                    trace_id=payload.get("trace_id"),
                    order_id=payload.get("order_id"),
                    source=CaptureSource.FIDDLER_PUSH,
                )
                on_session(sess)
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'{"ok":true}')
            except Exception as e:  # noqa: BLE001
                self.send_response(400)
                self.end_headers()
                self.wfile.write(str(e).encode("utf-8"))

        def log_message(self, *args):  # 静默访问日志
            pass

    return Handler


def start(port: int = 9090, on_session: Callable[[Session], None] = print) -> ThreadingHTTPServer:
    srv = ThreadingHTTPServer(("127.0.0.1", port), _make_handler(on_session))
    return srv  # 调用方 srv.serve_forever()
