"""核心工作流逻辑：扫描、预案、执行、撤销"""
from __future__ import annotations

import os
import fnmatch
import shutil
import csv
import json
from typing import List, Optional, Tuple, Dict, Any
from collections import defaultdict

from .models import (
    Config, Rule, ScannedFile, PlannedMove, ExecutedMove, UndoRecord, PlanSummary,
    ConfigSnapshot, BatchSnapshot, UnmatchedFileInfo, NewDirInfo,
    ConfigDiffResult, ImportValidationResult,
    PlanDiffResult, FileMoveDiff, UnmatchedFileDiff,
    generate_id, now_iso,
)
from .storage import StateStore


def scan_directory(config: Config) -> List[ScannedFile]:
    """按配置扫描目录"""
    result: List[ScannedFile] = []

    if not os.path.exists(config.source_dir):
        raise FileNotFoundError(f"源目录不存在: {config.source_dir}")

    def _match_extension(filename: str) -> bool:
        if not config.file_extensions:
            return True
        ext = os.path.splitext(filename)[1].lower().lstrip(".")
        return ext in [e.lower().lstrip(".") for e in config.file_extensions]

    def _match_rule(filename: str) -> Optional[str]:
        for rule in config.rules:
            if fnmatch.fnmatch(filename.lower(), rule.pattern.lower()):
                return rule.name
        return None

    if config.recursive:
        for root, dirs, files in os.walk(config.source_dir):
            for fname in files:
                if not _match_extension(fname):
                    continue
                full_path = os.path.join(root, fname)
                stat = os.stat(full_path)
                matched = _match_rule(fname)
                result.append(ScannedFile(
                    id=generate_id(),
                    source_path=full_path,
                    filename=fname,
                    size=stat.st_size,
                    mtime=stat.st_mtime,
                    matched_rule=matched,
                ))
    else:
        for fname in os.listdir(config.source_dir):
            full_path = os.path.join(config.source_dir, fname)
            if not os.path.isfile(full_path):
                continue
            if not _match_extension(fname):
                continue
            stat = os.stat(full_path)
            matched = _match_rule(fname)
            result.append(ScannedFile(
                id=generate_id(),
                source_path=full_path,
                filename=fname,
                size=stat.st_size,
                mtime=stat.st_mtime,
                matched_rule=matched,
            ))

    return result


def build_plan(config: Config, scanned_files: List[ScannedFile]) -> Tuple[List[PlannedMove], List[str]]:
    """
    根据扫描结果和配置构建归档预案
    返回: (移动计划列表, 错误信息列表)
    """
    errors: List[str] = []
    moves: List[PlannedMove] = []

    rule_map: Dict[str, Rule] = {r.name: r for r in config.rules}

    target_counter: Dict[str, List[str]] = defaultdict(list)

    for sf in scanned_files:
        if not sf.matched_rule:
            continue

        rule = rule_map.get(sf.matched_rule)
        if not rule:
            continue

        target_dir = os.path.join(config.dest_dir, rule.target)
        target_path = os.path.join(target_dir, sf.filename)

        target_counter[target_path].append(sf.source_path)

    for sf in scanned_files:
        if not sf.matched_rule:
            continue

        rule = rule_map.get(sf.matched_rule)
        if not rule:
            continue

        target_dir = os.path.join(config.dest_dir, rule.target)
        target_path = os.path.join(target_dir, sf.filename)

        conflict_type = None
        conflict_detail = None

        sources_for_target = target_counter.get(target_path, [])
        if len(sources_for_target) > 1:
            conflict_type = "duplicate_target"
            conflict_detail = f"多个源文件将移动到同一目标: {', '.join(sources_for_target)}"
        elif os.path.exists(target_path):
            conflict_type = "target_exists"
            conflict_detail = f"目标文件已存在: {target_path}"

        moves.append(PlannedMove(
            id=generate_id(),
            source_path=sf.source_path,
            target_path=target_path,
            filename=sf.filename,
            matched_rule=sf.matched_rule,
            conflict_type=conflict_type,
            conflict_detail=conflict_detail,
        ))

    return moves, errors


def build_plan_summary(
    config: Config,
    scanned_files: List[ScannedFile],
    moves: List[PlannedMove],
) -> PlanSummary:
    """
    构建预案摘要：未命中文件、同目录规则、新建目录等
    """
    summary = PlanSummary()

    summary.total_files = len(scanned_files)
    summary.matched_files = sum(1 for f in scanned_files if f.matched_rule)
    summary.unmatched_files = summary.total_files - summary.matched_files

    target_to_rules: Dict[str, List[str]] = defaultdict(list)
    for rule in config.rules:
        full_target = os.path.join(config.dest_dir, rule.target)
        target_to_rules[full_target].append(rule.name)

    for target, rule_names in target_to_rules.items():
        if len(rule_names) > 1:
            summary.rules_with_same_target[target] = rule_names

    files_per_rule: Dict[str, int] = defaultdict(int)
    files_per_target: Dict[str, int] = defaultdict(int)
    target_dirs_used: set = set()

    for move in moves:
        files_per_rule[move.matched_rule] += 1
        target_dir = os.path.dirname(move.target_path)
        files_per_target[target_dir] += 1
        target_dirs_used.add(target_dir)

    summary.files_per_rule = dict(files_per_rule)
    summary.files_per_target_dir = dict(files_per_target)

    new_dirs = []
    for td in sorted(target_dirs_used):
        if not os.path.exists(td):
            new_dirs.append(td)
    summary.new_target_dirs = new_dirs

    conflicts = [m for m in moves if m.conflict_type is not None]
    summary.conflict_count = len(conflicts)
    summary.conflict_details = [
        f"{m.filename}: {m.conflict_type} - {m.conflict_detail}"
        for m in conflicts
    ]

    return summary


