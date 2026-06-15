"""状态持久化存储 - JSON 文件存储"""
from __future__ import annotations

import json
import os
from typing import Dict, List, Optional, Any
from datetime import datetime

from .models import (
    ScannedFile, PlannedMove, ExecutedMove, UndoRecord,
    BatchSnapshot, ImportLog, PlanLock, LockViolation, ConfigSnapshot,
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
            "snapshots": {},
            "import_logs": [],
            "plan_locks": [],
            "lock_violations": [],
            "plan_diffs": [],
            "last_scan": None,
            "last_plan": None,
            "last_snapshot": None,
            "active_lock_id": None,
            "created_at": now_iso(),
        }

    def save(self) -> None:
        """保存状态到文件"""
        os.makedirs(os.path.dirname(os.path.abspath(self.state_file)) or ".", exist_ok=True)
        with open(self.state_file, "w", encoding="utf-8") as f:
            json.dump(self._data, f, ensure_ascii=False, indent=2)

    # ---- 扫描结果 ----

    def save_scan(self, files: List[ScannedFile], config_snapshot: Optional[ConfigSnapshot] = None) -> None:
        """保存扫描结果"""
        self._data["scanned_files"] = [f.to_dict() for f in files]
        self._data["last_scan"] = now_iso()
        if config_snapshot is not None:
            self._data["scan_config"] = config_snapshot.to_dict()
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

    def get_scan_config(self) -> Optional[ConfigSnapshot]:
        """获取上次扫描时的配置快照"""
        data = self._data.get("scan_config")
        if data:
            return ConfigSnapshot.from_dict(data)
        return None

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

    # ---- 批次快照 ----

    def save_snapshot(self, snapshot: BatchSnapshot) -> None:
        """保存批次快照"""
        self._data["snapshots"][snapshot.snapshot_id] = snapshot.to_dict()
        self._data["last_snapshot"] = snapshot.snapshot_id
        self.save()

    def get_snapshot(self, snapshot_id: str) -> Optional[BatchSnapshot]:
        """获取指定快照"""
        data = self._data.get("snapshots", {}).get(snapshot_id)
        if data:
            return BatchSnapshot.from_dict(data)
        return None

    def get_last_snapshot(self) -> Optional[BatchSnapshot]:
        """获取最近的快照"""
        snapshot_id = self._data.get("last_snapshot")
        if snapshot_id:
            return self.get_snapshot(snapshot_id)
        return None

    def get_last_snapshot_id(self) -> Optional[str]:
        """获取最近的快照 ID"""
        return self._data.get("last_snapshot")

    def list_snapshots(self) -> List[Dict[str, Any]]:
        """列出所有快照（摘要信息）"""
        result = []
        for sid, sdata in self._data.get("snapshots", {}).items():
            result.append({
                "snapshot_id": sid,
                "created_at": sdata.get("created_at", ""),
                "plan_id": sdata.get("plan_id", ""),
                "move_count": len(sdata.get("moves", [])),
                "has_conflicts": sdata.get("has_conflicts", False),
                "imported": sdata.get("imported", False),
                "import_source": sdata.get("import_source"),
            })
        return sorted(result, key=lambda x: x["created_at"], reverse=True)

    def get_snapshot_by_plan_id(self, plan_id: str) -> Optional[BatchSnapshot]:
        """根据预案 ID 查找对应的快照"""
        for sid, sdata in self._data.get("snapshots", {}).items():
            if sdata.get("plan_id") == plan_id:
                return BatchSnapshot.from_dict(sdata)
        return None

    # ---- 导入日志 ----

    def add_import_log(self, log: ImportLog) -> None:
        """添加一条导入日志（成功/失败都记）"""
        if "import_logs" not in self._data:
            self._data["import_logs"] = []
        self._data["import_logs"].append(log.to_dict())
        self.save()

    def get_import_logs(self) -> List[ImportLog]:
        """获取所有导入日志"""
        logs = self._data.get("import_logs", [])
        return [ImportLog.from_dict(l) for l in logs]

    def get_last_import_log(self) -> Optional[ImportLog]:
        """获取最近一条导入日志"""
        logs = self._data.get("import_logs", [])
        if logs:
            return ImportLog.from_dict(logs[-1])
        return None

    # ---- 预案版本锁定 ----

    def create_lock(self, snapshot_id: str, plan_id: str, reason: Optional[str] = None) -> PlanLock:
        """创建一个新的预案版本锁定

        同时会先释放之前的活动锁定。
        """
        self.release_active_lock(reason="新锁定取代旧锁定")

        lock = PlanLock(
            lock_id=generate_id(),
            snapshot_id=snapshot_id,
            plan_id=plan_id,
            locked_at=now_iso(),
            reason=reason,
            is_active=True,
        )

        if "plan_locks" not in self._data:
            self._data["plan_locks"] = []
        self._data["plan_locks"].append(lock.to_dict())
        self._data["active_lock_id"] = lock.lock_id
        self.save()
        return lock

    def get_active_lock(self) -> Optional[PlanLock]:
        """获取当前活动的锁定"""
        active_lock_id = self._data.get("active_lock_id")
        if not active_lock_id:
            return None

        for lock_data in self._data.get("plan_locks", []):
            if lock_data.get("lock_id") == active_lock_id and lock_data.get("is_active", False):
                return PlanLock.from_dict(lock_data)
        return None

    def get_lock(self, lock_id: str) -> Optional[PlanLock]:
        """获取指定的锁定记录"""
        for lock_data in self._data.get("plan_locks", []):
            if lock_data.get("lock_id") == lock_id:
                return PlanLock.from_dict(lock_data)
        return None

    def get_all_locks(self) -> List[PlanLock]:
        """获取所有锁定记录"""
        return [
            PlanLock.from_dict(l)
            for l in self._data.get("plan_locks", [])
        ]

    def release_active_lock(self, reason: Optional[str] = None) -> bool:
        """释放当前活动的锁定"""
        active_lock = self.get_active_lock()
        if not active_lock:
            return False

        for lock_data in self._data.get("plan_locks", []):
            if lock_data.get("lock_id") == active_lock.lock_id:
                lock_data["is_active"] = False
                lock_data["released_at"] = now_iso()
                lock_data["release_reason"] = reason or "手动释放"
                break

        self._data["active_lock_id"] = None
        self.save()
        return True

    def release_lock(self, lock_id: str, reason: Optional[str] = None) -> bool:
        """释放指定的锁定"""
        found = False
        for lock_data in self._data.get("plan_locks", []):
            if lock_data.get("lock_id") == lock_id:
                lock_data["is_active"] = False
                lock_data["released_at"] = now_iso()
                lock_data["release_reason"] = reason or "手动释放"
                found = True
                break

        if found and self._data.get("active_lock_id") == lock_id:
            self._data["active_lock_id"] = None

        if found:
            self.save()
        return found

    # ---- 锁定违规记录 ----

    def add_lock_violation(self, violation: LockViolation) -> None:
        """添加一条锁定违规记录"""
        if "lock_violations" not in self._data:
            self._data["lock_violations"] = []
        self._data["lock_violations"].append(violation.to_dict())
        self.save()

    def get_lock_violations(self) -> List[LockViolation]:
        """获取所有锁定违规记录"""
        return [
            LockViolation.from_dict(v)
            for v in self._data.get("lock_violations", [])
        ]

    def get_lock_violations_by_lock(self, lock_id: str) -> List[LockViolation]:
        """获取指定锁定的所有违规记录"""
        return [
            v for v in self.get_lock_violations()
            if v.lock_id == lock_id
        ]

    # ---- 预案差异记录 ----

    def save_plan_diff(self, diff_data: Dict[str, Any]) -> str:
        """保存预案差异记录

        返回差异记录的 ID
        """
        diff_id = generate_id()
        diff_record = {
            "diff_id": diff_id,
            "created_at": now_iso(),
            "diff_data": diff_data,
        }
        if "plan_diffs" not in self._data:
            self._data["plan_diffs"] = []
        self._data["plan_diffs"].append(diff_record)
        self.save()
        return diff_id

    def get_plan_diff(self, diff_id: str) -> Optional[Dict[str, Any]]:
        """获取指定的预案差异记录"""
        for diff_record in self._data.get("plan_diffs", []):
            if diff_record.get("diff_id") == diff_id:
                return diff_record.get("diff_data")
        return None

    def list_plan_diffs(self) -> List[Dict[str, Any]]:
        """列出所有预案差异记录（摘要）"""
        result = []
        for diff_record in self._data.get("plan_diffs", []):
            diff_data = diff_record.get("diff_data", {})
            result.append({
                "diff_id": diff_record.get("diff_id"),
                "created_at": diff_record.get("created_at"),
                "old_plan_id": diff_data.get("old_plan_id"),
                "new_plan_id": diff_data.get("new_plan_id"),
                "has_changes": diff_data.get("has_changes", False),
                "total_changed_moves": diff_data.get("total_changed_moves", 0),
            })
        return sorted(result, key=lambda x: x["created_at"], reverse=True)

    # ---- 完整数据导出 ----

    def get_full_state(self) -> Dict[str, Any]:
        """获取完整状态数据（用于 export）"""
        return dict(self._data)
