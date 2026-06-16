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
