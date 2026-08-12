"""AI 层：基于异常上下文做根因分析并生成可提交 BUG 单。

仅在校验层发现异常（矩阵 ✗ 或 日志 ERROR）后调用。
"""
from __future__ import annotations

from captrace.config.models import AiModelConfig, BugReport, MatrixField, Session
from captrace.connectors.ai import AiGateway

SYSTEM_PROMPT = (
    "你是一名资深测试开发工程师。下面给出一次接口操作的抓取数据、"
    "三方一致性矩阵结果、数据库落库值与服务器日志。请：\n"
    "1. 定位最可能的根因（精确到代码/计算/配置层面）；\n"
    "2. 输出一份结构化 BUG 单（标题/复现步骤/期望/实际/根因/严重级）。\n"
    "只输出 BUG 单内容，语言简洁、可提交。"
)


def _build_prompt(session: Session, matrix: list[MatrixField],
                  db_rows: list, log_lines: list[str]) -> str:
    parts = [
        f"接口：{session.method} {session.url}",
        f"traceId：{session.trace_id}",
        "一致性矩阵：",
    ]
    for m in matrix:
        parts.append(f"  - {m.name}: {m.conclusion} ({m.detail})")
    parts.append("数据库落库：")
    for r in db_rows:
        parts.append(f"  - {r}")
    parts.append("相关日志：")
    parts.extend(f"  - {l}" for l in (log_lines or ["(无)"])[:20])
    return "\n".join(parts)


def analyze(session: Session, matrix: list[MatrixField],
            db_rows: list, log_lines: list[str],
            ai_cfg: AiModelConfig) -> BugReport:
    gw = AiGateway(ai_cfg)
    prompt = _build_prompt(session, matrix, db_rows, log_lines)
    text = gw.complete(SYSTEM_PROMPT, prompt)
    return BugReport(
        title=f"[{session.method}] {session.url} 三方一致性异常",
        steps=f"1. 开启代理\n2. 操作触发 {session.url}\n3. 查看综合校验",
        expected="接口入参/出参/数据库/日志 四方一致",
        actual="一致性矩阵存在不一致项",
        root_cause=text,
        severity="Major",
        raw=text,
    )
