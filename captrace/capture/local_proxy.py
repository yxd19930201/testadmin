"""本地正向代理抓包（纯标准库，离线可用）。

监听 127.0.0.1:<port>，浏览器/App 把 HTTP 代理指向它即可抓包。
- HTTP 请求：明文可解密，记录 request/response body。
- HTTPS (CONNECT 隧道)：无法解密 body，但记录 host、method、status、headers、url。
- 域名过滤：支持白名单（只抓这些域名）或黑名单（排除这些域名）。

所有捕获的会话通过 on_session 回调推入同一会话池，后续规则匹配/查库/拉日志流程不变。
"""

from __future__ import annotations

import socket
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Callable, Optional
from urllib.parse import urlparse

from captrace.config.models import CaptureSource, Session


class _ProxyHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    # 由 LocalProxy 在 serve_forever 前注入
    on_session: Callable[[Session], None] = lambda s: None
    domain_filter: dict = {"mode": "off", "domains": []}
    upstream: Optional[str] = None  # 可选上游代理 "http://127.0.0.1:7897"，为空则直连

    def _allowed(self, host: str) -> bool:
        f = self.domain_filter
        mode = (f.get("mode") or "off").lower()
        domains = [d.strip().lower() for d in (f.get("domains") or []) if d.strip()]
        if mode == "off" or not domains:
            return True
        h = host.lower()
        matched = any((d in h) or (h in d) for d in domains)
        if mode == "whitelist":
            return matched
        if mode == "blacklist":
            return not matched
        return True

    def _connect_upstream(self, host: str, port: int) -> Optional[socket.socket]:
        """建立到真实目标的连接：若配置了上游代理则走代理，否则直连。"""
        if self.upstream:
            # 走上游代理：向上游发起 CONNECT 隧道
            u = urlparse(self.upstream)
            ph = u.hostname or "127.0.0.1"
            pp = u.port or (443 if u.scheme == "https" else 80)
            try:
                sock = socket.create_connection((ph, pp), timeout=10)
            except OSError as e:
                print(f"[captrace] 连接上游代理失败 {ph}:{pp}: {e}")
                return None
            sock.sendall(f"CONNECT {host}:{port} HTTP/1.1\r\nHost: {host}:{port}\r\n\r\n".encode())
            # 读取上游响应状态行 + 头部
            buf = b""
            while b"\r\n\r\n" not in buf:
                chunk = sock.recv(4096)
                if not chunk:
                    sock.close()
                    return None
                buf += chunk
            status_line = buf[:buf.find(b"\r\n")].decode("utf-8", "replace")
            parts = status_line.split(" ", 2)
            code = int(parts[1]) if len(parts) >= 2 else 0
            if code != 200:
                print(f"[captrace] 上游代理拒绝 CONNECT: {status_line}")
                sock.close()
                return None
            return sock
        # 直连目标
        try:
            return socket.create_connection((host, port), timeout=10)
        except OSError as e:
            print(f"[captrace] 直连目标失败 {host}:{port}: {e}")
            return None

    # ---------- CONNECT (HTTPS) ----------
    def do_CONNECT(self):
        host, _, port = self.path.partition(":")
        port = int(port) if port else 443
        if not self._allowed(host):
            self.send_response(403)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        upstream = self._connect_upstream(host, port)
        if upstream is None:
            self.send_response(502)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self.send_response(200)
        self.end_headers()
        # 建立双向隧道，但不解密；仅记录连接元数据
        self._build_session(
            method="CONNECT",
            url=f"https://{host}:{port}",
            status_code=200,
            request_body="",
            response_body="",
            host=host,
        )
        # 透传字节流（不解析）
        self._tunnel(self.connection, upstream)

    def _tunnel(self, client, upstream):
        def pipe(src, dst):
            try:
                while True:
                    buf = src.recv(65536)
                    if not buf:
                        break
                    dst.sendall(buf)
            except OSError:
                pass

        t1 = threading.Thread(target=pipe, args=(client, upstream), daemon=True)
        t2 = threading.Thread(target=pipe, args=(upstream, client), daemon=True)
        t1.start()
        t2.start()
        t1.join()
        t2.join()
        for s in (client, upstream):
            try:
                s.close()
            except OSError:
                pass

    # ---------- 普通 HTTP 请求 ----------
    def do_GET(self):
        self._handle_http("GET")

    def do_POST(self):
        self._handle_http("POST")

    def do_PUT(self):
        self._handle_http("PUT")

    def do_DELETE(self):
        self._handle_http("DELETE")

    def do_PATCH(self):
        self._handle_http("PATCH")

    def _handle_http(self, method: str):
        parsed = urlparse(self.path)
        host = parsed.netloc or self.headers.get("Host", "")
        if not self._allowed(host):
            self.send_response(403)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        body = b""
        cl = int(self.headers.get("Content-Length", 0) or 0)
        if cl:
            body = self.rfile.read(cl)
        # 转发到目标（经上游代理或直连）
        try:
            if self.upstream:
                # 经上游 HTTP 代理转发：直接把完整 URL 作为 path
                u = urlparse(self.upstream)
                ph = u.hostname or "127.0.0.1"
                pp = u.port or (443 if u.scheme == "https" else 80)
                with socket.create_connection((ph, pp), timeout=10) as sock:
                    sock.sendall(f"{method} {self.path} HTTP/1.1\r\n".encode())
                    for k, v in self.headers.items():
                        if k.lower() in ("proxy-connection", "proxy-authorization"):
                            continue
                        sock.sendall(f"{k}: {v}\r\n".encode())
                    sock.sendall(b"Connection: close\r\n\r\n")
                    if body:
                        sock.sendall(body)
                    resp = self._recv_all(sock)
            else:
                scheme = "https" if parsed.scheme == "https" else "http"
                target = (parsed.netloc or host).partition(":")[0]
                port = int((parsed.netloc or host).partition(":")[2] or (443 if scheme == "https" else 80))
                with socket.create_connection((target, port), timeout=10) as sock:
                    if scheme == "https":
                        import ssl
                        ctx = ssl.create_default_context()
                        sock = ctx.wrap_socket(sock, server_hostname=target)
                    req_line = f"{method} {self.path} HTTP/1.1\r\n"
                    sock.sendall(req_line.encode())
                    for k, v in self.headers.items():
                        if k.lower() in ("proxy-connection", "proxy-authorization"):
                            continue
                        sock.sendall(f"{k}: {v}\r\n".encode())
                    sock.sendall(b"Connection: close\r\n\r\n")
                    if body:
                        sock.sendall(body)
                    resp = self._recv_all(sock)
        except OSError as e:
            self.send_response(502)
            self.send_header("Content-Length", "0")
            self.end_headers()
            self._build_session(method, self.path, 502, body.decode("utf-8", "replace"), str(e), host)
            return
        # 解析响应状态行与 body
        status_code = 0
        resp_body = ""
        header_end = resp.find(b"\r\n\r\n")
        if header_end != -1:
            status_line = resp[:resp.find(b"\r\n")].decode("utf-8", "replace")
            parts = status_line.split(" ", 2)
            if len(parts) >= 2:
                try:
                    status_code = int(parts[1])
                except ValueError:
                    status_code = 0
            resp_body = resp[header_end + 4:].decode("utf-8", "replace")
        # 回写客户端
        self.send_response(status_code or 502)
        for line in resp.split(b"\r\n\r\n", 1)[0].split(b"\r\n")[1:]:
            try:
                k, v = line.decode("utf-8", "replace").partition(":")[::2]
                if k.strip().lower() in ("transfer-encoding", "connection"):
                    continue
                self.send_header(k.strip(), v.strip())
            except Exception:
                pass
        self.end_headers()
        if resp_body:
            self.wfile.write(resp_body.encode("utf-8", "replace"))
        self._build_session(
            method, self.path, status_code or 502,
            body.decode("utf-8", "replace"), resp_body, host,
        )

    @staticmethod
    def _recv_all(sock) -> bytes:
        data = b""
        sock.settimeout(5)
        try:
            while True:
                buf = sock.recv(65536)
                if not buf:
                    break
                data += buf
        except OSError:
            pass
        return data

    def _build_session(self, method, url, status_code, request_body, response_body, host, trace_id=""):
        sid = f"local-{threading.get_ident()}-{id(self)}"
        session = Session(
            id=sid,
            method=method,
            url=url,
            host=host,
            request_body=request_body or None,
            response_body=response_body or None,
            status_code=status_code,
            trace_id=trace_id,
            source=CaptureSource.BUILTIN_PROXY,
        )
        try:
            self.on_session(session)
        except Exception:
            pass

    def log_message(self, format, *args):  # noqa: A002
        return  # 静默


