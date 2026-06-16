"""状态持久化存储 - JSON 文件存储"""
from __future__ import annotations

import json
import os
from typing import Dict, List, Optional, Any
from datetime import datetime

from .models import (
    ScannedFile, PlannedMove, ExecutedMove, UndoRecord,
    BatchSnapshot, ImportLog, PlanLock, LockViolation, ConfigSnapshot,
    SnapshotRemark, RemarkHistory, RemarkFieldChange,
    SignoffRecord, SignoffConflictState, SignoffValidationHistory,
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
            "remark_histories": [],
            "signoff_records": [],
            "signoff_conflicts": [],
            "validation_history": [],
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

    def list_snapshots_with_remark(self) -> List[Dict[str, Any]]:
        """列出所有快照（含备注信息）"""
        result = []
        for sid, sdata in self._data.get("snapshots", {}).items():
            remark = sdata.get("remark", {})
            result.append({
                "snapshot_id": sid,
                "created_at": sdata.get("created_at", ""),
                "plan_id": sdata.get("plan_id", ""),
                "move_count": len(sdata.get("moves", [])),
                "has_conflicts": sdata.get("has_conflicts", False),
                "imported": sdata.get("imported", False),
                "import_source": sdata.get("import_source"),
                "remark": remark.get("remark", ""),
                "tags": remark.get("tags", []),
                "handler": remark.get("handler", ""),
                "notes": remark.get("notes", ""),
                "remark_updated_at": remark.get("updated_at", ""),
                "remark_updated_by": remark.get("updated_by", ""),
            })
        return sorted(result, key=lambda x: x["created_at"], reverse=True)

    def update_snapshot_remark(
        self,
        snapshot_id: str,
        new_remark: SnapshotRemark,
        changed_by: str = "cli",
        change_source: str = "cli",
        allow_overwrite: bool = False,
    ) -> Tuple[bool, Optional[RemarkHistory], List[str]]:
        """
        更新快照备注

        返回: (是否成功, 历史记录, 错误信息列表)
        """
        from .workflow import diff_remarks

        snapshot = self.get_snapshot(snapshot_id)
        if not snapshot:
            return False, None, [f"快照不存在: {snapshot_id}"]

        old_remark = snapshot.remark

        state_file_dir = os.path.dirname(os.path.abspath(self.state_file)) or "."
        if not os.access(state_file_dir, os.W_OK):
            return False, None, [f"状态文件目录无写权限: {state_file_dir}"]

        changed_fields = diff_remarks(old_remark, new_remark)

        conflicts = []
        for fc in changed_fields:
            if fc.field_name == "remark" and old_remark.remark:
                conflicts.append(f"备注内容冲突: 旧='{old_remark.remark[:50]}...' 新='{new_remark.remark[:50]}...'")
            elif fc.field_name == "handler" and old_remark.handler:
                conflicts.append(f"交接人冲突: 旧='{old_remark.handler}' 新='{new_remark.handler}'")
            elif fc.field_name == "notes" and old_remark.notes:
                conflicts.append(f"注意事项冲突: 旧内容长度={len(old_remark.notes)} 新内容长度={len(new_remark.notes)}")
            elif fc.field_name == "tags" and old_remark.tags:
                old_set = set(old_remark.tags)
                new_set = set(new_remark.tags)
                added = new_set - old_set
                removed = old_set - new_set
                if added or removed:
                    conflicts.append(f"标签冲突: 新增={sorted(added)} 删除={sorted(removed)}")

        conflict_detected = len(conflicts) > 0
        conflict_detail = "; ".join(conflicts) if conflicts else ""

        if conflict_detected and not allow_overwrite:
            history = RemarkHistory(
                history_id=generate_id(),
                snapshot_id=snapshot_id,
                old_remark=old_remark,
                new_remark=new_remark,
                changed_at=now_iso(),
                changed_by=changed_by,
                change_source=change_source,
                conflict_detected=True,
                conflict_detail=conflict_detail,
                forced=False,
                changed_fields=changed_fields,
            )
            self._add_remark_history(history)
            self.save()
            return False, history, conflicts

        snapshot.remark = new_remark
        self._data["snapshots"][snapshot_id] = snapshot.to_dict()

        history = RemarkHistory(
            history_id=generate_id(),
            snapshot_id=snapshot_id,
            old_remark=old_remark,
            new_remark=new_remark,
            changed_at=now_iso(),
            changed_by=changed_by,
            change_source=change_source,
            conflict_detected=conflict_detected,
            conflict_detail=conflict_detail,
            forced=conflict_detected and allow_overwrite,
            changed_fields=changed_fields,
        )
        self._add_remark_history(history)
        self.save()

        return True, history, []

    def _add_remark_history(self, history: RemarkHistory) -> None:
        """添加备注修改历史记录"""
        if "remark_histories" not in self._data:
            self._data["remark_histories"] = []
        self._data["remark_histories"].append(history.to_dict())

    def get_remark_history(self, snapshot_id: Optional[str] = None) -> List[RemarkHistory]:
        """获取备注修改历史，可按快照 ID 筛选"""
        histories = self._data.get("remark_histories", [])
        result = [RemarkHistory.from_dict(h) for h in histories]
        if snapshot_id:
            result = [h for h in result if h.snapshot_id == snapshot_id]
        return sorted(result, key=lambda h: h.changed_at, reverse=True)

    def get_remark_conflicts(self) -> List[RemarkHistory]:
        """获取所有备注冲突记录"""
        histories = self.get_remark_history()
        return [h for h in histories if h.conflict_detected]

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

    # ---- 签收记录 ----

    def add_signoff(self, signoff: SignoffRecord) -> None:
        """添加一条签收记录

        同时会将同一快照的其他签收记录标记为非活动（被新签收取代）
        """
        if "signoff_records" not in self._data:
            self._data["signoff_records"] = []

        for existing in self._data["signoff_records"]:
            if (existing.get("snapshot_id") == signoff.snapshot_id and
                existing.get("is_active", True) and
                existing.get("signoff_id") != signoff.signoff_id):
                existing["is_active"] = False
                existing["superseded_by"] = signoff.signoff_id
                existing["superseded_at"] = now_iso()

        self._data["signoff_records"].append(signoff.to_dict())
        self.save()

    def get_signoff(self, signoff_id: str) -> Optional[SignoffRecord]:
        """根据 ID 获取签收记录"""
        for s in self._data.get("signoff_records", []):
            if s.get("signoff_id") == signoff_id:
                return SignoffRecord.from_dict(s)
        return None

    def get_active_signoff(self, snapshot_id: str) -> Optional[SignoffRecord]:
        """获取指定快照当前活动的签收记录"""
        active_signoffs = []
        for s in self._data.get("signoff_records", []):
            if s.get("snapshot_id") == snapshot_id and s.get("is_active", True):
                active_signoffs.append(SignoffRecord.from_dict(s))

        if not active_signoffs:
            return None

        active_signoffs.sort(key=lambda x: x.signed_at, reverse=True)
        return active_signoffs[0]

    def get_signoffs_by_snapshot(self, snapshot_id: str) -> List[SignoffRecord]:
        """获取指定快照的所有签收记录（按时间倒序）"""
        result = []
        for s in self._data.get("signoff_records", []):
            if s.get("snapshot_id") == snapshot_id:
                result.append(SignoffRecord.from_dict(s))
        result.sort(key=lambda x: x.signed_at, reverse=True)
        return result

    def get_all_signoffs(self) -> List[SignoffRecord]:
        """获取所有签收记录（按时间倒序）"""
        result = []
        for s in self._data.get("signoff_records", []):
            result.append(SignoffRecord.from_dict(s))
        result.sort(key=lambda x: x.signed_at, reverse=True)
        return result

    def get_signoff_by_run(self, run_id: str) -> Optional[SignoffRecord]:
        """根据执行记录查找当时使用的签收记录"""
        run = self.get_run(run_id)
        if not run:
            return None

        signoff_id = run.get("signoff_id")
        if signoff_id:
            return self.get_signoff(signoff_id)

        snapshot_id = run.get("snapshot_id")
        if snapshot_id:
            return self.get_active_signoff(snapshot_id)

        return None

    def update_run_signoff(self, run_id: str, signoff_id: str, snapshot_id: Optional[str] = None) -> bool:
        """更新执行记录关联的签收记录 ID"""
        if run_id in self._data.get("runs", {}):
            self._data["runs"][run_id]["signoff_id"] = signoff_id
            if snapshot_id is not None:
                self._data["runs"][run_id]["snapshot_id"] = snapshot_id
            self.save()
            return True
        return False

    def list_snapshots_with_signoff(self) -> List[Dict[str, Any]]:
        """列出所有快照（含签收信息）"""
        snapshots = self.list_snapshots_with_remark()

        for s in snapshots:
            signoff = self.get_active_signoff(s["snapshot_id"])
            if signoff:
                status_map = {"signed": "已签收", "rejected": "已拒绝", "pending": "待处理"}
                s["signoff_status"] = status_map.get(signoff.status, signoff.status)
                s["signoff_status_raw"] = signoff.status
                s["signed_by"] = signoff.signed_by
                s["signed_at"] = signoff.signed_at
                s["signoff_deadline"] = signoff.deadline
                s["signoff_notes"] = signoff.notes
                s["signoff_id"] = signoff.signoff_id
                s["signoff_forced"] = signoff.forced
            else:
                s["signoff_status"] = "未签收"
                s["signoff_status_raw"] = "unsigned"
                s["signed_by"] = ""
                s["signed_at"] = ""
                s["signoff_deadline"] = ""
                s["signoff_notes"] = ""
                s["signoff_id"] = ""
                s["signoff_forced"] = False

        return snapshots

    def deactivate_all_signoffs(self, snapshot_id: str, reason: str = "") -> int:
        """将指定快照的所有签收标记为非活动

        返回被标记的记录数
        """
        count = 0
        for s in self._data.get("signoff_records", []):
            if s.get("snapshot_id") == snapshot_id and s.get("is_active", True):
                s["is_active"] = False
                s["superseded_at"] = now_iso()
                if reason:
                    s["conflict_detail"] = reason + "; " + s.get("conflict_detail", "")
                count += 1
        if count > 0:
            self.save()
        return count

    # ---- 签收冲突状态 ----

    def save_signoff_conflict(self, conflict: SignoffConflictState) -> None:
        """保存签收冲突状态（新冲突或更新已有冲突）"""
        if "signoff_conflicts" not in self._data:
            self._data["signoff_conflicts"] = []

        existing_idx = None
        for i, c in enumerate(self._data["signoff_conflicts"]):
            if c.get("conflict_id") == conflict.conflict_id:
                existing_idx = i
                break

        if existing_idx is not None:
            self._data["signoff_conflicts"][existing_idx] = conflict.to_dict()
        else:
            self._data["signoff_conflicts"].append(conflict.to_dict())

        self.save()

    def get_signoff_conflict(self, conflict_id: str) -> Optional[SignoffConflictState]:
        """根据ID获取签收冲突状态"""
        for c in self._data.get("signoff_conflicts", []):
            if c.get("conflict_id") == conflict_id:
                return SignoffConflictState.from_dict(c)
        return None

    def get_pending_conflict_by_snapshot(self, snapshot_id: str) -> Optional[SignoffConflictState]:
        """获取指定快照当前未解决（pending）的签收冲突"""
        for c in self._data.get("signoff_conflicts", []):
            if (c.get("snapshot_id") == snapshot_id and
                    c.get("status") == "pending"):
                return SignoffConflictState.from_dict(c)
        return None

    def list_signoff_conflicts(self, snapshot_id: Optional[str] = None,
                               only_pending: bool = False) -> List[SignoffConflictState]:
        """列出签收冲突状态

        参数:
            snapshot_id: 按快照ID筛选（可选）
            only_pending: 只列出未解决的冲突
        """
        result = []
        for c in self._data.get("signoff_conflicts", []):
            if snapshot_id and c.get("snapshot_id") != snapshot_id:
                continue
            if only_pending and c.get("status") != "pending":
                continue
            result.append(SignoffConflictState.from_dict(c))
        return sorted(result, key=lambda x: x.detected_at, reverse=True)

    def resolve_signoff_conflict(
        self,
        conflict_id: str,
        resolution: str,  # "resolved_keep_local" | "resolved_keep_imported" | "resolved_new"
        resolved_by: str,
        resolution_note: str = "",
        new_signoff_id: Optional[str] = None,
    ) -> Tuple[bool, Optional[SignoffConflictState], List[str]]:
        """解决签收冲突

        返回: (是否成功, 冲突状态对象, 错误信息列表)
        """
        errors: List[str] = []

        if resolution not in ["resolved_keep_local", "resolved_keep_imported", "resolved_new"]:
            errors.append(f"无效的处理方式: {resolution}")
            return False, None, errors

        conflict = self.get_signoff_conflict(conflict_id)
        if not conflict:
            errors.append(f"签收冲突不存在: {conflict_id}")
            return False, None, errors

        if conflict.is_resolved:
            errors.append(f"签收冲突已处理（状态: {conflict.status}），无需重复处理")
            return False, conflict, errors

        snapshot = self.get_snapshot(conflict.snapshot_id)
        if not snapshot:
            errors.append(f"快照不存在: {conflict.snapshot_id}")
            return False, conflict, errors

        if resolution == "resolved_keep_local":
            self.deactivate_all_signoffs(conflict.snapshot_id, reason=f"冲突处理: 保留本地签收")
            local_signoff = self.get_signoff(conflict.local_signoff_id)
            if local_signoff:
                local_signoff.is_active = True
                local_signoff.superseded_by = None
                local_signoff.superseded_at = None
                for s in self._data.get("signoff_records", []):
                    if s.get("signoff_id") == conflict.local_signoff_id:
                        s["is_active"] = True
                        s["superseded_by"] = None
                        s["superseded_at"] = None
                        s["forced"] = False
                        s["conflict_detail"] = ""
                        break
            else:
                errors.append(f"本地签收记录不存在: {conflict.local_signoff_id}")
                return False, conflict, errors

        elif resolution == "resolved_keep_imported":
            self.deactivate_all_signoffs(conflict.snapshot_id, reason=f"冲突处理: 保留导入签收")
            imported_signoff = self.get_signoff(conflict.imported_signoff_id)
            if imported_signoff:
                imported_signoff.is_active = True
                imported_signoff.superseded_by = None
                imported_signoff.superseded_at = None
                for s in self._data.get("signoff_records", []):
                    if s.get("signoff_id") == conflict.imported_signoff_id:
                        s["is_active"] = True
                        s["superseded_by"] = None
                        s["superseded_at"] = None
                        s["forced"] = False
                        s["conflict_detail"] = ""
                        break
            else:
                errors.append(f"导入签收记录不存在: {conflict.imported_signoff_id}")
                return False, conflict, errors

        elif resolution == "resolved_new":
            if not new_signoff_id:
                errors.append("新建签收方式需要提供 new_signoff_id")
                return False, conflict, errors
            new_signoff = self.get_signoff(new_signoff_id)
            if not new_signoff:
                errors.append(f"新签收记录不存在: {new_signoff_id}")
                return False, conflict, errors
            self.deactivate_all_signoffs(conflict.snapshot_id, reason=f"冲突处理: 新建签收替代")
            for s in self._data.get("signoff_records", []):
                if s.get("signoff_id") == new_signoff_id:
                    s["is_active"] = True
                    s["superseded_by"] = None
                    s["superseded_at"] = None
                    s["forced"] = False
                    s["conflict_detail"] = ""
                    break

        conflict.status = resolution
        conflict.resolved_at = now_iso()
        conflict.resolved_by = resolved_by
        conflict.resolution_note = resolution_note
        conflict.new_signoff_id = new_signoff_id

        for c in self._data.get("signoff_conflicts", []):
            if c.get("conflict_id") == conflict_id:
                c["status"] = resolution
                c["resolved_at"] = conflict.resolved_at
                c["resolved_by"] = resolved_by
                c["resolution_note"] = resolution_note
                c["new_signoff_id"] = new_signoff_id
                break

        self.save()
        return True, conflict, errors

    def get_last_import_source(self, snapshot_id: str) -> Optional[str]:
        """获取指定快照最近一次的导入来源"""
        import_logs = sorted(
            [l for l in self.get_import_logs() if l.snapshot_id == snapshot_id],
            key=lambda x: x.timestamp
        )
        if import_logs:
            return import_logs[-1].source_file
        snapshot = self.get_snapshot(snapshot_id)
        if snapshot and snapshot.import_source:
            return snapshot.import_source
        signoffs = self.get_signoffs_by_snapshot(snapshot_id)
        imported = [s for s in signoffs if s.import_source]
        if imported:
            return imported[-1].import_source
        return None

    # ---- 签收校验历史 ----

    def add_validation_history(self, record: SignoffValidationHistory) -> None:
        """添加签收校验历史记录"""
        self._data.setdefault("validation_history", []).append(record.to_dict())
        self.save()

    def get_validation_history(self, snapshot_id: Optional[str] = None, limit: int = 20) -> List[SignoffValidationHistory]:
        """获取校验历史，可按快照过滤，按时间倒序排列"""
        records = [
            SignoffValidationHistory.from_dict(r)
            for r in self._data.get("validation_history", [])
        ]
        if snapshot_id:
            records = [r for r in records if r.snapshot_id == snapshot_id]
        records.sort(key=lambda r: r.triggered_at, reverse=True)
        return records[:limit]

    def get_latest_validation(self, snapshot_id: Optional[str] = None) -> Optional[SignoffValidationHistory]:
        """获取最近一次校验记录"""
        records = self.get_validation_history(snapshot_id=snapshot_id, limit=1)
        return records[0] if records else None

    def update_validation_resolution(
        self,
        validation_id: str,
        resolved_by: str,
        resolution_note: str,
        resolution_command: str,
    ) -> Optional[SignoffValidationHistory]:
        """标记校验记录为已解决"""
        for r in self._data.get("validation_history", []):
            if r.get("validation_id") == validation_id:
                r["resolved_at"] = now_iso()
                r["resolved_by"] = resolved_by
                r["resolution_note"] = resolution_note
                r["resolution_command"] = resolution_command
                self.save()
                return SignoffValidationHistory.from_dict(r)
        return None

    def invalidate_validation_for_snapshot(self, snapshot_id: str) -> None:
        """快照状态变化时，标记相关未解决的校验记录为已过期（自动标记解决）"""
        for r in self._data.get("validation_history", []):
            if (
                r.get("snapshot_id") == snapshot_id
                and r.get("status") == "blocked"
                and not r.get("resolved_at")
            ):
                r["resolved_at"] = now_iso()
                r["resolved_by"] = "system"
                r["resolution_note"] = "快照状态变化，原校验结果已失效"
                r["resolution_command"] = "state_refresh"
        self.save()
