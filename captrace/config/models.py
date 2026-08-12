"""领域模型：贯穿抓包、校验、AI 全流程的数据结构。"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional


class CaptureSource(str, Enum):
    FIDDLER_PUSH = "fiddler_push"
    SAZ_IMPORT = "saz_import"
    BUILTIN_PROXY = "builtin_proxy"


class Env(str, Enum):
    TEST = "test"
    STAGING = "staging"
    PROD = "prod"


class DbType(str, Enum):
    MYSQL = "mysql"
    POSTGRES = "postgres"
    HOLOGRAM = "hologres"
    SQLITE = "sqlite"


class LogType(str, Enum):
    ELK = "elk"
    REMOTE_FILE = "remote_file"
    CONTAINER = "container"


@dataclass
class Session:
    """一条抓包会话（一次 HTTP 请求-响应）。"""
    id: str
    method: str
    url: str
    request_body: Optional[str] = None
    response_body: Optional[str] = None
    status_code: Optional[int] = None
    trace_id: Optional[str] = None
    order_id: Optional[str] = None
    timestamp: Optional[float] = None
    source: CaptureSource = CaptureSource.FIDDLER_PUSH


@dataclass
class DbConnection:
    name: str
    db_type: DbType
    host: str
    port: int
    database: str
    user: str
    password: str = ""
    read_only: bool = True  # 强制只读，禁止写


@dataclass
class LogSource:
    name: str
    log_type: LogType
    address: str            # ELK endpoint / 文件路径 / container@host
    index: str = ""
    read_only: bool = True


@dataclass
class AiModelConfig:
    provider: str = "openai"
    base_url: str = "https://api.openai.com/v1"
    model: str = "gpt-4o-mini"
    api_key: str = ""
    temperature: float = 0.2


@dataclass
class TableMapping:
    """校验规则：一个接口操作关联的一张数据库表。

    不再依赖人工指定 join_field/remote_field；
    运行时按接口入参字段自动去表中查最近一条匹配记录，
    并按字段名归一化后比对入参与库值是否一致。
    """
    table: str
    # 以下字段已弃用，保留仅用于兼容旧项目配置
    join_field: str = ""
    remote_field: str = ""


@dataclass
class CheckRule:
    api_pattern: str        # 如 "POST /api/order/create" 或 "POST /api/order/*"
    tables: list[TableMapping] = field(default_factory=list)


@dataclass
class MatrixField:
    """一致性矩阵中的一行：某业务字段在多方来源的比对结果。"""
    name: str
    values: dict[str, Any]                      # {request/response/db/log: value}
    conclusion: str                             # consistent | inconsistent | partial
    detail: str = ""


@dataclass
class BugReport:
    title: str
    steps: str
    expected: str
    actual: str
    root_cause: str
    severity: str = "Major"
    raw: str = ""
