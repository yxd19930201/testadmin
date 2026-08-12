"""接口校验：从抓包 Session 提取入参/出参字段，供矩阵比对。"""
from __future__ import annotations

import json
from typing import Any

from captrace.config.models import Session


def _safe_json(text: str | None) -> dict:
    if not text:
        return {}
    try:
        return json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return {}


def extract_request_fields(session: Session) -> dict[str, Any]:
    """提取入参扁平字段（用于矩阵 request 来源）。"""
    data = _safe_json(session.request_body)
    return {k: v for k, v in data.items() if isinstance(v, (str, int, float, bool))}


def extract_response_fields(session: Session) -> dict[str, Any]:
    """提取出参扁平字段（用于矩阵 response 来源）。"""
    data = _safe_json(session.response_body)
    # 常见包装：{data: {...}}，下钻一层
    if isinstance(data.get("data"), dict):
        data = data["data"]
    return {k: v for k, v in data.items() if isinstance(v, (str, int, float, bool))}
