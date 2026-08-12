"""方案C：解析 Fiddler .saz 存档为 Session 列表。

.saz 是 ZIP 包，经典结构含 raw/<id>_c.txt(请求) 与 raw/<id>_s.txt(响应)；
新版亦可能含 Sessions.json。这里解析 raw 文本文件。
"""
from __future__ import annotations

import re
import zipfile

from captrace.config.models import CaptureSource, Session


def import_saz(path: str) -> list[Session]:
    sessions: list[Session] = []
    with zipfile.ZipFile(path) as z:
        names = z.namelist()
        req_map, resp_map = {}, {}
        for n in names:
            m = re.match(r"raw/(\d+)_c\.txt$", n)
            if m:
                req_map[m.group(1)] = n
            m = re.match(r"raw/(\d+)_s\.txt$", n)
            if m:
                resp_map[m.group(1)] = n

        for sid in sorted(set(req_map) | set(resp_map), key=int):
            req_text = z.read(req_map[sid]).decode("utf-8", "ignore") if sid in req_map else ""
            resp_text = z.read(resp_map[sid]).decode("utf-8", "ignore") if sid in resp_map else ""
            sessions.append(_parse_raw(sid, req_text, resp_text))
    return sessions


def _parse_raw(sid: str, req_text: str, resp_text: str) -> Session:
    method, url, req_body = _split_http(req_text)
    _, _, resp_body = _split_http(resp_text)
    status = None
    m = re.match(r"HTTP/\d\.\d\s+(\d+)", resp_text.split("\r\n", 1)[0] if resp_text else "")
    if m:
        status = int(m.group(1))
    return Session(
        id=f"saz-{sid}",
        method=method or "GET",
        url=url or "",
        request_body=req_body or None,
        response_body=resp_body or None,
        status_code=status,
        source=CaptureSource.SAZ_IMPORT,
    )


def _split_http(text: str) -> tuple[str, str, str]:
    if not text:
        return "", "", ""
    head, _, body = text.partition("\r\n\r\n")
    first = head.split("\r\n", 1)[0] if head else ""
    m = re.match(r"(\w+)\s+(\S+)", first)
    method, url = (m.group(1), m.group(2)) if m else ("", "")
    return method, url, body
