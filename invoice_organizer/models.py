"""配置加载和数据模型定义"""
from __future__ import annotations

import os
from dataclasses import dataclass, field, asdict
from typing import List, Optional, Dict, Any
from datetime import datetime
import yaml
import uuid


def generate_id() -> str:
    """生成唯一 ID"""
    return uuid.uuid4().hex[:12]


def now_iso() -> str:
    """当前时间 ISO 格式"""
    return datetime.now().isoformat()


@dataclass
class Rule:
    """归档规则：匹配文件模式 -> 目标目录"""
    name: str
    pattern: str
    target: str
    description: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class Config:
    """应用配置"""
    source_dir: str
    dest_dir: str
    rules: List[Rule]
    state_file: str = ".invoice_organizer_state.json"
    recursive: bool = True
    file_extensions: Optional[List[str]] = None

    def to_dict(self) -> Dict[str, Any]:
        result = {
            "source_dir": self.source_dir,
            "dest_dir": self.dest_dir,
            "rules": [r.to_dict() for r in self.rules],
            "state_file": self.state_file,
            "recursive": self.recursive,
        }
        if self.file_extensions:
            result["file_extensions"] = self.file_extensions
        return result


@dataclass
class ScannedFile:
    """扫描到的文件信息"""
    id: str
    source_path: str
    filename: str
    size: int
    mtime: float
    matched_rule: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class PlannedMove:
    """预案中的一条移动计划"""
    id: str
    source_path: str
    target_path: str
    filename: str
    matched_rule: str
    conflict_type: Optional[str] = None  # "target_exists" | "duplicate_target" | "rule_conflict"
    conflict_detail: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class PlanSummary:
    """预案摘要信息"""
    total_files: int = 0
    matched_files: int = 0
    unmatched_files: int = 0
    new_target_dirs: List[str] = field(default_factory=list)
    rules_with_same_target: Dict[str, List[str]] = field(default_factory=dict)
    files_per_rule: Dict[str, int] = field(default_factory=dict)
    files_per_target_dir: Dict[str, int] = field(default_factory=dict)
    conflict_count: int = 0
    conflict_details: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class ExecutedMove:
    """已执行的移动记录"""
    id: str
    run_id: str
    source_path: str
    target_path: str
    filename: str
    matched_rule: str
    status: str  # "moved" | "skipped_conflict" | "skipped_manual" | "failed" | "unmatched"
    timestamp: str
    error_message: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class UndoRecord:
    """撤销记录"""
    run_id: str
    undo_timestamp: str
    moves_restored: int
    status: str  # "completed" | "partial" | "failed"

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class ConfigSnapshot:
    """配置快照 - plan 时刻的配置完整记录"""
    source_dir: str
    dest_dir: str
    rules: List[Dict[str, Any]]
    state_file: str
    recursive: bool
    file_extensions: Optional[List[str]] = None
    config_path: Optional[str] = None
    config_mtime: Optional[float] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ConfigSnapshot":
        return cls(
            source_dir=data["source_dir"],
            dest_dir=data["dest_dir"],
            rules=data["rules"],
            state_file=data["state_file"],
            recursive=data["recursive"],
            file_extensions=data.get("file_extensions"),
            config_path=data.get("config_path"),
            config_mtime=data.get("config_mtime"),
        )


@dataclass
class UnmatchedFileInfo:
    """未命中规则的文件信息"""
    filename: str
    source_path: str
    size: int
    mtime: float
    reason: str  # "no_rule_match" | "extension_filtered"

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "UnmatchedFileInfo":
        return cls(
            filename=data["filename"],
            source_path=data["source_path"],
            size=data["size"],
            mtime=data["mtime"],
            reason=data["reason"],
        )


@dataclass
class NewDirInfo:
    """预期新建的目录信息"""
    path: str
    rule_names: List[str]
    file_count: int

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "NewDirInfo":
        return cls(
            path=data["path"],
            rule_names=data["rule_names"],
            file_count=data["file_count"],
        )


@dataclass
class SnapshotRemark:
    """快照备注信息

    用于记录批次备注、标签、交接人和注意事项。
    支持多次修改，保留修改历史用于冲突检测。
    """
    remark: str = ""
    tags: List[str] = field(default_factory=list)
    handler: str = ""
    notes: str = ""
    updated_at: str = ""
    updated_by: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Optional[Dict[str, Any]]) -> "SnapshotRemark":
        if not data:
            return cls()
        return cls(
            remark=data.get("remark", ""),
            tags=list(data.get("tags", [])),
            handler=data.get("handler", ""),
            notes=data.get("notes", ""),
            updated_at=data.get("updated_at", ""),
            updated_by=data.get("updated_by", ""),
        )

    def is_empty(self) -> bool:
        return not self.remark and not self.tags and not self.handler and not self.notes


@dataclass
class RemarkFieldChange:
    """备注单个字段的变化记录"""
    field_name: str
    old_value: Any
    new_value: Any

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "RemarkFieldChange":
        return cls(
            field_name=data["field_name"],
            old_value=data.get("old_value"),
            new_value=data.get("new_value"),
        )