def filter_moves(
    moves: List[PlannedMove],
    filter_rules: Optional[List[str]] = None,
    filter_file_types: Optional[List[str]] = None,
    filter_target_dirs: Optional[List[str]] = None,
) -> Tuple[List[PlannedMove], List[PlannedMove]]:
    """
    按条件筛选移动计划
    返回: (选中的, 被跳过的)
    """
    selected: List[PlannedMove] = []
    skipped: List[PlannedMove] = []

    for move in moves:
        keep = True

        if filter_rules:
            if move.matched_rule not in filter_rules:
                keep = False

        if keep and filter_file_types:
            ext = os.path.splitext(move.filename)[1].lower().lstrip(".")
            if ext not in [e.lower().lstrip(".") for e in filter_file_types]:
                keep = False

        if keep and filter_target_dirs:
            target_dir = os.path.dirname(move.target_path)
            matched_dir = False
            for fd in filter_target_dirs:
                if target_dir == fd or target_dir.endswith(fd) or fd in target_dir:
                    matched_dir = True
                    break
            if not matched_dir:
                keep = False

        if keep:
            selected.append(move)
        else:
            skipped.append(move)

    return selected, skipped


def apply_plan(
    config: Config,
    store: StateStore,
    plan_id: str,
    moves: List[PlannedMove],
    dry_run: bool = False,
    filter_rules: Optional[List[str]] = None,
    filter_file_types: Optional[List[str]] = None,
    filter_target_dirs: Optional[List[str]] = None,
) -> Tuple[str, List[ExecutedMove], int, int, int, int]:
    """
    执行归档预案
    返回: (run_id, 执行记录, 成功数, 跳过冲突数, 人工跳过数, 失败数)
    """
    run_id = store.create_run(plan_id, dry_run)
    executed: List[ExecutedMove] = []
    success = 0
    skipped_conflict = 0
    skipped_manual = 0
    failed = 0

    selected_moves, manually_skipped = filter_moves(
        moves,
        filter_rules=filter_rules,
        filter_file_types=filter_file_types,
        filter_target_dirs=filter_target_dirs,
    )

    for move in manually_skipped:
        em = ExecutedMove(
            id=generate_id(),
            run_id=run_id,
            source_path=move.source_path,
            target_path=move.target_path,
            filename=move.filename,
            matched_rule=move.matched_rule,
            status="skipped_manual",
            timestamp=now_iso(),
            error_message=_describe_skip_reason(move, filter_rules, filter_file_types, filter_target_dirs),
        )
        executed.append(em)
        store.add_executed_move(run_id, em)
        skipped_manual += 1

    for move in selected_moves:
        if move.conflict_type is not None:
            em = ExecutedMove(
                id=generate_id(),
                run_id=run_id,
                source_path=move.source_path,
                target_path=move.target_path,
                filename=move.filename,
                matched_rule=move.matched_rule,
                status="skipped_conflict",
                timestamp=now_iso(),
                error_message=move.conflict_detail,
            )
            executed.append(em)
            store.add_executed_move(run_id, em)
            skipped_conflict += 1
            continue

        if dry_run:
            em = ExecutedMove(
                id=generate_id(),
                run_id=run_id,
                source_path=move.source_path,
                target_path=move.target_path,
                filename=move.filename,
                matched_rule=move.matched_rule,
                status="moved",
                timestamp=now_iso(),
            )
            executed.append(em)
            store.add_executed_move(run_id, em)
            success += 1
            continue

        try:
            target_dir = os.path.dirname(move.target_path)
            os.makedirs(target_dir, exist_ok=True)

            if os.path.exists(move.target_path):
                em = ExecutedMove(
                    id=generate_id(),
                    run_id=run_id,
                    source_path=move.source_path,
                    target_path=move.target_path,
                    filename=move.filename,
                    matched_rule=move.matched_rule,
                    status="skipped_conflict",
                    timestamp=now_iso(),
                    error_message=f"执行时目标已存在: {move.target_path}",
                )
                executed.append(em)
                store.add_executed_move(run_id, em)
                skipped_conflict += 1
                continue

            shutil.move(move.source_path, move.target_path)

            em = ExecutedMove(
                id=generate_id(),
                run_id=run_id,
                source_path=move.source_path,
                target_path=move.target_path,
                filename=move.filename,
                matched_rule=move.matched_rule,
                status="moved",
                timestamp=now_iso(),
            )
            executed.append(em)
            store.add_executed_move(run_id, em)
            success += 1
        except Exception as e:
            em = ExecutedMove(
                id=generate_id(),
                run_id=run_id,
                source_path=move.source_path,
                target_path=move.target_path,
                filename=move.filename,
                matched_rule=move.matched_rule,
                status="failed",
                timestamp=now_iso(),
                error_message=str(e),
            )
            executed.append(em)
            store.add_executed_move(run_id, em)
            failed += 1

    store.complete_run(run_id)
    return run_id, executed, success, skipped_conflict, skipped_manual, failed


