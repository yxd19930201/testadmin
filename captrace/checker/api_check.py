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


def _flatten_dict(obj: Any, prefix: str = "") -> dict[str, Any]:
    """递归扁平化 dict/list，只保留叶子简单值；键用 '.' 连接。"""
    result: dict[str, Any] = {}
    if isinstance(obj, dict):
        for k, v in obj.items():
            key = f"{prefix}.{k}" if prefix else str(k)
            if isinstance(v, (str, int, float, bool)):
                result[key] = v
            elif isinstance(v, dict):
                result.update(_flatten_dict(v, key))
            elif isinstance(v, list) and v:
                # 列表只取第一项展开，避免把大量结果行 flatten 成爆炸字段
                result.update(_flatten_dict(v[0], key))
    elif isinstance(obj, list) and obj:
        result.update(_flatten_dict(obj[0], prefix))
    return result


def extract_request_fields(session: Session) -> dict[str, Any]:
    """提取入参扁平字段（用于矩阵 request 来源）。"""
    data = _safe_json(session.request_body)
    return _flatten_dict(data)


def extract_response_fields(session: Session) -> dict[str, Any]:
    """提取出参扁平字段（用于矩阵 response 来源）。"""
    data = _safe_json(session.response_body)
    # 常见包装：{status, msg, data}
    if isinstance(data.get("data"), dict):
        data = data["data"]
    # 支持多种列表包装：{data: {list: [...]}} / {data: {xxxList: [...]}} /
    # 顶层直接 {xxxList: [...]}
    list_key = None
    if isinstance(data.get("list"), list):
        list_key = "list"
    else:
        for k, v in data.items():
            if isinstance(v, list) and v:
                list_key = k
                break

    if list_key and data[list_key]:
        first = data[list_key][0]
        if isinstance(first, dict):
            # 同时把分页字段也带出来
            flat = _flatten_dict(first)
            flat.update(_flatten_dict({k: v for k, v in data.items() if k != list_key}))
            # 额外生成无前缀别名，让入参字段（如 companyName）能和列表首项里的
            # 同名字段直接对齐，否则会出现 request 有 companyName、response 为空。
            aliases = {k: v for k, v in first.items()
                       if isinstance(v, (str, int, float, bool)) and k not in flat}
            flat.update(aliases)
            return flat
    return _flatten_dict(data)


def extract_response_list_first(session: Session) -> dict[str, Any] | None:
    """返回出参的「首行」。

    兼容两种形态：
    - 列表接口：data.list[0] 或 data.<任意list字段>[0]
    - 单对象接口（如 submit 写接口）：data 本身就是一条记录（含 id 等字段），
      直接把该 dict 当作首行返回，使下游优先用其 id 精确反查刚写入的行。
    """
    data = _safe_json(session.response_body)
    if isinstance(data.get("data"), dict):
        data = data["data"]

    # 列表形态
    list_key = None
    if isinstance(data.get("list"), list):
        list_key = "list"
    else:
        for k, v in data.items():
            if isinstance(v, list) and v:
                list_key = k
                break
    if list_key and data[list_key]:
        first = data[list_key][0]
        if isinstance(first, dict):
            return first

    # 单对象形态：data 本身是一条记录
    if isinstance(data, dict) and data:
        return data
    return None


def extract_response_list_first_id(session: Session) -> Any | None:
    """取「首行」的主键（id / supplierId / orderId 等）。

    不仅列表接口，单对象写接口（submit）返回的对象含 id 时同样返回，
    使写接口的自动反查能直接按返回主键精确命中库里刚落地的行。
    """
    first = extract_response_list_first(session)
    if not first:
        return None

    id_keys = (
        "id", "euserId", "supplierId", "orderId", "userId", "customerId",
        "recordId", "categoryId", "goodId", "productId", "portId",
        "applicationId", "applicationNo", "procurementApplicationId",
        "paymentApplicationId", "detailId", "subOrderId",
    )
    for key in id_keys:
        v = first.get(key)
        if v not in (None, ""):
            return v
    return None
