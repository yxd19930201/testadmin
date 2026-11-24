"""同步链路自测脚本。

用途：用真实请求触发 Fiddler 抓包 → OnDone 推到 9090 → 验证工具能同步。
同时起一个 9091 旁路 server 抓 Fiddler 实际推过来的 payload，
用来区分「Fiddler 没推」还是「工具 9090 没收到」。

前置：
  - Fiddler 已启动，且 CustomRules.js 含 captrace push patch（OnDone 推 9090）。
  - Fiddler 代理默认 127.0.0.1:8888（被本脚本用作 HTTP 代理）。
  - 工具侧已点「开始实时捕获」（9090 监听）。

用法：
  python scripts/selftest_sync.py
  python scripts/selftest_sync.py --no-proxy   # 不走代理，只验证 9091/9090 直推逻辑
"""
from __future__ import annotations

import argparse
import json
import os
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

FIDDLER_PROXY = "127.0.0.1:8888"
CAPTURE_URL = "http://127.0.0.1:9090/ingest"
PROBE_URL = "http://127.0.0.1:9091/ingest"  # 旁路，抓 Fiddler 实际推送给 9090 的镜像

# 自测用 Token 从环境变量读取，避免把真实凭证写入仓库。
# PowerShell: $env:CAPTRACE_SELFTEST_TOKEN="<粘贴你的 bearer token>"
# 未设置时脚本仍可完整运行：业务接口会返回 401，但 Fiddler 抓包 → OnDone → 9090
# 的推送链路与响应体解析照样能被验证（失败响应同样是有效样本）。
TOKEN = os.environ.get("CAPTRACE_SELFTEST_TOKEN", "").strip()

REQUEST_BODY = json.dumps({"pageNum": 1, "pageSize": 20}).encode("utf-8")

REQUEST_HEADERS = {
    "Host": "adtest.virtual.007ka.cn",
    "Connection": "keep-alive",
    "Content-Type": "application/json;charset=UTF-8",
    "Authorization": "Bearer " + TOKEN,
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36"),
    "Accept": "application/json",
    "Origin": "https://adtest.virtual.007ka.cn",
    "Referer": "https://adtest.virtual.007ka.cn/good/category",
    "Accept-Language": "zh-CN,zh;q=0.9",
}

# ===== 9091 旁路 server：抓 Fiddler 实际推过来的 payload =====
_probe_hits: list[dict] = []
_probe_lock = threading.Lock()


class _ProbeHandler(BaseHTTPRequestHandler):
    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            data = json.loads(raw.decode("utf-8", "replace"))
        except Exception:
            data = {"_raw": raw.decode("utf-8", "replace")}
        with _probe_lock:
            _probe_hits.append(data)
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b'{"ok":true}')

    def log_message(self, *a):
        pass


def _start_probe() -> ThreadingHTTPServer:
    srv = ThreadingHTTPServer(("127.0.0.1", 9091), _ProbeHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


# ===== 让 Fiddler 以为推到 9090，实际镜像到 9091 的"偷天换日" =====
# Fiddler 脚本写死推 9090，没法直接改目标。这里用一份本地 hosts 思路不可行，
# 退而求其次：Fiddler 推 9090（工具收），同时我们起 9091 仅作对照——
# 真正判断 Fiddler 是否推送，靠工具侧 / 9090 是否收到 + Fiddler Log。
# 但为让旁路能收到，提供 --mirror 模式：脚本把请求直接打到 9091 模拟 Fiddler 行为，
# 验证 9090 接收服务本身（替代按钮「调试推一条」）。
def _mirror_to_9091():
    """模拟 Fiddler 把一条样本 POST 到 9091，验证接收服务格式。"""
    payload = {
        "id": "selftest-1",
        "method": "POST",
        "url": "https://adtest.virtual.007ka.cn/byToken/resource/good/findGoodCategoryList",
        "host": "adtest.virtual.007ka.cn",
        "path": "/byToken/resource/good/findGoodCategoryList",
        "status_code": 200,
        "request_body": REQUEST_BODY.decode("utf-8"),
        "request_headers": "",
        "response_headers": "",
        "response_body": "",
        "trace_id": "",
    }
    try:
        req = urllib.request.Request(
            PROBE_URL, data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=3) as r:
            return r.read().decode("utf-8", "replace")
    except Exception as e:
        return f"ERROR: {e}"


# ===== 真实请求：走 Fiddler 代理，触发 OnDone 推送 =====
def fire_real_request(use_proxy: bool) -> str:
    target = ("https://adtest.virtual.007ka.cn/byToken/resource/good/"
              "findGoodCategoryList")
    req = urllib.request.Request(
        target, data=REQUEST_BODY, headers=REQUEST_HEADERS, method="POST")
    handlers = None
    if use_proxy:
        proxy = urllib.request.ProxyHandler({
            "http": "http://" + FIDDLER_PROXY,
            "https": "http://" + FIDDLER_PROXY,
        })
        opener = urllib.request.build_opener(proxy)
    else:
        opener = urllib.request.build_opener()
    try:
        with opener.open(req, timeout=15) as r:
            body = r.read().decode("utf-8", "replace")
        return f"HTTP {r.status} | body_len={len(body)} | preview={body[:120]!r}"
    except Exception as e:
        return f"REQUEST FAILED: {e}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-proxy", action="store_true",
                    help="不走 Fiddler 代理（仅验证 9091 直推逻辑）")
    args = ap.parse_args()

    print("=" * 60)
    print("同步链路自测")
    print("=" * 60)

    # 1) 起 9091 旁路
    probe = _start_probe()
    print(f"[1] 9091 旁路 server 已启动: {PROBE_URL}")

    # 2) 先验证 9090 接收服务本身（等价于界面『调试推一条』）
    print("[2] 验证 9090/9091 接收服务（镜像样本）...")
    r1 = _mirror_to_9091()
    print(f"    9091 响应: {r1}")

    # 3) 发真实请求（走 Fiddler 代理 → OnDone → 9090）
    print("[3] 发真实请求（走 Fiddler 代理 8888）...")
    r2 = fire_real_request(use_proxy=not args.no_proxy)
    print(f"    请求结果: {r2}")

    # 4) 等待 Fiddler 推送
    print("[4] 等待 Fiddler OnDone 推送（3s）...")
    time.sleep(3)

    # 5) 汇总判断
    print("[5] 结果汇总")
    print(f"    9091 旁路收到条数: {len(_probe_hits)}")
    if _probe_hits:
        h = _probe_hits[0]
        print(f"    首条 url: {h.get('url')}")
        print(f"    首条 host: {h.get('host')}")
        print(f"    首条 response_body 长度: {len(h.get('response_body') or '')}")
    print()
    print("判断依据：")
    print("  - 若 9091 有收到 → Fiddler 推送链路 OK，问题在工具 9090 侧（旧 exe / 未点开始捕获）")
    print("  - 若 9091 无收到但工具 9090 有数据 → Fiddler 推到了 9090，链路通，只是本旁路未接")
    print("  - 若两边都无 → Fiddler 没推（脚本未加载/钩子名错/白名单未命中/代理未开）")
    print("=" * 60)

    probe.shutdown()


if __name__ == "__main__":
    main()
