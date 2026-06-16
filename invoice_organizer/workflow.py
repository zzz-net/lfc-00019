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
    SnapshotRemark, RemarkValidationResult, RemarkFieldChange,
    SignoffRecord, SignoffValidationResult, SignoffFieldChange,
    SignoffConflictState, SignoffValidationHistory,
    TargetDirFingerprint, FileFingerprint, ManualRenameRecord,
    LandingFingerprint, LandingFingerprintDiff, LandingImportValidationResult,
    LandingImportLog, LandingVerifyResult,
    LANDING_FINGERPRINT_VERSION, LANDING_REQUIRED_FIELDS,
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

    def _resolve_rule(sf: ScannedFile) -> Optional[Rule]:
        rule = rule_map.get(sf.matched_rule)
        if rule:
            return rule
        for r in config.rules:
            if fnmatch.fnmatch(sf.filename.lower(), r.pattern.lower()):
                return r
        return None

    target_counter: Dict[str, List[str]] = defaultdict(list)

    for sf in scanned_files:
        if not sf.matched_rule:
            continue

        rule = _resolve_rule(sf)
        if not rule:
            continue

        target_dir = os.path.join(config.dest_dir, rule.target)
        target_path = os.path.join(target_dir, sf.filename)

        target_counter[target_path].append(sf.source_path)

    for sf in scanned_files:
        if not sf.matched_rule:
            continue

        rule = _resolve_rule(sf)
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
            matched_rule=rule.name,
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

    store.update_landing_undone(run_id, is_undone=True)

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

            writer.writerow([])
            writer.writerow(["=== 版本锁定记录 ==="])
            writer.writerow(["锁定ID", "快照ID", "预案ID", "锁定时间", "释放时间", "锁定原因", "释放原因", "状态"])
            for lk in full_state.get("plan_locks", []):
                writer.writerow([
                    lk.get("lock_id", ""),
                    lk.get("snapshot_id", ""),
                    lk.get("plan_id", ""),
                    lk.get("locked_at", ""),
                    lk.get("released_at", ""),
                    lk.get("lock_reason", ""),
                    lk.get("release_reason", ""),
                    "已释放" if lk.get("released_at") else "活动",
                ])

            writer.writerow([])
            writer.writerow(["=== 锁定违规记录 ==="])
            writer.writerow(["违规时间", "锁定ID", "违规类型", "详情", "已拦截"])
            for lv in full_state.get("lock_violations", []):
                writer.writerow([
                    lv.get("timestamp", ""),
                    lv.get("lock_id", ""),
                    lv.get("violation_type", ""),
                    lv.get("detail", ""),
                    lv.get("blocked", ""),
                ])

            writer.writerow([])
            writer.writerow(["=== 预案差异记录 ==="])
            writer.writerow(["差异ID", "对比时间", "旧快照ID", "新快照ID", "旧预案ID", "新预案ID", "目标变化数", "新增数", "删除数", "新增规则", "删除规则", "修改规则"])
            for pd in full_state.get("plan_diffs", []):
                data = pd.get("data", pd)
                writer.writerow([
                    pd.get("diff_id", ""),
                    data.get("compared_at", ""),
                    data.get("old_snapshot_id", ""),
                    data.get("new_snapshot_id", ""),
                    data.get("old_plan_id", ""),
                    data.get("new_plan_id", ""),
                    data.get("target_changed_count", ""),
                    data.get("added_count", ""),
                    data.get("removed_count", ""),
                    ", ".join(data.get("added_rules", [])),
                    ", ".join(data.get("removed_rules", [])),
                    ", ".join(data.get("modified_rules", [])),
                ])

            writer.writerow([])
            writer.writerow(["=== 批次快照 ==="])
            writer.writerow(["快照ID", "创建时间", "预案ID", "移动项数", "有冲突", "备注", "标签", "交接人", "注意事项", "备注更新时间", "备注更新人"])
            for sid, s in full_state.get("snapshots", {}).items():
                remark = s.get("remark", {})
                writer.writerow([
                    sid,
                    s.get("created_at", ""),
                    s.get("plan_id", ""),
                    len(s.get("moves", [])),
                    "是" if s.get("has_conflicts") else "否",
                    remark.get("remark", ""),
                    ", ".join(remark.get("tags", [])),
                    remark.get("handler", ""),
                    remark.get("notes", ""),
                    remark.get("updated_at", ""),
                    remark.get("updated_by", ""),
                ])

            writer.writerow([])
            writer.writerow(["=== 备注修改历史 ==="])
            writer.writerow(["历史ID", "快照ID", "修改时间", "修改人", "修改来源", "是否冲突", "冲突详情", "是否强制", "变化字段"])
            for rh in full_state.get("remark_histories", []):
                changed_fields = rh.get("changed_fields", [])
                field_summaries = []
                for fc in changed_fields:
                    fn = fc.get("field_name", "")
                    if fn == "tags":
                        old_set = set(fc.get("old_value", [])) if fc.get("old_value") else set()
                        new_set = set(fc.get("new_value", [])) if fc.get("new_value") else set()
                        added = sorted(new_set - old_set)
                        removed = sorted(old_set - new_set)
                        parts = []
                        if added:
                            parts.append(f"+{added}")
                        if removed:
                            parts.append(f"-{removed}")
                        field_summaries.append(f"标签: {'; '.join(parts)}")
                    elif fn == "remark":
                        field_summaries.append(f"正文: '{fc.get('old_value', '')}' -> '{fc.get('new_value', '')}'")
                    elif fn == "handler":
                        field_summaries.append(f"交接人: '{fc.get('old_value', '')}' -> '{fc.get('new_value', '')}'")
                    elif fn == "notes":
                        old_len = len(fc.get("old_value", "")) if fc.get("old_value") else 0
                        new_len = len(fc.get("new_value", "")) if fc.get("new_value") else 0
                        field_summaries.append(f"注意事项: 长度 {old_len}->{new_len}")
                    else:
                        field_summaries.append(f"{fn}: {fc.get('old_value', '')} -> {fc.get('new_value', '')}")
                writer.writerow([
                    rh.get("history_id", ""),
                    rh.get("snapshot_id", ""),
                    rh.get("changed_at", ""),
                    rh.get("changed_by", ""),
                    rh.get("change_source", ""),
                    "是" if rh.get("conflict_detected") else "否",
                    rh.get("conflict_detail", ""),
                    "是" if rh.get("forced") else "否",
                    "; ".join(field_summaries) if field_summaries else "无变化",
                ])

            writer.writerow([])
            writer.writerow(["=== 导入日志 ==="])
            writer.writerow(["导入ID", "时间", "状态", "快照ID", "预案ID", "移动项数", "源文件", "是否强制", "备注冲突", "错误", "警告"])
            for il in full_state.get("import_logs", []):
                writer.writerow([
                    il.get("import_id", ""),
                    il.get("timestamp", ""),
                    il.get("status", ""),
                    il.get("snapshot_id", ""),
                    il.get("plan_id", ""),
                    il.get("move_count", ""),
                    il.get("source_file", ""),
                    "是" if il.get("forced") else "否",
                    il.get("remark_conflict_detail", ""),
                    "; ".join(il.get("errors", [])) if il.get("errors") else "",
                    "; ".join(il.get("warnings", [])) if il.get("warnings") else "",
                ])

            writer.writerow([])
            writer.writerow(["=== 签收记录 ==="])
            writer.writerow(["签收ID", "快照ID", "预案ID", "状态", "签收人", "签收时间", "截止时间", "补充说明", "创建时间", "创建人", "是否强制", "是否活动", "被取代ID", "冲突详情", "冲突ID"])
            for sr in full_state.get("signoff_records", []):
                writer.writerow(export_signoff_to_csv_row(SignoffRecord.from_dict(sr)))

            writer.writerow([])
            writer.writerow(["=== 签收冲突记录 ==="])
            writer.writerow(["冲突ID", "快照ID", "预案ID", "状态", "本地签收ID", "导入签收ID", "检测时间", "导入来源", "差异字段", "差异详情", "解决时间", "解决人", "解决说明", "新签收ID"])
            for sc in full_state.get("signoff_conflicts", []):
                writer.writerow(export_signoff_conflict_to_csv_row(SignoffConflictState.from_dict(sc)))

            writer.writerow([])
            writer.writerow(["=== 签收校验历史 ==="])
            writer.writerow([
                "校验ID", "快照ID", "预案ID", "触发命令", "触发时间",
                "状态", "阻塞类型", "是否过期", "配置不一致", "快照已更新",
                "有未解决冲突", "锁定不一致", "活动签收ID", "冲突ID", "锁定ID",
                "错误信息", "警告信息", "解决时间", "解决人", "解决说明", "解决命令"
            ])
            block_type_cn = {
                "signoff_expired": "签收过期",
                "config_mismatch": "配置不一致",
                "unresolved_signoff_conflict": "未解决签收冲突",
                "lock_mismatch": "锁定快照不一致",
                "no_signoff": "未签收",
                "not_signed": "非已签收状态",
                "conflicting_signoffs": "冲突的签收记录",
                "snapshot_replaced": "快照已更新",
            }
            for vh in full_state.get("validation_history", []):
                block_types = vh.get("block_types", [])
                block_types_cn = "; ".join([block_type_cn.get(bt, bt) for bt in block_types])
                status_cn = "通过" if vh.get("status") == "passed" else "阻塞"
                writer.writerow([
                    vh.get("validation_id", ""),
                    vh.get("snapshot_id", ""),
                    vh.get("plan_id", ""),
                    vh.get("triggered_by", ""),
                    vh.get("triggered_at", ""),
                    status_cn,
                    block_types_cn,
                    "是" if vh.get("is_expired") else "否",
                    "是" if vh.get("config_mismatch") else "否",
                    "是" if vh.get("snapshot_replaced") else "否",
                    "是" if vh.get("has_unresolved_conflict") else "否",
                    "是" if vh.get("has_lock_mismatch") else "否",
                    vh.get("active_signoff_id", ""),
                    vh.get("conflict_id", ""),
                    vh.get("lock_id", ""),
                    "; ".join(vh.get("errors", [])) if vh.get("errors") else "",
                    "; ".join(vh.get("warnings", [])) if vh.get("warnings") else "",
                    vh.get("resolved_at", ""),
                    vh.get("resolved_by", ""),
                    vh.get("resolution_note", ""),
                    vh.get("resolution_command", ""),
                ])

            writer.writerow([])
            writer.writerow(["=== 执行批次归档包 ==="])
            writer.writerow([
                "Bundle ID", "归档包版本", "创建时间",
                "Run ID", "预案ID", "快照ID",
                "总记录数", "成功数", "冲突跳过数", "人工跳过数", "失败数",
                "是否预演", "是否撤销",
                "签收状态", "签收人", "签收ID",
                "校验和", "是否导入", "导入来源", "导入时间"
            ])
            bundles_dict = full_state.get("execution_bundles", {})
            bundle_items = sorted(
                bundles_dict.items(),
                key=lambda kv: kv[1].get("created_at", ""),
                reverse=True,
            )
            for bundle_id, bd in bundle_items:
                summary = bd.get("summary", {})
                run_details = bd.get("run_details", {})
                writer.writerow([
                    bd.get("bundle_id", bundle_id),
                    bd.get("bundle_version", ""),
                    bd.get("created_at", ""),
                    bd.get("run_id", ""),
                    bd.get("plan_id", ""),
                    bd.get("snapshot_id", ""),
                    summary.get("total_moves", 0),
                    summary.get("success_count", 0),
                    summary.get("skipped_conflict_count", 0),
                    summary.get("skipped_manual_count", 0),
                    summary.get("failed_count", 0),
                    "是" if summary.get("dry_run") else "否",
                    "是" if summary.get("is_undone") else "否",
                    summary.get("signoff_status", ""),
                    summary.get("signed_by", ""),
                    summary.get("signoff_id", ""),
                    bd.get("checksum", ""),
                    "是" if bd.get("imported") else "否",
                    bd.get("import_source", ""),
                    bd.get("imported_at", ""),
                ])

            writer.writerow([])
            writer.writerow(["=== 归档包导入日志 ==="])
            writer.writerow([
                "导入日志ID", "Bundle ID", "Run ID", "快照ID",
                "导入时间", "状态", "源文件", "是否强制导入", "导入人",
                "错误详情", "警告详情", "冲突类型"
            ])
            import_logs = full_state.get("bundle_import_logs", [])
            for il in import_logs:
                status_cn = {
                    "success": "成功",
                    "failed": "失败",
                    "skipped": "跳过",
                    "forced": "强制",
                }.get(il.get("status", ""), il.get("status", ""))
                writer.writerow([
                    il.get("import_log_id", ""),
                    il.get("bundle_id", ""),
                    il.get("run_id", ""),
                    il.get("snapshot_id", ""),
                    il.get("timestamp", ""),
                    status_cn,
                    il.get("source_file", ""),
                    "是" if il.get("forced") else "否",
                    il.get("imported_by", ""),
                    "; ".join(il.get("errors", [])) if il.get("errors") else "",
                    "; ".join(il.get("warnings", [])) if il.get("warnings") else "",
                    "; ".join(il.get("conflict_types", [])) if il.get("conflict_types") else "",
                ])

            writer.writerow([])
            writer.writerow(["=== 落点指纹清单 ==="])
            writer.writerow([
                "Landing ID", "创建时间", "Run ID", "快照ID", "预案ID",
                "目标根目录", "源目录",
                "成功移动数", "冲突跳过数", "人工跳过数", "失败数",
                "目标目录数", "文件指纹数", "手工改名数",
                "是否预演", "是否撤销", "撤销时间",
                "状态", "校验和", "是否导入", "导入来源", "导入时间", "签收ID"
            ])
            landings_dict = full_state.get("landings", {})
            landing_items = sorted(
                landings_dict.items(),
                key=lambda kv: kv[1].get("created_at", ""),
                reverse=True,
            )
            for landing_id, ld in landing_items:
                writer.writerow([
                    ld.get("landing_id", landing_id),
                    ld.get("created_at", ""),
                    ld.get("run_id", ""),
                    ld.get("snapshot_id", ""),
                    ld.get("plan_id", ""),
                    ld.get("dest_dir", ""),
                    ld.get("source_dir", ""),
                    ld.get("total_moved_count", 0),
                    ld.get("total_skipped_conflict_count", 0),
                    ld.get("total_skipped_manual_count", 0),
                    ld.get("total_failed_count", 0),
                    len(ld.get("target_dirs", [])),
                    len(ld.get("file_fingerprints", [])),
                    len(ld.get("manual_renames", [])),
                    "是" if ld.get("is_dry_run") else "否",
                    "是" if ld.get("is_undone") else "否",
                    ld.get("undone_at", ""),
                    ld.get("status", ""),
                    ld.get("checksum", ""),
                    "是" if ld.get("imported") else "否",
                    ld.get("import_source", ""),
                    ld.get("imported_at", ""),
                    ld.get("signoff_id", ""),
                ])

            writer.writerow([])
            writer.writerow(["=== 落点指纹-目标目录明细 ==="])
            writer.writerow([
                "Landing ID", "目标目录", "目录内文件数", "目录路径摘要哈希"
            ])
            for landing_id, ld in landing_items:
                for td in ld.get("target_dirs", []):
                    writer.writerow([
                        landing_id,
                        td.get("target_dir", ""),
                        td.get("file_count", 0),
                        td.get("dir_path_digest", ""),
                    ])

            writer.writerow([])
            writer.writerow(["=== 落点指纹-文件明细 ==="])
            writer.writerow([
                "Landing ID", "指纹ID", "文件名", "源路径", "目标路径",
                "匹配规则", "文件大小", "修改时间", "内容摘要哈希"
            ])
            for landing_id, ld in landing_items:
                for fp in ld.get("file_fingerprints", []):
                    writer.writerow([
                        landing_id,
                        fp.get("fingerprint_id", ""),
                        fp.get("filename", ""),
                        fp.get("source_path", ""),
                        fp.get("target_path", ""),
                        fp.get("matched_rule", ""),
                        fp.get("file_size", 0),
                        fp.get("mtime", 0),
                        fp.get("content_digest", ""),
                    ])

            writer.writerow([])
            writer.writerow(["=== 落点指纹-手工改名明细 ==="])
            writer.writerow([
                "Landing ID", "改名ID", "原始目标路径", "最终目标路径",
                "改名原因", "改名时间", "改名人"
            ])
            for landing_id, ld in landing_items:
                for mr in ld.get("manual_renames", []):
                    writer.writerow([
                        landing_id,
                        mr.get("rename_id", ""),
                        mr.get("original_target_path", ""),
                        mr.get("final_target_path", ""),
                        mr.get("rename_reason", ""),
                        mr.get("renamed_at", ""),
                        mr.get("renamed_by", ""),
                    ])

            writer.writerow([])
            writer.writerow(["=== 落点指纹导入日志 ==="])
            writer.writerow([
                "导入日志ID", "Landing ID", "Run ID", "快照ID",
                "导入时间", "状态", "源文件", "是否强制导入", "导入人",
                "错误详情", "警告详情", "冲突类型"
            ])
            landing_import_logs = full_state.get("landing_import_logs", [])
            for lil in landing_import_logs:
                status_cn = {
                    "success": "成功",
                    "failed": "失败",
                    "skipped": "跳过",
                    "forced": "强制",
                }.get(lil.get("status", ""), lil.get("status", ""))
                writer.writerow([
                    lil.get("import_log_id", ""),
                    lil.get("landing_id", ""),
                    lil.get("run_id", ""),
                    lil.get("snapshot_id", ""),
                    lil.get("timestamp", ""),
                    status_cn,
                    lil.get("source_file", ""),
                    "是" if lil.get("forced") else "否",
                    lil.get("imported_by", ""),
                    "; ".join(lil.get("errors", [])) if lil.get("errors") else "",
                    "; ".join(lil.get("warnings", [])) if lil.get("warnings") else "",
                    "; ".join(lil.get("conflict_details", [])) if lil.get("conflict_details") else "",
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
    remark: Optional[SnapshotRemark] = None,
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
        remark=remark or SnapshotRemark(),
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


# ============================================================
# 快照备注相关功能
# ============================================================

MAX_REMARK_LENGTH = 500
MAX_HANDLER_LENGTH = 50
MAX_NOTES_LENGTH = 1000
MAX_TAG_LENGTH = 30
MAX_TAGS_COUNT = 10


def diff_remarks(old_remark: SnapshotRemark, new_remark: SnapshotRemark) -> List[RemarkFieldChange]:
    """
    字段级对比两个备注对象，返回各字段变化明细

    对比字段：remark(正文)、tags(标签)、handler(交接人)、notes(注意事项)
    """
    changes: List[RemarkFieldChange] = []

    if old_remark.remark != new_remark.remark:
        changes.append(RemarkFieldChange(
            field_name="remark",
            old_value=old_remark.remark,
            new_value=new_remark.remark,
        ))

    old_tags = list(old_remark.tags)
    new_tags = list(new_remark.tags)
    if old_tags != new_tags:
        changes.append(RemarkFieldChange(
            field_name="tags",
            old_value=old_tags,
            new_value=new_tags,
        ))

    if old_remark.handler != new_remark.handler:
        changes.append(RemarkFieldChange(
            field_name="handler",
            old_value=old_remark.handler,
            new_value=new_remark.handler,
        ))

    if old_remark.notes != new_remark.notes:
        changes.append(RemarkFieldChange(
            field_name="notes",
            old_value=old_remark.notes,
            new_value=new_remark.notes,
        ))

    return changes


def format_remark_change(fc: RemarkFieldChange) -> str:
    """将单字段变化格式化为可读字符串"""
    if fc.field_name == "tags":
        old_set = set(fc.old_value) if fc.old_value else set()
        new_set = set(fc.new_value) if fc.new_value else set()
        added = sorted(new_set - old_set)
        removed = sorted(old_set - new_set)
        parts = []
        if added:
            parts.append(f"+{added}")
        if removed:
            parts.append(f"-{removed}")
        return f"标签: {'; '.join(parts)}" if parts else "标签: 无实质变化"
    elif fc.field_name == "remark":
        old_short = (fc.old_value[:40] + "...") if fc.old_value and len(fc.old_value) > 40 else (fc.old_value or "(空)")
        new_short = (fc.new_value[:40] + "...") if fc.new_value and len(fc.new_value) > 40 else (fc.new_value or "(空)")
        return f"正文: '{old_short}' -> '{new_short}'"
    elif fc.field_name == "handler":
        return f"交接人: '{fc.old_value or '(空)'}' -> '{fc.new_value or '(空)'}'"
    elif fc.field_name == "notes":
        old_len = len(fc.old_value) if fc.old_value else 0
        new_len = len(fc.new_value) if fc.new_value else 0
        return f"注意事项: 长度 {old_len} -> {new_len}"
    return f"{fc.field_name}: {fc.old_value} -> {fc.new_value}"


def validate_remark(remark: SnapshotRemark) -> RemarkValidationResult:
    """
    验证备注信息的合法性

    检查项：
    - 备注长度限制
    - 交接人长度限制
    - 注意事项长度限制
    - 标签长度和数量限制
    - 标签重复检测
    """
    errors: List[str] = []
    warnings: List[str] = []

    if len(remark.remark) > MAX_REMARK_LENGTH:
        errors.append(
            f"备注内容过长: {len(remark.remark)} 字符，最大允许 {MAX_REMARK_LENGTH} 字符"
        )

    if len(remark.handler) > MAX_HANDLER_LENGTH:
        errors.append(
            f"交接人过长: {len(remark.handler)} 字符，最大允许 {MAX_HANDLER_LENGTH} 字符"
        )

    if len(remark.notes) > MAX_NOTES_LENGTH:
        errors.append(
            f"注意事项过长: {len(remark.notes)} 字符，最大允许 {MAX_NOTES_LENGTH} 字符"
        )

    if len(remark.tags) > MAX_TAGS_COUNT:
        errors.append(
            f"标签数量过多: {len(remark.tags)} 个，最大允许 {MAX_TAGS_COUNT} 个"
        )

    seen_tags = set()
    duplicate_tags = []
    for tag in remark.tags:
        if len(tag) > MAX_TAG_LENGTH:
            errors.append(
                f"标签过长: '{tag}' ({len(tag)} 字符)，最大允许 {MAX_TAG_LENGTH} 字符"
            )
        if not tag.strip():
            errors.append("标签不能为空字符串")
        if tag in seen_tags:
            duplicate_tags.append(tag)
        seen_tags.add(tag)

    if duplicate_tags:
        errors.append(f"标签重复: {', '.join(set(duplicate_tags))}")

    if not remark.remark and not remark.tags and not remark.handler and not remark.notes:
        warnings.append("未提供任何备注信息")

    valid = len(errors) == 0
    return RemarkValidationResult(valid=valid, errors=errors, warnings=warnings)


def build_remark(
    remark: Optional[str] = None,
    tags: Optional[List[str]] = None,
    handler: Optional[str] = None,
    notes: Optional[str] = None,
    updated_by: str = "cli",
) -> SnapshotRemark:
    """构建备注对象并设置更新时间"""
    return SnapshotRemark(
        remark=remark or "",
        tags=list(tags) if tags else [],
        handler=handler or "",
        notes=notes or "",
        updated_at=now_iso(),
        updated_by=updated_by,
    )


# ============================================================
# 签收相关功能
# ============================================================

from .models import (
    SignoffRecord, SignoffValidationResult, SignoffFieldChange,
    MAX_SIGNOFF_NOTES_LENGTH, MAX_SIGNOFF_BY_LENGTH,
)


def validate_signoff(signoff: SignoffRecord) -> SignoffValidationResult:
    """
    验证签收信息的合法性

    检查项：
    - 签收人长度限制
    - 补充说明长度限制
    - 状态值合法性
    - 截止时间格式合法性
    """
    errors: List[str] = []
    warnings: List[str] = []

    if len(signoff.signed_by) > MAX_SIGNOFF_BY_LENGTH:
        errors.append(
            f"签收人过长: {len(signoff.signed_by)} 字符，最大允许 {MAX_SIGNOFF_BY_LENGTH} 字符"
        )

    if len(signoff.notes) > MAX_SIGNOFF_NOTES_LENGTH:
        errors.append(
            f"补充说明过长: {len(signoff.notes)} 字符，最大允许 {MAX_SIGNOFF_NOTES_LENGTH} 字符"
        )

    if signoff.status not in ["signed", "rejected", "pending"]:
        errors.append(f"无效的签收状态: {signoff.status}，必须是 signed/rejected/pending")

    if signoff.deadline:
        try:
            from datetime import datetime
            datetime.fromisoformat(signoff.deadline.replace('Z', '+00:00'))
        except (ValueError, TypeError):
            errors.append(f"截止时间格式无效: {signoff.deadline}，请使用 ISO 格式")

    if not signoff.signed_by.strip():
        errors.append("签收人不能为空")

    valid = len(errors) == 0
    return SignoffValidationResult(valid=valid, errors=errors, warnings=warnings)


def build_signoff(
    snapshot_id: str,
    plan_id: str,
    status: str,
    signed_by: str,
    deadline: Optional[str] = None,
    notes: Optional[str] = None,
    config_snapshot: Optional[Dict[str, Any]] = None,
    created_by: str = "cli",
) -> SignoffRecord:
    """构建签收记录并设置时间戳"""
    return SignoffRecord(
        signoff_id=generate_id(),
        snapshot_id=snapshot_id,
        plan_id=plan_id,
        status=status,
        signed_by=signed_by,
        signed_at=now_iso(),
        deadline=deadline or "",
        notes=notes or "",
        config_snapshot=config_snapshot,
        created_at=now_iso(),
        created_by=created_by,
    )


def diff_signoffs(old_signoff: Optional[SignoffRecord], new_signoff: SignoffRecord) -> List[SignoffFieldChange]:
    """
    字段级对比两个签收记录，返回各字段变化明细
    """
    changes: List[SignoffFieldChange] = []

    old_dict = old_signoff.to_dict() if old_signoff else {}

    new_dict = new_signoff.to_dict()

    compare_fields = ["status", "signed_by", "deadline", "notes"]

    for field in compare_fields:
        old_val = old_dict.get(field, "")
        new_val = new_dict.get(field, "")
        if old_val != new_val:
            changes.append(SignoffFieldChange(
                field_name=field,
                old_value=old_val,
                new_value=new_val,
            ))

    return changes


def format_signoff_change(fc: SignoffFieldChange) -> str:
    """将签收单字段变化格式化为可读字符串"""
    field_names = {
        "status": "状态",
        "signed_by": "签收人",
        "deadline": "截止时间",
        "notes": "补充说明",
    }
    display_name = field_names.get(fc.field_name, fc.field_name)
    old_val = fc.old_value or "(空)"
    new_val = fc.new_value or "(空)"

    if fc.field_name == "status":
        status_map = {"signed": "已签收", "rejected": "已拒绝", "pending": "待处理"}
        old_val = status_map.get(str(old_val), str(old_val))
        new_val = status_map.get(str(new_val), str(new_val))
    elif fc.field_name == "notes":
        old_len = len(str(old_val)) if old_val else 0
        new_len = len(str(new_val)) if new_val else 0
        return f"{display_name}: 长度 {old_len} -> {new_len}"

    return f"{display_name}: '{old_val}' -> '{new_val}'"


def check_signoff_expired(signoff: SignoffRecord) -> bool:
    """检查签收是否已过期"""
    if not signoff.deadline:
        return False

    try:
        from datetime import datetime
        deadline = datetime.fromisoformat(signoff.deadline.replace('Z', '+00:00'))
        now = datetime.now()
        return now > deadline
    except (ValueError, TypeError):
        return False


def validate_signoff_for_apply(
    store: StateStore,
    snapshot: BatchSnapshot,
    current_config: Optional[Config] = None,
    require_signed: bool = True,
) -> SignoffValidationResult:
    """
    执行 apply 前的签收状态校验

    检查项：
    1. 是否有有效的签收记录
    2. 签收是否已过期
    3. 当前配置与签收时配置是否一致
    4. 快照是否已被新版本替代
    5. 是否存在冲突的签收记录

    返回：校验结果
    """
    from .models import Config as ConfigModel

    errors: List[str] = []
    warnings: List[str] = []
    is_expired = False
    config_mismatch = False
    snapshot_replaced = False
    conflicting_signoffs: List[str] = []
    unresolved_conflict: Optional[SignoffConflictState] = None

    unresolved_conflict = has_unresolved_signoff_conflict(store, snapshot.snapshot_id)

    if unresolved_conflict:
        resolution_hint = (
            f"请使用 resolve-signoff-conflict 命令处理冲突：\n"
            f"       python -m invoice_organizer resolve-signoff-conflict "
            f"-c <config> --snapshot-id {snapshot.snapshot_id} "
            f"--resolution <keep-local|keep-imported|new-signoff>"
        )
        errors.append(
            f"快照 {snapshot.snapshot_id} 存在未解决的签收冲突！"
            f"冲突ID: {unresolved_conflict.conflict_id}，"
            f"差异: {unresolved_conflict.conflict_summary}。\n"
            f"  {resolution_hint}"
        )

    active_signoff = store.get_active_signoff(snapshot.snapshot_id)

    if not active_signoff:
        if require_signed:
            errors.append(f"快照 {snapshot.snapshot_id} 没有有效的签收记录，请先执行 sign-off 命令签收")
        return SignoffValidationResult(
            valid=not require_signed and not unresolved_conflict,
            errors=errors,
            warnings=warnings,
            is_expired=False,
            config_mismatch=False,
            snapshot_replaced=False,
            conflicting_signoffs=[],
            active_signoff=None,
            unresolved_conflict=unresolved_conflict,
        )

    if require_signed and active_signoff.status != "signed":
        status_map = {"rejected": "已拒绝", "pending": "待处理"}
        errors.append(
            f"快照 {snapshot.snapshot_id} 签收状态为「{status_map.get(active_signoff.status, active_signoff.status)}」，"
            f"需要「已签收」状态才能执行"
        )

    if check_signoff_expired(active_signoff):
        is_expired = True
        errors.append(
            f"签收已过期！截止时间: {active_signoff.deadline}，请重新签收或延长截止时间"
        )

    if current_config and active_signoff.config_snapshot:
        from .models import ConfigSnapshot as ConfigSnapshotModel
        signoff_config = ConfigSnapshotModel.from_dict(active_signoff.config_snapshot)
        diff_result = diff_config(current_config, signoff_config)
        if diff_result.has_diff:
            config_mismatch = True
            detail_parts = []
            if diff_result.source_dir_changed:
                detail_parts.append(f"源目录: {signoff_config.source_dir} -> {current_config.source_dir}")
            if diff_result.dest_dir_changed:
                detail_parts.append(f"目标目录: {signoff_config.dest_dir} -> {current_config.dest_dir}")
            if diff_result.added_rules:
                detail_parts.append(f"新增规则: {', '.join(diff_result.added_rules)}")
            if diff_result.removed_rules:
                detail_parts.append(f"删除规则: {', '.join(diff_result.removed_rules)}")
            if diff_result.modified_rules:
                current_rule_map = {r.name: r for r in current_config.rules}
                snapshot_rule_map = {r["name"]: r for r in signoff_config.rules}
                modified_details = []
                for rule_name in diff_result.modified_rules:
                    curr = current_rule_map.get(rule_name)
                    snap = snapshot_rule_map.get(rule_name)
                    if curr and snap:
                        changes = []
                        if curr.pattern != snap.get("pattern"):
                            changes.append(f"pattern: {snap.get('pattern')} -> {curr.pattern}")
                        if curr.target != snap.get("target"):
                            changes.append(f"target: {snap.get('target')} -> {curr.target}")
                        if curr.description != snap.get("description"):
                            changes.append(f"description: {snap.get('description')} -> {curr.description}")
                        if changes:
                            modified_details.append(f"{rule_name}({', '.join(changes)})")
                        else:
                            modified_details.append(rule_name)
                    else:
                        modified_details.append(rule_name)
                detail_parts.append(f"修改规则: {', '.join(modified_details)}")
            errors.append(
                f"当前配置与签收时不一致！变更: {'; '.join(detail_parts)}"
            )

    all_snapshots = store.list_snapshots()
    newer_snapshots = [
        s for s in all_snapshots
        if s["created_at"] > snapshot.created_at and s["snapshot_id"] != snapshot.snapshot_id
    ]
    if newer_snapshots:
        snapshot_replaced = True
        newer_ids = ", ".join(s["snapshot_id"] for s in newer_snapshots)
        warnings.append(
            f"该快照已被新版本替代，更新的快照: {newer_ids}"
        )

    if active_signoff.forced and active_signoff.conflict_detail and active_signoff.import_source:
        errors.append(
            f"存在未解决的签收冲突！该签收为强制导入，冲突原因: {active_signoff.conflict_detail}"
        )

    all_signoffs = store.get_signoffs_by_snapshot(snapshot.snapshot_id)
    other_active = [
        s for s in all_signoffs
        if s.is_active and s.signoff_id != active_signoff.signoff_id
    ]
    if other_active:
        for s in other_active:
            conflicting_signoffs.append(
                f"ID: {s.signoff_id}, 签收人: {s.signed_by}, 状态: {s.status}"
            )
        errors.append(
            f"存在 {len(other_active)} 条冲突的签收记录，请确认使用哪一份"
        )

    valid = len(errors) == 0

    return SignoffValidationResult(
        valid=valid,
        errors=errors,
        warnings=warnings,
        is_expired=is_expired,
        config_mismatch=config_mismatch,
        snapshot_replaced=snapshot_replaced,
        conflicting_signoffs=conflicting_signoffs,
        active_signoff=active_signoff,
        unresolved_conflict=unresolved_conflict,
    )


def config_snapshot_to_dict(config: Config, config_path: Optional[str] = None) -> Dict[str, Any]:
    """将当前配置转换为字典（用于签收时记录配置快照）"""
    return create_config_snapshot(config, config_path).to_dict()


def export_signoff_to_csv_row(signoff: SignoffRecord) -> List[str]:
    """将签收记录转换为 CSV 行"""
    status_map = {"signed": "已签收", "rejected": "已拒绝", "pending": "待处理"}
    return [
        signoff.signoff_id,
        signoff.snapshot_id,
        signoff.plan_id,
        status_map.get(signoff.status, signoff.status),
        signoff.signed_by,
        signoff.signed_at,
        signoff.deadline,
        signoff.notes,
        signoff.created_at,
        signoff.created_by,
        "是" if signoff.forced else "否",
        "活动" if signoff.is_active else "已失效",
        signoff.superseded_by or "",
        signoff.conflict_detail,
        signoff.conflict_id or "",
    ]


# ============================================================
# 签收冲突检测与处理相关功能
# ============================================================

SIGNOFF_COMPARE_FIELDS = ["signed_by", "status", "notes", "deadline"]


def detect_and_create_signoff_conflict(
    store: StateStore,
    snapshot_id: str,
    plan_id: str,
    local_signoff: SignoffRecord,
    imported_signoff: SignoffRecord,
    import_source: str = "",
) -> SignoffConflictState:
    """检测签收冲突并创建冲突状态记录

    比对本地活跃签收和导入签收，发现不一致时创建 SignoffConflictState，
    并将 conflict_id 写入两条签收记录。

    返回创建的冲突状态对象。
    """
    field_changes = diff_signoffs(local_signoff, imported_signoff)
    diff_fields = [fc.field_name for fc in field_changes]

    summary_parts = []
    for fc in field_changes:
        old_v = fc.old_value or "(空)"
        new_v = fc.new_value or "(空)"
        if fc.field_name == "status":
            smap = {"signed": "已签收", "rejected": "已拒绝", "pending": "待处理"}
            old_v = smap.get(str(old_v), str(old_v))
            new_v = smap.get(str(new_v), str(new_v))
        if fc.field_name == "notes":
            old_v = (str(old_v)[:30] + "...") if len(str(old_v)) > 30 else old_v
            new_v = (str(new_v)[:30] + "...") if len(str(new_v)) > 30 else new_v
        field_cn = {
            "signed_by": "签收人", "status": "状态",
            "notes": "说明", "deadline": "截止时间"
        }.get(fc.field_name, fc.field_name)
        summary_parts.append(f"{field_cn}: '{old_v}' -> '{new_v}'")

    conflict_summary = "; ".join(summary_parts) if summary_parts else "未知字段差异"

    conflict = SignoffConflictState(
        conflict_id=generate_id(),
        snapshot_id=snapshot_id,
        plan_id=plan_id,
        status="pending",
        local_signoff_id=local_signoff.signoff_id,
        imported_signoff_id=imported_signoff.signoff_id,
        detected_at=now_iso(),
        import_source=import_source,
        diff_fields=diff_fields,
        conflict_summary=conflict_summary,
    )

    store.save_signoff_conflict(conflict)

    local_signoff.conflict_id = conflict.conflict_id
    imported_signoff.conflict_id = conflict.conflict_id

    for s in store._data.get("signoff_records", []):
        if s.get("signoff_id") == local_signoff.signoff_id:
            s["conflict_id"] = conflict.conflict_id
        if s.get("signoff_id") == imported_signoff.signoff_id:
            s["conflict_id"] = conflict.conflict_id
    store.save()

    return conflict


def format_signoff_conflict_summary(conflict: SignoffConflictState) -> str:
    """将冲突状态格式化为 CLI 可读的多行字符串"""
    status_map = {
        "pending": "待处理",
        "resolved_keep_local": "已解决（保留本地）",
        "resolved_keep_imported": "已解决（保留导入）",
        "resolved_new": "已解决（新建签收）",
    }
    lines = []
    lines.append(f"冲突 ID: {conflict.conflict_id}")
    lines.append(f"状态: {status_map.get(conflict.status, conflict.status)}")
    lines.append(f"快照 ID: {conflict.snapshot_id}")
    lines.append(f"预案 ID: {conflict.plan_id}")
    lines.append(f"检测时间: {conflict.detected_at}")
    if conflict.import_source:
        lines.append(f"导入来源: {conflict.import_source}")
    if conflict.diff_fields:
        field_cn_map = {
            "signed_by": "签收人", "status": "状态",
            "notes": "说明", "deadline": "截止时间"
        }
        cn_fields = [field_cn_map.get(f, f) for f in conflict.diff_fields]
        lines.append(f"差异字段: {', '.join(cn_fields)}")
    if conflict.conflict_summary:
        lines.append(f"差异详情: {conflict.conflict_summary}")
    lines.append(f"本地签收 ID: {conflict.local_signoff_id}")
    lines.append(f"导入签收 ID: {conflict.imported_signoff_id}")
    if conflict.is_resolved:
        lines.append(f"解决时间: {conflict.resolved_at}")
        lines.append(f"解决人: {conflict.resolved_by}")
        if conflict.resolution_note:
            lines.append(f"解决说明: {conflict.resolution_note}")
        if conflict.new_signoff_id:
            lines.append(f"新签收 ID: {conflict.new_signoff_id}")
    return "\n".join(lines)


def export_signoff_conflict_to_csv_row(conflict: SignoffConflictState) -> List[str]:
    """将签收冲突状态转换为 CSV 行"""
    status_map = {
        "pending": "待处理",
        "resolved_keep_local": "已解决（保留本地）",
        "resolved_keep_imported": "已解决（保留导入）",
        "resolved_new": "已解决（新建签收）",
    }
    field_cn_map = {
        "signed_by": "签收人", "status": "状态",
        "notes": "说明", "deadline": "截止时间"
    }
    cn_fields = [field_cn_map.get(f, f) for f in conflict.diff_fields]
    return [
        conflict.conflict_id,
        conflict.snapshot_id,
        conflict.plan_id,
        status_map.get(conflict.status, conflict.status),
        conflict.local_signoff_id,
        conflict.imported_signoff_id,
        conflict.detected_at,
        conflict.import_source,
        ", ".join(cn_fields),
        conflict.conflict_summary,
        conflict.resolved_at or "",
        conflict.resolved_by or "",
        conflict.resolution_note or "",
        conflict.new_signoff_id or "",
    ]


def has_unresolved_signoff_conflict(store: StateStore, snapshot_id: str) -> Optional[SignoffConflictState]:
    """检查指定快照是否有未解决的签收冲突

    返回冲突状态对象，如果没有则返回 None
    """
    return store.get_pending_conflict_by_snapshot(snapshot_id)


def save_validation_history(
    store: StateStore,
    snapshot: BatchSnapshot,
    signoff_validation: SignoffValidationResult,
    triggered_by: str,
    lock_allowed: bool = True,
    lock_reject_reason: Optional[str] = None,
    active_lock_snapshot_id: Optional[str] = None,
) -> SignoffValidationHistory:
    """
    持久化签收校验结果，支持重启后回看和导出复核。

    Args:
        store: 状态存储
        snapshot: 当前快照
        signoff_validation: 签收校验结果
        triggered_by: 触发命令 ("check-signoff" | "apply-dry-run" | "apply")
        lock_allowed: 锁定检查是否通过
        lock_reject_reason: 锁定拒绝原因
        active_lock_snapshot_id: 当前锁定的快照 ID

    Returns:
        已持久化的校验历史记录
    """
    block_types: List[str] = []
    has_unresolved_conflict = False
    has_lock_mismatch = not lock_allowed
    conflict_id: Optional[str] = None
    lock_id: Optional[str] = None
    active_signoff_id: Optional[str] = None

    if signoff_validation.unresolved_conflict:
        has_unresolved_conflict = True
        conflict_id = signoff_validation.unresolved_conflict.conflict_id
        block_types.append("unresolved_signoff_conflict")

    if signoff_validation.active_signoff:
        active_signoff_id = signoff_validation.active_signoff.signoff_id

    if signoff_validation.is_expired:
        block_types.append("signoff_expired")

    if signoff_validation.config_mismatch:
        block_types.append("config_mismatch")

    if signoff_validation.snapshot_replaced:
        block_types.append("snapshot_replaced")

    if signoff_validation.conflicting_signoffs:
        block_types.append("conflicting_signoffs")

    if not signoff_validation.active_signoff:
        block_types.append("no_signoff")
    elif signoff_validation.active_signoff.status != "signed":
        block_types.append("not_signed")

    if has_lock_mismatch:
        block_types.append("lock_mismatch")
        active_lock = store.get_active_lock()
        if active_lock:
            lock_id = active_lock.lock_id

    status = "passed" if signoff_validation.valid and lock_allowed else "blocked"

    record = SignoffValidationHistory(
        validation_id=generate_id(),
        snapshot_id=snapshot.snapshot_id,
        plan_id=snapshot.plan_id,
        triggered_by=triggered_by,
        triggered_at=now_iso(),
        status=status,
        block_types=block_types,
        errors=list(signoff_validation.errors),
        warnings=list(signoff_validation.warnings),
        is_expired=signoff_validation.is_expired,
        config_mismatch=signoff_validation.config_mismatch,
        snapshot_replaced=signoff_validation.snapshot_replaced,
        has_unresolved_conflict=has_unresolved_conflict,
        has_lock_mismatch=has_lock_mismatch,
        active_signoff_id=active_signoff_id,
        conflict_id=conflict_id,
        lock_id=lock_id,
    )

    if has_lock_mismatch and lock_reject_reason:
        if lock_reject_reason not in record.errors:
            record.errors.append(lock_reject_reason)

    store.add_validation_history(record)
    return record


# ============================================================
# 执行批次归档包相关功能
# ============================================================

from .models import (
    ExecutionBundle, BundleSummary, BundleRunDetails,
    BundleValidationResult, BundleImportLog,
    BUNDLE_VERSION, BUNDLE_REQUIRED_FIELDS,
)


def _compute_bundle_checksum(bundle_dict: Dict[str, Any]) -> str:
    """计算归档包内容的简单校验和（用于完整性检测）"""
    import hashlib
    relevant = {
        "bundle_id": bundle_dict.get("bundle_id"),
        "run_id": bundle_dict.get("run_id"),
        "plan_id": bundle_dict.get("plan_id"),
        "snapshot_id": bundle_dict.get("snapshot_id"),
        "created_at": bundle_dict.get("created_at"),
        "summary": bundle_dict.get("summary"),
        "moves": [
            {"filename": m.get("filename"), "status": m.get("status")}
            for m in bundle_dict.get("run_details", {}).get("moves", [])
        ],
    }
    raw = json.dumps(relevant, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def create_execution_bundle(
    store: StateStore,
    run_id: str,
    filter_rules: Optional[List[str]] = None,
    filter_file_types: Optional[List[str]] = None,
    filter_target_dirs: Optional[List[str]] = None,
) -> ExecutionBundle:
    """
    根据执行记录创建完整的执行批次归档包

    归档包包含：
    - 批次快照
    - 签收信息
    - 校验结果
    - 移动明细
    - 冲突和人工跳过原因
    """
    run = store.get_run(run_id)
    if not run:
        raise ValueError(f"执行记录不存在: {run_id}")

    plan_id = run.get("plan_id", "")
    snapshot_id = run.get("snapshot_id", "")
    if not snapshot_id:
        snapshot = store.get_snapshot_by_plan_id(plan_id)
    else:
        snapshot = store.get_snapshot(snapshot_id)
    if not snapshot:
        raise ValueError(f"找不到与执行 {run_id} 关联的快照")
    snapshot_id = snapshot.snapshot_id
    plan_id = snapshot.plan_id

    all_signoffs = store.get_signoffs_by_snapshot(snapshot_id)
    all_signoff_conflicts = store.list_signoff_conflicts(snapshot_id=snapshot_id)
    validation_history = store.get_validation_history(snapshot_id=snapshot_id, limit=50)

    move_data = run.get("moves", [])
    success_count = sum(1 for m in move_data if m.get("status") == "moved")
    skipped_conflict_count = sum(1 for m in move_data if m.get("status") == "skipped_conflict")
    skipped_manual_count = sum(1 for m in move_data if m.get("status") == "skipped_manual")
    failed_count = sum(1 for m in move_data if m.get("status") == "failed")

    conflict_details = [
        f"{m.get('filename')}: {m.get('error_message', '')}"
        for m in move_data if m.get("status") == "skipped_conflict"
    ]
    manual_skip_reasons = [
        f"{m.get('filename')}: {m.get('error_message', '')}"
        for m in move_data if m.get("status") == "skipped_manual"
    ]

    active_signoff = store.get_active_signoff(snapshot_id)
    run_signoff_id = run.get("signoff_id", "")
    if run_signoff_id:
        signoff_for_run = store.get_signoff(run_signoff_id)
    else:
        signoff_for_run = active_signoff

    has_signoff = signoff_for_run is not None
    if has_signoff and signoff_for_run:
        signoff_status_display = {
            "signed": "已签收", "rejected": "已拒绝", "pending": "待处理"
        }.get(signoff_for_run.status, signoff_for_run.status)
        signed_by_display = signoff_for_run.signed_by
        signoff_id_display = signoff_for_run.signoff_id
    else:
        signoff_status_display = ""
        signed_by_display = ""
        signoff_id_display = ""

    undo_records_for_run = [
        u.to_dict() for u in store.get_undo_records()
        if u.run_id == run_id
    ]

    summary = BundleSummary(
        total_moves=len(move_data),
        success_count=success_count,
        skipped_conflict_count=skipped_conflict_count,
        skipped_manual_count=skipped_manual_count,
        failed_count=failed_count,
        dry_run=run.get("dry_run", False),
        is_undone=run.get("is_undone", False),
        conflict_details=conflict_details,
        manual_skip_reasons=manual_skip_reasons,
        has_signoff=has_signoff,
        signoff_status=signoff_status_display,
        signed_by=signed_by_display,
        signoff_id=signoff_id_display,
    )

    run_details = BundleRunDetails(
        moves=list(move_data),
        filter_rules=list(filter_rules) if filter_rules else [],
        filter_file_types=list(filter_file_types) if filter_file_types else [],
        filter_target_dirs=list(filter_target_dirs) if filter_target_dirs else [],
        created_at=run.get("created_at", ""),
        completed_at=run.get("completed_at", ""),
        undo_records=undo_records_for_run,
    )

    bundle_id = generate_id()
    bundle = ExecutionBundle(
        bundle_id=bundle_id,
        bundle_version=BUNDLE_VERSION,
        created_at=now_iso(),
        run_id=run_id,
        plan_id=plan_id,
        snapshot_id=snapshot_id,
        summary=summary,
        snapshot=snapshot,
        run_details=run_details,
        signoffs=all_signoffs,
        signoff_conflicts=all_signoff_conflicts,
        validation_history=validation_history,
    )

    bundle_dict = bundle.to_dict()
    bundle.checksum = _compute_bundle_checksum(bundle_dict)
    store.save_bundle(bundle)

    return bundle


def load_bundle_from_file(file_path: str) -> ExecutionBundle:
    """
    从 JSON 文件加载执行归档包

    会检查：
    1. 文件存在性
    2. 必填字段完整性
    3. JSON 格式合法性
    """
    if not os.path.exists(file_path):
        raise FileNotFoundError(f"归档包文件不存在: {file_path}")

    try:
        with open(file_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except json.JSONDecodeError as e:
        raise ValueError(f"归档包文件格式错误，不是合法的 JSON: {e}")

    missing_fields = [f for f in BUNDLE_REQUIRED_FIELDS if f not in data]
    if missing_fields:
        raise ValueError(
            f"归档包缺少必填字段: {', '.join(missing_fields)}。"
            f" 归档包文件可能已损坏或不是有效的执行批次归档包。"
        )

    if "snapshot" in data:
        snap_missing = [f for f in ["snapshot_id", "config_snapshot", "moves"] if f not in data["snapshot"]]
        if snap_missing:
            raise ValueError(
                f"归档包内快照不完整，缺少字段: {', '.join(snap_missing)}"
            )

    if "run_details" in data and "moves" in data["run_details"]:
        for i, m in enumerate(data["run_details"]["moves"]):
            req_move_fields = ["filename", "source_path", "target_path", "status"]
            move_missing = [f for f in req_move_fields if f not in m]
            if move_missing:
                raise ValueError(
                    f"归档包执行明细第 {i+1} 条记录缺少字段: {', '.join(move_missing)}"
                )

    if "summary" in data:
        sum_req_fields = ["total_moves", "success_count", "skipped_conflict_count",
                          "skipped_manual_count", "failed_count"]
        sum_missing = [f for f in sum_req_fields if f not in data["summary"]]
        if sum_missing:
            raise ValueError(
                f"归档包摘要缺少字段: {', '.join(sum_missing)}"
            )

    return ExecutionBundle.from_dict(data)


def export_bundle_to_file(bundle: ExecutionBundle, output_path: str) -> None:
    """导出归档包到 JSON 文件"""
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(bundle.to_dict(), f, ensure_ascii=False, indent=2)


def validate_bundle_for_import(
    store: StateStore,
    bundle: ExecutionBundle,
    check_snapshot: bool = True,
) -> BundleValidationResult:
    """
    验证归档包是否可以导入

    检查项：
    1. 同一批次重复导入（bundle_id 或 run_id 已存在）
    2. 快照版本对不上（snapshot_id 在本地存在但内容不一致）
    3. 日志缺字段

    返回验证结果
    """
    errors: List[str] = []
    warnings: List[str] = []
    conflict_types: List[str] = []
    existing_bundle: Optional[ExecutionBundle] = None
    existing_run: Optional[Dict[str, Any]] = None

    bundle_dict = bundle.to_dict()
    required_top_fields = [
        "bundle_id", "bundle_version", "run_id", "plan_id",
        "snapshot_id", "summary", "snapshot", "run_details"
    ]
    for f in required_top_fields:
        if not bundle_dict.get(f):
            errors.append(f"归档包缺少顶层字段（值为空）: {f}")
            if "日志缺字段" not in conflict_types:
                conflict_types.append("日志缺字段")

    summary = bundle.summary.to_dict()
    summary_fields = [
        "total_moves", "success_count", "skipped_conflict_count",
        "skipped_manual_count", "failed_count"
    ]
    for f in summary_fields:
        if f not in summary:
            errors.append(f"归档包摘要缺字段: {f}")
            if "日志缺字段" not in conflict_types:
                conflict_types.append("日志缺字段")

    moves = bundle.run_details.moves
    for i, m in enumerate(moves):
        move_required = ["id", "filename", "source_path", "target_path", "status", "timestamp"]
        for f in move_required:
            if f not in m:
                errors.append(f"归档包执行明细第 {i+1} 条缺字段: {f}")
                if "日志缺字段" not in conflict_types:
                    conflict_types.append("日志缺字段")

    local_bundle_by_id = store.get_bundle(bundle.bundle_id)
    if local_bundle_by_id:
        existing_bundle = local_bundle_by_id
        errors.append(
            f"重复导入: bundle_id {bundle.bundle_id} 已存在于本地归档。"
            f" 原归档创建于 {local_bundle_by_id.created_at}"
        )
        conflict_types.append("同一批次重复导入")

    local_bundle_by_run = store.get_bundle_by_run_id(bundle.run_id)
    if local_bundle_by_run and local_bundle_by_run.bundle_id != bundle.bundle_id:
        if existing_bundle is None:
            existing_bundle = local_bundle_by_run
        errors.append(
            f"重复导入: run_id {bundle.run_id} 已被归档包 {local_bundle_by_run.bundle_id} 占用。"
            f" 同一执行批次不能重复归档。"
        )
        if "同一批次重复导入" not in conflict_types:
            conflict_types.append("同一批次重复导入")

    local_run = store.get_run(bundle.run_id)
    if local_run:
        existing_run = local_run
        errors.append(
            f"执行 ID {bundle.run_id} 已存在于本地状态文件。"
            f" 执行创建于 {local_run.get('created_at', 'N/A')}"
        )
        if "同一批次重复导入" not in conflict_types:
            conflict_types.append("同一批次重复导入")

    if check_snapshot:
        local_snapshot = store.get_snapshot(bundle.snapshot_id)
        if local_snapshot:
            local_dict = local_snapshot.to_dict()
            bundle_dict_snap = bundle.snapshot.to_dict()

            def _strip_imported(d):
                return {k: v for k, v in d.items() if k not in ["imported", "import_source"]}

            local_stripped = _strip_imported(local_dict)
            bundle_stripped = _strip_imported(bundle_dict_snap)

            local_signature = {
                "move_count": len(local_stripped.get("moves", [])),
                "unmatched_count": len(local_stripped.get("unmatched_files", [])),
                "has_conflicts": local_stripped.get("has_conflicts"),
            }
            bundle_signature = {
                "move_count": len(bundle_stripped.get("moves", [])),
                "unmatched_count": len(bundle_stripped.get("unmatched_files", [])),
                "has_conflicts": bundle_stripped.get("has_conflicts"),
            }
            if local_signature != bundle_signature:
                errors.append(
                    f"快照版本不一致: snapshot_id {bundle.snapshot_id} 在本地存在但内容不同。"
                    f" 本地: {local_signature}，归档包内: {bundle_signature}"
                )
                conflict_types.append("快照版本对不上")
            else:
                warnings.append(
                    f"snapshot_id {bundle.snapshot_id} 本地已存在，内容一致，将跳过快照写入。"
                )
        else:
            warnings.append(
                f"snapshot_id {bundle.snapshot_id} 本地不存在，将从归档包导入。"
            )

    if bundle.checksum:
        recomputed = _compute_bundle_checksum(bundle_dict)
        if recomputed != bundle.checksum:
            warnings.append(
                f"归档包校验和不一致（期望: {bundle.checksum}，实际: {recomputed}），"
                f"文件可能在传输/保存过程中被修改。"
            )

    valid = len(errors) == 0
    return BundleValidationResult(
        valid=valid,
        errors=errors,
        warnings=warnings,
        conflict_types=conflict_types,
        existing_bundle=existing_bundle,
        existing_run=existing_run,
    )


def import_bundle_into_store(
    store: StateStore,
    bundle: ExecutionBundle,
    import_source: str,
    force: bool = False,
    imported_by: str = "cli",
    check_snapshot: bool = True,
) -> Tuple[bool, BundleValidationResult, BundleImportLog, List[str]]:
    """
    将归档包导入到本地状态存储

    步骤：
    1. 验证归档包
    2. 写入快照（如本地无此快照）
    3. 写入签收记录
    4. 写入签收冲突历史
    5. 写入校验历史
    6. 写入执行记录
    7. 写入归档包
    8. 写入撤销记录
    9. 记录导入日志

    返回: (是否成功, 验证结果, 导入日志, 提示信息列表)
    """
    info_messages: List[str] = []

    validation = validate_bundle_for_import(store, bundle, check_snapshot=check_snapshot)

    def _make_log(status: str, forced_flag: bool = False) -> BundleImportLog:
        return BundleImportLog(
            import_log_id=generate_id(),
            bundle_id=bundle.bundle_id,
            run_id=bundle.run_id,
            snapshot_id=bundle.snapshot_id,
            timestamp=now_iso(),
            status=status,
            source_file=os.path.abspath(import_source),
            errors=list(validation.errors),
            warnings=list(validation.warnings),
            conflict_details=list(validation.conflict_types),
            forced=forced_flag,
            imported_by=imported_by,
        )

    if validation.has_errors and not force:
        store.add_bundle_import_log(_make_log("failed"))
        return False, validation, _make_log("failed"), info_messages

    snapshot_written = False
    local_snapshot = store.get_snapshot(bundle.snapshot_id)
    if not local_snapshot:
        snap_to_save = bundle.snapshot
        snap_to_save.imported = True
        snap_to_save.import_source = os.path.abspath(import_source)
        store.save_snapshot(snap_to_save)
        snapshot_written = True
        info_messages.append(f"快照 {bundle.snapshot_id} 已从归档包导入")
    else:
        info_messages.append(f"快照 {bundle.snapshot_id} 本地已存在，跳过导入")

    signoffs_written = 0
    for signoff in bundle.signoffs:
        local_signoff = store.get_signoff(signoff.signoff_id)
        if not local_signoff:
            sig_to_save = signoff
            sig_to_save.import_source = os.path.abspath(import_source)
            store.add_signoff(sig_to_save)
            signoffs_written += 1
    if signoffs_written:
        info_messages.append(f"已导入 {signoffs_written} 条签收记录")

    conflicts_restored = 0
    pending_skipped = 0
    for sc in bundle.signoff_conflicts:
        existing_sc = store.get_signoff_conflict(sc.conflict_id)
        if existing_sc:
            continue
        if (sc.status == "pending" and
                store.get_pending_conflict_by_snapshot(sc.snapshot_id)):
            pending_skipped += 1
            continue
        store.save_signoff_conflict(sc)
        conflicts_restored += 1
    if conflicts_restored:
        info_messages.append(f"已恢复 {conflicts_restored} 条签收冲突历史")
    if pending_skipped:
        info_messages.append(f"跳过 {pending_skipped} 条已存在的待处理冲突")

    vh_written = 0
    for vh in bundle.validation_history:
        existing_vh = None
        for r in store._data.get("validation_history", []):
            if r.get("validation_id") == vh.validation_id:
                existing_vh = r
                break
        if not existing_vh:
            store.add_validation_history(vh)
            vh_written += 1
    if vh_written:
        info_messages.append(f"已导入 {vh_written} 条校验历史记录")

    if not store.get_run(bundle.run_id):
        run_data = {
            "id": bundle.run_id,
            "plan_id": bundle.plan_id,
            "snapshot_id": bundle.snapshot_id,
            "created_at": bundle.run_details.created_at,
            "completed_at": bundle.run_details.completed_at,
            "dry_run": bundle.summary.dry_run,
            "is_undone": bundle.summary.is_undone,
            "moves": list(bundle.run_details.moves),
        }
        if bundle.summary.signoff_id:
            run_data["signoff_id"] = bundle.summary.signoff_id
        store._data["runs"][bundle.run_id] = run_data
        store.save()
        info_messages.append(f"执行记录 {bundle.run_id} 已导入")
    else:
        info_messages.append(f"执行记录 {bundle.run_id} 本地已存在，跳过导入")

    if bundle.run_details.undo_records:
        existing_run_ids = {u.run_id for u in store.get_undo_records()}
        for urd in bundle.run_details.undo_records:
            if urd.get("run_id") in existing_run_ids:
                continue
            from .models import UndoRecord
            ur = UndoRecord(
                run_id=urd.get("run_id", ""),
                undo_timestamp=urd.get("undo_timestamp", now_iso()),
                moves_restored=urd.get("moves_restored", 0),
                status=urd.get("status", "completed"),
            )
            store.add_undo_record(ur)

    if not store.get_bundle(bundle.bundle_id):
        bundle_to_save = bundle
        bundle_to_save.imported = True
        bundle_to_save.import_source = os.path.abspath(import_source)
        bundle_to_save.imported_at = now_iso()
        store.save_bundle(bundle_to_save)
        info_messages.append(f"归档包 {bundle.bundle_id} 已导入本地状态")
    else:
        info_messages.append(f"归档包 {bundle.bundle_id} 本地已存在，跳过导入")

    if force and validation.has_errors:
        store.add_bundle_import_log(_make_log("forced", forced_flag=True))
    else:
        store.add_bundle_import_log(_make_log("success"))

    return True, validation, _make_log("success"), info_messages


# ============================================================
# 落点指纹清单（Landing Fingerprint）核心逻辑
# ============================================================

def compute_file_content_digest(file_path: str, sample_kb: int = 64) -> str:
    """
    计算文件内容摘要哈希

    策略：读取文件前 sample_kb KB + 文件大小字符串 + 文件后 sample_kb KB，
    然后计算 SHA256，避免大文件全量哈希。

    如果文件不存在或读取失败，返回空字符串。
    """
    import hashlib

    if not os.path.exists(file_path) or not os.path.isfile(file_path):
        return ""

    try:
        stat = os.stat(file_path)
        file_size = stat.st_size
        sample_bytes = sample_kb * 1024

        h = hashlib.sha256()
        h.update(f"SIZE:{file_size}".encode("utf-8"))

        with open(file_path, "rb") as f:
            if file_size > 0:
                head = f.read(min(sample_bytes, file_size))
                h.update(f"HEAD:{len(head)}".encode("utf-8"))
                h.update(head)

                if file_size > sample_bytes * 2:
                    f.seek(-sample_bytes, os.SEEK_END)
                    tail = f.read(sample_bytes)
                    h.update(f"TAIL:{len(tail)}".encode("utf-8"))
                    h.update(tail)
                elif file_size > sample_bytes:
                    remaining = file_size - sample_bytes
                    f.seek(sample_bytes, os.SEEK_SET)
                    tail = f.read(remaining)
                    h.update(f"TAIL:{len(tail)}".encode("utf-8"))
                    h.update(tail)

        return h.hexdigest()
    except Exception:
        return ""


def _compute_landing_checksum(landing_dict: Dict[str, Any]) -> str:
    """计算落点指纹清单的校验和（用于完整性检测）"""
    import hashlib

    target_dirs_sig = [
        {"target_dir": td.get("target_dir"), "file_count": td.get("file_count")}
        for td in landing_dict.get("target_dirs", [])
    ]
    file_fps_sig = [
        {
            "target_path": fp.get("target_path"),
            "file_size": fp.get("file_size"),
            "content_digest": fp.get("content_digest"),
        }
        for fp in landing_dict.get("file_fingerprints", [])
    ]

    relevant = {
        "landing_id": landing_dict.get("landing_id"),
        "run_id": landing_dict.get("run_id"),
        "plan_id": landing_dict.get("plan_id"),
        "snapshot_id": landing_dict.get("snapshot_id"),
        "created_at": landing_dict.get("created_at"),
        "dest_dir": landing_dict.get("dest_dir"),
        "total_moved_count": landing_dict.get("total_moved_count"),
        "total_skipped_conflict_count": landing_dict.get("total_skipped_conflict_count"),
        "total_skipped_manual_count": landing_dict.get("total_skipped_manual_count"),
        "total_failed_count": landing_dict.get("total_failed_count"),
        "target_dirs": target_dirs_sig,
        "file_fingerprints": file_fps_sig,
    }
    raw = json.dumps(relevant, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def create_landing_fingerprint(
    store: StateStore,
    run_id: str,
    config: Optional[Config] = None,
) -> LandingFingerprint:
    """
    根据执行记录创建落点指纹清单

    收集：
    - 目标目录列表（每个目录的文件数和路径摘要）
    - 每个成功移动文件的指纹（路径、大小、mtime、内容摘要哈希）
    - 手工改名记录
    - 四个摘要哈希（配置/目标目录/target_paths/文件内容）

    返回 LandingFingerprint 对象，并自动保存到状态存储。
    """
    import hashlib

    run = store.get_run(run_id)
    if not run:
        raise ValueError(f"执行记录不存在: {run_id}")

    plan_id = run.get("plan_id", "")
    snapshot_id = run.get("snapshot_id", "")
    signoff_id = run.get("signoff_id", "")

    if not snapshot_id:
        snapshot = store.get_snapshot_by_plan_id(plan_id)
    else:
        snapshot = store.get_snapshot(snapshot_id)

    if not snapshot:
        raise ValueError(f"找不到与执行 {run_id} 关联的快照")

    snapshot_id = snapshot.snapshot_id
    plan_id = snapshot.plan_id

    if config:
        dest_dir = config.dest_dir
        source_dir = config.source_dir
    else:
        config_snap = snapshot.config_snapshot
        dest_dir = config_snap.dest_dir if config_snap else ""
        source_dir = config_snap.source_dir if config_snap else ""

    move_data = run.get("moves", [])
    success_count = sum(1 for m in move_data if m.get("status") == "moved")
    skipped_conflict_count = sum(1 for m in move_data if m.get("status") == "skipped_conflict")
    skipped_manual_count = sum(1 for m in move_data if m.get("status") == "skipped_manual")
    failed_count = sum(1 for m in move_data if m.get("status") == "failed")

    target_dir_map: Dict[str, List[str]] = defaultdict(list)
    file_fingerprints: List[FileFingerprint] = []
    manual_renames: List[ManualRenameRecord] = []
    all_target_paths: List[str] = []
    all_content_digests: List[str] = []

    for move in move_data:
        status = move.get("status", "")
        target_path = move.get("target_path", "")
        source_path = move.get("source_path", "")
        filename = move.get("filename", "")
        matched_rule = move.get("matched_rule", "")

        if target_path:
            target_dir = os.path.dirname(target_path)
            if target_dir:
                target_dir_map[target_dir].append(target_path)

        if status == "moved" and target_path:
            all_target_paths.append(target_path)

            file_size = 0
            mtime = 0.0
            content_digest = ""

            if os.path.exists(target_path):
                try:
                    stat = os.stat(target_path)
                    file_size = stat.st_size
                    mtime = stat.st_mtime
                    content_digest = compute_file_content_digest(target_path)
                except Exception:
                    pass

            fp = FileFingerprint(
                fingerprint_id=generate_id(),
                source_path=source_path,
                target_path=target_path,
                filename=filename,
                matched_rule=matched_rule,
                file_size=file_size,
                mtime=mtime,
                content_digest=content_digest,
            )
            file_fingerprints.append(fp)
            if content_digest:
                all_content_digests.append(content_digest)

        error_msg = move.get("error_message", "")
        if error_msg and ("改名" in error_msg or "rename" in error_msg.lower()):
            mr = ManualRenameRecord(
                rename_id=generate_id(),
                original_target_path=target_path,
                final_target_path=target_path,
                rename_reason=error_msg,
                renamed_at=move.get("timestamp", now_iso()),
                renamed_by="system",
            )
            manual_renames.append(mr)

    target_dirs: List[TargetDirFingerprint] = []
    for tdir, files in sorted(target_dir_map.items()):
        actual_count = 0
        if os.path.isdir(tdir):
            try:
                actual_count = len([f for f in os.listdir(tdir) if os.path.isfile(os.path.join(tdir, f))])
            except Exception:
                actual_count = len(files)
        else:
            actual_count = len(files)

        td = TargetDirFingerprint(
            target_dir=tdir,
            file_count=actual_count,
            dir_path_digest=hashlib.sha256(tdir.encode("utf-8")).hexdigest()[:16],
        )
        target_dirs.append(td)

    config_snapshot_digest = ""
    if snapshot.config_snapshot:
        try:
            snap_dict = snapshot.config_snapshot.to_dict()
            raw = json.dumps(snap_dict, sort_keys=True, ensure_ascii=False)
            config_snapshot_digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]
        except Exception:
            pass

    dest_dir_digest = hashlib.sha256(dest_dir.encode("utf-8")).hexdigest()[:16] if dest_dir else ""

    sorted_target_paths = sorted(all_target_paths)
    if sorted_target_paths:
        raw_paths = json.dumps(sorted_target_paths, ensure_ascii=False)
        move_target_paths_digest = hashlib.sha256(raw_paths.encode("utf-8")).hexdigest()[:16]
    else:
        move_target_paths_digest = ""

    sorted_digests = sorted(all_content_digests)
    if sorted_digests:
        raw_digests = "|".join(sorted_digests)
        file_digests_summary = hashlib.sha256(raw_digests.encode("utf-8")).hexdigest()[:16]
    else:
        file_digests_summary = ""

    landing_id = generate_id()

    change_summary_parts = []
    change_summary_parts.append(f"成功移动 {success_count} 个文件")
    if skipped_conflict_count > 0:
        change_summary_parts.append(f"冲突跳过 {skipped_conflict_count} 个")
    if skipped_manual_count > 0:
        change_summary_parts.append(f"手工跳过 {skipped_manual_count} 个")
    if failed_count > 0:
        change_summary_parts.append(f"失败 {failed_count} 个")
    change_summary_parts.append(f"涉及 {len(target_dirs)} 个目标目录")
    if len(manual_renames) > 0:
        change_summary_parts.append(f"含 {len(manual_renames)} 条改名记录")
    change_summary = "；".join(change_summary_parts)

    landing = LandingFingerprint(
        landing_id=landing_id,
        run_id=run_id,
        snapshot_id=snapshot_id,
        plan_id=plan_id,
        created_at=now_iso(),
        dest_dir=dest_dir,
        source_dir=source_dir,
        total_moved_count=success_count,
        total_skipped_conflict_count=skipped_conflict_count,
        total_skipped_manual_count=skipped_manual_count,
        total_failed_count=failed_count,
        is_dry_run=run.get("dry_run", False),
        is_undone=run.get("is_undone", False),
        undone_at=None,
        target_dirs=target_dirs,
        file_fingerprints=file_fingerprints,
        manual_renames=manual_renames,
        config_snapshot_digest=config_snapshot_digest,
        dest_dir_digest=dest_dir_digest,
        move_target_paths_digest=move_target_paths_digest,
        file_digests_summary=file_digests_summary,
        status="active",
        checksum="",
        imported=False,
        import_source=None,
        imported_at=None,
        signoff_id=signoff_id if signoff_id else None,
        signoff_snapshot_config_digest="",
        change_summary=change_summary,
        export_result="",
    )

    landing_dict = landing.to_dict()
    landing_dict["landing_version"] = LANDING_FINGERPRINT_VERSION
    landing.checksum = _compute_landing_checksum(landing_dict)
    store.save_landing(landing)

    return landing


def diff_landing_fingerprints(
    local: Optional[LandingFingerprint],
    imported: LandingFingerprint,
) -> LandingFingerprintDiff:
    """
    深度对比两份落点指纹清单

    对比维度：
    1. duplicate_import: landing_id/run_id 是否已存在
    2. config_changed: 配置快照摘要是否一致
    3. dest_dir_changed: 目标根目录是否一致
    4. target_paths_diff: 逐条比较 target_path 列表
    5. file_fingerprints_diff: 逐个比较文件指纹（content_digest + size）
    6. file_count_mismatch: 文件数量不一致
    """
    diff_id = generate_id()
    has_diff = False
    diff_fields: List[str] = []
    diff_details: Dict[str, Any] = {}
    target_dirs_diff: List[Dict[str, Any]] = []
    target_paths_diff: List[Dict[str, Any]] = []
    file_fingerprints_diff: List[Dict[str, Any]] = []
    dest_dir_changed = False
    config_changed = False
    duplicate_import = False
    file_count_mismatch = False

    if local is not None:
        duplicate_import = True
        has_diff = True
        diff_fields.append("duplicate_import")
        diff_details["duplicate_import"] = f"本地已存在 landing_id={local.landing_id}"

        if local.dest_dir != imported.dest_dir:
            dest_dir_changed = True
            has_diff = True
            diff_fields.append("dest_dir")
            diff_details["dest_dir"] = {
                "local": local.dest_dir,
                "imported": imported.dest_dir,
            }

        if local.config_snapshot_digest and imported.config_snapshot_digest:
            if local.config_snapshot_digest != imported.config_snapshot_digest:
                config_changed = True
                has_diff = True
                diff_fields.append("config_snapshot_digest")
                diff_details["config_snapshot_digest"] = {
                    "local": local.config_snapshot_digest,
                    "imported": imported.config_snapshot_digest,
                }

        local_td_map = {td.target_dir: td for td in local.target_dirs}
        imported_td_map = {td.target_dir: td for td in imported.target_dirs}
        all_tdirs = set(local_td_map.keys()) | set(imported_td_map.keys())
        for tdir in sorted(all_tdirs):
            ltd = local_td_map.get(tdir)
            itd = imported_td_map.get(tdir)
            if ltd is None or itd is None or ltd.file_count != itd.file_count:
                target_dirs_diff.append({
                    "target_dir": tdir,
                    "local_file_count": ltd.file_count if ltd else None,
                    "imported_file_count": itd.file_count if itd else None,
                })

        if target_dirs_diff:
            has_diff = True
            diff_fields.append("target_dirs")
            diff_details["target_dirs_changed"] = len(target_dirs_diff)

        local_fp_map = {fp.target_path: fp for fp in local.file_fingerprints}
        imported_fp_map = {fp.target_path: fp for fp in imported.file_fingerprints}
        all_fps = set(local_fp_map.keys()) | set(imported_fp_map.keys())

        if len(local.file_fingerprints) != len(imported.file_fingerprints):
            file_count_mismatch = True
            has_diff = True
            diff_fields.append("file_count")
            diff_details["file_count"] = {
                "local": len(local.file_fingerprints),
                "imported": len(imported.file_fingerprints),
            }

        for tp in sorted(all_fps):
            lfp = local_fp_map.get(tp)
            ifp = imported_fp_map.get(tp)
            if lfp is None or ifp is None:
                target_paths_diff.append({
                    "target_path": tp,
                    "status": "missing_in_local" if lfp is None else "missing_in_imported",
                })
            elif (lfp.content_digest != ifp.content_digest or lfp.file_size != ifp.file_size):
                file_fingerprints_diff.append({
                    "target_path": tp,
                    "local_size": lfp.file_size,
                    "imported_size": ifp.file_size,
                    "local_digest": lfp.content_digest,
                    "imported_digest": ifp.content_digest,
                })

        if target_paths_diff:
            has_diff = True
            diff_fields.append("target_paths")
            diff_details["target_paths_changed"] = len(target_paths_diff)

        if file_fingerprints_diff:
            has_diff = True
            diff_fields.append("file_fingerprints")
            diff_details["file_fingerprints_changed"] = len(file_fingerprints_diff)

        if (local.move_target_paths_digest and imported.move_target_paths_digest
                and local.move_target_paths_digest != imported.move_target_paths_digest):
            if "target_paths" not in diff_fields:
                diff_fields.append("move_target_paths_digest")
                has_diff = True
            diff_details["move_target_paths_digest"] = {
                "local": local.move_target_paths_digest,
                "imported": imported.move_target_paths_digest,
            }

        if (local.file_digests_summary and imported.file_digests_summary
                and local.file_digests_summary != imported.file_digests_summary):
            if "file_digests_summary" not in diff_fields:
                diff_fields.append("file_digests_summary")
                has_diff = True
            diff_details["file_digests_summary"] = {
                "local": local.file_digests_summary,
                "imported": imported.file_digests_summary,
            }

    return LandingFingerprintDiff(
        diff_id=diff_id,
        landing_id_local=local.landing_id if local else None,
        landing_id_imported=imported.landing_id,
        compared_at=now_iso(),
        has_diff=has_diff,
        diff_fields=diff_fields,
        diff_details=diff_details,
        dest_dir_changed=dest_dir_changed,
        target_dirs_diff=target_dirs_diff,
        target_paths_diff=target_paths_diff,
        file_fingerprints_diff=file_fingerprints_diff,
        file_count_mismatch=file_count_mismatch,
        config_changed=config_changed,
        duplicate_import=duplicate_import,
    )


def validate_landing_for_import(
    store: StateStore,
    landing: LandingFingerprint,
    check_duplicate: bool = True,
    current_config: Optional[Config] = None,
) -> LandingImportValidationResult:
    """
    验证落点指纹清单是否可以导入

    深度比对项（不能只看 snapshot_id 和计数）：
    1. 同批次重复导入（landing_id 或 run_id 已存在）
    2. 本地配置改动（目标目录 dest_dir 不一致）
    3. move.target_path 变化（路径列表摘要不同）
    4. 清单内容与现场对不上（文件指纹不一致）
    5. 必填字段缺失
    6. 当前配置 dest_dir 与清单 dest_dir 不一致（切换配置目录检测）

    Args:
        check_duplicate: 是否检查重复导入。本地自核对时传 False。
        current_config: 当前配置，用于检测切换配置目录后的 dest_dir 不一致。
    """
    errors: List[str] = []
    warnings: List[str] = []
    conflict_types: List[str] = []
    existing_landing: Optional[LandingFingerprint] = None
    existing_run: Optional[Dict[str, Any]] = None

    landing_dict = landing.to_dict()
    for f in LANDING_REQUIRED_FIELDS:
        if f not in landing_dict or landing_dict.get(f) in (None, "", [], {}):
            if f == "landing_version":
                continue
            errors.append(f"落点指纹清单缺少必填字段（值为空）: {f}")
            if "清单缺字段" not in conflict_types:
                conflict_types.append("清单缺字段")

    local_landing_by_id = store.get_landing(landing.landing_id)
    if local_landing_by_id:
        existing_landing = local_landing_by_id
        if check_duplicate:
            errors.append(
                f"重复导入: landing_id {landing.landing_id} 已存在于本地。"
                f" 原清单创建于 {local_landing_by_id.created_at}"
            )
            if "同批次重复导入" not in conflict_types:
                conflict_types.append("同批次重复导入")

    local_landing_by_run = store.get_landing_by_run_id(landing.run_id)
    if local_landing_by_run and local_landing_by_run.landing_id != landing.landing_id:
        if existing_landing is None:
            existing_landing = local_landing_by_run
        if check_duplicate:
            errors.append(
                f"重复导入: run_id {landing.run_id} 已被清单 {local_landing_by_run.landing_id} 占用。"
                f" 同一执行批次不能重复导入。"
            )
            if "同批次重复导入" not in conflict_types:
                conflict_types.append("同批次重复导入")

    local_run = store.get_run(landing.run_id)
    if local_run:
        existing_run = local_run

    diff_result = diff_landing_fingerprints(existing_landing, landing)

    if diff_result.dest_dir_changed:
        errors.append(
            f"目标目录不一致: 本地={diff_result.diff_details.get('dest_dir', {}).get('local')}，"
            f"导入={diff_result.diff_details.get('dest_dir', {}).get('imported')}"
        )
        if "本地配置改动" not in conflict_types:
            conflict_types.append("本地配置改动")

    if current_config and landing.dest_dir != current_config.dest_dir:
        errors.append(
            f"清单目标目录与当前配置不一致: "
            f"清单 dest_dir={landing.dest_dir}, 当前配置 dest_dir={current_config.dest_dir}"
        )
        if "配置目录切换" not in conflict_types:
            conflict_types.append("配置目录切换")
        if diff_result and not diff_result.dest_dir_changed:
            has_diff = True
            diff_fields = list(diff_result.diff_fields)
            diff_fields.append("dest_dir_vs_current_config")
            diff_details = dict(diff_result.diff_details)
            diff_details["dest_dir_vs_current_config"] = {
                "landing": landing.dest_dir,
                "current_config": current_config.dest_dir,
            }
            diff_result = LandingFingerprintDiff(
                diff_id=diff_result.diff_id,
                landing_id_local=diff_result.landing_id_local,
                landing_id_imported=diff_result.landing_id_imported,
                compared_at=diff_result.compared_at,
                has_diff=True,
                diff_fields=diff_fields,
                diff_details=diff_details,
                dest_dir_changed=True,
                target_dirs_diff=diff_result.target_dirs_diff,
                target_paths_diff=diff_result.target_paths_diff,
                file_fingerprints_diff=diff_result.file_fingerprints_diff,
                file_count_mismatch=diff_result.file_count_mismatch,
                config_changed=diff_result.config_changed,
                duplicate_import=diff_result.duplicate_import,
            )

    if diff_result.config_changed:
        errors.append(
            f"配置快照不一致: 本地配置和导入清单对应的配置不同。"
            f" (config_snapshot_digest 不一致)"
        )
        if "本地配置改动" not in conflict_types:
            conflict_types.append("本地配置改动")

    if diff_result.target_paths_diff:
        for tpd in diff_result.target_paths_diff:
            errors.append(
                f"目标路径不一致: {tpd.get('target_path')} - {tpd.get('status')}"
            )
        if "move.target_path 变化" not in conflict_types:
            conflict_types.append("move.target_path 变化")

    if diff_result.file_fingerprints_diff:
        for ffd in diff_result.file_fingerprints_diff:
            errors.append(
                f"文件指纹不一致: {ffd.get('target_path')} - "
                f"本地(size={ffd.get('local_size')}, digest={ffd.get('local_digest')[:8]}...) vs "
                f"导入(size={ffd.get('imported_size')}, digest={ffd.get('imported_digest')[:8]}...)"
            )
        if "清单内容与现场对不上" not in conflict_types:
            conflict_types.append("清单内容与现场对不上")

    if diff_result.file_count_mismatch:
        errors.append(
            f"文件数量不一致: {diff_result.diff_details.get('file_count', {})}"
        )
        if "清单内容与现场对不上" not in conflict_types:
            conflict_types.append("清单内容与现场对不上")

    if diff_result.target_dirs_diff:
        for tdd in diff_result.target_dirs_diff:
            warnings.append(
                f"目标目录文件数变化: {tdd.get('target_dir')} - "
                f"本地={tdd.get('local_file_count')}, 导入={tdd.get('imported_file_count')}"
            )

    if landing.checksum:
        recomputed = _compute_landing_checksum(landing_dict)
        if recomputed != landing.checksum:
            warnings.append(
                f"清单校验和不一致（期望: {landing.checksum}，实际: {recomputed}），"
                f"文件可能在传输/保存过程中被修改。"
            )

    valid = len(errors) == 0
    return LandingImportValidationResult(
        valid=valid,
        errors=errors,
        warnings=warnings,
        conflict_types=conflict_types,
        diff_result=diff_result,
        existing_landing=existing_landing,
        existing_run=existing_run,
    )


def import_landing_into_store(
    store: StateStore,
    landing: LandingFingerprint,
    import_source: str,
    force: bool = False,
    imported_by: str = "cli",
    current_config: Optional[Config] = None,
) -> Tuple[bool, LandingImportValidationResult, LandingImportLog, List[str]]:
    """
    将落点指纹清单导入到本地状态存储

    返回: (是否成功, 验证结果, 导入日志, 提示信息列表)
    """
    info_messages: List[str] = []

    validation = validate_landing_for_import(
        store, landing,
        current_config=current_config,
    )

    def _make_log(status: str, forced_flag: bool = False) -> LandingImportLog:
        return LandingImportLog(
            import_log_id=generate_id(),
            landing_id=landing.landing_id,
            run_id=landing.run_id,
            snapshot_id=landing.snapshot_id,
            timestamp=now_iso(),
            status=status,
            source_file=os.path.abspath(import_source),
            errors=list(validation.errors),
            warnings=list(validation.warnings),
            conflict_details=list(validation.conflict_types),
            forced=forced_flag,
            imported_by=imported_by,
        )

    if validation.has_errors and not force:
        store.add_landing_import_log(_make_log("failed"))
        return False, validation, _make_log("failed"), info_messages

    if not store.get_landing(landing.landing_id):
        landing_to_save = landing
        landing_to_save.imported = True
        landing_to_save.import_source = os.path.abspath(import_source)
        landing_to_save.imported_at = now_iso()
        store.save_landing(landing_to_save)
        info_messages.append(f"落点指纹清单 {landing.landing_id} 已导入本地状态")
    else:
        info_messages.append(f"落点指纹清单 {landing.landing_id} 本地已存在，跳过导入")

    if force and validation.has_errors:
        store.add_landing_import_log(_make_log("forced", forced_flag=True))
    else:
        store.add_landing_import_log(_make_log("success"))

    return True, validation, _make_log("success"), info_messages


def export_landing_to_file(
    landing: LandingFingerprint,
    output_path: str,
    store: Optional[StateStore] = None,
) -> str:
    """导出落点指纹清单到 JSON 文件

    Args:
        landing: 落点指纹清单对象
        output_path: 输出文件路径
        store: 状态存储，若提供则更新 export_result 字段

    Returns:
        导出结果描述字符串
    """
    data = landing.to_dict()
    data["landing_version"] = LANDING_FINGERPRINT_VERSION
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    abs_path = os.path.abspath(output_path)
    file_size = os.path.getsize(abs_path)
    export_result = (
        f"导出成功: {abs_path} "
        f"(大小: {file_size} 字节, "
        f"文件指纹数: {len(landing.file_fingerprints)}, "
        f"校验和: {landing.checksum[:16]}...)"
    )

    if store:
        landing.export_result = export_result
        store.save_landing(landing)

    return export_result


def load_landing_from_file(file_path: str) -> LandingFingerprint:
    """
    从 JSON 文件加载落点指纹清单

    会检查：
    1. 文件存在性
    2. 必填字段完整性
    3. JSON 格式合法性
    """
    if not os.path.exists(file_path):
        raise FileNotFoundError(f"落点指纹清单文件不存在: {file_path}")

    try:
        with open(file_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except json.JSONDecodeError as e:
        raise ValueError(f"落点指纹清单文件格式错误，不是合法的 JSON: {e}")

    missing_fields = [f for f in LANDING_REQUIRED_FIELDS if f not in data and f != "landing_version"]
    if missing_fields:
        raise ValueError(
            f"落点指纹清单缺少必填字段: {', '.join(missing_fields)}。"
            f" 清单文件可能已损坏或不是有效的落点指纹清单。"
        )

    if "target_dirs" in data:
        for i, td in enumerate(data["target_dirs"]):
            td_missing = [f for f in ["target_dir", "file_count"] if f not in td]
            if td_missing:
                raise ValueError(
                    f"落点指纹清单第 {i+1} 条目标目录记录缺少字段: {', '.join(td_missing)}"
                )

    if "file_fingerprints" in data:
        for i, fp in enumerate(data["file_fingerprints"]):
            fp_required = ["fingerprint_id", "source_path", "target_path", "filename",
                           "matched_rule", "file_size", "mtime"]
            fp_missing = [f for f in fp_required if f not in fp]
            if fp_missing:
                raise ValueError(
                    f"落点指纹清单第 {i+1} 条文件指纹记录缺少字段: {', '.join(fp_missing)}"
                )

    return LandingFingerprint.from_dict(data)


def verify_landing_file(
    file_path: str,
    store: Optional[StateStore] = None,
    current_config: Optional[Config] = None,
    check_duplicate: bool = True,
    verify_source: str = "cli",
) -> LandingVerifyResult:
    """
    核对单个落点清单文件，返回三类分类结果

    分类规则:
    - invalid: 清单本身有问题（文件不存在、JSON 格式错误、缺少必填字段等）
    - conflict: 清单与当前配置或本地状态存在差异（dest_dir 不一致、目标路径变化、重复导入等）
    - valid: 清单有效，与当前配置和状态一致

    Args:
        file_path: 清单文件路径
        store: 状态存储，用于对比本地记录（为 None 时只做格式校验）
        current_config: 当前配置，用于检测配置目录切换
        check_duplicate: 是否检查重复导入
        verify_source: 核对来源标记

    Returns:
        LandingVerifyResult 三类分类结果
    """
    landing = None
    errors: List[str] = []
    warnings: List[str] = []
    conflict_types: List[str] = []

    try:
        landing = load_landing_from_file(file_path)
    except FileNotFoundError as e:
        return LandingVerifyResult(
            status="invalid",
            landing_id="unknown",
            run_id="unknown",
            errors=[f"清单文件不存在: {e}"],
            verify_source=verify_source,
        )
    except json.JSONDecodeError as e:
        return LandingVerifyResult(
            status="invalid",
            landing_id="unknown",
            run_id="unknown",
            errors=[f"清单 JSON 格式错误: {e}"],
            verify_source=verify_source,
        )
    except ValueError as e:
        return LandingVerifyResult(
            status="invalid",
            landing_id="unknown",
            run_id="unknown",
            errors=[f"清单内容无效: {e}"],
            verify_source=verify_source,
        )
    except Exception as e:
        return LandingVerifyResult(
            status="invalid",
            landing_id="unknown",
            run_id="unknown",
            errors=[f"读取清单时发生未知错误: {type(e).__name__}: {e}"],
            verify_source=verify_source,
        )

    if store is None:
        return LandingVerifyResult(
            status="valid",
            landing_id=landing.landing_id,
            run_id=landing.run_id,
            snapshot_id=landing.snapshot_id,
            plan_id=landing.plan_id,
            current_config_dest_dir=current_config.dest_dir if current_config else "",
            landing_dest_dir=landing.dest_dir,
            verify_source=verify_source,
        )

    validation = validate_landing_for_import(
        store, landing,
        check_duplicate=check_duplicate,
        current_config=current_config,
    )

    if validation.has_errors:
        has_conflict_only = True
        for err in validation.errors:
            if "缺少必填字段" in err or "格式错误" in err:
                has_conflict_only = False
                break

        if has_conflict_only:
            status = "conflict"
        else:
            status = "invalid"

        return LandingVerifyResult(
            status=status,
            landing_id=landing.landing_id,
            run_id=landing.run_id,
            snapshot_id=landing.snapshot_id,
            plan_id=landing.plan_id,
            errors=list(validation.errors),
            warnings=list(validation.warnings),
            conflict_types=list(validation.conflict_types),
            diff_result=validation.diff_result,
            current_config_dest_dir=current_config.dest_dir if current_config else "",
            landing_dest_dir=landing.dest_dir,
            verify_source=verify_source,
        )

    return LandingVerifyResult(
        status="valid",
        landing_id=landing.landing_id,
        run_id=landing.run_id,
        snapshot_id=landing.snapshot_id,
        plan_id=landing.plan_id,
        warnings=list(validation.warnings),
        diff_result=validation.diff_result,
        current_config_dest_dir=current_config.dest_dir if current_config else "",
        landing_dest_dir=landing.dest_dir,
        verify_source=verify_source,
    )
