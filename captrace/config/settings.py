"""项目配置：一个项目 = 一套可复用的 DB / 接口 / 日志 / AI 配置。"""
from __future__ import annotations

from dataclasses import asdict

from captrace.config.models import (
    AiModelConfig, CheckRule, DbConnection, Env, LogSource,
)


class ProjectConfig:
    def __init__(self, name: str, env: Env = Env.TEST):
        self.name = name
        self.env = env
        self.db_connections: list[DbConnection] = []
        self.log_sources: list[LogSource] = []
        self.rules: list[CheckRule] = []
        self.ai: AiModelConfig = AiModelConfig()
        self.table_pool: list[str] = []   # 自动读取的库表清单（'连接名.表名'），用于表级反查

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "env": self.env.value,
            "db_connections": [asdict(d) for d in self.db_connections],
            "log_sources": [asdict(l) for l in self.log_sources],
            "rules": [asdict(r) for r in self.rules],
            "ai": asdict(self.ai),
            "table_pool": self.table_pool,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "ProjectConfig":
        p = cls(d["name"], Env(d.get("env", "test")))
        p.db_connections = [DbConnection(**x) for x in d.get("db_connections", [])]
        p.log_sources = [LogSource(**x) for x in d.get("log_sources", [])]
        p.rules = [CheckRule(**x) for x in d.get("rules", [])]
        p.table_pool = d.get("table_pool", []) or []
        if d.get("ai"):
            p.ai = AiModelConfig(**d["ai"])
        return p