def _describe_skip_reason(
    move: PlannedMove,
    filter_rules: Optional[List[str]],
    filter_file_types: Optional[List[str]],
    filter_target_dirs: Optional[List[str]],
) -> str:
    """生成人工跳过的原因描述"""
    reasons = []
    if filter_rules and move.matched_rule not in filter_rules:
        reasons.append(f"规则不在筛选范围内: {move.matched_rule}")
    if filter_file_types:
        ext = os.path.splitext(move.filename)[1].lower().lstrip(".")
        if ext not in [e.lower().lstrip(".") for e in filter_file_types]:
            reasons.append(f"文件类型不在筛选范围内: .{ext}")
    if filter_target_dirs:
        target_dir = os.path.dirname(move.target_path)
        matched = False
        for fd in filter_target_dirs:
            if target_dir == fd or target_dir.endswith(fd) or fd in target_dir:
                matched = True
                break
        if not matched:
            reasons.append(f"目标目录不在筛选范围内: {target_dir}")
    return "; ".join(reasons) if reasons else "人工筛选跳过"


def undo_run(
    store: StateStore,
    run_id: str,
) -> Tuple[bool, int, int, List[str]]:
    """
    撤销某次执行
    返回: (是否完全成功, 恢复数, 失败数, 错误详情)
    """
    run = store.get_run(run_id)
    if not run:
        return False, 0, 0, [f"执行记录不存在: {run_id}"]

    if run.get("dry_run"):
        store.mark_run_undone(run_id)
        store.add_undo_record(UndoRecord(
            run_id=run_id,
            undo_timestamp=now_iso(),
            moves_restored=0,
            status="completed",
        ))
        return True, 0, 0, []

    if run.get("is_undone"):
        return False, 0, 0, [f"该执行已被撤销: {run_id}"]

    restored = 0
    errors_detail: List[str] = []

    moves_data = run.get("moves", [])
    reversed_moves = list(reversed(moves_data))

    for move_data in reversed_moves:
        if move_data["status"] != "moved":
            continue

        source = move_data["source_path"]
        target = move_data["target_path"]

        try:
            if not os.path.exists(target):
                errors_detail.append(f"无法撤销 (目标不存在): {target} -> {source}")
                continue

            source_dir = os.path.dirname(source)
            os.makedirs(source_dir, exist_ok=True)

            if os.path.exists(source):
                errors_detail.append(f"无法撤销 (源位置已存在文件): {source}")
                continue

            shutil.move(target, source)
            restored += 1
        except Exception as e:
            errors_detail.append(f"撤销失败 {target} -> {source}: {str(e)}")

    failed = len(errors_detail)
    total_moved = sum(1 for m in moves_data if m["status"] == "moved")
    status = "completed" if restored == total_moved else ("partial" if restored > 0 else "failed")

    store.mark_run_undone(run_id)
    store.add_undo_record(UndoRecord(
        run_id=run_id,
        undo_timestamp=now_iso(),
        moves_restored=restored,
        status=status,
    ))

    success = (failed == 0)
    return success, restored, failed, errors_detail


def export_logs(
    store: StateStore,
    output_path: str,
    format: str = "json",
) -> int:
    """
    导出操作日志
    返回: 导出记录条数
    """
    full_state = store.get_full_state()

    if format == "json":
        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(full_state, f, ensure_ascii=False, indent=2)
        return 1

    elif format == "csv":
        with open(output_path, "w", encoding="utf-8-sig", newline="") as f:
            writer = csv.writer(f)

            writer.writerow(["=== 扫描记录 ==="])
            writer.writerow(["扫描时间", full_state.get("last_scan", "N/A")])
            writer.writerow(["文件ID", "源路径", "文件名", "大小", "修改时间", "匹配规则"])
            for sf in full_state.get("scanned_files", []):
                writer.writerow([
                    sf.get("id", ""),
                    sf.get("source_path", ""),
                    sf.get("filename", ""),
                    sf.get("size", ""),
                    sf.get("mtime", ""),
                    sf.get("matched_rule", ""),
                ])

            writer.writerow([])
            writer.writerow(["=== 归档预案 ==="])
            writer.writerow(["预案ID", "创建时间", "文件", "源路径", "目标路径", "匹配规则", "冲突类型", "冲突详情"])
            for plan_id, plan in full_state.get("plans", {}).items():
                for move in plan.get("moves", []):
                    writer.writerow([
                        plan_id,
                        plan.get("created_at", ""),
                        move.get("filename", ""),
                        move.get("source_path", ""),
                        move.get("target_path", ""),
                        move.get("matched_rule", ""),
                        move.get("conflict_type", ""),
                        move.get("conflict_detail", ""),
                    ])

            writer.writerow([])
            writer.writerow(["=== 执行记录 ==="])
            writer.writerow(["执行ID", "预案ID", "是否预演", "撤销状态", "创建时间", "完成时间", "文件", "源->目标", "状态", "执行时间", "错误信息"])
            for run_id, run in full_state.get("runs", {}).items():
                for move in run.get("moves", []):
                    writer.writerow([
                        run_id,
                        run.get("plan_id", ""),
                        "是" if run.get("dry_run") else "否",
                        "已撤销" if run.get("is_undone") else "未撤销",
                        run.get("created_at", ""),
                        run.get("completed_at", ""),
                        move.get("filename", ""),
                        f"{move.get('source_path', '')} -> {move.get('target_path', '')}",
                        move.get("status", ""),
                        move.get("timestamp", ""),
                        move.get("error_message", ""),
                    ])

            writer.writerow([])
            writer.writerow(["=== 撤销记录 ==="])
            writer.writerow(["执行ID", "撤销时间", "恢复数量", "状态"])
            for ur in full_state.get("undo_records", []):
                writer.writerow([
                    ur.get("run_id", ""),
                    ur.get("undo_timestamp", ""),
                    ur.get("moves_restored", ""),
                    ur.get("status", ""),
                ])

        return 1

    else:
        raise ValueError(f"不支持的导出格式: {format}")