class LocalProxy:
    def __init__(self, on_session: Callable[[Session], None], port: int = 9091, upstream: str = "") -> None:
        self._on_session = on_session
        self._port = port
        self._upstream = upstream or ""
        self._server: Optional[HTTPServer] = None
        self._thread: Optional[threading.Thread] = None
        self._domain_filter: dict = {"mode": "off", "domains": []}

    def set_filter(self, mode: str, domains: list[str]) -> None:
        self._domain_filter = {"mode": (mode or "off").lower(), "domains": domains or []}

    def set_upstream(self, upstream: str) -> None:
        self._upstream = upstream or ""

    def start(self) -> dict:
        if self._server:
            return {"ok": True, "port": self._port, "running": True}
        handler = _ProxyHandler
        handler.on_session = self._on_session
        handler.domain_filter = self._domain_filter
        handler.upstream = self._upstream or None
        try:
            self._server = HTTPServer(("127.0.0.1", self._port), handler)
        except OSError as e:
            return {"ok": False, "error": str(e)}
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return {"ok": True, "port": self._port, "running": True}

    def stop(self) -> dict:
        if self._server:
            try:
                self._server.shutdown()
                self._server.server_close()
            except Exception:
                pass
            self._server = None
            self._thread = None
        return {"ok": True, "running": False}

    def status(self) -> dict:
        return {
            "running": self._server is not None,
            "port": self._port,
            "filter": self._domain_filter,
            "upstream": self._upstream,
        }
