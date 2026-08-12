"""桌面壳：用 pywebview 加载前端 HTML，并通过 Api 类把后端逻辑暴露给 JS。

运行方式：
    python -m captrace.app_gui            # 开发态（直接加载 assets/app.html）
    # 打包后由 captrace-gui.exe 启动（资源随 exe 一同打包）

前端通过 window.pywebview.api.<方法>(...) 调用；所有方法返回 JSON 可序列化对象。
"""
from __future__ import annotations

import ctypes
import json
import os
import re
import threading
from ctypes import wintypes
from pathlib import Path
from typing import Any, Optional

import webview

from captrace import __version__
from captrace.ai.analyzer import analyze as ai_analyze_fn
from captrace.capture.receiver import start as start_receiver
from captrace.checker import matrix as matrix_mod
from captrace.checker.api_check import (
    extract_request_fields,
    extract_response_fields,
)
from captrace.checker.rules import match_rule
from captrace.config.models import (
    AiModelConfig, CaptureSource, CheckRule, DbConnection, DbType,
    Env, LogSource, LogType, Session, TableMapping,
)
from captrace.config.settings import ProjectConfig
from captrace.storage.projects import ProjectStore

# 资源目录：开发态用仓库 assets/，打包态随 exe 平铺或置于 _internal
import sys as _sys
_BASE = Path(getattr(_sys, "_MEIPASS", Path(__file__).resolve().parent.parent))
for _cand in (_BASE, _BASE / "_internal"):
    if (_cand / "assets" / "app.html").exists():
        BASE_DIR = _cand
        break
else:
    BASE_DIR = _BASE
ASSETS_DIR = BASE_DIR / "assets"
HTML_PATH = ASSETS_DIR / "app.html"

# 配置目录强制锁定到当前用户家目录，与 exe 安装目录分离：
#  - 重装 / 升级程序不会清掉用户配置；
#  - 打包分发时不会把配置带进安装包。
# 注意：不再读取 CAPTRACE_HOME，避免有人误指向安装目录导致重装即丢失。
DEFAULT_DIR = Path.home() / ".captrace" / "projects"

# Win32 常量：无边框窗口需要手动实现最大化/最小化
SWP_FRAMECHANGED = 0x0020
SWP_SHOWWINDOW = 0x0040
MONITOR_DEFAULTTONEAREST = 2
SW_MINIMIZE = 6


class _MONITORINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("rcMonitor", wintypes.RECT),
        ("rcWork", wintypes.RECT),
        ("dwFlags", wintypes.DWORD),
    ]