@dataclass
class RemarkHistory:
    """备注修改历史记录

    用于冲突检测和审计追踪。
    """
    history_id: str
    snapshot_id: str
    old_remark: SnapshotRemark
    new_remark: SnapshotRemark
    changed_at: str
    changed_by: str
    change_source: str  # "cli" | "import" | "api"
    conflict_detected: bool = False
    conflict_detail: str = ""
    forced: bool = False
    changed_fields: List[RemarkFieldChange] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        result = asdict(self)
        result["old_remark"] = self.old_remark.to_dict()
        result["new_remark"] = self.new_remark.to_dict()
        result["changed_fields"] = [fc.to_dict() for fc in self.changed_fields]
        return result

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "RemarkHistory":
        changed_fields_data = data.get("changed_fields", [])
        changed_fields = [RemarkFieldChange.from_dict(fc) for fc in changed_fields_data]
        return cls(
            history_id=data["history_id"],
            snapshot_id=data["snapshot_id"],
            old_remark=SnapshotRemark.from_dict(data.get("old_remark")),
            new_remark=SnapshotRemark.from_dict(data.get("new_remark")),
            changed_at=data["changed_at"],
            changed_by=data.get("changed_by", ""),
            change_source=data.get("change_source", "cli"),
            conflict_detected=data.get("conflict_detected", False),
            conflict_detail=data.get("conflict_detail", ""),
            forced=data.get("forced", False),
            changed_fields=changed_fields,
        )


@dataclass
class RemarkValidationResult:
    """备注验证结果"""
    valid: bool
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)

    @property
    def has_errors(self) -> bool:
        return len(self.errors) > 0

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class BatchSnapshot:
    """批次快照 - plan 产出的完整固化记录

    每次 plan 生成的所有信息都固化在快照中，
    后续 apply / export / undo 都基于此快照执行。
    """
    snapshot_id: str
    created_at: str
    plan_id: str
    config_snapshot: ConfigSnapshot
    scanned_files: List[Dict[str, Any]]
    moves: List[Dict[str, Any]]
    unmatched_files: List[UnmatchedFileInfo]
    new_target_dirs: List[NewDirInfo]
    has_conflicts: bool
    summary: Dict[str, Any]
    imported: bool = False
    import_source: Optional[str] = None
    remark: SnapshotRemark = field(default_factory=SnapshotRemark)
    signoffs: List["SignoffRecord"] = field(default_factory=list)
    signoff_conflicts: List["SignoffConflictState"] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        result = asdict(self)
        result["config_snapshot"] = self.config_snapshot.to_dict()
        result["unmatched_files"] = [u.to_dict() for u in self.unmatched_files]
        result["new_target_dirs"] = [n.to_dict() for n in self.new_target_dirs]
        result["remark"] = self.remark.to_dict()
        result["signoffs"] = [s.to_dict() for s in self.signoffs]
        result["signoff_conflicts"] = [sc.to_dict() for sc in self.signoff_conflicts]
        return result

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "BatchSnapshot":
        signoffs_data = data.get("signoffs", [])
        signoffs = [SignoffRecord.from_dict(s) for s in signoffs_data] if signoffs_data else []
        conflicts_data = data.get("signoff_conflicts", [])
        signoff_conflicts = [SignoffConflictState.from_dict(sc) for sc in conflicts_data] if conflicts_data else []
        return cls(
            snapshot_id=data["snapshot_id"],
            created_at=data["created_at"],
            plan_id=data["plan_id"],
            config_snapshot=ConfigSnapshot.from_dict(data["config_snapshot"]),
            scanned_files=data["scanned_files"],
            moves=data["moves"],
            unmatched_files=[UnmatchedFileInfo.from_dict(u) for u in data["unmatched_files"]],
            new_target_dirs=[NewDirInfo.from_dict(n) for n in data["new_target_dirs"]],
            has_conflicts=data["has_conflicts"],
            summary=data["summary"],
            imported=data.get("imported", False),
            import_source=data.get("import_source"),
            remark=SnapshotRemark.from_dict(data.get("remark")),
            signoffs=signoffs,
            signoff_conflicts=signoff_conflicts,
        )


@dataclass
class ConfigDiffResult:
    """配置差异检测结果"""
    has_diff: bool
    added_rules: List[str]
    removed_rules: List[str]
    modified_rules: List[str]
    source_dir_changed: bool
    dest_dir_changed: bool
    extensions_changed: bool
    recursive_changed: bool

    @property
    def has_rule_changes(self) -> bool:
        return len(self.added_rules) > 0 or len(self.removed_rules) > 0 or len(self.modified_rules) > 0

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class ImportValidationResult:
    """快照导入验证结果"""
    valid: bool
    errors: List[str]
    warnings: List[str]
    conflicting_files: List[str]
    missing_source_files: List[str]
    unwritable_dirs: List[str]

    @property
    def has_errors(self) -> bool:
        return len(self.errors) > 0

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class ImportLog:
    """导入日志记录 - 成功和失败的导入尝试都要记

    用于跨重启回查：为什么某次导入成功/失败了。
    """
    import_id: str
    timestamp: str
    status: str  # "success" | "failed" | "forced" | "cancelled"
    source_file: str
    snapshot_id: Optional[str] = None
    plan_id: Optional[str] = None
    move_count: Optional[int] = None
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    config_diff: Optional[Dict[str, Any]] = None
    forced: bool = False
    remark_conflict_detail: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ImportLog":
        return cls(
            import_id=data["import_id"],
            timestamp=data["timestamp"],
            status=data["status"],
            source_file=data["source_file"],
            snapshot_id=data.get("snapshot_id"),
            plan_id=data.get("plan_id"),
            move_count=data.get("move_count"),
            errors=data.get("errors", []),
            warnings=data.get("warnings", []),
            config_diff=data.get("config_diff"),
            forced=data.get("forced", False),
            remark_conflict_detail=data.get("remark_conflict_detail", ""),
        )


@dataclass
class FileMoveDiff:
    """单个文件的移动计划差异"""
    filename: str
    source_path: str
    change_type: str  # "added" | "removed" | "target_changed" | "rule_changed" | "conflict_changed" | "unchanged"
    old_target_path: Optional[str] = None
    new_target_path: Optional[str] = None
    old_matched_rule: Optional[str] = None
    new_matched_rule: Optional[str] = None
    old_conflict_type: Optional[str] = None
    new_conflict_type: Optional[str] = None
    old_conflict_detail: Optional[str] = None
    new_conflict_detail: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "FileMoveDiff":
        return cls(**data)


