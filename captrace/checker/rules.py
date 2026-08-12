"""校验规则匹配：根据 Session 找到对应的 CheckRule（API → 多表映射）。"""
from __future__ import annotations

import fnmatch
from urllib.parse import urlparse

from captrace.config.models import CheckRule, Session


def _api_key(session: Session) -> str:
    path = urlparse(session.url).path
    return f"{session.method.upper()} {path}"


def match_rule(session: Session, rules: list[CheckRule]) -> CheckRule | None:
    key = _api_key(session)
    for r in rules:
        if fnmatch.fnmatch(key, r.api_pattern) or fnmatch.fnmatch(key, r.api_pattern.replace("/api/", "*/api/")):
            return r
    return None
