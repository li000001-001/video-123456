"""任务存储：内存态 + JSON 落盘，服务重启后任务历史不丢。"""
from __future__ import annotations

import json
import threading
import time
import uuid
from pathlib import Path
from typing import Dict, List, Optional

from ..settings import settings

_TASKS_FILE = settings.storage_root / "tasks.json"
_LOCK = threading.RLock()


class TaskStore:
    def __init__(self, path: Path = _TASKS_FILE):
        self.path = path
        self._tasks: Dict[str, dict] = {}
        self.load()

    def load(self) -> None:
        with _LOCK:
            if self.path.exists():
                try:
                    data = json.loads(self.path.read_text(encoding="utf-8"))
                    if isinstance(data, dict):
                        self._tasks = data
                    elif isinstance(data, list):
                        self._tasks = {t["id"]: t for t in data if t.get("id")}
                except Exception:
                    self._tasks = {}
            # 重启后把中断的任务标记为失败
            changed = False
            for t in self._tasks.values():
                if t.get("status") in {"pending", "running"}:
                    t["status"] = "failed"
                    t["error"] = "服务重启导致任务中断，请重新提交"
                    t["stage"] = "已中断"
                    changed = True
            if changed:
                self._persist()

    def _persist(self) -> None:
        with _LOCK:
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(
                json.dumps(self._tasks, ensure_ascii=False, indent=1),
                encoding="utf-8",
            )
            tmp.replace(self.path)

    def create(self, url: str, platform: str, platform_label: str, options: dict) -> dict:
        with _LOCK:
            tid = uuid.uuid4().hex[:10]
            now = time.time()
            task = {
                "id": tid,
                "url": url,
                "platform": platform,
                "platform_label": platform_label,
                "status": "pending",
                "stage": "排队中",
                "progress": 0.0,
                "title": "",
                "author": "",
                "duration": 0,
                "thumbnail": "",
                "category": "",
                "emoji": "",
                "confidence": 0.0,
                "score": 0.0,
                "matched": [],
                "ranking": [],
                "subtitle_text": "",
                "files": {},
                "notes": [],
                "error": "",
                "options": options,
                "created_at": now,
                "updated_at": now,
            }
            self._tasks[tid] = task
            self._persist()
            return task

    def update(self, tid: str, persist: bool = True, **fields) -> Optional[dict]:
        with _LOCK:
            task = self._tasks.get(tid)
            if not task:
                return None
            task.update(fields)
            task["updated_at"] = time.time()
            if persist:
                self._persist()
            return task

    def add_note(self, tid: str, text: str) -> None:
        with _LOCK:
            task = self._tasks.get(tid)
            if not task:
                return
            task.setdefault("notes", []).append(text)
            task["updated_at"] = time.time()

    def get(self, tid: str) -> Optional[dict]:
        with _LOCK:
            t = self._tasks.get(tid)
            return dict(t) if t else None

    def list(self, limit: int = 100) -> List[dict]:
        with _LOCK:
            items = sorted(
                self._tasks.values(), key=lambda t: t.get("created_at", 0), reverse=True
            )
            return [dict(t) for t in items[:limit]]

    def delete(self, tid: str) -> bool:
        with _LOCK:
            if tid not in self._tasks:
                return False
            del self._tasks[tid]
            self._persist()
            return True

    def cleanup_old_files(self) -> int:
        """按 keep_hours 清理过期任务的媒体文件。"""
        keep_hours = float(settings.get("storage", "keep_hours", default=72) or 0)
        if keep_hours <= 0:
            return 0
        deadline = time.time() - keep_hours * 3600
        removed = 0
        with _LOCK:
            for tid, t in list(self._tasks.items()):
                if t.get("created_at", 0) > deadline:
                    continue
                work = settings.storage_root / "work" / tid
                if work.exists():
                    for f in work.glob("*"):
                        try:
                            f.unlink()
                            removed += 1
                        except Exception:
                            pass
        return removed


store = TaskStore()