@dataclass
class UnmatchedFileDiff:
    """未命中文件的差异"""
    filename: str
    source_path: str
    change_type: str  # "added" | "removed" | "reason_changed" | "unchanged"
    old_reason: Optional[str] = None
    new_reason: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "UnmatchedFileDiff":
        return cls(**data)


@dataclass
class PlanDiffResult:
    """两版预案的完整差异对比结果"""
    old_plan_id: str
    new_plan_id: str
    old_snapshot_id: Optional[str] = None
    new_snapshot_id: Optional[str] = None
    diff_timestamp: str = field(default_factory=now_iso)

    old_move_count: int = 0
    new_move_count: int = 0
    old_unmatched_count: int = 0
    new_unmatched_count: int = 0

    added_moves: List[FileMoveDiff] = field(default_factory=list)
    removed_moves: List[FileMoveDiff] = field(default_factory=list)
    target_changed: List[FileMoveDiff] = field(default_factory=list)
    rule_changed: List[FileMoveDiff] = field(default_factory=list)
    conflict_changed: List[FileMoveDiff] = field(default_factory=list)
    unchanged_moves: List[FileMoveDiff] = field(default_factory=list)

    added_unmatched: List[UnmatchedFileDiff] = field(default_factory=list)
    removed_unmatched: List[UnmatchedFileDiff] = field(default_factory=list)
    unchanged_unmatched: List[UnmatchedFileDiff] = field(default_factory=list)

    added_rules: List[str] = field(default_factory=list)
    removed_rules: List[str] = field(default_factory=list)
    modified_rules: List[str] = field(default_factory=list)

    config_diff: Optional[Dict[str, Any]] = None

    @property
    def has_changes(self) -> bool:
        return (
            len(self.added_moves) > 0 or
            len(self.removed_moves) > 0 or
            len(self.target_changed) > 0 or
            len(self.rule_changed) > 0 or
            len(self.conflict_changed) > 0 or
            len(self.added_unmatched) > 0 or
            len(self.removed_unmatched) > 0 or
            len(self.added_rules) > 0 or
            len(self.removed_rules) > 0 or
            len(self.modified_rules) > 0
        )

    @property
    def total_changed_moves(self) -> int:
        return (
            len(self.added_moves) +
            len(self.removed_moves) +
            len(self.target_changed) +
            len(self.rule_changed) +
            len(self.conflict_changed)
        )

    def to_dict(self) -> Dict[str, Any]:
        result = {
            "old_plan_id": self.old_plan_id,
            "new_plan_id": self.new_plan_id,
            "old_snapshot_id": self.old_snapshot_id,
            "new_snapshot_id": self.new_snapshot_id,
            "diff_timestamp": self.diff_timestamp,
            "old_move_count": self.old_move_count,
            "new_move_count": self.new_move_count,
            "old_unmatched_count": self.old_unmatched_count,
            "new_unmatched_count": self.new_unmatched_count,
            "added_moves": [m.to_dict() for m in self.added_moves],
            "removed_moves": [m.to_dict() for m in self.removed_moves],
            "target_changed": [m.to_dict() for m in self.target_changed],
            "rule_changed": [m.to_dict() for m in self.rule_changed],
            "conflict_changed": [m.to_dict() for m in self.conflict_changed],
            "unchanged_moves": [m.to_dict() for m in self.unchanged_moves],
            "added_unmatched": [u.to_dict() for u in self.added_unmatched],
            "removed_unmatched": [u.to_dict() for u in self.removed_unmatched],
            "unchanged_unmatched": [u.to_dict() for u in self.unchanged_unmatched],
            "added_rules": self.added_rules,
            "removed_rules": self.removed_rules,
            "modified_rules": self.modified_rules,
            "has_changes": self.has_changes,
            "total_changed_moves": self.total_changed_moves,
        }
        if self.config_diff:
            result["config_diff"] = self.config_diff
        return result

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "PlanDiffResult":
        return cls(
            old_plan_id=data["old_plan_id"],
            new_plan_id=data["new_plan_id"],
            old_snapshot_id=data.get("old_snapshot_id"),
            new_snapshot_id=data.get("new_snapshot_id"),
            diff_timestamp=data.get("diff_timestamp", now_iso()),
            old_move_count=data.get("old_move_count", 0),
            new_move_count=data.get("new_move_count", 0),
            old_unmatched_count=data.get("old_unmatched_count", 0),
            new_unmatched_count=data.get("new_unmatched_count", 0),
            added_moves=[FileMoveDiff.from_dict(m) for m in data.get("added_moves", [])],
            removed_moves=[FileMoveDiff.from_dict(m) for m in data.get("removed_moves", [])],
            target_changed=[FileMoveDiff.from_dict(m) for m in data.get("target_changed", [])],
            rule_changed=[FileMoveDiff.from_dict(m) for m in data.get("rule_changed", [])],
            conflict_changed=[FileMoveDiff.from_dict(m) for m in data.get("conflict_changed", [])],
            unchanged_moves=[FileMoveDiff.from_dict(m) for m in data.get("unchanged_moves", [])],
            added_unmatched=[UnmatchedFileDiff.from_dict(u) for u in data.get("added_unmatched", [])],
            removed_unmatched=[UnmatchedFileDiff.from_dict(u) for u in data.get("removed_unmatched", [])],
            unchanged_unmatched=[UnmatchedFileDiff.from_dict(u) for u in data.get("unchanged_unmatched", [])],
            added_rules=data.get("added_rules", []),
            removed_rules=data.get("removed_rules", []),
            modified_rules=data.get("modified_rules", []),
            config_diff=data.get("config_diff"),
        )


