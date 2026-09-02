"""read_docs — 读取项目文档（doc/ 目录或相对路径）。

让 Agent 能查阅自身的文档（DESIGN.md 等），是演示"文档工具"的好例子。
限定的基础目录由 DOCS_ROOT 控制，防止越权读取任意文件（安全边界）。

schema: {"path": str（相对 doc/ 或项目根）, "max_chars": int} →
        {"path", "content", "found"}
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any


class DocsReader:
    def __init__(self, project_root: Path) -> None:
        self.root = project_root.resolve()
        # 允许读取根目录下的 doc/、README.md、src/ 下的文档等
        self.base_dirs = [
            self.root / "doc",
            self.root,
        ]

    def read(self, path: str, max_chars: int = 4000) -> dict[str, Any]:
        candidate = self._resolve_safe(path)
        if candidate is None or not candidate.is_file():
            return {"found": False, "path": path, "content": "", "error": f"未找到文件: {path}"}
        try:
            content = candidate.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            return {"found": False, "path": path, "content": "", "error": f"读取失败: {e}"}
        if max_chars and len(content) > max_chars:
            content = content[:max_chars] + "\n…[已截断]"
        return {
            "found": True,
            "path": str(candidate.relative_to(self.root) if self._inside_root(candidate) else candidate),
            "content": content,
            "bytes": len(content.encode("utf-8")),
        }

    def _inside_root(self, p: Path) -> bool:
        try:
            p.resolve().relative_to(self.root)
            return True
        except ValueError:
            return False

    def _resolve_safe(self, path: str) -> Path | None:
        """解析路径且阻止目录穿越（.. 逃逸到 doc/ 之外）。"""
        p = Path(path)
        if p.is_absolute() or ".." in p.parts:
            return None
        for base in self.base_dirs:
            cand = base / p
            if self._inside_root(cand):
                return cand
        return None


DEFAULT_TOOL = DocsReader(Path(os.environ.get("AURORA_PROJECT_ROOT", "."))).read