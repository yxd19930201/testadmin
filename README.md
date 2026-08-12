# 抓包测效助手 (captrace)

测试工程师提效工具：**Fiddler 抓包 → 接口/数据库(多表)/日志 三方一致性校验 → AI 根因分析 → 一键生成可提交 BUG**。

> 关键设计边界：**一致性矩阵由工具确定性规则比对生成（不依赖 AI）；AI 仅用于不一致/报错时的根因分析与 BUG 生成。** 矩阵比对是纯本地、可审计、可离线的逻辑；AI 是后期智能环节。

## 整体架构

```
┌──────────────────────────────────────────────────────────────────┐
│                         桌面客户端 (C/S)                            │
│  标题栏(项目切换/代理状态/命令面板) · 综合校验页 · 配置页 · AI页    │
└───────────────┬──────────────────────────────────┬───────────────┘
                │ 导入 .saz / 实时捕获                │
      ┌─────────▼──────────┐              ┌──────────▼──────────┐
      │  capture(接入层)    │              │  config(配置层)      │
      │  receiver(方案A)    │              │  项目/DB/日志/AI配置 │
      │  saz_import(方案C)  │              │  storage 持久化JSON  │
      └─────────┬──────────┘              └──────────┬──────────┘
                │ sessions                  rules/config│
      ┌─────────▼──────────────────────────────────────▼──────────┐
      │                      checker(校验层)                        │
      │  api_check → db_check(多表,只读) → log_check → matrix(核心) │
      │  矩阵 = 确定性数值/字符串比对，输出 ✓/✗                      │
      └─────────┬──────────────────────────────────────┬──────────┘
                │ 发现异常(✗ 或 日志ERROR)               │
      ┌─────────▼──────────┐              ┌─────────────▼──────────┐
      │  connectors(连接)   │              │  ai(智能层)             │
      │  db(只读SELECT)     │              │  analyzer 根因+BUG生成  │
      │  log(ELK/文件/容器)  │              │  → 调用 connectors.ai  │
      │  ai(API网关)        │              └────────────────────────┘
      └────────────────────┘
```

## 目录结构

```
captrace/
├── main.py                 # 编排入口 + CLI（演示完整闭环）
├── config/
│   ├── models.py           # 领域模型（Session/DbConnection/LogSource/AiModelConfig/CheckRule/MatrixField/BugReport）
│   └── settings.py         # ProjectConfig 序列化
├── capture/
│   ├── receiver.py          # 方案A：接收 Fiddler CustomRules 推送（本地 HTTP 服务）
│   └── saz_import.py        # 方案C：解析 .saz 存档为 Session 列表
├── checker/
│   ├── matrix.py            # ★ 三方一致性矩阵（确定性比对核心，纯 stdlib）
│   ├── rules.py             # 校验规则匹配（API → 多表映射）
│   └── api_check.py         # 接口入参/出参提取
├── connectors/
│   ├── db.py                # 数据库只读连接（强制 SELECT，参数绑定）
│   ├── log.py               # 日志源检索（ELK / 远程文件 / 容器）
│   └── ai.py                # AI 模型网关（OpenAI 兼容，key 本地加密）
├── ai/
│   └── analyzer.py          # 根因分析 + BUG 单生成（消费 connectors.ai）
└── storage/
    └── projects.py          # 项目配置持久化（按项目隔离 JSON）
tests/
└── test_matrix.py           # 核心比对逻辑冒烟测试
```

## 三种数据源接入

| 方案 | 实现 | 实时性 |
|---|---|---|
| A. FiddlerScript 推送 | `capture/receiver.py` 监听 `127.0.0.1:9090/ingest` | 毫秒级 |
| B. 内置 MITM 代理 | 后续扩展（接入层新增 proxy 模块） | 天然实时 |
| C. 导入 .saz | `capture/saz_import.py` 解包解析 | 批次 |

## 运行

```bash
pip install -e .
python -m captrace.main --demo        # 跑通 POST /api/order/create 闭环样例
pytest tests/                          # 核心比对逻辑测试
```

## 安全约束

- 数据库连接器强制 `read_only`，仅允许 `SELECT`，参数绑定防注入。
- AI Key 存于本地加密配置，调用经本地服务中转，绝不出现在前端/HTML。
- 项目配置按 `%CAPTRACE_HOME%`(默认 `~/.captrace/projects`) 隔离存储。
