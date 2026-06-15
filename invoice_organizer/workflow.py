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
