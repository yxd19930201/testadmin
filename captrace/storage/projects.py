"""项目配置持久化：按项目隔离的 JSON 存储（可导出/导入共享）。"""
from __future__ import annotations

import json
import shutil
from datetime import datetime
from pathlib import Path

from captrace.config.settings import ProjectConfig

# 配置目录强制锁定到当前用户家目录，与 exe 安装目录分离：
#  - 重装 / 升级程序不会清掉用户配置；
#  - 打包分发时不会把配置带进安装包。
# 注意：不再读取 CAPTRACE_HOME，避免有人误指向安装目录导致重装即丢失。
DEFAULT_DIR = Path.home() / ".captrace" / "projects"


class ProjectStore:
    def __init__(self, base_dir: Path = DEFAULT_DIR):
        self.base_dir = Path(base_dir)
        self.base_dir.mkdir(parents=True, exist_ok=True)

    @property
    def backup_dir(self) -> Path:
        d = self.base_dir.parent / "backups"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def save(self, cfg: ProjectConfig) -> Path:
        path = self.base_dir / f"{cfg.name}.json"
        # 保存前自动备份：保留最近 N 份历史，避免误操作/误清空后无法恢复
        if path.exists() and path.stat().st_size > 0:
            self._backup(path)
        path.write_text(
            json.dumps(cfg.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return path

    def _backup(self, path: Path, keep: int = 20):
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        backup_name = f"{ts}-{path.name}"
        backup_path = self.backup_dir / backup_name
        shutil.copy2(path, backup_path)
        # 保留最近 keep 份
        all_backups = sorted(
            self.backup_dir.glob(f"*-{path.name}"),
            key=lambda p: p.stat().st_mtime,
        )
        for old in all_backups[:-keep]:
            old.unlink(missing_ok=True)

    def load(self, name: str) -> ProjectConfig:
        path = self.base_dir / f"{name}.json"
        if not path.exists():
            raise FileNotFoundError(f"项目配置不存在: {path}")
        return ProjectConfig.from_dict(json.loads(path.read_text(encoding="utf-8")))

    def list_projects(self) -> list[str]:
        return sorted(p.stem for p in self.base_dir.glob("*.json"))

    def list_backups(self, name: str) -> list[str]:
        """列出某个项目的可恢复备份文件名（按时间倒序）。"""
        return sorted(
            (p.name for p in self.backup_dir.glob(f"*-{name}.json")),
            reverse=True,
        )

    def restore_backup(self, name: str, backup_name: str) -> Path:
        """从备份恢复指定项目配置。"""
        src = self.backup_dir / backup_name
        if not src.exists():
            raise FileNotFoundError(f"备份不存在: {src}")
        dst = self.base_dir / f"{name}.json"
        # 恢复前也把当前文件备份一下，防止误恢复
        if dst.exists():
            self._backup(dst)
        shutil.copy2(src, dst)
        return dst