@dataclass
class PlanLock:
    """预案版本锁定记录

    锁定后 apply 只能执行被锁定的版本，
    如果配置变更或重新 plan 导致版本不一致，执行将被拦截。
    """
    lock_id: str
    snapshot_id: str
    plan_id: str
    locked_at: str
    locked_by: str = "cli"
    reason: Optional[str] = None
    is_active: bool = True
    released_at: Optional[str] = None
    release_reason: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "PlanLock":
        return cls(
            lock_id=data["lock_id"],
            snapshot_id=data["snapshot_id"],
            plan_id=data["plan_id"],
            locked_at=data["locked_at"],
            locked_by=data.get("locked_by", "cli"),
            reason=data.get("reason"),
            is_active=data.get("is_active", True),
            released_at=data.get("released_at"),
            release_reason=data.get("release_reason"),
        )


@dataclass
class LockViolation:
    """锁定被违反的记录 - 用于审计和拦截提示"""
    violation_id: str
    lock_id: str
    snapshot_id: str
    plan_id: str
    violation_timestamp: str
    violation_type: str  # "replan_detected" | "config_changed" | "wrong_snapshot"
    violation_detail: str
    blocked: bool = True  # 是否被拦截

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "LockViolation":
        return cls(
            violation_id=data["violation_id"],
            lock_id=data["lock_id"],
            snapshot_id=data["snapshot_id"],
            plan_id=data["plan_id"],
            violation_timestamp=data["violation_timestamp"],
            violation_type=data["violation_type"],
            violation_detail=data["violation_detail"],
            blocked=data.get("blocked", True),
        )


MAX_SIGNOFF_NOTES_LENGTH: int = 500
MAX_SIGNOFF_BY_LENGTH: int = 50


@dataclass
class SignoffRecord:
    """签收记录

    用于记录预案/快照的签收状态、签收人、时间、截止时间和补充说明。
    支持多次签收，保留签收历史用于冲突检测和审计。
    """
    signoff_id: str
    snapshot_id: str
    plan_id: str
    status: str  # "signed" | "rejected" | "pending"
    signed_by: str
    signed_at: str
    deadline: str = ""
    notes: str = ""
    config_snapshot: Optional[Dict[str, Any]] = None
    created_at: str = ""
    created_by: str = "cli"
    import_source: Optional[str] = None
    conflict_detail: str = ""
    forced: bool = False
    is_active: bool = True
    superseded_by: Optional[str] = None
    superseded_at: Optional[str] = None
    conflict_id: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "SignoffRecord":
        return cls(
            signoff_id=data["signoff_id"],
            snapshot_id=data["snapshot_id"],
            plan_id=data["plan_id"],
            status=data["status"],
            signed_by=data["signed_by"],
            signed_at=data["signed_at"],
            deadline=data.get("deadline", ""),
            notes=data.get("notes", ""),
            config_snapshot=data.get("config_snapshot"),
            created_at=data.get("created_at", ""),
            created_by=data.get("created_by", "cli"),
            import_source=data.get("import_source"),
            conflict_detail=data.get("conflict_detail", ""),
            forced=data.get("forced", False),
            is_active=data.get("is_active", True),
            superseded_by=data.get("superseded_by"),
            superseded_at=data.get("superseded_at"),
            conflict_id=data.get("conflict_id"),
        )


@dataclass
class SignoffValidationResult:
    """签收校验结果

    用于 apply 执行前的签收状态校验。
    """
    valid: bool
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    is_expired: bool = False
    config_mismatch: bool = False
    snapshot_replaced: bool = False
    conflicting_signoffs: List[str] = field(default_factory=list)
    active_signoff: Optional[SignoffRecord] = None
    unresolved_conflict: Optional[SignoffConflictState] = None

    @property
    def has_errors(self) -> bool:
        return len(self.errors) > 0

    def to_dict(self) -> Dict[str, Any]:
        result = asdict(self)
        if self.active_signoff:
            result["active_signoff"] = self.active_signoff.to_dict()
        if self.unresolved_conflict:
            result["unresolved_conflict"] = self.unresolved_conflict.to_dict()
        return result


@dataclass
class SignoffFieldChange:
    """签收单个字段的变化记录"""
    field_name: str
    old_value: Any
    new_value: Any

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "SignoffFieldChange":
        return cls(
            field_name=data["field_name"],
            old_value=data.get("old_value"),
            new_value=data.get("new_value"),
        )


@dataclass
class SignoffConflictState:
    """签收冲突状态

    当导入快照的签收记录与本地活跃签收不一致时，
    创建此冲突状态记录，用于持久化冲突信息、跟踪处理进度。

    冲突处理流程：
    1. detected (pending) -> 2. 用户选择处理方式 -> 3. resolved_*
    """
    conflict_id: str
    snapshot_id: str
    plan_id: str
    status: str  # "pending" | "resolved_keep_local" | "resolved_keep_imported" | "resolved_new"
    local_signoff_id: str
    imported_signoff_id: str
    detected_at: str
    import_source: str = ""
    diff_fields: List[str] = field(default_factory=list)
    conflict_summary: str = ""
    resolved_at: Optional[str] = None
    resolved_by: str = ""
    resolution_note: str = ""
    new_signoff_id: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "SignoffConflictState":
        return cls(
            conflict_id=data["conflict_id"],
            snapshot_id=data["snapshot_id"],
            plan_id=data["plan_id"],
            status=data["status"],
            local_signoff_id=data["local_signoff_id"],
            imported_signoff_id=data["imported_signoff_id"],
            detected_at=data["detected_at"],
            import_source=data.get("import_source", ""),
            diff_fields=list(data.get("diff_fields", [])),
            conflict_summary=data.get("conflict_summary", ""),
            resolved_at=data.get("resolved_at"),
            resolved_by=data.get("resolved_by", ""),
            resolution_note=data.get("resolution_note", ""),
            new_signoff_id=data.get("new_signoff_id"),
        )

    @property
    def is_resolved(self) -> bool:
        return self.status != "pending"