def export_plan(
    plan_data: Dict[str, Any],
    summary: PlanSummary,
    scanned_files: List[ScannedFile],
    output_path: str,
    format: str = "json",
) -> None:
    """
    导出单个预案及其摘要，便于人工复核
    """
    unmatched_files = [f for f in scanned_files if not f.matched_rule]

    if format == "json":
        export_data = {
            "plan_id": plan_data.get("id"),
            "created_at": plan_data.get("created_at"),
            "has_conflicts": plan_data.get("has_conflicts"),
            "summary": summary.to_dict(),
            "moves": plan_data.get("moves", []),
            "unmatched_files": [f.to_dict() for f in unmatched_files],
        }
        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(export_data, f, ensure_ascii=False, indent=2)

    elif format == "csv":
        with open(output_path, "w", encoding="utf-8-sig", newline="") as f:
            writer = csv.writer(f)

            writer.writerow(["=== 预案摘要 ==="])
            writer.writerow(["预案ID", plan_data.get("id", "")])
            writer.writerow(["创建时间", plan_data.get("created_at", "")])
            writer.writerow(["总文件数", summary.total_files])
            writer.writerow(["匹配规则数", summary.matched_files])
            writer.writerow(["未匹配规则数", summary.unmatched_files])
            writer.writerow(["冲突项数", summary.conflict_count])
            writer.writerow(["新建目录数", len(summary.new_target_dirs)])

            writer.writerow([])
            writer.writerow(["=== 各规则文件数 ==="])
            writer.writerow(["规则名", "文件数"])
            for rule, count in sorted(summary.files_per_rule.items()):
                writer.writerow([rule, count])

            writer.writerow([])
            writer.writerow(["=== 各目标目录文件数 ==="])
            writer.writerow(["目标目录", "文件数", "是否新建"])
            for tdir, count in sorted(summary.files_per_target_dir.items()):
                is_new = "是" if tdir in summary.new_target_dirs else "否"
                writer.writerow([tdir, count, is_new])

            writer.writerow([])
            writer.writerow(["=== 同目标目录的规则 ==="])
            writer.writerow(["目标目录", "规则列表"])
            for tdir, rules in sorted(summary.rules_with_same_target.items()):
                writer.writerow([tdir, ", ".join(rules)])

            writer.writerow([])
            writer.writerow(["=== 移动计划详情 ==="])
            writer.writerow(["文件名", "源路径", "目标路径", "匹配规则", "冲突类型", "冲突详情"])
            for move in plan_data.get("moves", []):
                writer.writerow([
                    move.get("filename", ""),
                    move.get("source_path", ""),
                    move.get("target_path", ""),
                    move.get("matched_rule", ""),
                    move.get("conflict_type", ""),
                    move.get("conflict_detail", ""),
                ])

            writer.writerow([])
            writer.writerow(["=== 未匹配规则文件 ==="])
            writer.writerow(["文件名", "源路径", "大小"])
            for f in unmatched_files:
                writer.writerow([f.filename, f.source_path, f.size])

    else:
        raise ValueError(f"不支持的导出格式: {format}")


# ============================================================
# 批次快照相关功能
# ============================================================

def create_config_snapshot(config: Config, config_path: Optional[str] = None) -> ConfigSnapshot:
    """
    从当前配置创建配置快照
    """
    config_mtime = None
    if config_path and os.path.exists(config_path):
        config_mtime = os.path.getmtime(config_path)

    return ConfigSnapshot(
        source_dir=config.source_dir,
        dest_dir=config.dest_dir,
        rules=[r.to_dict() for r in config.rules],
        state_file=config.state_file,
        recursive=config.recursive,
        file_extensions=config.file_extensions,
        config_path=config_path,
        config_mtime=config_mtime,
    )


