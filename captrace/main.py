"""入口编排：把抓包 → 校验 → (异常)AI 全流程串起来，并提供 CLI。

用法：
    python -m captrace.main --demo       # 跑通样例闭环
    python -m captrace.main --serve      # 启动实时捕获接收服务(方案A)
"""
from __future__ import annotations

import argparse
import sys

from captrace.capture.receiver import start as start_receiver
from captrace.checker import matrix as matrix_mod
from captrace.checker.api_check import (
    extract_request_fields,
    extract_response_fields,
)
from captrace.checker.rules import match_rule
from captrace.config.models import (
    AiModelConfig, CaptureSource, CheckRule, DbConnection, DbType,
    LogSource, LogType, Session, TableMapping,
)
from captrace.config.settings import ProjectConfig


def _demo_session() -> Session:
    return Session(
        id="demo-1",
        method="POST",
        url="https://shop.test/api/order/create",
        request_body='{"orderId":88231,"amount":99.00,"couponId":5}',
        response_body='{"code":0,"data":{"orderId":88231,"payAmount":99.00,"status":"PAID"}}',
        status_code=200,
        trace_id="tx-88231-abc",
        order_id="88231",
        source=CaptureSource.FIDDLER_PUSH,
    )


def _demo_project() -> ProjectConfig:
    p = ProjectConfig("订单域-测试", env=__import__("captrace.config.models", fromlist=["Env"]).Env.TEST)
    p.db_connections.append(DbConnection("order_db", DbType.MYSQL, "10.20.3.11", 3306, "order_db", "qa", read_only=True))
    p.log_sources.append(LogSource("order-svc", LogType.REMOTE_FILE, "/var/log/order-svc/*.log", read_only=True))
    p.rules.append(CheckRule("POST /api/order/create", tables=[
        TableMapping("t_order", "orderId", "order_id"),
        TableMapping("t_order_item", "orderId", "order_id"),
        TableMapping("t_coupon_record", "couponId", "coupon_id"),
    ]))
    return p


def run_checks(session: Session, project: ProjectConfig) -> dict:
    """核心：接口 → 矩阵（确定性比对）。"""
    req = extract_request_fields(session)
    resp = extract_response_fields(session)

    # 期望 DB/日志值来源映射（真实实现由 db_check / log_check 填充）
    fields = {
        "orderId": {"request": req.get("orderId"), "response": resp.get("orderId"), "db": 88231, "log": 88231},
        "amount/pay_amount": {"request": req.get("amount"), "response": resp.get("payAmount"), "db": 99.00, "log": "expected 104.00"},
        "status": {"response": resp.get("status"), "db": "PAID", "log": "PAID"},
        "coupon/discount": {"request": req.get("couponId"), "db": 5, "log": "5"},
    }
    result = matrix_mod.build_matrix(fields)
    print(matrix_mod.summarize(result))
    for m in result:
        mark = "✓" if m.conclusion == "consistent" else ("✗" if m.conclusion == "inconsistent" else "·")
        print(f"  {mark} {m.name}: {m.detail}")
    return {"matrix": result, "has_anomaly": matrix_mod.has_anomaly(result)}


def cli():
    ap = argparse.ArgumentParser(description="抓包测效助手")
    ap.add_argument("--demo", action="store_true", help="跑通样例闭环")
    ap.add_argument("--serve", action="store_true", help="启动实时捕获接收服务")
    ap.add_argument("--port", type=int, default=9090)
    args = ap.parse_args()

    if args.serve:
        srv = start_receiver(port=args.port, on_session=lambda s: print(f"[ingest] {s.method} {s.url}"))
        print(f"实时捕获接收服务已启动: http://127.0.0.1:{args.port}/ingest")
        try:
            srv.serve_forever()
        except KeyboardInterrupt:
            srv.shutdown()
        return

    # 默认 demo
    session = _demo_session()
    project = _demo_project()
    print(f"== 综合校验: {session.method} {session.url} ==")
    out = run_checks(session, project)
    if out["has_anomaly"]:
        print("[提示] 检测到不一致，可调用 ai.analyzer.analyze(...) 生成 BUG 单（需配置 AI 模型）。")


if __name__ == "__main__":
    cli()