@dataclass
class SignoffConflictDiff:
    """签收冲突的字段级差异详情"""
    conflict_id: str
    local_signoff: SignoffRecord
    imported_signoff: SignoffRecord
    field_changes: List[SignoffFieldChange]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "conflict_id": self.conflict_id,
            "local_signoff": self.local_signoff.to_dict(),
            "imported_signoff": self.imported_signoff.to_dict(),
            "field_changes": [fc.to_dict() for fc in self.field_changes],
        }


@dataclass
class SignoffValidationHistory:
    """签收校验历史记录

    用于持久化每次 check-signoff、apply --dry-run 和正式 apply 的校验结果，
    支持重启后回看和导出复核。

    阻塞类型：
    - signoff_expired: 签收已过期
    - config_mismatch: 当前配置与签收时不一致
    - unresolved_signoff_conflict: 存在未解决的签收冲突
    - lock_mismatch: 锁定快照与执行快照不一致
    - no_signoff: 未签收
    - not_signed: 签收状态不是 signed
    - conflicting_signoffs: 存在多条冲突的活动签收
    """
    validation_id: str
    snapshot_id: str
    plan_id: str
    triggered_by: str  # "check-signoff" | "apply-dry-run" | "apply"
    triggered_at: str
    status: str  # "passed" | "blocked"
    block_types: List[str] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    is_expired: bool = False
    config_mismatch: bool = False
    snapshot_replaced: bool = False
    has_unresolved_conflict: bool = False
    has_lock_mismatch: bool = False
    active_signoff_id: Optional[str] = None
    conflict_id: Optional[str] = None
    lock_id: Optional[str] = None
    resolved_at: Optional[str] = None
    resolved_by: Optional[str] = None
    resolution_note: str = ""
    resolution_command: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "SignoffValidationHistory":
        return cls(
            validation_id=data["validation_id"],
            snapshot_id=data["snapshot_id"],
            plan_id=data["plan_id"],
            triggered_by=data["triggered_by"],
            triggered_at=data["triggered_at"],
            status=data["status"],
            block_types=list(data.get("block_types", [])),
            errors=list(data.get("errors", [])),
            warnings=list(data.get("warnings", [])),
            is_expired=data.get("is_expired", False),
            config_mismatch=data.get("config_mismatch", False),
            snapshot_replaced=data.get("snapshot_replaced", False),
            has_unresolved_conflict=data.get("has_unresolved_conflict", False),
            has_lock_mismatch=data.get("has_lock_mismatch", False),
            active_signoff_id=data.get("active_signoff_id"),
            conflict_id=data.get("conflict_id"),
            lock_id=data.get("lock_id"),
            resolved_at=data.get("resolved_at"),
            resolved_by=data.get("resolved_by"),
            resolution_note=data.get("resolution_note", ""),
            resolution_command=data.get("resolution_command", ""),
        )

    @property
    def is_resolved(self) -> bool:
        return self.resolved_at is not None


BUNDLE_VERSION: str = "1.0"
BUNDLE_REQUIRED_FIELDS: List[str] = [
    "bundle_id", "bundle_version", "created_at",
    "run_id", "plan_id", "snapshot_id",
    "summary", "snapshot", "run_details",
]


@dataclass
class BundleSummary:
    """执行批次归档包摘要"""
    total_moves: int = 0
    success_count: int = 0
    skipped_conflict_count: int = 0
    skipped_manual_count: int = 0
    failed_count: int = 0
    dry_run: bool = False
    is_undone: bool = False
    conflict_details: List[str] = field(default_factory=list)
    manual_skip_reasons: List[str] = field(default_factory=list)
    has_signoff: bool = False
    signoff_status: str = ""
    signed_by: str = ""
    signoff_id: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "BundleSummary":
        return cls(
            total_moves=data.get("total_moves", 0),
            success_count=data.get("success_count", 0),
            skipped_conflict_count=data.get("skipped_conflict_count", 0),
            skipped_manual_count=data.get("skipped_manual_count", 0),
            failed_count=data.get("failed_count", 0),
            dry_run=data.get("dry_run", False),
            is_undone=data.get("is_undone", False),
            conflict_details=list(data.get("conflict_details", [])),
            manual_skip_reasons=list(data.get("manual_skip_reasons", [])),
            has_signoff=data.get("has_signoff", False),
            signoff_status=data.get("signoff_status", ""),
            signed_by=data.get("signed_by", ""),
            signoff_id=data.get("signoff_id", ""),
        )