def create_batch_snapshot(
    config: Config,
    scanned_files: List[ScannedFile],
    moves: List[PlannedMove],
    plan_id: str,
    summary: PlanSummary,
    config_path: Optional[str] = None,
) -> BatchSnapshot:
    """
    从 plan 结果创建完整的批次快照
    """
    config_snapshot = create_config_snapshot(config, config_path)

    unmatched_infos: List[UnmatchedFileInfo] = []
    for sf in scanned_files:
        if not sf.matched_rule:
            reason = "no_rule_match"
            if config.file_extensions:
                ext = os.path.splitext(sf.filename)[1].lower().lstrip(".")
                if ext not in [e.lower().lstrip(".") for e in config.file_extensions]:
                    reason = "extension_filtered"
            unmatched_infos.append(UnmatchedFileInfo(
                filename=sf.filename,
                source_path=sf.source_path,
                size=sf.size,
                mtime=sf.mtime,
                reason=reason,
            ))

    new_dir_infos: List[NewDirInfo] = []
    target_dir_rules: Dict[str, List[str]] = defaultdict(list)
    target_dir_file_count: Dict[str, int] = defaultdict(int)

    for move in moves:
        tdir = os.path.dirname(move.target_path)
        if move.matched_rule not in target_dir_rules[tdir]:
            target_dir_rules[tdir].append(move.matched_rule)
        target_dir_file_count[tdir] += 1

    for nd in summary.new_target_dirs:
        new_dir_infos.append(NewDirInfo(
            path=nd,
            rule_names=target_dir_rules.get(nd, []),
            file_count=target_dir_file_count.get(nd, 0),
        ))

    has_conflicts = any(m.conflict_type is not None for m in moves)

    snapshot_id = generate_id()

    return BatchSnapshot(
        snapshot_id=snapshot_id,
        created_at=now_iso(),
        plan_id=plan_id,
        config_snapshot=config_snapshot,
        scanned_files=[sf.to_dict() for sf in scanned_files],
        moves=[m.to_dict() for m in moves],
        unmatched_files=unmatched_infos,
        new_target_dirs=new_dir_infos,
        has_conflicts=has_conflicts,
        summary=summary.to_dict(),
    )


def diff_config(current_config: Config, snapshot_config: ConfigSnapshot) -> ConfigDiffResult:
    """
    比较当前配置与快照配置的差异
    """
    added_rules: List[str] = []
    removed_rules: List[str] = []
    modified_rules: List[str] = []

    current_rule_map: Dict[str, Rule] = {r.name: r for r in current_config.rules}
    snapshot_rule_map: Dict[str, Dict[str, Any]] = {r["name"]: r for r in snapshot_config.rules}

    all_rule_names = set(current_rule_map.keys()) | set(snapshot_rule_map.keys())

    for name in all_rule_names:
        if name in current_rule_map and name not in snapshot_rule_map:
            added_rules.append(name)
        elif name not in current_rule_map and name in snapshot_rule_map:
            removed_rules.append(name)
        else:
            curr = current_rule_map[name]
            snap = snapshot_rule_map[name]
            if (curr.pattern != snap.get("pattern") or
                curr.target != snap.get("target") or
                curr.description != snap.get("description")):
                modified_rules.append(name)

    source_dir_changed = current_config.source_dir != snapshot_config.source_dir
    dest_dir_changed = current_config.dest_dir != snapshot_config.dest_dir
    extensions_changed = current_config.file_extensions != snapshot_config.file_extensions
    recursive_changed = current_config.recursive != snapshot_config.recursive

    has_diff = (
        len(added_rules) > 0 or
        len(removed_rules) > 0 or
        len(modified_rules) > 0 or
        source_dir_changed or
        dest_dir_changed or
        extensions_changed or
        recursive_changed
    )

    return ConfigDiffResult(
        has_diff=has_diff,
        added_rules=added_rules,
        removed_rules=removed_rules,
        modified_rules=modified_rules,
        source_dir_changed=source_dir_changed,
        dest_dir_changed=dest_dir_changed,
        extensions_changed=extensions_changed,
        recursive_changed=recursive_changed,
    )


def validate_import_snapshot(snapshot: BatchSnapshot) -> ImportValidationResult:
    """
    验证导入的快照是否可用于当前环境

    检查项：
    - 源文件是否存在（是否被外部移动）
    - 目标文件是否冲突
    - 目标目录是否有写权限
    """
    errors: List[str] = []
    warnings: List[str] = []
    conflicting_files: List[str] = []
    missing_source_files: List[str] = []
    unwritable_dirs: List[str] = []

    for move_data in snapshot.moves:
        source_path = move_data["source_path"]
        target_path = move_data["target_path"]

        if not os.path.exists(source_path):
            missing_source_files.append(source_path)
            errors.append(f"源文件不存在 (可能已被外部移动): {source_path}")

        if os.path.exists(target_path):
            conflicting_files.append(target_path)
            warnings.append(f"目标文件已存在 (执行时将跳过): {target_path}")

    checked_dirs = set()
    for nd in snapshot.new_target_dirs:
        dir_path = nd.path
        parent = os.path.dirname(dir_path)
        if parent and parent not in checked_dirs:
            checked_dirs.add(parent)
            if os.path.exists(parent):
                if not os.access(parent, os.W_OK):
                    unwritable_dirs.append(parent)
                    errors.append(f"父目录无写权限: {parent}")
            else:
                ancestor = parent
                while ancestor and not os.path.exists(ancestor):
                    ancestor = os.path.dirname(ancestor)
                if ancestor and not os.access(ancestor, os.W_OK):
                    unwritable_dirs.append(ancestor)
                    errors.append(f"最近的现存祖先目录无写权限: {ancestor}")

    dest_dir = snapshot.config_snapshot.dest_dir
    if os.path.exists(dest_dir):
        if not os.access(dest_dir, os.W_OK):
            if dest_dir not in unwritable_dirs:
                unwritable_dirs.append(dest_dir)
                errors.append(f"目标根目录无写权限: {dest_dir}")

    valid = len(errors) == 0

    return ImportValidationResult(
        valid=valid,
        errors=errors,
        warnings=warnings,
        conflicting_files=conflicting_files,
        missing_source_files=missing_source_files,
        unwritable_dirs=unwritable_dirs,
    )


