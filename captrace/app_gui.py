"""桌面壳：用 pywebview 加载前端 HTML，并通过 Api 类把后端逻辑暴露给 JS。

运行方式：
    python -m captrace.app_gui            # 开发态（直接加载 assets/app.html）
    # 打包后由 captrace-gui.exe 启动（资源随 exe 一同打包）

前端通过 window.pywebview.api.<方法>(...) 调用；所有方法返回 JSON 可序列化对象。
"""
from __future__ import annotations

import ctypes
import difflib
import json
import os
import re
import threading
import time
from ctypes import wintypes
from decimal import Decimal
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
    extract_response_list_first,
    extract_response_list_first_id,
)
from captrace.checker.rules import match_rule
from captrace.config.models import (
    AiModelConfig, CaptureSource, CheckRule, DbConnection, DbType,
    Env, LogSource, LogType, Session, TableMapping,
)
from captrace.config.settings import ProjectConfig, _normalize_port
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

    # ---------- 工具 ----------
    def export_check_log(self, text: str) -> dict:
        """把校验日志保存到项目目录下的 logs/ 文件夹，返回保存路径。"""
        try:
            base_dir = Path(DEFAULT_DIR).parent if str(DEFAULT_DIR).endswith("projects") else Path(DEFAULT_DIR)
            log_dir = base_dir / "logs"
            log_dir.mkdir(parents=True, exist_ok=True)
            filename = f"check-log-{time.strftime('%Y-%m-%d-%H-%M-%S')}.txt"
            path = log_dir / filename
            path.write_text(text, encoding="utf-8")
            return {"ok": True, "path": str(path)}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": str(e)}

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
        from captrace.capture.receiver import is_port_listening
        listening = is_port_listening(9090)
        return {
            "running": self._receiver is not None,
            "listening": listening,
            "port": 9090,
        }

    # ---------- 调试：直接 POST 一条 Fiddler 风格样本到本地 9090 ----------
    # 用途：pywebview/某些 webview 禁止 JS 直接 fetch localhost。
    # 让前端调这个 API，由后端用 urllib 推送，绕开浏览器限制。
    def debug_inject_sample(self) -> dict:
        import json
        import urllib.request
        if self._receiver is None:
            return {"ok": False, "error": "接收服务未启动，请先点『开始实时捕获』"}
        payload = {
            "method": "POST",
            "url": "https://adtest.virtual.007ka.cn/byToken/resource/good/findGoodCategoryList",
            "status_code": 200,
            "trace_id": "debug-inject-" + str(int.from_bytes(os.urandom(4), "big")),
            "request_body": json.dumps({"pagination": {"pageSize": 10, "current": 1, "total": 3972}}, ensure_ascii=False),
            "response_body": json.dumps({"data": {"list": [
                {"id": 0, "categoryContent": "AUTO_BASE_GOOD_ORDER_RANDOM", "categoryName": "AUTO_BASE_GOOD_ORDER_RANDOM"},
                {"id": 1, "categoryContent": "DEBUG_INJECT_OK", "categoryName": "链路验证成功"},
            ]}, "status": "ok"}, ensure_ascii=False),
            "headers": {"Content-Type": "application/json;charset=UTF-8"},
        }
        try:
            req = urllib.request.Request(
                "http://127.0.0.1:9090/ingest",
                data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=3) as r:
                body = r.read().decode("utf-8", errors="replace")
            return {"ok": True, "ingest_response": body}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": f"推送失败: {e}"}

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
                     order_id, rules, db_connections, log_sources,
                     selected_db_name, selected_log_name}。

        返回 {matrix:[...], summary, has_anomaly, logs:[...]}。

        已闭环：命中规则后自动去数据库按 join_field 查行、按 trace_id 拉日志，
        把 db / log 来源值填入一致性矩阵。
        """
        logs: list[dict[str, str]] = []
        t0 = time.time()

        def _log(level: str, msg: str) -> None:
            logs.append({"time": time.strftime("%H:%M:%S"), "level": level, "msg": msg})

        try:
            return self._run_check_core(payload, logs, t0, _log)
        except Exception as e:  # noqa: BLE001
            err = f"{type(e).__name__}: {e}"
            _log("ERROR", f"校验过程异常：{err}")
            return {
                "matrix": [],
                "summary": {"total": 0, "consistent": 0, "inconsistent": 0, "missing": 0},
                "has_anomaly": True,
                "logs": logs,
                "error": err,
            }

    def _run_check_core(
        self,
        payload: dict,
        logs: list[dict[str, str]],
        t0: float,
        _log: Any,
    ) -> dict:
        _log("INFO", "开始综合校验：" + payload.get("method", "POST") + " " + payload.get("url", ""))

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
        list_first_id = extract_response_list_first_id(session)
        response_first_row = extract_response_list_first(session)
        _log("INFO", f"提取接口入参字段 {len(req)} 个：{', '.join(req.keys()) or '—'}")
        _log("INFO", f"提取接口出参字段 {len(resp)} 个：{', '.join(resp.keys()) or '—'}")
        if list_first_id is not None:
            _log("INFO", f"检测到列表接口，取首条记录主键值：{list_first_id}")

        # 分页类字段只是定位/过滤条件，不应作为业务对比项进入矩阵；
        # 入参里的非分页业务字段（如 status=1）需纳入对比（request 侧）。
        pagination_like = {"pageno", "pagenumber", "page", "current", "pagesize", "limit", "offset", "total", "pages", "paginationcurrent", "paginationpagesize", "paginationtotal", "pagenumber"}
        req_biz = {k: v for k, v in req.items()
                   if v not in (None, "") and self._norm_key(k) not in pagination_like}

        # 用归一化字段名（_norm_field_key：兼顾驼峰和下划线）作为合并键，
        # 保证入参 snapshotDateStart 能和出参/库列 snapshot_date 映射到同一行。
        # 每条字段保留"出参原始名"作为显示，其余来源按归一化键汇总。
        fields: dict[str, dict[str, Any]] = {}
        display_name: dict[str, str] = {}

        def _merge(target: str, source: str, source_name: str, value: Any) -> None:
            display_name[target] = display_name.get(target, source_name)
            fields.setdefault(target, {"request": None, "response": None, "db": None, "log": None})
            if source_name.startswith(("pagination", "pageSize", "pageNum", "current", "total")):
                return
            fields[target][source] = value

        for k, v in resp.items():
            nk = self._norm_field_key(k)
            _merge(nk, "response", k, v)
        for k, v in req_biz.items():
            nk = self._norm_field_key(k)
            _merge(nk, "request", k, v)

        # 把 fields 的键换成"显示用的原名"（优先出参名，其次入参名），
        # 保证矩阵列里显示的是人类可读字段名。
        rendered: dict[str, dict[str, Any]] = {}
        for nk, sources in fields.items():
            shown = display_name[nk]
            if shown not in rendered:
                rendered[shown] = sources
            else:
                # 同一归一化键已存在显示名，合并 request/response
                for side in ("request", "response"):
                    if sources.get(side) not in (None, "") and rendered[shown].get(side) in (None, ""):
                        rendered[shown][side] = sources[side]
        fields = rendered

        # ---- 自动数据库取值 ----
        rules = [CheckRule(**x) for x in (payload.get("rules") or [])]
        db_conns = [DbConnection(**x) for x in (payload.get("db_connections") or [])]
        table_pool = payload.get("table_pool") or []   # 方案2：自动读取的库表清单
        log_sources = [LogSource(**x) for x in (payload.get("log_sources") or [])]
        selected_db_name = payload.get("selected_db_name")
        selected_log_name = payload.get("selected_log_name")

        if selected_db_name:
            db_conns = [c for c in db_conns if c.name == selected_db_name]
            if db_conns:
                _log("INFO", f"已指定本次使用数据库连接：{selected_db_name}")
            else:
                _log("WARN", f"未找到名为 {selected_db_name} 的数据库连接，将使用全部连接")
                db_conns = [DbConnection(**x) for x in (payload.get("db_connections") or [])]
        else:
            _log("INFO", f"未指定数据库连接，本次使用全部 {len(db_conns)} 个连接做反查")
        if selected_log_name:
            log_sources = [s for s in log_sources if s.name == selected_log_name]
            if log_sources:
                _log("INFO", f"已指定本次使用日志源：{selected_log_name}")
            else:
                _log("WARN", f"未找到名为 {selected_log_name} 的日志源，将使用全部日志源")
                log_sources = [LogSource(**x) for x in (payload.get("log_sources") or [])]
        else:
            _log("INFO", f"未指定日志源，本次使用全部 {len(log_sources)} 个日志源做反查")

        if db_conns:
            rule = match_rule(session, rules) if rules else None
            if rule:
                _log("INFO", f"命中校验规则：{rule.name}")
            else:
                _log("WARN", "未命中任何校验规则，将使用自动库表反查")
            checked_tables = set()
            db_hit = False
            if rule:
                for tm in rule.tables:
                    if not tm.table:
                        continue
                    checked_tables.add(self._norm_key(tm.table))
                    _log("INFO", f"正在查询数据库表：{tm.table}\n{self._dump_query_info(tm, req, response_first_row, list_first_id, db_conns)}")
                    row = self._query_table(
                        tm, req, db_conns, logs,
                        fallback_id=list_first_id,
                        prefer_fallback_id=list_first_id is not None,
                        response_first_row=response_first_row,
                    )
                    if row is None:
                        _log("WARN", f"表 {tm.table} 未查到匹配记录")
                        continue
                    db_hit = True
                    _log("OK", f"表 {tm.table} 查到 {len(row)} 列数据")
                    # 把查到的整行按列名映射到业务字段（驼峰/下划线归一，含前缀兼容）
                    norm_row = {self._norm_field_key(k): (k, v) for k, v in row.items()}
                    for biz_field in list(fields.keys()):
                        col = self._norm_field_key(biz_field)
                        # 若当前字段已命中数据库值，不再被后续表覆盖（按表相关度优先保留）
                        if fields.get(biz_field, {}).get("db") not in (None, ""):
                            continue
                        if col in norm_row and norm_row[col][1] not in (None, ""):
                            fields.setdefault(biz_field, {})["db"] = norm_row[col][1]
                            continue
                        for cnk, (orig_col, val) in norm_row.items():
                            if val in (None, ""):
                                continue
                            for suffix in ("_start", "_end", "_begin", "_finish"):
                                if col.endswith(suffix):
                                    base = col[: -len(suffix)]
                                    if cnk == base or cnk.startswith(base + "_"):
                                        if fields.get(biz_field, {}).get("db") in (None, ""):
                                            fields.setdefault(biz_field, {})["db"] = val
                                        break
                            else:
                                if cnk.startswith(col + "_") or cnk == col:
                                    if fields.get(biz_field, {}).get("db") in (None, ""):
                                        fields.setdefault(biz_field, {})["db"] = val
            else:
                # 方案2 fallback：未命中规则时，按接口路径关键字自动匹配库表清单里的
                # 实体表（如 findOrderList → order，匹配所有含 order 的表）逐一反查，
                # 把所有查到的字段汇总到矩阵「数据库」列做比对。用户无需为每个接口
                # 维护规则，纯靠路径关键字 + 库表清单就能自动校验。
                req_norm = {self._norm_key(k): v for k, v in req.items() if v not in (None, "")}
                if req_norm and table_pool:
                    path = session.url.split("?")[0]
                    inferred = self._infer_table(path, table_pool)
                    candidates = list(inferred) if inferred else table_pool[:20]
                    scope = "路径推断的实体表（自动汇总多源比对）" if inferred else f"未匹配到实体，按表清单兜底扫描（前 {len(candidates)} 张）"
                    _log("INFO", f"{scope}：{', '.join(candidates)}")
                    for full in candidates:
                        if self._norm_key(full) in checked_tables:
                            continue
                        checked_tables.add(self._norm_key(full))
                        _log("INFO", f"正在查询数据库表：{full}\n{self._dump_query_info(TableMapping(table=full), req, response_first_row, list_first_id, db_conns)}")
                        row = self._query_table(
                            TableMapping(table=full), req, db_conns, logs,
                            fallback_id=list_first_id,
                            prefer_fallback_id=list_first_id is not None,
                            response_first_row=response_first_row,
                        )
                        if row is None:
                            continue
                        db_hit = True
                        _log("OK", f"表 {full} 查到 {len(row)} 列数据")
                        norm_row = {self._norm_field_key(k): (k, v) for k, v in row.items()}
                        # 优先完全匹配；其次前缀匹配（库列是业务字段的前缀：如库列
                        # snapshot_date 同时填到 snapshot_date_start/_end 行）；
                        # 让库列的单个字段值能覆盖入参里的多个 _Start/_End 时间段。
                        for biz_field in list(fields.keys()):
                            col = self._norm_field_key(biz_field)
                            # 已命中则保留，按候选表相关度优先，避免后查表覆盖
                            if fields.get(biz_field, {}).get("db") not in (None, ""):
                                continue
                            # 1. 完全匹配
                            if col in norm_row and norm_row[col][1] not in (None, ""):
                                fields.setdefault(biz_field, {})["db"] = norm_row[col][1]
                                continue
                            # 2. 前缀匹配：库列是 biz_field 归一化键的前缀（业务字段带后缀）
                            #    或 biz_field 归一化键是库列的前缀（业务字段不带后缀）
                            for cnk, (orig_col, val) in norm_row.items():
                                if val in (None, ""):
                                    continue
                                # 业务字段带后缀 _start/_end，库列是其前缀
                                for suffix in ("_start", "_end", "_begin", "_finish"):
                                    if col.endswith(suffix):
                                        base = col[: -len(suffix)]
                                        if cnk == base or cnk.startswith(base + "_"):
                                            if fields.get(biz_field, {}).get("db") in (None, ""):
                                                fields.setdefault(biz_field, {})["db"] = val
                                            break
                                else:
                                    # 业务字段无后缀，库列是其后缀变体（如 _value, _str）
                                    if cnk.startswith(col + "_") or cnk == col:
                                        if fields.get(biz_field, {}).get("db") in (None, ""):
                                            fields.setdefault(biz_field, {})["db"] = val
                        # 继续遍历剩余实体表，把多源字段全部汇到矩阵里
                else:
                    _log("WARN", "未配置规则且无库表清单，跳过数据库校验")
            if not db_hit:
                _log("WARN", "数据库侧未取到任何有效值")
        else:
            _log("WARN", "未配置数据库连接，跳过数据库校验")

        # ---- 自动日志取值 ----
        if log_sources and session.trace_id:
            _log("INFO", f"按 trace_id={session.trace_id} 拉取日志")
            log_vals = self._query_logs(session.trace_id, log_sources, logs)
            if log_vals:
                _log("OK", f"日志匹配字段 {len(log_vals)} 个：{', '.join(log_vals.keys())}")
                norm_log = {self._norm_field_key(k): v for k, v in log_vals.items()}
                for biz_field in list(fields.keys()):
                    col = self._norm_field_key(biz_field)
                    if col in norm_log and norm_log[col] not in (None, ""):
                        fields.setdefault(biz_field, {})["log"] = norm_log[col]
            else:
                _log("WARN", f"未找到 trace_id={session.trace_id} 的日志记录")
        else:
            if not log_sources:
                _log("WARN", "未配置日志源，跳过日志校验")
            if not session.trace_id:
                _log("WARN", "请求未填写 trace_id，跳过日志校验")

        matrix = matrix_mod.build_matrix(fields)
        ok_count = sum(1 for m in matrix if m.conclusion == "consistent")
        bad_count = sum(1 for m in matrix if m.conclusion == "inconsistent")
        partial_count = sum(1 for m in matrix if m.conclusion == "partial")
        _log("OK" if bad_count == 0 else "WARN", f"矩阵生成完成：{ok_count} 项一致 / {bad_count} 项不一致 / {partial_count} 项缺少对照")
        _log("INFO", f"校验结束，耗时 {time.time() - t0:.2f}s")
        return {
            "matrix": [m.__dict__ for m in matrix],
            "summary": matrix_mod.summarize(matrix),
            "has_anomaly": matrix_mod.has_anomaly(matrix),
            "logs": logs,
        }

    # ---------- 自动取值辅助 ----------
    @staticmethod
    def _norm_key(k: str) -> str:
        return str(k).strip().lower().replace("_", "").replace("-", "")

    @staticmethod
    def _norm_field_key(k: str) -> str:
        """字段名归一化：把驼峰转成下划线分隔的小写，再去掉连字符/多余下划线。
        例：snapshotDateStart -> snapshot_date_start；snapshot_date -> snapshot_date；
        snapshotDateEnd -> snapshot_date_end；snapshot-date-end -> snapshot_date_end。
        归一化到 snake_case，让驼峰和下划线两种命名能匹配同一字段。"""
        import re as _re
        s = str(k).strip()
        # 驼峰转下划线：camelCase -> camel_Case；连续大写 (HTTPRequest) -> HTTP_Request
        s = _re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", s)
        s = _re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1_\2", s)
        s = s.lower()
        # 替换连字符为空格，统一压缩
        s = s.replace("-", " ")
        s = _re.sub(r"\s+", "_", s)
        # 多个下划线压缩成一个
        s = _re.sub(r"_+", "_", s)
        return s.strip("_")

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

    def _query_table(
        self,
        tm: TableMapping,
        req: dict,
        db_conns: list[DbConnection],
        logs: list[dict] | None = None,
        fallback_id: Any | None = None,
        prefer_fallback_id: bool = False,
        response_first_row: dict[str, Any] | None = None,
    ) -> Optional[dict]:
        """按接口入参字段自动查询关联表最近一条匹配记录。

        规则：
        1. table 支持 'db_name.table' 或 'table'（后者取第一个数据库连接）。
        2. 把请求中非空字段名归一化，与表列名归一化后匹配，构造 WHERE。
        3. 列表接口（prefer_fallback_id=True）优先用 fallback_id（返回列表首条记录的
           主键）定位库里的真实行，再 AND 上入参中的业务过滤条件（如 status=1）。
           若 id 查不到，会尝试用返回首行的业务字段（如 multiple_batch_goods_id）
           或入参业务字段兜底反查。
        4. 非列表接口则用入参业务字段定位，按 id 倒序取最新一条。
        """
        response_first_row = response_first_row or {}
        table_full = tm.table
        db_name = None
        if "." in table_full:
            db_name, table = table_full.split(".", 1)
        else:
            table = table_full
        conn = None
        if db_name:
            conn = next(
                (c for c in db_conns if self._norm_key(c.name) == self._norm_key(db_name)),
                None,
            )
        if conn is None:
            conn = db_conns[0]
        if conn is None:
            return None
        if logs:
            logs.append(
                {
                    "time": time.strftime("%H:%M:%S"),
                    "level": "INFO",
                    "msg": f"连接 {conn.name} 查表 {table_full}",
                }
            )
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
                engine = create_engine(url, pool_pre_ping=True)
            else:
                port = _normalize_port(conn.port, db_type)
                url = f"{driver}://{conn.user}:{conn.password}@{conn.host}:{port}/{conn.database}"
                # connect_timeout 控制建连；read_timeout/write_timeout 控制单条 SQL 执行
                # 避免大表/锁表卡死。pool_pre_ping 避免拿到死连接。
                if db_type in ("mysql", "mariadb"):
                    qargs = {"connect_timeout": 8, "read_timeout": 12, "write_timeout": 12}
                elif db_type in ("postgres", "hologres"):
                    qargs = {"connect_timeout": 8, "options": "-c statement_timeout=12000"}
                else:
                    qargs = {"connect_timeout": 8}
                engine = create_engine(url, connect_args=qargs, pool_pre_ping=True)

            # 请求字段归一化，只保留非空值（用 _norm_field_key 把驼峰和下划线统一）
            norm_req = {
                self._norm_field_key(k): (k, v)
                for k, v in (req or {}).items()
                if v not in (None, "")
            }

            with engine.connect() as c:
                cols = self._get_table_columns(c, table, db_type)
                norm_cols = {self._norm_field_key(col): col for col in cols}

                idx = 0
                pagination_like = {"pageno", "pagenumber", "page", "current", "pagesize", "limit", "offset", "total", "pages", "paginationcurrent", "paginationpagesize", "paginationtotal", "pagenumber"}

                if logs:
                    cols_preview = ",".join(sorted(norm_cols.keys())[:40])
                    logs.append({"time": time.strftime("%H:%M:%S"), "level": "DEBUG", "msg": f"[诊断] 表 {table} 列归一名(前40): {cols_preview}"})

                # 根据表名语义，推断该表主键在出参首行中对应的字段值。
                # 例如 supplier 表本身的主键列叫 id，但出参里对应字段是 supplierId；
                # 而 supplier_settlement_account 表对应出参 id，所以不会误用 supplierId。
                table_bare_for_pk = self._norm_key(table).split(".")[-1]
                table_specific_pk_value = None
                table_specific_pk_key = None
                for p in [p for p in table_bare_for_pk.split("_") if p]:
                    candidate_resp_key = f"{p}Id"
                    if candidate_resp_key in (response_first_row or {}) and response_first_row[candidate_resp_key] not in (None, ""):
                        if table_bare_for_pk == p or table_bare_for_pk.endswith(f"_{p}"):
                            table_specific_pk_value = response_first_row[candidate_resp_key]
                            table_specific_pk_key = candidate_resp_key
                            break

                def _add_condition(target: list, target_params: dict, col: str, op: str, v: Any) -> None:
                    nonlocal idx
                    target.append(f"{col} {op} :p{idx}")
                    target_params[f"p{idx}"] = v
                    idx += 1

                def _find_col(nk: str, orig_k: str, v: Any, target_conditions: list, target_params: dict) -> bool:
                    """尝试把入参/出参字段匹配到库列，成功则追加条件到 target。"""
                    # 1. 完全匹配
                    if nk in norm_cols:
                        _add_condition(target_conditions, target_params, norm_cols[nk], "=", v)
                        return True
                    # 2. 库列是入参字段的前缀（snapshot_date_start -> snapshot_date）
                    for cnk, col in norm_cols.items():
                        if cnk.startswith(nk + "_") or cnk == nk:
                            _add_condition(target_conditions, target_params, col, "=", v)
                            return True
                    # 3. 入参字段是库列的前缀，且带 start/end/begin/finish/min/max 后缀
                    for suffix in ("_start", "_end", "_begin", "_finish", "_min", "_max"):
                        if nk.endswith(suffix):
                            base = nk[: -len(suffix)]
                            if base in norm_cols:
                                op = "<=" if any(x in suffix for x in ("end", "finish", "max")) else ">="
                                _add_condition(target_conditions, target_params, norm_cols[base], op, v)
                                return True
                    # 4. 入参字段是库列的前缀（snapshotDate -> snapshot_date）
                    for cnk, col in norm_cols.items():
                        if nk.startswith(cnk + "_") or nk == cnk:
                            _add_condition(target_conditions, target_params, col, "=", v)
                            return True
                    return False

                # 入参业务条件
                biz_conditions: list[str] = []
                biz_params: dict = {}
                for nk, (orig_k, v) in norm_req.items():
                    if self._norm_key(orig_k) in pagination_like:
                        continue
                    _find_col(nk, orig_k, v, biz_conditions, biz_params)

                # 出参首行业务条件（用于 id 反查失败时兜底）
                resp_conditions: list[str] = []
                resp_params: dict = {}
                if response_first_row:
                    for k, v in response_first_row.items():
                        if v in (None, "") or self._norm_key(k) in pagination_like:
                            continue
                        nk = self._norm_field_key(k)
                        _find_col(nk, k, v, resp_conditions, resp_params)

                def _exec(
                    conditions: list[str],
                    params: dict,
                    label: str,
                    limit: int = 1,
                    order_by: str | None = None,
                ) -> list[dict]:
                    if not conditions:
                        return []
                    where = " AND ".join(conditions)
                    order_clause = f" ORDER BY {order_by}" if order_by else ""
                    sql = text(f"SELECT * FROM {table} WHERE {where}{order_clause} LIMIT {limit}")
                    sql_log = str(sql) + (f" 参数: {params}" if params else "")
                    if logs:
                        logs.append({"time": time.strftime("%H:%M:%S"), "level": "INFO", "msg": f"[{label}] {sql_log}"})
                    result = c.execute(sql, params)
                    rows = [dict(r) for r in result.mappings().all()]
                    from datetime import date as _date, datetime as _datetime, time as _time
                    for row in rows:
                        for k, v in row.items():
                            if isinstance(v, Decimal):
                                row[k] = float(v)
                            elif isinstance(v, (_datetime, _date, _time)):
                                row[k] = v.isoformat()
                            elif isinstance(v, bytes):
                                try:
                                    row[k] = v.decode("utf-8")
                                except Exception:
                                    row[k] = v.hex()
                    return rows

                def _best_match(rows: list[dict], target: dict[str, Any]) -> dict | None:
                    """在 rows 中找到与 target 字段值最匹配的一行。"""
                    if not rows:
                        return None
                    if not target:
                        return rows[0]
                    norm_target = {self._norm_field_key(k): v for k, v in target.items() if v not in (None, "")}
                    best: dict | None = None
                    best_score = -1
                    for row in rows:
                        score = 0
                        for k, v in row.items():
                            nk = self._norm_field_key(k)
                            if nk in norm_target:
                                if str(v) == str(norm_target[nk]):
                                    score += 2
                                else:
                                    score -= 1
                        if score > best_score:
                            best_score = score
                            best = row
                    # 至少有一个字段完全匹配才认为找到对应行，否则退回第一行
                    return best if best_score > 0 else rows[0]

                def _first(rows: list[dict]) -> Optional[dict]:
                    return rows[0] if rows else None

                def _build_resp_conditions(resp_row: dict[str, Any], only_unique: bool = True) -> tuple[list[str], dict]:
                    """根据出参某一行字段构造精确查询条件，优先使用主键/业务字段。

                    only_unique=True 时只选能唯一标识一行的字段（id / 表名+id），
                    避免用普通业务字段直接等值导致命中错误行。
                    """
                    conds: list[str] = []
                    params: dict = {}
                    nonlocal idx
                    table_norm = self._norm_key(table)
                    table_bare = table_norm.split(".")[-1]
                    # 优先 id / 表名+id / {表名前缀}_id / 表名去下划线前缀_id
                    # 优先使用与当前表名语义相关的 id 字段（如 supplier 表用 supplier_id），
                    # 再用通用 id；避免拿主表主键 id 去反查外键表导致错行。
                    id_candidates = [
                        f"{table_norm}id",
                        f"{table_norm}_id",
                        f"{table_bare}id",
                        f"{table_bare}_id",
                    ]
                    # 对 euser_info/supplier_settlement_account 这种复合表名，
                    # 把每一段都作为实体 id 前缀尝试（如 supplier_id / settlement_account_id）
                    parts = [p for p in table_bare.split("_") if p]
                    for p in parts:
                        id_candidates.append(f"{p}id")
                        id_candidates.append(f"{p}_id")
                    # 兜底：通用主键 id
                    id_candidates.append("id")
                    for key in id_candidates:
                        norm_key = self._norm_field_key(key)
                        # 通用 id 列，尝试用表名语义对应的外键值（如 supplier 表用 supplierId）
                        if norm_key == "id" and table_specific_pk_value is not None and "id" in norm_cols:
                            conds.append(f"{norm_cols['id']} = :p{idx}")
                            params[f"p{idx}"] = table_specific_pk_value
                            idx += 1
                            return conds, params
                        matched_field = None
                        for k, v in resp_row.items():
                            if v in (None, ""):
                                continue
                            if self._norm_field_key(k) == norm_key:
                                matched_field = (k, v)
                                break
                        if matched_field and norm_key in norm_cols:
                            conds.append(f"{norm_cols[norm_key]} = :p{idx}")
                            params[f"p{idx}"] = matched_field[1]
                            idx += 1
                            return conds, params
                    if only_unique:
                        # id类候选未命中时，兜底尝试出参里所有“以id结尾”的字段
                        # （如 euserId -> euser_id / userId -> user_id），命中即用其精确反查
                        if not conds:
                            for k, v in resp_row.items():
                                if v in (None, ""):
                                    continue
                                nk = self._norm_field_key(k)
                                if not nk.endswith("id"):
                                    continue
                                if nk in norm_cols:
                                    conds.append(f"{norm_cols[nk]} = :p{idx}")
                                    params[f"p{idx}"] = v
                                    idx += 1
                                    return conds, params
                        return conds, params
                    # 否则尝试所有能匹配库列的出参业务字段
                    for k, v in resp_row.items():
                        if v in (None, "") or self._norm_key(k) in pagination_like:
                            continue
                        nk = self._norm_field_key(k)
                        if nk in norm_cols:
                            conds.append(f"{norm_cols[nk]} = :p{idx}")
                            params[f"p{idx}"] = v
                            idx += 1
                    return conds, params

                def _query_by_response_row(resp_row: dict[str, Any], label: str) -> Optional[dict]:
                    """用出参单行字段在库里精确定位。"""
                    if not resp_row:
                        return None
                    conds, params = _build_resp_conditions(resp_row, only_unique=True)
                    if not conds:
                        return None
                    conds.extend(biz_conditions)
                    params.update(biz_params)
                    rows = _exec(conds, params, label, limit=1)
                    return _first(rows)

                # 列表接口：多策略兜底反查，优先精确命中返回的每一行
                if prefer_fallback_id and fallback_id is not None:
                    # 策略 A：用出参首行的 id / 表名+id 精确反查（最可能命中同一条）
                    row = _query_by_response_row(response_first_row or {}, "首行id精确反查")
                    if row:
                        return row

                    # 策略 B：id + 入参业务条件（兼容旧逻辑）
                    id_col = None
                    b_cands = ["id", f"{self._norm_key(table)}id", f"{self._norm_key(table)}_id"]
                    for p in [x for x in self._norm_key(table).split(".")[-1].split("_") if x]:
                        b_cands.append(f"{p}id")
                        b_cands.append(f"{p}_id")
                    for cand in b_cands:
                        if cand in norm_cols:
                            id_col = norm_cols[cand]
                            break
                    if id_col:
                        # 通用 id 列，优先用表名语义对应的外键值（如 supplier 表用 supplierId）
                        if self._norm_field_key(id_col) == "id" and table_specific_pk_value is not None:
                            pk_value = table_specific_pk_value
                            pk_log_key = table_specific_pk_key
                        else:
                            pk_value = fallback_id
                            pk_log_key = id_col
                        cond = [f"{id_col} = :p{idx}"]
                        params = {f"p{idx}": pk_value}
                        idx += 1
                        cond.extend(biz_conditions)
                        params.update(biz_params)
                        if logs:
                            logs.append({"time": time.strftime("%H:%M:%S"), "level": "INFO", "msg": f"用返回主键 {pk_log_key}={pk_value} (库列 {id_col}) 定位库行，带上入参业务条件 {len(biz_conditions)} 个"})
                        rows = _exec(cond, params, "id反查", limit=1)
                        if rows:
                            return _first(rows)

                    # 策略 C：用入参业务条件查一批，然后在 Python 里按字段值匹配出参首行
                    if biz_conditions:
                        if logs:
                            logs.append({"time": time.strftime("%H:%M:%S"), "level": "INFO", "msg": f"按入参业务条件拉取最多 50 行，再匹配出参首行：{', '.join(c.split(' = ')[0] for c in biz_conditions)}"})
                        rows = _exec(biz_conditions, biz_params, "入参业务条件批量匹配", limit=50)
                        if rows:
                            return _best_match(rows, response_first_row or {})

                    if logs:
                        logs.append({"time": time.strftime("%H:%M:%S"), "level": "INFO", "msg": f"表 {table_full} 所有反查策略均无结果"})
                    return None

                # 非列表接口（多为 submit/保存类写接口）：
                # 写接口出参通常返回刚写入行的主键，应优先用出参首行的语义主键精确反查，
                # 而不是直接拿入参草稿去匹配存量行（极易因字段差异落空）。
                # 若出参无 id，则按入参业务条件反查，并做弱字段分层回退。
                if response_first_row:
                    conds, params = _build_resp_conditions(response_first_row, only_unique=True)
                    # 仅保留与当前表列对应的条件（去掉通用 id 兜底的空条件）
                    conds = [c for c in conds if c]
                    if conds:
                        if logs:
                            logs.append({"time": time.strftime("%H:%M:%S"), "level": "INFO", "msg": f"非列表接口：用出参首行主键反查 {table_full}：{' AND '.join(conds)} 参数: {params}"})
                        rows = _exec(conds, params, "出参主键反查", limit=1)
                        if rows:
                            return _first(rows)
                        if logs:
                            logs.append({"time": time.strftime("%H:%M:%S"), "level": "INFO", "msg": f"出参主键在 {table_full} 未命中，回退入参条件反查"})

                # 弱业务字段：这些字段差异大（描述/备注/背景等），不宜全部 AND 等值拼接
                weak_biz_fields = {
                    "remark", "remarks", "description", "descriptions", "background",
                    "backgroundinfo", "note", "notes", "memo", "comment", "comments",
                    "requirementbackground", "paymentdescription", "supplierchanneldescription",
                    "summary", "remarkinfo",
                }

                if biz_conditions:
                    # 第一层：全部入参业务条件（强+弱）
                    rows = _exec(biz_conditions, biz_params, "入参业务条件(全)", limit=1)
                    if rows:
                        if logs:
                            logs.append({"time": time.strftime("%H:%M:%S"), "level": "INFO", "msg": f"非列表接口：入参业务条件命中 {table_full}"})
                        return _first(rows)

                    # 第二层：仅强业务字段（去掉弱字段），避免描述类差异导致整条落空
                    strong_conditions = []
                    strong_params = {}
                    for cond in biz_conditions:
                        # cond 形如 "col = :pN"，提取对应参数名
                        token = cond.split(" = ")[-1].strip()
                        param_name = token[1:] if token.startswith(":") else None
                        if param_name and param_name in biz_params:
                            # 用列名归一化判断是否为弱字段
                            col = cond.split(" ")[0]
                            if self._norm_field_key(col) in weak_biz_fields:
                                continue
                            strong_conditions.append(cond)
                            strong_params[param_name] = biz_params[param_name]
                    if strong_conditions:
                        if logs:
                            logs.append({"time": time.strftime("%H:%M:%S"), "level": "INFO", "msg": f"非列表接口：全条件落空，去掉弱字段重试 {table_full}：{', '.join(c.split(' = ')[0] for c in strong_conditions)}"})
                        rows = _exec(strong_conditions, strong_params, "入参业务条件(强字段)", limit=1)
                        if rows:
                            return _first(rows)

                if logs:
                    logs.append({"time": time.strftime("%H:%M:%S"), "level": "INFO", "msg": f"表 {table_full} 所有反查策略均无结果，跳过反查"})
                return None
        except Exception as e:  # noqa: BLE001
            msg = f"查表 {table_full} 失败: {type(e).__name__}: {e}"
            print(f"[captrace] {msg}")
            if logs:
                logs.append({"time": time.strftime("%H:%M:%S"), "level": "ERROR", "msg": msg})
            return None

    def _dump_query_info(
        self,
        tm: TableMapping,
        req: dict,
        resp_first_row: dict[str, Any] | None,
        fallback_id: Any | None,
        db_conns: list[DbConnection],
    ) -> str:
        """生成一份可读的反查诊断信息，用于前端日志展示。"""
        lines = []
        lines.append(f"反查目标表：{tm.table}")
        lines.append(f"入参业务字段：{ {k: v for k, v in (req or {}).items() if v not in (None, '')} }")
        if resp_first_row:
            lines.append(f"出参首行字段：{resp_first_row}")
            lines.append(f"提取首行主键值：{fallback_id}")
            lines.append(f"可用数据库连接：{[getattr(c, 'alias', getattr(c, 'name', str(c))) for c in db_conns]}")
            try:
                import sys
                sys.stdout.flush()
                print(f"[captrace][diag] {lines[-1]}", flush=True)
            except Exception:
                pass
            return "\n".join(lines)

    def _query_logs(
        self,
        trace_id: str,
        log_sources: list[LogSource],
        logs: list[dict] | None = None,
    ) -> dict:
        """按 trace_id 拉日志关键值，返回扁平键值对（JSON 解析或 key=value 正则）。"""
        merged: dict = {}
        for src in log_sources:
            if logs:
                logs.append(
                    {
                        "time": time.strftime("%H:%M:%S"),
                        "level": "INFO",
                        "msg": f"拉取日志源：{src.name}",
                    }
                )
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
                msg = f"拉日志 {src.name} 失败: {type(e).__name__}: {e}"
                print(f"[captrace] {msg}")
                if logs:
                    logs.append({"time": time.strftime("%H:%M:%S"), "level": "ERROR", "msg": msg})
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
        env_raw = (cfg.get("env") or "test").lower()
        # 兼容中文/别名：测试->test 生产->prod
        env_alias = {"测试": "test", "生产": "prod", "test": "test", "prod": "prod"}
        env_val = env_alias.get(env_raw, "test")
        p = ProjectConfig(
            cfg["name"],
            Env(env_val),
        )
        db_conns = []
        for x in cfg.get("db_connections", []):
            x = dict(x)
            db_type = x.get("db_type", "mysql")
            x["port"] = _normalize_port(x.get("port"), db_type)
            x["db_type"] = DbType(db_type)
            db_conns.append(DbConnection(**x))
        p.db_connections = db_conns
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
            port = _normalize_port(cfg.get("port"), db_type)
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
        port = _normalize_port(cfg.get("port"), db_type)
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
        1. 取路径中所有段，去掉动作词（create/update/list...）、版本号、通用前缀
        2. 保留的实体段转 snake_case 并拆分，收集所有候选词
        3. 对候选词做组合（支持 1~3 个连续段），在真实表中做子串/模糊匹配
        4. 越长的连续词命中权重越高
        """
        action_words = {"create", "add", "new", "update", "edit", "save", "modify",
                        "delete", "remove", "get", "detail", "list", "query", "search",
                        "info", "submit", "do", "api", "v1", "v2", "v3", "app", "web",
                        "find", "page", "export", "import", "summary", "bytoken",
                        "by", "resource", "service", "rpc", "token"}
        raw_segs = [p for p in api_path.rstrip("/").split("?")[0].split("/") if p]
        # 去动作词、版本号、通用前缀，保留实体段
        segs = [p for p in raw_segs if p.lower() not in action_words]
        if not segs:
            return []

        suffix_skip = {"list", "page", "vo", "dto", "info", "detail", "query", "record",
                       "summary", "export", "import"}
        prefix_skip = {"find", "save", "update", "delete", "remove", "get", "select",
                       "add", "create", "edit", "modify", "submit", "do"}

        def _clean_parts(text: str) -> list[str]:
            snake = self._camel_to_snake(text).strip("_")
            parts = [p for p in snake.split("_")
                     if p and p not in suffix_skip and p not in prefix_skip and len(p) > 1]
            return parts

        # 收集候选词：单段、连续段组合
        candidates: list[tuple[str, int]] = []  # (词, 段数)
        seen = set()
        for seg in segs:
            parts = _clean_parts(seg)
            for p in parts:
                for cand in (p, p.rstrip("s")):
                    if cand and cand not in seen:
                        seen.add(cand)
                        candidates.append((cand, 1))

        # 连续 2~3 段组合
        for length in (2, 3):
            for i in range(len(segs) - length + 1):
                combo_parts: list[str] = []
                for seg in segs[i:i + length]:
                    combo_parts.extend(_clean_parts(seg))
                if combo_parts:
                    combo = "_".join(combo_parts)
                    if combo not in seen:
                        seen.add(combo)
                        candidates.append((combo, length))

        if not candidates:
            return []

        norm_tables = {t: self._norm_key(t) for t in tables}
        bare_norm: dict[str, str] = {}
        for t in tables:
            tbl = t.split(".", 1)[-1] if "." in t else t
            bare_norm[t] = self._norm_key(tbl)

        scores: dict[str, float] = {t: 0.0 for t in tables}
        for t in tables:
            bnk = bare_norm[t]
            for cand, seg_len in candidates:
                ncand = cand.replace("_", "")
                if not ncand:
                    continue
                # 完全相等（去复数）
                if ncand == bnk or ncand == bnk.rstrip("s") or bnk == ncand.rstrip("s"):
                    scores[t] += 2.0 + seg_len
                # 子串：候选在表名中
                elif ncand in bnk:
                    scores[t] += 1.0 + 0.3 * seg_len
                # 子串：表名在候选中
                elif bnk and bnk in ncand:
                    scores[t] += 0.5 + 0.1 * seg_len
                else:
                    ratio = difflib.SequenceMatcher(None, ncand, bnk).ratio()
                    if ratio >= 0.6:
                        scores[t] += ratio * 0.5

        ranked = sorted(((t, s) for t, s in scores.items() if s > 0),
                        key=lambda x: x[1], reverse=True)
        if ranked:
            return [t for t, _ in ranked]
        return []

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
            "logs": [
                {"time": time.strftime("%H:%M:%S"), "level": "INFO", "msg": "载入样例数据"},
                {"time": time.strftime("%H:%M:%S"), "level": "INFO", "msg": "提取接口入参字段 3 个：amount, couponId, orderId"},
                {"time": time.strftime("%H:%M:%S"), "level": "INFO", "msg": "提取接口出参字段 2 个：orderId, payAmount, status"},
                {"time": time.strftime("%H:%M:%S"), "level": "WARN", "msg": "未配置数据库连接，跳过数据库校验"},
                {"time": time.strftime("%H:%M:%S"), "level": "WARN", "msg": "请求未填写 trace_id，跳过日志校验"},
                {"time": time.strftime("%H:%M:%S"), "level": "WARN", "msg": "矩阵生成完成：2 项一致 / 2 项不一致"},
            ],
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
    # 启动即自动开启 9090 接收服务：省去手动点「开始实时捕获」，
    # Fiddler 推送随时可进，端口被外部占用时静默跳过（前端会显示「外部占用」）。
    try:
        started = api.serve_start(9090)
        if started.get("ok"):
            print(f"[captrace] 9090 接收服务已自动启动：{started}", flush=True)
        else:
            print(f"[captrace] 9090 自动启动失败（可忽略，手动点按钮重试）：{started}", flush=True)
    except Exception as _e:  # noqa: BLE001
        print(f"[captrace] 9090 自动启动异常：{_e}", flush=True)
    webview.start()


if __name__ == "__main__":
    main()
