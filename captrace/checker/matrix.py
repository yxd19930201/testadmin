"""三方一致性矩阵 —— 工具自带能力（确定性比对，不依赖 AI）。

把接口入参 / 出参 / 数据库 / 日志 四类信号归一化为 (业务字段, 来源, 值)，
按业务字段做相等性比较，输出 ✓(consistent) / ✗(inconsistent) / 部分(partial)。

设计要点：
- 纯本地、可审计、可离线、不幻觉。
- 数值类字段按浮点比较（99.00 == 99），文本类按归一化字符串比较。
"""
from __future__ import annotations

from typing import Any

from captrace.config.models import MatrixField

SOURCE_ORDER = ["request", "response", "db", "log"]


def _norm_str(v: Any) -> str:
    return "" if v is None else str(v).strip()


def _to_float(v: Any):
    try:
        return float(str(v).strip())
    except (ValueError, TypeError):
        return None


def build_matrix(fields: dict[str, dict[str, Any]]) -> list[MatrixField]:
    """fields: {业务字段: {来源: 值}}，来源 ∈ request/response/db/log。"""
    results: list[MatrixField] = []
    for name, sources in fields.items():
        present = {k: v for k, v in sources.items() if _norm_str(v) != ""}
        if not present:
            results.append(MatrixField(name, sources, "partial", "无可用数据"))
            continue

        values = list(present.values())
        # 只有单侧来源时，不存在"一致"或"不一致"，应标为 partial（缺少对照）
        if len(values) == 1:
            only_source = list(present.keys())[0]
            results.append(
                MatrixField(name, sources, "partial", f"仅 {only_source} 侧有值，缺少其他来源对照")
            )
            continue

        floats = [_to_float(v) for v in values]
        if all(f is not None for f in floats):
            # 数值比较（带极小容差）
            lo, hi = min(floats), max(floats)
            if hi - lo < 1e-9:
                results.append(MatrixField(name, sources, "consistent", "数值全部一致"))
            else:
                diff = " | ".join(f"{k}={present[k]}" for k in present)
                results.append(MatrixField(name, sources, "inconsistent", f"数值差异: {diff}"))
        else:
            norm = {k: _norm_str(v) for k, v in present.items()}
            if len(set(norm.values())) == 1:
                results.append(MatrixField(name, sources, "consistent", "文本全部一致"))
            else:
                diff = " | ".join(f"{k}={v}" for k, v in norm.items())
                results.append(MatrixField(name, sources, "inconsistent", f"文本差异: {diff}"))
    return results


def has_anomaly(matrix: list[MatrixField]) -> bool:
    return any(m.conclusion == "inconsistent" for m in matrix)


def summarize(matrix: list[MatrixField]) -> str:
    bad = sum(1 for m in matrix if m.conclusion == "inconsistent")
    ok = sum(1 for m in matrix if m.conclusion == "consistent")
    partial = sum(1 for m in matrix if m.conclusion == "partial")
    return f"✗ {bad} 项不一致 · ✓ {ok} 项一致 · ◑ {partial} 项缺少对照"