def load_snapshot_from_file(file_path: str) -> BatchSnapshot:
    """
    从 JSON 文件加载快照
    """
    if not os.path.exists(file_path):
        raise FileNotFoundError(f"快照文件不存在: {file_path}")

    with open(file_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    if "snapshot_id" not in data:
        raise ValueError("无效的快照文件：缺少 snapshot_id 字段")
    if "config_snapshot" not in data:
        raise ValueError("无效的快照文件：缺少 config_snapshot 字段")
    if "moves" not in data:
        raise ValueError("无效的快照文件：缺少 moves 字段")

    return BatchSnapshot.from_dict(data)


def export_snapshot_to_file(snapshot: BatchSnapshot, output_path: str) -> None:
    """
    导出快照到 JSON 文件
    """
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(snapshot.to_dict(), f, ensure_ascii=False, indent=2)


def snapshot_to_planned_moves(snapshot: BatchSnapshot) -> List[PlannedMove]:
    """
    从快照中提取 PlannedMove 列表（用于 apply）
    """
    return [
        PlannedMove(
            id=m["id"],
            source_path=m["source_path"],
            target_path=m["target_path"],
            filename=m["filename"],
            matched_rule=m["matched_rule"],
            conflict_type=m.get("conflict_type"),
            conflict_detail=m.get("conflict_detail"),
        )
        for m in snapshot.moves
    ]


# ============================================================
# 预案版本对比相关功能
# ============================================================

def diff_plans(
    old_snapshot: BatchSnapshot,
    new_snapshot: BatchSnapshot,
) -> PlanDiffResult:
    """
    对比两版预案/快照的差异

    返回详细的差异结果，包括：
    - 文件去向变化
    - 规则增删改
    - 冲突状态变化
    - 未命中文件变化
    """
    result = PlanDiffResult(
        old_plan_id=old_snapshot.plan_id,
        new_plan_id=new_snapshot.plan_id,
        old_snapshot_id=old_snapshot.snapshot_id,
        new_snapshot_id=new_snapshot.snapshot_id,
        old_move_count=len(old_snapshot.moves),
        new_move_count=len(new_snapshot.moves),
        old_unmatched_count=len(old_snapshot.unmatched_files),
        new_unmatched_count=len(new_snapshot.unmatched_files),
    )

    old_moves_by_path: Dict[str, Dict[str, Any]] = {}
    for m in old_snapshot.moves:
        old_moves_by_path[m["source_path"]] = m

    new_moves_by_path: Dict[str, Dict[str, Any]] = {}
    for m in new_snapshot.moves:
        new_moves_by_path[m["source_path"]] = m

    old_unmatched_by_path: Dict[str, UnmatchedFileInfo] = {}
    for u in old_snapshot.unmatched_files:
        old_unmatched_by_path[u.source_path] = u

    new_unmatched_by_path: Dict[str, UnmatchedFileInfo] = {}
    for u in new_snapshot.unmatched_files:
        new_unmatched_by_path[u.source_path] = u

    all_source_paths = set(old_moves_by_path.keys()) | set(new_moves_by_path.keys())

    for src_path in sorted(all_source_paths):
        old_move = old_moves_by_path.get(src_path)
        new_move = new_moves_by_path.get(src_path)

        if old_move and not new_move:
            result.removed_moves.append(FileMoveDiff(
                filename=old_move["filename"],
                source_path=src_path,
                change_type="removed",
                old_target_path=old_move["target_path"],
                old_matched_rule=old_move["matched_rule"],
                old_conflict_type=old_move.get("conflict_type"),
                old_conflict_detail=old_move.get("conflict_detail"),
            ))
        elif not old_move and new_move:
            result.added_moves.append(FileMoveDiff(
                filename=new_move["filename"],
                source_path=src_path,
                change_type="added",
                new_target_path=new_move["target_path"],
                new_matched_rule=new_move["matched_rule"],
                new_conflict_type=new_move.get("conflict_type"),
                new_conflict_detail=new_move.get("conflict_detail"),
            ))
        else:
            target_changed = old_move["target_path"] != new_move["target_path"]
            rule_changed = old_move["matched_rule"] != new_move["matched_rule"]
            conflict_changed = (
                old_move.get("conflict_type") != new_move.get("conflict_type") or
                old_move.get("conflict_detail") != new_move.get("conflict_detail")
            )

            diff = FileMoveDiff(
                filename=old_move["filename"],
                source_path=src_path,
                change_type="unchanged",
                old_target_path=old_move["target_path"],
                new_target_path=new_move["target_path"],
                old_matched_rule=old_move["matched_rule"],
                new_matched_rule=new_move["matched_rule"],
                old_conflict_type=old_move.get("conflict_type"),
                new_conflict_type=new_move.get("conflict_type"),
                old_conflict_detail=old_move.get("conflict_detail"),
                new_conflict_detail=new_move.get("conflict_detail"),
            )

            if target_changed:
                diff.change_type = "target_changed"
                result.target_changed.append(diff)
            elif rule_changed:
                diff.change_type = "rule_changed"
                result.rule_changed.append(diff)
            elif conflict_changed:
                diff.change_type = "conflict_changed"
                result.conflict_changed.append(diff)
            else:
                result.unchanged_moves.append(diff)

    all_unmatched_paths = set(old_unmatched_by_path.keys()) | set(new_unmatched_by_path.keys())

    for src_path in sorted(all_unmatched_paths):
        old_u = old_unmatched_by_path.get(src_path)
        new_u = new_unmatched_by_path.get(src_path)

        if old_u and not new_u:
            result.removed_unmatched.append(UnmatchedFileDiff(
                filename=old_u.filename,
                source_path=src_path,
                change_type="removed",
                old_reason=old_u.reason,
            ))
        elif not old_u and new_u:
            result.added_unmatched.append(UnmatchedFileDiff(
                filename=new_u.filename,
                source_path=src_path,
                change_type="added",
                new_reason=new_u.reason,
            ))
        else:
            if old_u.reason != new_u.reason:
                result.added_unmatched.append(UnmatchedFileDiff(
                    filename=old_u.filename,
                    source_path=src_path,
                    change_type="reason_changed",
                    old_reason=old_u.reason,
                    new_reason=new_u.reason,
                ))
            else:
                result.unchanged_unmatched.append(UnmatchedFileDiff(
                    filename=old_u.filename,
                    source_path=src_path,
                    change_type="unchanged",
                    old_reason=old_u.reason,
                    new_reason=new_u.reason,
                ))

    old_rule_map: Dict[str, Dict[str, Any]] = {r["name"]: r for r in old_snapshot.config_snapshot.rules}
    new_rule_map: Dict[str, Dict[str, Any]] = {r["name"]: r for r in new_snapshot.config_snapshot.rules}

    all_rule_names = set(old_rule_map.keys()) | set(new_rule_map.keys())

    for name in sorted(all_rule_names):
        if name in new_rule_map and name not in old_rule_map:
            result.added_rules.append(name)
        elif name not in new_rule_map and name in old_rule_map:
            result.removed_rules.append(name)
        else:
            old_r = old_rule_map[name]
            new_r = new_rule_map[name]
            if (old_r.get("pattern") != new_r.get("pattern") or
                old_r.get("target") != new_r.get("target") or
                old_r.get("description") != new_r.get("description")):
                result.modified_rules.append(name)

    config_diff = diff_config_snapshots(old_snapshot.config_snapshot, new_snapshot.config_snapshot)
    if config_diff.has_diff:
        result.config_diff = config_diff.to_dict()

    return result


def diff_config_snapshots(old_config: ConfigSnapshot, new_config: ConfigSnapshot) -> ConfigDiffResult:
    """
    比较两个配置快照的差异
    """
    added_rules: List[str] = []
    removed_rules: List[str] = []
    modified_rules: List[str] = []

    old_rule_map: Dict[str, Dict[str, Any]] = {r["name"]: r for r in old_config.rules}
    new_rule_map: Dict[str, Dict[str, Any]] = {r["name"]: r for r in new_config.rules}

    all_rule_names = set(old_rule_map.keys()) | set(new_rule_map.keys())

    for name in all_rule_names:
        if name in new_rule_map and name not in old_rule_map:
            added_rules.append(name)
        elif name not in new_rule_map and name in old_rule_map:
            removed_rules.append(name)
        else:
            old_r = old_rule_map[name]
            new_r = new_rule_map[name]
            if (old_r.get("pattern") != new_r.get("pattern") or
                old_r.get("target") != new_r.get("target") or
                old_r.get("description") != new_r.get("description")):
                modified_rules.append(name)

    source_dir_changed = old_config.source_dir != new_config.source_dir
    dest_dir_changed = old_config.dest_dir != new_config.dest_dir
    extensions_changed = old_config.file_extensions != new_config.file_extensions
    recursive_changed = old_config.recursive != new_config.recursive

    has_diff = (
        len(added_rules) > 0 or
        len(removed_rules) > 0 or
        len(modified_rules) > 0 or
        source_dir_changed or
        dest_dir_changed or
        extensions_changed or
        recursive_changed
    )

    return ConfigDiffResult(
        has_diff=has_diff,
        added_rules=sorted(added_rules),
        removed_rules=sorted(removed_rules),
        modified_rules=sorted(modified_rules),
        source_dir_changed=source_dir_changed,
        dest_dir_changed=dest_dir_changed,
        extensions_changed=extensions_changed,
        recursive_changed=recursive_changed,
    )


def export_plan_diff(
    diff_result: PlanDiffResult,
    output_path: str,
    format: str = "json",
) -> None:
    """
    导出预案差异对比结果为 JSON 或 CSV 格式
    """
    if format == "json":
        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(diff_result.to_dict(), f, ensure_ascii=False, indent=2)

    elif format == "csv":
        with open(output_path, "w", encoding="utf-8-sig", newline="") as f:
            writer = csv.writer(f)

            writer.writerow(["=== 预案差异对比摘要 ==="])
            writer.writerow(["旧预案ID", diff_result.old_plan_id])
            writer.writerow(["新预案ID", diff_result.new_plan_id])
            writer.writerow(["旧快照ID", diff_result.old_snapshot_id or ""])
            writer.writerow(["新快照ID", diff_result.new_snapshot_id or ""])
            writer.writerow(["对比时间", diff_result.diff_timestamp])
            writer.writerow(["旧移动计划数", diff_result.old_move_count])
            writer.writerow(["新移动计划数", diff_result.new_move_count])
            writer.writerow(["旧未命中数", diff_result.old_unmatched_count])
            writer.writerow(["新未命中数", diff_result.new_unmatched_count])
            writer.writerow(["有变化", "是" if diff_result.has_changes else "否"])
            writer.writerow(["变化的移动项数", diff_result.total_changed_moves])

            writer.writerow([])
            writer.writerow(["=== 规则变化 ==="])
            writer.writerow(["变化类型", "规则名"])
            for r in diff_result.added_rules:
                writer.writerow(["新增", r])
            for r in diff_result.removed_rules:
                writer.writerow(["删除", r])
            for r in diff_result.modified_rules:
                writer.writerow(["修改", r])

            if diff_result.config_diff:
                writer.writerow([])
                writer.writerow(["=== 配置差异 ==="])
                cd = diff_result.config_diff
                if cd.get("source_dir_changed"):
                    writer.writerow(["源目录变更", "是"])
                if cd.get("dest_dir_changed"):
                    writer.writerow(["目标目录变更", "是"])
                if cd.get("extensions_changed"):
                    writer.writerow(["扩展名过滤变更", "是"])
                if cd.get("recursive_changed"):
                    writer.writerow(["递归扫描变更", "是"])

            writer.writerow([])
            writer.writerow(["=== 新增移动项 ==="])
            writer.writerow(["文件名", "源路径", "目标路径", "匹配规则", "冲突类型"])
            for m in diff_result.added_moves:
                writer.writerow([
                    m.filename, m.source_path,
                    m.new_target_path or "", m.new_matched_rule or "",
                    m.new_conflict_type or "",
                ])

            writer.writerow([])
            writer.writerow(["=== 删除移动项 ==="])
            writer.writerow(["文件名", "源路径", "原目标路径", "原匹配规则", "原冲突类型"])
            for m in diff_result.removed_moves:
                writer.writerow([
                    m.filename, m.source_path,
                    m.old_target_path or "", m.old_matched_rule or "",
                    m.old_conflict_type or "",
                ])

            writer.writerow([])
            writer.writerow(["=== 目标路径变化 ==="])
            writer.writerow(["文件名", "源路径", "原目标路径", "新目标路径", "原规则", "新规则"])
            for m in diff_result.target_changed:
                writer.writerow([
                    m.filename, m.source_path,
                    m.old_target_path or "", m.new_target_path or "",
                    m.old_matched_rule or "", m.new_matched_rule or "",
                ])

            writer.writerow([])
            writer.writerow(["=== 匹配规则变化 ==="])
            writer.writerow(["文件名", "源路径", "原规则", "新规则", "目标路径"])
            for m in diff_result.rule_changed:
                writer.writerow([
                    m.filename, m.source_path,
                    m.old_matched_rule or "", m.new_matched_rule or "",
                    m.new_target_path or "",
                ])

            writer.writerow([])
            writer.writerow(["=== 冲突状态变化 ==="])
            writer.writerow(["文件名", "源路径", "原冲突类型", "新冲突类型", "原冲突详情", "新冲突详情"])
            for m in diff_result.conflict_changed:
                writer.writerow([
                    m.filename, m.source_path,
                    m.old_conflict_type or "", m.new_conflict_type or "",
                    m.old_conflict_detail or "", m.new_conflict_detail or "",
                ])

            writer.writerow([])
            writer.writerow(["=== 新增未命中文件 ==="])
            writer.writerow(["文件名", "源路径", "原因"])
            for u in diff_result.added_unmatched:
                writer.writerow([u.filename, u.source_path, u.new_reason or ""])

            writer.writerow([])
            writer.writerow(["=== 删除未命中文件 ==="])
            writer.writerow(["文件名", "源路径", "原因"])
            for u in diff_result.removed_unmatched:
                writer.writerow([u.filename, u.source_path, u.old_reason or ""])

    else:
        raise ValueError(f"不支持的导出格式: {format}")
