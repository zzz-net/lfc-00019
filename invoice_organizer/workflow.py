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
    Config, Rule, ScannedFile, PlannedMove, ExecutedMove, UndoRecord,
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

    rule_targets: Dict[str, List[str]] = defaultdict(list)
    for rule in config.rules:
        full_target = os.path.join(config.dest_dir, rule.target)
        rule_targets[full_target].append(rule.name)

    for full_target, rule_names in rule_targets.items():
        if len(rule_names) > 1:
            errors.append(
                f"规则冲突检测失败: 多条规则 ({', '.join(rule_names)}) 映射到同一目标路径: {full_target}"
            )

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


def apply_plan(
    config: Config,
    store: StateStore,
    plan_id: str,
    moves: List[PlannedMove],
    dry_run: bool = False,
) -> Tuple[str, List[ExecutedMove], int, int, int]:
    """
    执行归档预案
    返回: (run_id, 执行记录, 成功数, 跳过冲突数, 失败数)
    """
    run_id = store.create_run(plan_id, dry_run)
    executed: List[ExecutedMove] = []
    success = 0
    skipped = 0
    failed = 0

    for move in moves:
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
            skipped += 1
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
                skipped += 1
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
    return run_id, executed, success, skipped, failed


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
