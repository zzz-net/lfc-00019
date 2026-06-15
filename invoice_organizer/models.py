"""配置加载和数据模型定义"""
from __future__ import annotations

import os
from dataclasses import dataclass, field, asdict
from typing import List, Optional, Dict, Any
from datetime import datetime
import yaml
import uuid


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

    def to_dict(self) -> Dict[str, Any]:
        result = asdict(self)
        result["config_snapshot"] = self.config_snapshot.to_dict()
        result["unmatched_files"] = [u.to_dict() for u in self.unmatched_files]
        result["new_target_dirs"] = [n.to_dict() for n in self.new_target_dirs]
        return result

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "BatchSnapshot":
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


def generate_id() -> str:
    """生成唯一 ID"""
    return uuid.uuid4().hex[:12]


def now_iso() -> str:
    """当前时间 ISO 格式"""
    return datetime.now().isoformat()


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
