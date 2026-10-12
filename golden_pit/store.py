"""扫描结果持久化：原子写入、历史归档（自动保留最近 N 份）。"""
from __future__ import annotations

import json
import os
import re
import tempfile
import threading
from pathlib import Path
from typing import Optional

HISTORY_KEEP = 60
HISTORY_NAME_RE = re.compile(r"^scan_\d{8}_\d{6}\.json$")


class Store:
    def __init__(self, data_dir: Path):
        self.dir = Path(data_dir)
        self.history = self.dir / "history"
        self.history.mkdir(parents=True, exist_ok=True)
        self.latest_path = self.dir / "latest.json"
        self._lock = threading.Lock()

    @staticmethod
    def _atomic_write(path: Path, obj) -> None:
        fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(obj, f, ensure_ascii=False, allow_nan=False)
            os.replace(tmp, path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def save_scan(self, scan: dict) -> None:
        with self._lock:
            self._atomic_write(self.latest_path, scan)
            self._atomic_write(self.history / f"scan_{scan['scan_id']}.json", scan)
            for old in sorted(self.history.glob("scan_*.json"))[:-HISTORY_KEEP]:
                old.unlink(missing_ok=True)

    def latest(self) -> Optional[dict]:
        try:
            return json.loads(self.latest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def list_history(self) -> list[dict]:
        out = []
        for p in sorted(self.history.glob("scan_*.json"), reverse=True):
            try:
                s = json.loads(p.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            out.append({"name": p.name, "finished_at": s.get("finished_at"), "trigger": s.get("trigger"),
                        "count": len(s.get("results", [])), "summary": s.get("summary", {})})
        return out

    def load_history(self, name: str) -> Optional[dict]:
        if not HISTORY_NAME_RE.match(name):  # 白名单文件名，杜绝路径穿越
            return None
        try:
            return json.loads((self.history / name).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