class Api:
    """暴露给前端的桥接接口（所有方法返回 dict / list / str）。"""

    def __init__(self) -> None:
        self._store = ProjectStore(DEFAULT_DIR)
        self._receiver = None
        self._receiver_thread = None
        self._local_proxy = None
        self._sessions: list[Session] = []
        self._window: Any = None
        self._is_maximized = False
        self._normal_bounds: tuple[int, int, int, int] | None = None

    def _hwnd(self) -> int:
        """获取当前主窗口句柄；无边框窗口主要靠标题匹配。"""
        try:
            if self._window is None:
                return 0
            title = getattr(self._window, "title", None) or "抓包测效助手"
            return ctypes.windll.user32.FindWindowW(None, title)
        except Exception:
            return 0

    # ---------- 元信息 ----------
    def version(self) -> str:
        return __version__

    # ---------- 实时捕获（方案A） ----------
    def serve_start(self, port: int = 9090) -> dict:
        if self._receiver is not None:
            return {"ok": True, "running": True, "port": port}
        try:
            self._receiver = start_receiver(
                port=port,
                on_session=lambda s: self._sessions.append(s),
            )
            self._receiver_thread = threading.Thread(
                target=self._receiver.serve_forever, daemon=True
            )
            self._receiver_thread.start()
            return {"ok": True, "running": True, "port": port}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": str(e)}

    def serve_stop(self) -> dict:
        if self._receiver is not None:
            self._receiver.shutdown()
            self._receiver = None
            self._receiver_thread = None
        return {"ok": True, "running": False}

    def serve_status(self) -> dict:
        return {"running": self._receiver is not None}

    # ---------- 本地代理抓包 ----------
    def proxy_start(self, port: int = 9091, mode: str = "off", domains: list | None = None, upstream: str = "") -> dict:
        from captrace.capture.local_proxy import LocalProxy

        if self._local_proxy is None:
            self._local_proxy = LocalProxy(
                on_session=lambda s: self._sessions.append(s),
                port=port,
                upstream=upstream or "",
            )
        self._local_proxy.set_filter(mode or "off", domains or [])
        return self._local_proxy.start()

    def proxy_set_filter(self, mode: str = "off", domains: list | None = None, upstream: str = "") -> dict:
        if self._local_proxy is not None:
            self._local_proxy.set_filter(mode or "off", domains or [])
            self._local_proxy.set_upstream(upstream or "")
        return {"ok": True, "filter": {"mode": mode, "domains": domains or []}, "upstream": upstream or ""}

    def proxy_stop(self) -> dict:
        if self._local_proxy is not None:
            res = self._local_proxy.stop()
            self._local_proxy = None
            return res
        return {"ok": True, "running": False}

    def proxy_status(self) -> dict:
        if self._local_proxy is None:
            return {"running": False, "port": 9091, "filter": {"mode": "off", "domains": []}}
        return self._local_proxy.status()

    def get_sessions(self) -> list[dict]:
        """把后端内存中的抓包会话返回给前端（用于轮询同步）。"""
        return [self._session_to_dict(s) for s in self._sessions]

    def clear_sessions(self) -> dict:
        self._sessions.clear()
        return {"ok": True}

    def _session_to_dict(self, s: Session) -> dict:
        return {
            "id": s.id,
            "method": s.method,
            "url": s.url,
            "host": getattr(s, "host", None),
            "request_body": s.request_body,
            "response_body": s.response_body,
            "status_code": s.status_code,
            "trace_id": s.trace_id,
            "order_id": s.order_id,
            "source": s.source.value if hasattr(s.source, "value") else s.source,
        }

    # ---------- 校验核心 ----------
    def run_check(self, payload: dict) -> dict:
        """payload: {method,url,request_body,response_body,status_code,trace_id,
                     order_id, rules, db_connections, log_sources}。

        返回 {matrix:[...], summary, has_anomaly}。

        已闭环：命中规则后自动去数据库按 join_field 查行、按 trace_id 拉日志，
        把 db / log 来源值填入一致性矩阵。
        """
        session = Session(
            id=payload.get("id") or "local-1",
            method=payload.get("method", "POST"),
            url=payload.get("url", ""),
            request_body=payload.get("request_body"),
            response_body=payload.get("response_body"),
            status_code=payload.get("status_code"),
            trace_id=payload.get("trace_id"),
            order_id=payload.get("order_id"),
            source=CaptureSource.FIDDLER_PUSH,
        )
        req = extract_request_fields(session)
        resp = extract_response_fields(session)
        fields: dict[str, dict[str, Any]] = {}
        for k, v in {**req, **resp}.items():
            fields[k] = {"request": req.get(k), "response": resp.get(k)}

        # ---- 自动数据库取值 ----
        rules = [CheckRule(**x) for x in (payload.get("rules") or [])]
        db_conns = [DbConnection(**x) for x in (payload.get("db_connections") or [])]
        table_pool = payload.get("table_pool") or []   # 方案2：自动读取的库表清单
        log_sources = [LogSource(**x) for x in (payload.get("log_sources") or [])]
        if db_conns:
            rule = match_rule(session, rules) if rules else None
            checked_tables = set()
            if rule:
                for tm in rule.tables:
                    if not tm.table:
                        continue
                    checked_tables.add(self._norm_key(tm.table))
                    row = self._query_table(tm, req, db_conns)
                    if row is None:
                        continue
                    # 把查到的整行按列名映射到业务字段（大小写/下划线归一）
                    norm_row = {self._norm_key(k): v for k, v in row.items()}
                    for biz_field in list(fields.keys()) + list(norm_row.keys()):
                        col = self._norm_key(biz_field)
                        if col in norm_row and norm_row[col] not in (None, ""):
                            fields.setdefault(biz_field, {})["db"] = norm_row[col]
            else:
                # 方案2 fallback：未命中规则时，按接口入参字段名在所有库表里做同名列反查。
                # 这样新增接口无需维护规则，也能校验「入参是否写入库」。
                req_norm = {self._norm_key(k): v for k, v in req.items() if v not in (None, "")}
                if req_norm and table_pool:
                    for full in table_pool:
                        if self._norm_key(full) in checked_tables:
                            continue
                        checked_tables.add(self._norm_key(full))
                        row = self._query_table(TableMapping(table=full), req, db_conns)
                        if row is None:
                            continue
                        norm_row = {self._norm_key(k): v for k, v in row.items()}
                        for biz_field in list(fields.keys()) + list(norm_row.keys()):
                            col = self._norm_key(biz_field)
                            if col in norm_row and norm_row[col] not in (None, ""):
                                fields.setdefault(biz_field, {})["db"] = norm_row[col]

        # ---- 自动日志取值 ----
        if log_sources and session.trace_id:
            log_vals = self._query_logs(session.trace_id, log_sources)
            if log_vals:
                norm_log = {self._norm_key(k): v for k, v in log_vals.items()}
                for biz_field in list(fields.keys()) + list(norm_log.keys()):
                    col = self._norm_key(biz_field)
                    if col in norm_log and norm_log[col] not in (None, ""):
                        fields.setdefault(biz_field, {})["log"] = norm_log[col]

        matrix = matrix_mod.build_matrix(fields)
        return {
            "matrix": [m.__dict__ for m in matrix],
            "summary": matrix_mod.summarize(matrix),
            "has_anomaly": matrix_mod.has_anomaly(matrix),
        }

    # ---------- 自动取值辅助 ----------
    @staticmethod
    def _norm_key(k: str) -> str:
        return str(k).strip().lower().replace("_", "").replace("-", "")

    def _get_table_columns(self, conn, table: str, db_type: str) -> list[str]:
        """获取指定表的所有列名。"""
        from sqlalchemy import text

        try:
            if db_type == "sqlite":
                rs = conn.execute(text(f"PRAGMA table_info({table})"))
                return [row["name"] for row in rs.mappings()]
            if db_type in ("postgres", "postgresql", "hologres"):
                schema = "public"
                tbl = table
                if "." in table:
                    schema, tbl = table.split(".", 1)
                rs = conn.execute(
                    text(
                        "SELECT column_name FROM information_schema.columns "
                        "WHERE table_schema=:s AND table_name=:t"
                    ),
                    {"s": schema, "t": tbl},
                )
                return [r["column_name"] for r in rs.mappings()]
            # mysql / mariadb
            rs = conn.execute(text(f"SHOW COLUMNS FROM {table}"))
            return [r["Field"] for r in rs.mappings()]
        except Exception as e:  # noqa: BLE001
            print(f"[captrace] 获取表 {table} 列失败: {e}")
            return []

    def _query_table(self, tm: TableMapping, req: dict, db_conns: list[DbConnection]) -> Optional[dict]:
        """按接口入参字段自动查询关联表最近一条匹配记录。

        规则：
        1. table 支持 'db_name.table' 或 'table'（后者取第一个数据库连接）。
        2. 把请求中非空字段名归一化，与表列名归一化后匹配，构造 WHERE。
        3. 按 id 主键倒序取第一条，保证拿到最新写入/更新的记录。
        """
        table_full = tm.table
        db_name = None
        if "." in table_full:
            db_name, table = table_full.split(".", 1)
        else:
            table = table_full
        conn = None
        if db_name:
            conn = next((c for c in db_conns if self._norm_key(c.name) == self._norm_key(db_name)), None)
        if conn is None:
            conn = db_conns[0]
        if conn is None:
            return None
        try:
            from sqlalchemy import create_engine, text

            driver_map = {
                "mysql": "mysql+pymysql",
                "postgres": "postgresql+psycopg2",
                "hologres": "postgresql+psycopg2",
                "sqlite": "sqlite",
            }
            db_type = conn.db_type.value if hasattr(conn.db_type, "value") else conn.db_type
            driver = driver_map.get(db_type, "mysql+pymysql")
            if db_type == "sqlite":
                url = f"sqlite:///{conn.database}"
                engine = create_engine(url)
            else:
                url = f"{driver}://{conn.user}:{conn.password}@{conn.host}:{conn.port}/{conn.database}"
                engine = create_engine(url, connect_args={"connect_timeout": 8})

            # 请求字段归一化，只保留非空值
            norm_req = {
                self._norm_key(k): (k, v)
                for k, v in (req or {}).items()
                if v not in (None, "")
            }
            if not norm_req:
                return None

            with engine.connect() as c:
                cols = self._get_table_columns(c, table, db_type)
                norm_cols = {self._norm_key(col): col for col in cols}

                conditions = []
                params = {}
                idx = 0
                for nk, (orig_k, v) in norm_req.items():
                    if nk in norm_cols:
                        col = norm_cols[nk]
                        conditions.append(f"{col} = :p{idx}")
                        params[f"p{idx}"] = v
                        idx += 1

                if conditions:
                    where = " AND ".join(conditions)
                    # 优先按 id 主键倒序，否则退化为 LIMIT 1
                    order_col = None
                    for cand in ["id", f"{self._norm_key(table)}id"]:
                        for nk, col in norm_cols.items():
                            if nk == cand:
                                order_col = col
                                break
                        if order_col:
                            break
                    if order_col:
                        sql = text(
                            f"SELECT * FROM {table} WHERE {where} ORDER BY {order_col} DESC LIMIT 1"
                        )
                    else:
                        sql = text(f"SELECT * FROM {table} WHERE {where} LIMIT 1")
                else:
                    # 没有任何字段能匹配到表列时，只能兜底取最新一条
                    sql = text(f"SELECT * FROM {table} ORDER BY id DESC LIMIT 1")

                result = c.execute(sql, params)
                row = result.mappings().first()
                return dict(row) if row else None
        except Exception as e:  # noqa: BLE001
            print(f"[captrace] 查表 {table_full} 失败: {e}")
            return None

    def _query_logs(self, trace_id: str, log_sources: list[LogSource]) -> dict:
        """按 trace_id 拉日志关键值，返回扁平键值对（JSON 解析或 key=value 正则）。"""
        merged: dict = {}
        for src in log_sources:
            try:
                log_type = src.log_type.value if hasattr(src.log_type, "value") else src.log_type
                if log_type == "elk":
                    import requests
                    q = (src.index + "/_search") if src.index else "_search"
                    url = f"{src.address.rstrip('/')}/{q}"
                    body = {"size": 50, "query": {"query_string": {"query": trace_id}}}
                    r = requests.post(url, json=body, timeout=8)
                    hits = r.json().get("hits", {}).get("hits", [])
                    for h in hits:
                        src_obj = h.get("_source", {})
                        self._merge_log_json(merged, src_obj)
                elif log_type == "remote_file":
                    import re
                    p = Path(src.address)
                    if not p.exists():
                        continue
                    text_blob = p.read_text(encoding="utf-8", errors="ignore")
                    for line in text_blob.splitlines():
                        if trace_id in line:
                            try:
                                self._merge_log_json(merged, json.loads(line))
                            except Exception:
                                for m in re.finditer(r"(\w+)=([^\s,]+)", line):
                                    merged.setdefault(m.group(1), m.group(2))
                else:
                    # container / 其他：暂按 remote_file 退化处理
                    continue
            except Exception as e:  # noqa: BLE001
                print(f"[captrace] 拉日志 {src.name} 失败: {e}")
                continue
        return merged

    @staticmethod
    def _merge_log_json(merged: dict, obj: dict) -> None:
        def _walk(o, prefix=""):
            if isinstance(o, dict):
                for k, v in o.items():
                    _walk(v, f"{prefix}{k}.")
            elif isinstance(o, list):
                for i, v in enumerate(o):
                    _walk(v, f"{prefix}{i}.")
            else:
                merged[prefix.rstrip(".")] = o
        _walk(obj)

    # ---------- AI 根因 ----------
    def ai_analyze(self, payload: dict) -> dict:
        """payload: {session:{...}, matrix:[...], db_rows:[...], log_lines:[...], ai:{...}}。"""
        try:
            ai_raw = payload.get("ai") or {}
            api_key = ai_raw.get("api_key") or ""
            if not api_key:
                return {"ok": False, "error": "AI 模型未配置：请在「连接配置 → AI 模型」中填写 API Key 后重试。"}

            s = payload.get("session", {})
            session = Session(
                id=s.get("id", "local-1"),
                method=s.get("method", "POST"),
                url=s.get("url", ""),
                request_body=s.get("request_body"),
                response_body=s.get("response_body"),
                status_code=s.get("status_code"),
                trace_id=s.get("trace_id"),
                order_id=s.get("order_id"),
            )
            MatrixField = __import__("captrace.config.models", fromlist=["MatrixField"]).MatrixField
            matrix = [
                MatrixField(m["name"], m.get("values", {}), m["conclusion"], m.get("detail", ""))
                for m in payload.get("matrix", [])
            ]
            ai_cfg = AiModelConfig(
                **{k: ai_raw[k] for k in AiModelConfig.__dataclass_fields__ if k in ai_raw}
            )
            bug = ai_analyze_fn(
                session, matrix,
                payload.get("db_rows", []),
                payload.get("log_lines", []),
                ai_cfg,
            )
            return {"ok": True, "bug": bug.__dict__}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": str(e)}

    # ---------- 项目管理 ----------
    def list_projects(self) -> list[str]:
        return self._store.list_projects()

    def load_project(self, name: str) -> Optional[dict]:
        try:
            d = self._store.load(name).to_dict()
            return d
        except FileNotFoundError:
            return None

    def save_project(self, cfg: dict) -> dict:
        env_raw = cfg.get("env") or "test"
        p = ProjectConfig(
            cfg["name"],
            Env(env_raw),
        )
        p.db_connections = [DbConnection(**x) for x in cfg.get("db_connections", [])]
        p.log_sources = [LogSource(**x) for x in cfg.get("log_sources", [])]
        p.rules = [CheckRule(**x) for x in cfg.get("rules", [])]
        p.table_pool = cfg.get("table_pool", []) or []
        if cfg.get("ai"):
            ai_raw = cfg["ai"]
            ai_kwargs = {k: ai_raw[k] for k in AiModelConfig.__dataclass_fields__ if k in ai_raw}
            # 不要把 AI 密钥明文落盘：保存时清空 api_key，运行时由界面内存提供
            ai_kwargs["api_key"] = ""
            p.ai = AiModelConfig(**ai_kwargs)
        path = self._store.save(p)
        return {"ok": True, "path": str(path)}

    def list_backups(self, name: str) -> dict:
        try:
            return {"ok": True, "backups": self._store.list_backups(name)}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": str(e)}

    def restore_backup(self, payload: dict) -> dict:
        try:
            path = self._store.restore_backup(payload["name"], payload["backup_name"])
            return {"ok": True, "path": str(path)}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": str(e)}

    def reset_config(self) -> dict:
        r"""清空当前用户的所有项目配置（保留 backups，方便误删后恢复）。

        注意：清空只影响当前用户目录 %USERPROFILE%\.captrace\projects，
        不会动安装目录，也不会影响其他用户或打包分发结果。
        """
        import shutil

        if self._store.base_dir.exists():
            shutil.rmtree(self._store.base_dir, ignore_errors=True)
            self._store.base_dir.mkdir(parents=True, exist_ok=True)
        return {"ok": True, "path": str(self._store.base_dir)}

    # ---------- 连接测试 ----------
    def test_db_connection(self, cfg: dict) -> dict:
        """测试数据库连通性。cfg: {name, db_type, host, port, database, user, password, read_only}。"""
        try:
            db_type = cfg.get("db_type", "mysql")
            host = cfg.get("host", "")
            port = cfg.get("port") or 3306
            database = cfg.get("database", "")
            user = cfg.get("user", "")
            password = cfg.get("password", "")

            if not host or not database or not user:
                return {"ok": False, "error": "请填写 host、database、user 后再测试连接。"}

            from sqlalchemy import create_engine, text
            driver_map = {
                "mysql": "mysql+pymysql",
                "postgres": "postgresql+psycopg2",
                "hologres": "postgresql+psycopg2",
                "sqlite": "sqlite",
            }
            driver = driver_map.get(db_type, db_type)
            if db_type == "sqlite":
                url = f"sqlite:///{database}"
                engine = create_engine(url)
            else:
                url = f"{driver}://{user}:{password}@{host}:{port}/{database}"
                engine = create_engine(url, connect_args={"connect_timeout": 8})
            with engine.connect() as conn:
                conn.execute(text("SELECT 1"))
                conn.commit()
            return {"ok": True, "message": f"{cfg.get('name') or database} 连接成功"}
        except ImportError as e:
            return {"ok": False, "error": f"缺少数据库驱动：{e}。请安装对应驱动（如 PyMySQL、psycopg2）。"}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": str(e)}

    def test_log_source(self, cfg: dict) -> dict:
        """测试日志源可达性。cfg: {name, log_type, address, index, read_only}。"""
        try:
            log_type = cfg.get("log_type", "elk")
            address = cfg.get("address", "")
            if not address:
                return {"ok": False, "error": "请填写日志路径或服务器地址后再测试连接。"}
            if log_type == "elk":
                import requests
                resp = requests.get(address, timeout=8)
                resp.raise_for_status()
                return {"ok": True, "message": f"{cfg.get('name') or address} 可达（HTTP {resp.status_code}）"}
            if log_type == "remote_file":
                from pathlib import Path
                p = Path(address)
                if p.exists():
                    return {"ok": True, "message": f"文件存在：{address}"}
                return {"ok": True, "message": f"文件路径已记录（当前不可达）：{address}"}
            # container / 其他
            return {"ok": True, "message": f"{cfg.get('name') or address} 配置已记录"}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": str(e)}

    # ---------- 自动读取库表 / 规则生成 ----------
    def _db_engine(self, cfg: dict):
        """根据连接配置创建一个 SQLAlchemy engine。"""
        from sqlalchemy import create_engine

        db_type = cfg.get("db_type", "mysql")
        host = cfg.get("host", "")
        port = cfg.get("port") or 3306
        database = cfg.get("database", "")
        user = cfg.get("user", "")
        password = cfg.get("password", "")
        driver_map = {
            "mysql": "mysql+pymysql",
            "postgres": "postgresql+psycopg2",
            "hologres": "postgresql+psycopg2",
            "sqlite": "sqlite",
        }
        driver = driver_map.get(db_type, db_type)
        if db_type == "sqlite":
            url = f"sqlite:///{database}"
            return create_engine(url)
        url = f"{driver}://{user}:{password}@{host}:{port}/{database}"
        return create_engine(url, connect_args={"connect_timeout": 8})

    def list_db_tables(self, cfg: dict) -> dict:
        """读取某个连接下所有表名（不含系统表）。cfg 同 test_db_connection。"""
        try:
            from sqlalchemy import text

            db_type = cfg.get("db_type", "mysql")
            engine = self._db_engine(cfg)
            with engine.connect() as conn:
                if db_type == "sqlite":
                    rs = conn.execute(text("SELECT name FROM sqlite_master WHERE type='table'"))
                    tables = [r["name"] for r in rs.mappings()]
                elif db_type in ("postgres", "postgresql", "hologres"):
                    rs = conn.execute(
                        text(
                            "SELECT tablename FROM pg_tables "
                            "WHERE schemaname NOT IN ('pg_catalog','information_schema')"
                        )
                    )
                    tables = [r["tablename"] for r in rs.mappings()]
                else:  # mysql / mariadb
                    rs = conn.execute(text("SHOW TABLES"))
                    tables = [list(r.values())[0] for r in rs.mappings()]
            # 过滤常见系统表
            skip = {"schema_migrations", "flyway_schema_history", "django_migrations",
                    "alembic_version", "migrations", "sqlite_sequence"}
            tables = [t for t in tables if t.lower() not in skip]
            return {"ok": True, "tables": sorted(tables), "db": cfg.get("name") or cfg.get("database")}
        except ImportError as e:
            return {"ok": False, "error": f"缺少数据库驱动：{e}"}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": str(e)}

    def _camel_to_snake(self, name: str) -> str:
        s = re.sub(r"(?<!^)(?=[A-Z])", "_", name)
        s = s.replace("-", "_").replace(" ", "_")
        return s.lower()

    def _infer_table(self, api_path: str, tables: list[str]) -> list[str]:
        """从接口路径推断可能写入的表名，并在真实表清单中模糊匹配。

        规则：
        1. 取路径中所有段，去掉动作词（create/update/list...）、版本号、空段
        2. 用「剩余最后一段」作为实体词（如 /api/supplier/create -> supplier）
        3. 实体词去复数、转 snake，在真实表中做精确/子串匹配
        """
        action_words = {"create", "add", "new", "update", "edit", "save", "modify",
                        "delete", "remove", "get", "detail", "list", "query", "search",
                        "info", "submit", "do", "api", "v1", "v2", "app", "web"}
        raw_segs = [p for p in api_path.rstrip("/").split("?")[0].split("/") if p]
        # 去动作词与版本号，保留实体段
        segs = [p for p in raw_segs if p.lower() not in action_words]
        if not segs:
            return []
        entity = segs[-1].lower()
        # 去复数：orders->order, suppliers->supplier
        ent_singular = re.sub(r"(ie)s$", r"\1", entity)
        ent_singular = re.sub(r"s$", "", ent_singular)
        ent_snake = self._camel_to_snake(entity)
        ent_snake_sing = self._camel_to_snake(ent_singular)

        norm_tables = {t: self._norm_key(t) for t in tables}
        matched = []
        for t, nk in norm_tables.items():
            if nk in (ent_snake, ent_snake_sing, entity, ent_singular):
                matched.append(t)
                continue
            if ent_snake and ent_snake in nk:
                matched.append(t)
                continue
            if ent_snake_sing and ent_snake_sing in nk:
                matched.append(t)
        return matched

    def auto_rules_from_sessions(self, payload: dict) -> dict:
        """根据已捕获的 Session + 数据库连接表清单，自动生成校验规则。

        payload: {
          sessions: [{method, url, request_body, ...}],
          db_connections: [{name, db_type, host, port, database, user, password}],
        }
        返回 {ok, rules:[{api_pattern, tables:[{table}]}]}
        """
        try:
            sessions = payload.get("sessions") or []
            db_conns = payload.get("db_connections") or []
            if not sessions:
                return {"ok": False, "error": "暂无抓包记录，请先抓包。"}
            if not db_conns:
                return {"ok": False, "error": "请先配置数据库连接，才能自动读取表并推断规则。"}

            # 汇总所有连接下的真实表（table 记成 '连接名.表名'）
            table_pool: list[str] = []
            for conn in db_conns:
                r = self.list_db_tables(conn)
                if r.get("ok"):
                    db_name = conn.get("name") or conn.get("database")
                    for t in r["tables"]:
                        table_pool.append(f"{db_name}.{t}")

            if not table_pool:
                return {"ok": False, "error": "未读取到任何表，请检查数据库连接或权限。"}

            rules = []
            seen = set()
            for s in sessions:
                method = (s.get("method") or "POST").upper()
                url = s.get("url") or ""
                path = url.split("?")[0]
                if not path:
                    continue
                api_pattern = f"{method} {path}"
                if api_pattern in seen:
                    continue
                seen.add(api_pattern)
                matched = self._infer_table(path, table_pool)
                rules.append({
                    "api_pattern": api_pattern,
                    "tables": [{"table": t, "join_field": "", "remote_field": ""} for t in matched],
                })
            return {"ok": True, "rules": rules, "table_pool_size": len(table_pool)}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": str(e)}

    # ---------- Window control ----------
    def window_minimize(self) -> bool:
        hwnd = self._hwnd()
        if hwnd:
            ctypes.windll.user32.ShowWindow(hwnd, SW_MINIMIZE)
        return True

    def window_maximize(self) -> dict:
        hwnd = self._hwnd()
        if not hwnd:
            return {"maximized": self._is_maximized}

        user32 = ctypes.windll.user32
        if not self._is_maximized:
            # 记录当前窗口位置/大小，以便还原
            rect = wintypes.RECT()
            user32.GetWindowRect(hwnd, ctypes.byref(rect))
            self._normal_bounds = (
                rect.left, rect.top,
                rect.right - rect.left, rect.bottom - rect.top,
            )

            # 最大化到当前显示器工作区（排除任务栏）
            hmon = user32.MonitorFromWindow(hwnd, MONITOR_DEFAULTTONEAREST)
            mi = _MONITORINFO()
            mi.cbSize = ctypes.sizeof(_MONITORINFO)
            user32.GetMonitorInfoW(hmon, ctypes.byref(mi))
            user32.SetWindowPos(
                hwnd, 0,
                mi.rcWork.left, mi.rcWork.top,
                mi.rcWork.right - mi.rcWork.left,
                mi.rcWork.bottom - mi.rcWork.top,
                SWP_FRAMECHANGED | SWP_SHOWWINDOW,
            )
            self._is_maximized = True
        else:
            x, y, w, h = self._normal_bounds or (100, 100, 1280, 800)
            user32.SetWindowPos(
                hwnd, 0, x, y, w, h,
                SWP_FRAMECHANGED | SWP_SHOWWINDOW,
            )
            self._is_maximized = False

        return {"maximized": self._is_maximized}

    def window_close(self) -> bool:
        if self._window:
            self._window.destroy()
        return True

    # ---------- Demo ----------
    def demo(self) -> dict:
        from captrace.main import _demo_session, _demo_project
        from captrace.checker import matrix as _m
        session = _demo_session()
        project = _demo_project()
        # 复刻 main.run_checks 的核心比对（不触发 GBK 打印）
        req = extract_request_fields(session)
        resp = extract_response_fields(session)
        fields = {
            "orderId": {"request": req.get("orderId"), "response": resp.get("orderId"), "db": 88231, "log": 88231},
            "amount/pay_amount": {"request": req.get("amount"), "response": resp.get("payAmount"), "db": 99.00, "log": "expected 104.00"},
            "status": {"response": resp.get("status"), "db": "PAID", "log": "PAID"},
            "coupon/discount": {"request": req.get("couponId"), "db": 5, "log": "5"},
        }
        matrix = _m.build_matrix(fields)
        return {
            "session": session.__dict__,
            "project": project.to_dict(),
            "matrix": [m.__dict__ for m in matrix],
            "has_anomaly": _m.has_anomaly(matrix),
        }


def main() -> None:
    api = Api()
    window = webview.create_window(
        "抓包测效助手",
        str(HTML_PATH),
        js_api=api,
        width=1180,
        height=760,
        min_size=(960, 640),
        frameless=True,  # 使用 HTML 内自定义 titlebar，去掉系统标题栏避免双抬头
    )
    api._window = window
    webview.start()


if __name__ == "__main__":
    main()
