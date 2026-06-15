"""状态持久化存储 - JSON 文件存储"""
from __future__ import annotations

import json
import os
from typing import Dict, List, Optional, Any
from datetime import datetime

from .models import (
    ScannedFile, PlannedMove, ExecutedMove, UndoRecord,
    generate_id, now_iso,
)


class StateStore:
    """JSON 文件状态存储"""

    def __init__(self, state_file: str):
        self.state_file = state_file
        self._data = self._load_or_init()

    def _load_or_init(self) -> Dict[str, Any]:
        """加载或初始化状态文件"""
        if os.path.exists(self.state_file):
            try:
                with open(self.state_file, "r", encoding="utf-8") as f:
                    return json.load(f)
            except (json.JSONDecodeError, IOError):
                pass
        return {
            "scanned_files": [],
            "plans": {},
            "runs": {},
            "undo_records": [],
            "last_scan": None,
            "last_plan": None,
            "created_at": now_iso(),
        }

    def save(self) -> None:
        """保存状态到文件"""
        os.makedirs(os.path.dirname(os.path.abspath(self.state_file)) or ".", exist_ok=True)
        with open(self.state_file, "w", encoding="utf-8") as f:
            json.dump(self._data, f, ensure_ascii=False, indent=2)

    # ---- 扫描结果 ----

    def save_scan(self, files: List[ScannedFile]) -> None:
        """保存扫描结果"""
        self._data["scanned_files"] = [f.to_dict() for f in files]
        self._data["last_scan"] = now_iso()
        self.save()

    def get_scan(self) -> List[ScannedFile]:
        """获取上次扫描结果"""
        return [
            ScannedFile(
                id=f["id"],
                source_path=f["source_path"],
                filename=f["filename"],
                size=f["size"],
                mtime=f["mtime"],
                matched_rule=f.get("matched_rule"),
            )
            for f in self._data.get("scanned_files", [])
        ]

    def get_last_scan_time(self) -> Optional[str]:
        return self._data.get("last_scan")

    # ---- 归档预案 ----

    def save_plan(self, plan_id: str, moves: List[PlannedMove], has_conflicts: bool) -> None:
        """保存归档预案"""
        self._data["plans"][plan_id] = {
            "id": plan_id,
            "created_at": now_iso(),
            "moves": [m.to_dict() for m in moves],
            "has_conflicts": has_conflicts,
        }
        self._data["last_plan"] = plan_id
        self.save()

    def get_plan(self, plan_id: str) -> Optional[Dict[str, Any]]:
        """获取指定预案"""
        return self._data.get("plans", {}).get(plan_id)

    def get_last_plan(self) -> Optional[Dict[str, Any]]:
        """获取最近的预案"""
        plan_id = self._data.get("last_plan")
        if plan_id:
            return self.get_plan(plan_id)
        return None

    def get_last_plan_id(self) -> Optional[str]:
        return self._data.get("last_plan")

    # ---- 执行记录 ----

    def create_run(self, plan_id: str, dry_run: bool) -> str:
        """创建一次执行记录，返回 run_id"""
        run_id = generate_id()
        self._data["runs"][run_id] = {
            "id": run_id,
            "plan_id": plan_id,
            "created_at": now_iso(),
            "dry_run": dry_run,
            "completed_at": None,
            "moves": [],
            "is_undone": False,
        }
        self.save()
        return run_id

    def add_executed_move(self, run_id: str, move: ExecutedMove) -> None:
        """添加一条执行记录"""
        if run_id in self._data["runs"]:
            self._data["runs"][run_id]["moves"].append(move.to_dict())
            self.save()

    def complete_run(self, run_id: str) -> None:
        """标记执行完成"""
        if run_id in self._data["runs"]:
            self._data["runs"][run_id]["completed_at"] = now_iso()
            self.save()

    def get_run(self, run_id: str) -> Optional[Dict[str, Any]]:
        """获取执行记录"""
        return self._data.get("runs", {}).get(run_id)

    def get_all_runs(self) -> List[Dict[str, Any]]:
        """获取所有执行记录"""
        return list(self._data.get("runs", {}).values())

    # ---- 撤销 ----

    def mark_run_undone(self, run_id: str) -> None:
        """标记执行已撤销"""
        if run_id in self._data["runs"]:
            self._data["runs"][run_id]["is_undone"] = True
            self.save()

    def add_undo_record(self, record: UndoRecord) -> None:
        """添加撤销记录"""
        self._data["undo_records"].append(record.to_dict())
        self.save()

    def get_undo_records(self) -> List[UndoRecord]:
        """获取所有撤销记录"""
        return [
            UndoRecord(
                run_id=r["run_id"],
                undo_timestamp=r["undo_timestamp"],
                moves_restored=r["moves_restored"],
                status=r["status"],
            )
            for r in self._data.get("undo_records", [])
        ]

    # ---- 完整数据导出 ----

    def get_full_state(self) -> Dict[str, Any]:
        """获取完整状态数据（用于 export）"""
        return dict(self._data)
