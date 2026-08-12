"""数据库连接器：强制只读，仅允许 SELECT，参数绑定防注入。"""
from __future__ import annotations

from captrace.config.models import DbConnection, DbType


def _build_uri(cfg: DbConnection) -> str:
    pwd = f":{cfg.password}" if cfg.password else ""
    if cfg.db_type == DbType.SQLITE:
        return f"sqlite:///{cfg.database}"
    if cfg.db_type == DbType.MYSQL:
        return f"mysql+pymysql://{cfg.user}{pwd}@{cfg.host}:{cfg.port}/{cfg.database}"
    # postgres / hologres 兼容 PG 协议
    return f"postgresql+psycopg://{cfg.user}{pwd}@{cfg.host}:{cfg.port}/{cfg.database}"


class DbConnector:
    def __init__(self, cfg: DbConnection):
        if not cfg.read_only:
            raise PermissionError("数据库连接器仅允许只读模式")
        self.cfg = cfg
        self._engine = None

    def _engine(self):
        if self._engine is None:
            from sqlalchemy import create_engine
            self._engine = create_engine(_build_uri(self.cfg), pool_pre_ping=True)
        return self._engine

    def query(self, table: str, column: str, join_col: str, value: str) -> dict | None:
        """按关联字段执行只读 SELECT，返回首行。"""
        from sqlalchemy import text

        sql = text(f"SELECT * FROM {table} WHERE {join_col} = :v LIMIT 1")
        with self._engine().connect() as conn:
            row = conn.execute(sql, {"v": value}).mappings().first()
        return dict(row) if row else None
