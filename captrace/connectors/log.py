"""日志连接器：按 traceId / orderId 检索日志（ELK / 远程文件 / 容器）。"""
from __future__ import annotations

import re
import subprocess

from captrace.config.models import LogSource, LogType


class LogConnector:
    def __init__(self, cfg: LogSource):
        if not cfg.read_only:
            raise PermissionError("日志连接器仅允许只读检索")
        self.cfg = cfg

    def query(self, keyword: str, lines: int = 200) -> list[str]:
        if self.cfg.log_type == LogType.REMOTE_FILE:
            return self._grep_file(self.cfg.address, keyword, lines)
        if self.cfg.log_type == LogType.ELK:
            return self._query_elk(keyword)
        if self.cfg.log_type == LogType.CONTAINER:
            return self._query_container(keyword, lines)
        return []

    def _grep_file(self, path: str, keyword: str, lines: int) -> list[str]:
        try:
            out = subprocess.run(
                ["grep", "-i", "-E", keyword, path],
                capture_output=True, text=True, timeout=20,
            )
            return out.stdout.splitlines()[:lines]
        except Exception as e:  # noqa: BLE001
            return [f"[检索失败] {e}"]

    def _query_elk(self, keyword: str) -> list[str]:
        # 真实实现：POST {address}/{index}/_search with query
        # 骨架返回占位，避免引入硬依赖
        return [f"[ELK] 应在 {self.cfg.address}/{self.cfg.index} 检索: {keyword}"]

    def _query_container(self, keyword: str, lines: int) -> list[str]:
        try:
            out = subprocess.run(
                ["kubectl", "logs", "-n", self.cfg.address, "--tail", str(lines)],
                capture_output=True, text=True, timeout=30,
            )
            pat = re.compile(keyword, re.I)
            return [l for l in out.stdout.splitlines() if pat.search(l)]
        except Exception as e:  # noqa: BLE001
            return [f"[容器日志检索失败] {e}"]
