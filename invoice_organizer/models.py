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