@dataclass
class BundleRunDetails:
    """执行明细 - 归档包内的完整执行记录"""
    moves: List[Dict[str, Any]] = field(default_factory=list)
    filter_rules: List[str] = field(default_factory=list)
    filter_file_types: List[str] = field(default_factory=list)
    filter_target_dirs: List[str] = field(default_factory=list)
    created_at: str = ""
    completed_at: str = ""
    undo_records: List[Dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "BundleRunDetails":
        return cls(
            moves=list(data.get("moves", [])),
            filter_rules=list(data.get("filter_rules", [])),
            filter_file_types=list(data.get("filter_file_types", [])),
            filter_target_dirs=list(data.get("filter_target_dirs", [])),
            created_at=data.get("created_at", ""),
            completed_at=data.get("completed_at", ""),
            undo_records=list(data.get("undo_records", [])),
        )


@dataclass
class ExecutionBundle:
    """执行批次归档包

    一次 apply 完成后的完整可导出归档包，包含：
    - 批次快照
    - 签收信息
    - 校验结果
    - 移动明细
    - 冲突和人工跳过原因

    支持 list-bundles、export-bundle、import-bundle 重新查阅或交接。
    """
    bundle_id: str
    bundle_version: str
    created_at: str
    run_id: str
    plan_id: str
    snapshot_id: str
    summary: BundleSummary
    snapshot: BatchSnapshot
    run_details: BundleRunDetails
    signoffs: List[SignoffRecord] = field(default_factory=list)
    signoff_conflicts: List[SignoffConflictState] = field(default_factory=list)
    validation_history: List[SignoffValidationHistory] = field(default_factory=list)
    imported: bool = False
    import_source: Optional[str] = None
    imported_at: Optional[str] = None
    checksum: str = ""

    def to_dict(self) -> Dict[str, Any]:
        result = {
            "bundle_id": self.bundle_id,
            "bundle_version": self.bundle_version,
            "created_at": self.created_at,
            "run_id": self.run_id,
            "plan_id": self.plan_id,
            "snapshot_id": self.snapshot_id,
            "summary": self.summary.to_dict(),
            "snapshot": self.snapshot.to_dict(),
            "run_details": self.run_details.to_dict(),
            "signoffs": [s.to_dict() for s in self.signoffs],
            "signoff_conflicts": [sc.to_dict() for sc in self.signoff_conflicts],
            "validation_history": [vh.to_dict() for vh in self.validation_history],
            "imported": self.imported,
            "import_source": self.import_source,
            "imported_at": self.imported_at,
            "checksum": self.checksum,
        }
        return result

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ExecutionBundle":
        signoffs_data = data.get("signoffs", [])
        signoffs = [SignoffRecord.from_dict(s) for s in signoffs_data] if signoffs_data else []
        conflicts_data = data.get("signoff_conflicts", [])
        signoff_conflicts = [SignoffConflictState.from_dict(sc) for sc in conflicts_data] if conflicts_data else []
        vh_data = data.get("validation_history", [])
        validation_history = [SignoffValidationHistory.from_dict(vh) for vh in vh_data] if vh_data else []
        return cls(
            bundle_id=data["bundle_id"],
            bundle_version=data.get("bundle_version", BUNDLE_VERSION),
            created_at=data["created_at"],
            run_id=data["run_id"],
            plan_id=data["plan_id"],
            snapshot_id=data["snapshot_id"],
            summary=BundleSummary.from_dict(data.get("summary", {})),
            snapshot=BatchSnapshot.from_dict(data["snapshot"]),
            run_details=BundleRunDetails.from_dict(data.get("run_details", {})),
            signoffs=signoffs,
            signoff_conflicts=signoff_conflicts,
            validation_history=validation_history,
            imported=data.get("imported", False),
            import_source=data.get("import_source"),
            imported_at=data.get("imported_at"),
            checksum=data.get("checksum", ""),
        )


@dataclass
class BundleImportLog:
    """归档包导入日志"""
    import_log_id: str
    bundle_id: str
    run_id: str
    snapshot_id: str
    timestamp: str
    status: str  # "success" | "failed" | "skipped" | "forced"
    source_file: str
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    conflict_details: List[str] = field(default_factory=list)
    forced: bool = False
    imported_by: str = "cli"

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "BundleImportLog":
        return cls(
            import_log_id=data["import_log_id"],
            bundle_id=data["bundle_id"],
            run_id=data["run_id"],
            snapshot_id=data["snapshot_id"],
            timestamp=data["timestamp"],
            status=data["status"],
            source_file=data["source_file"],
            errors=list(data.get("errors", [])),
            warnings=list(data.get("warnings", [])),
            conflict_details=list(data.get("conflict_details", [])),
            forced=data.get("forced", False),
            imported_by=data.get("imported_by", "cli"),
        )


@dataclass
class BundleValidationResult:
    """归档包验证结果"""
    valid: bool
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    conflict_types: List[str] = field(default_factory=list)
    existing_bundle: Optional[ExecutionBundle] = None
    existing_run: Optional[Dict[str, Any]] = None

    @property
    def has_errors(self) -> bool:
        return len(self.errors) > 0

    def to_dict(self) -> Dict[str, Any]:
        result = asdict(self)
        if self.existing_bundle:
            result["existing_bundle"] = self.existing_bundle.to_dict()
        return result


# ============================================================
# 落点指纹清单相关模型
# ============================================================


@dataclass
class TargetDirFingerprint:
    """目标目录指纹

    记录某个目标目录的摘要信息：
    - 目录路径
    - 该目录下的文件数量
    - 该目录的路径摘要哈希（路径哈希，用于快速比对
    """
    target_dir: str
    file_count: int
    dir_path_digest: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "TargetDirFingerprint":
        return cls(
            target_dir=data["target_dir"],
            file_count=data["file_count"],
            dir_path_digest=data.get("dir_path_digest", ""),
        )


@dataclass
class FileFingerprint:
    """单个文件的落点指纹

    用于在导入核对时逐文件深度比对的关键信息：
    - 源路径（原始文件名）
    - 目标路径（实际落点）
    - move 时文件名（可追溯）
    - 匹配的规则名
    - 文件大小
    - 修改时间戳
    - 内容摘要哈希（前 N KB + 大小 + 后 N KB 的哈希）
    """
    fingerprint_id: str
    source_path: str
    target_path: str
    filename: str
    matched_rule: str
    file_size: int
    mtime: float
    content_digest: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "FileFingerprint":
        return cls(
            fingerprint_id=data["fingerprint_id"],
            source_path=data["source_path"],
            target_path=data["target_path"],
            filename=data["filename"],
            matched_rule=data["matched_rule"],
            file_size=data["file_size"],
            mtime=data["mtime"],
            content_digest=data.get("content_digest", ""),
        )


@dataclass
class ManualRenameRecord:
    """手工改名记录

    如果 apply 过程中发生的手工改名（如目标存在时的改名策略）
    或用户手动改名）
    - 原始目标路径
    - 最终目标路径
    - 改名原因
    """
    rename_id: str
    original_target_path: str
    final_target_path: str
    rename_reason: str
    renamed_at: str
    renamed_by: str = "system"

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ManualRenameRecord":
        return cls(
            rename_id=data["rename_id"],
            original_target_path=data["original_target_path"],
            final_target_path=data["final_target_path"],
            rename_reason=data["rename_reason"],
            renamed_at=data["renamed_at"],
            renamed_by=data.get("renamed_by", "system"),
        )


@dataclass
class LandingFingerprint:
    """落点指纹清单

    一次 apply 执行后生成的完整落点追溯清单：
    包含所有关键信息，用于导入核对时深度比对

    清单状态跨重启保留，undo 后可回看上一次落点记录。
    """
    landing_id: str
    run_id: str
    snapshot_id: str
    plan_id: str
    created_at: str

    dest_dir: str
    source_dir: str

    total_moved_count: int
    total_skipped_conflict_count: int
    total_skipped_manual_count: int
    total_failed_count: int
    is_dry_run: bool
    is_undone: bool = False
    undone_at: Optional[str] = None

    target_dirs: List[TargetDirFingerprint] = field(default_factory=list)
    file_fingerprints: List[FileFingerprint] = field(default_factory=list)
    manual_renames: List[ManualRenameRecord] = field(default_factory=list)

    config_snapshot_digest: str = ""
    dest_dir_digest: str = ""
    move_target_paths_digest: str = ""
    file_digests_summary: str = ""

    status: str = "active"
    checksum: str = ""

    imported: bool = False
    import_source: Optional[str] = None
    imported_at: Optional[str] = None

    signoff_id: Optional[str] = None
    signoff_snapshot_config_digest: str = ""

    change_summary: str = ""
    export_result: str = ""

    def to_dict(self) -> Dict[str, Any]:
        result = asdict(self)
        result["target_dirs"] = [td.to_dict() for td in self.target_dirs]
        result["file_fingerprints"] = [fp.to_dict() for fp in self.file_fingerprints]
        result["manual_renames"] = [mr.to_dict() for mr in self.manual_renames]
        return result

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "LandingFingerprint":
        target_dirs_data = data.get("target_dirs", [])
        file_fps_data = data.get("file_fingerprints", [])
        manual_rns_data = data.get("manual_renames", [])
        return cls(
            landing_id=data["landing_id"],
            run_id=data["run_id"],
            snapshot_id=data["snapshot_id"],
            plan_id=data["plan_id"],
            created_at=data["created_at"],
            dest_dir=data["dest_dir"],
            source_dir=data["source_dir"],
            total_moved_count=data["total_moved_count"],
            total_skipped_conflict_count=data.get("total_skipped_conflict_count", 0),
            total_skipped_manual_count=data["total_skipped_manual_count"],
            total_failed_count=data["total_failed_count"],
            is_dry_run=data.get("is_dry_run", False),
            is_undone=data.get("is_undone", False),
            undone_at=data.get("undone_at"),
            target_dirs=[TargetDirFingerprint.from_dict(td) for td in target_dirs_data],
            file_fingerprints=[FileFingerprint.from_dict(fp) for fp in file_fps_data],
            manual_renames=[ManualRenameRecord.from_dict(mr) for mr in manual_rns_data],
            config_snapshot_digest=data.get("config_snapshot_digest", ""),
            dest_dir_digest=data.get("dest_dir_digest", ""),
            move_target_paths_digest=data.get("move_target_paths_digest", ""),
            file_digests_summary=data.get("file_digests_summary", ""),
            status=data.get("status", "active"),
            checksum=data.get("checksum", ""),
            imported=data.get("imported", False),
            import_source=data.get("import_source"),
            imported_at=data.get("imported_at"),
            signoff_id=data.get("signoff_id"),
            signoff_snapshot_config_digest=data.get("signoff_snapshot_config_digest", ""),
            change_summary=data.get("change_summary", ""),
            export_result=data.get("export_result", ""),
        )


LANDING_FINGERPRINT_VERSION: str = "1.0"


@dataclass
class LandingFingerprintDiff:
    """落点指纹清单的对比差异结果

    用于导入核对时的深度差异记录，逐字段对比两份清单的差异。
    """
    diff_id: str
    landing_id_local: Optional[str]
    landing_id_imported: str
    compared_at: str
    has_diff: bool
    diff_fields: List[str] = field(default_factory=list)
    diff_details: Dict[str, Any] = field(default_factory=dict)

    dest_dir_changed: bool = False
    target_dirs_diff: List[Dict[str, Any]] = field(default_factory=list)
    target_paths_diff: List[Dict[str, Any]] = field(default_factory=list)
    file_fingerprints_diff: List[Dict[str, Any]] = field(default_factory=list)
    file_count_mismatch: bool = False
    config_changed: bool = False
    duplicate_import: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "LandingFingerprintDiff":
        return cls(
            diff_id=data["diff_id"],
            landing_id_local=data.get("landing_id_local"),
            landing_id_imported=data["landing_id_imported"],
            compared_at=data["compared_at"],
            has_diff=data["has_diff"],
            diff_fields=list(data.get("diff_fields", [])),
            diff_details=dict(data.get("diff_details", {})),
            dest_dir_changed=data.get("dest_dir_changed", False),
            target_dirs_diff=list(data.get("target_dirs_diff", [])),
            target_paths_diff=list(data.get("target_paths_diff", [])),
            file_fingerprints_diff=list(data.get("file_fingerprints_diff", [])),
            file_count_mismatch=data.get("file_count_mismatch", False),
            config_changed=data.get("config_changed", False),
            duplicate_import=data.get("duplicate_import", False),
        )


@dataclass
class LandingImportValidationResult:
    """落点指纹清单导入校验结果

    用于导入核对时的完整校验结果。
    """
    valid: bool
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    conflict_types: List[str] = field(default_factory=list)
    diff_result: Optional[LandingFingerprintDiff] = None
    existing_landing: Optional[LandingFingerprint] = None
    existing_run: Optional[Dict[str, Any]] = None

    @property
    def has_errors(self) -> bool:
        return len(self.errors) > 0

    def to_dict(self) -> Dict[str, Any]:
        result = asdict(self)
        if self.diff_result:
            result["diff_result"] = self.diff_result.to_dict()
        if self.existing_landing:
            result["existing_landing"] = self.existing_landing.to_dict()
        return result

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "LandingImportValidationResult":
        diff_data = data.get("diff_result")
        diff_result = LandingFingerprintDiff.from_dict(diff_data) if diff_data else None
        existing_data = data.get("existing_landing")
        existing_landing = LandingFingerprint.from_dict(existing_data) if existing_data else None
        return cls(
            valid=data["valid"],
            errors=list(data.get("errors", [])),
            warnings=list(data.get("warnings", [])),
            conflict_types=list(data.get("conflict_types", [])),
            diff_result=diff_result,
            existing_landing=existing_landing,
            existing_run=data.get("existing_run"),
        )


@dataclass
class LandingVerifyResult:
    """落点清单核对结果（三类分类）

    用于 verify-landing -f 场景下的分类输出：
    - valid: 有效，清单与当前配置和状态一致
    - invalid: 无效，清单本身有问题（缺少字段、格式错误、无法解析等）
    - conflict: 冲突，清单与当前配置或本地状态存在差异
    """
    status: str  # "valid" | "invalid" | "conflict"
    landing_id: str
    run_id: str
    snapshot_id: str = ""
    plan_id: str = ""

    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    conflict_types: List[str] = field(default_factory=list)

    diff_result: Optional[LandingFingerprintDiff] = None
    current_config_dest_dir: str = ""
    landing_dest_dir: str = ""

    valid_items: List[Dict[str, Any]] = field(default_factory=list)
    invalid_items: List[Dict[str, Any]] = field(default_factory=list)
    conflict_items: List[Dict[str, Any]] = field(default_factory=list)

    verified_at: str = field(default_factory=now_iso)
    verify_source: str = ""

    @property
    def is_valid(self) -> bool:
        return self.status == "valid"

    @property
    def is_invalid(self) -> bool:
        return self.status == "invalid"

    @property
    def is_conflict(self) -> bool:
        return self.status == "conflict"

    def to_dict(self) -> Dict[str, Any]:
        result = asdict(self)
        if self.diff_result:
            result["diff_result"] = self.diff_result.to_dict()
        return result


@dataclass
class LandingImportLog:
    """落点指纹清单导入日志

    记录每次导入尝试（成功/失败/强制），用于跨重启回查。
    """
    import_log_id: str
    landing_id: str
    run_id: str
    snapshot_id: str
    timestamp: str
    status: str
    source_file: str
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    conflict_details: List[str] = field(default_factory=list)
    forced: bool = False
    imported_by: str = "cli"

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "LandingImportLog":
        return cls(
            import_log_id=data["import_log_id"],
            landing_id=data["landing_id"],
            run_id=data["run_id"],
            snapshot_id=data["snapshot_id"],
            timestamp=data["timestamp"],
            status=data["status"],
            source_file=data["source_file"],
            errors=list(data.get("errors", [])),
            warnings=list(data.get("warnings", [])),
            conflict_details=list(data.get("conflict_details", [])),
            forced=data.get("forced", False),
            imported_by=data.get("imported_by", "cli"),
        )


LANDING_REQUIRED_FIELDS: List[str] = [
    "landing_id", "landing_version", "created_at",
    "run_id", "plan_id", "snapshot_id",
    "dest_dir", "source_dir",
    "target_dirs", "file_fingerprints",
    "total_moved_count", "total_skipped_conflict_count",
    "total_skipped_manual_count", "total_failed_count",
]


def load_config(config_path: str) -> Config:
    """从 YAML 文件加载配置"""
    if not os.path.exists(config_path):
        raise FileNotFoundError(f"配置文件不存在: {config_path}")

    with open(config_path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)

    if not data:
        raise ValueError("配置文件为空")

    required_fields = ["source_dir", "dest_dir", "rules"]
    for field_name in required_fields:
        if field_name not in data:
            raise ValueError(f"配置缺少必要字段: {field_name}")

    rules = []
    for rule_data in data["rules"]:
        if "name" not in rule_data or "pattern" not in rule_data or "target" not in rule_data:
            raise ValueError(f"规则缺少必要字段 (name/pattern/target): {rule_data}")
        rules.append(Rule(
            name=rule_data["name"],
            pattern=rule_data["pattern"],
            target=rule_data["target"],
            description=rule_data.get("description"),
        ))

    return Config(
        source_dir=data["source_dir"],
        dest_dir=data["dest_dir"],
        rules=rules,
        state_file=data.get("state_file", ".invoice_organizer_state.json"),
        recursive=data.get("recursive", True),
        file_extensions=data.get("file_extensions"),
    )
