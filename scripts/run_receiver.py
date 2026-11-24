"""后台启动 9090 接收服务，打印收到的会话。用于自测验证。"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from captrace.capture.receiver import start

srv = start(port=9090, on_session=lambda x: print(
    "[INGEST]", x.method, x.url, "resp_len=", len(x.response_body or ""), flush=True))
print("9090 receiver started, listening on 127.0.0.1:9090", flush=True)
srv.serve_forever()
