"""CLI 入口 - 发票文件批量整理工作流"""
from __future__ import annotations

import json
import os
import sys
from typing import Optional, Tuple

import click

from .models import (
    load_config, generate_id, ImportLog, now_iso, LockViolation, ConfigSnapshot,
    SnapshotRemark, RemarkFieldChange, SignoffRecord, SignoffConflictState,
    MAX_SIGNOFF_NOTES_LENGTH, MAX_SIGNOFF_BY_LENGTH,
)
from .storage import StateStore
from .workflow import (
    scan_directory, build_plan, build_plan_summary, apply_plan,
    undo_run, export_logs, export_plan, filter_moves,
    create_batch_snapshot, diff_config, validate_import_snapshot,
    load_snapshot_from_file, export_snapshot_to_file,
    snapshot_to_planned_moves,
    diff_plans, export_plan_diff,
    validate_remark, build_remark, diff_remarks, format_remark_change,
    MAX_REMARK_LENGTH, MAX_HANDLER_LENGTH, MAX_NOTES_LENGTH, MAX_TAG_LENGTH, MAX_TAGS_COUNT,
    validate_signoff, build_signoff, diff_signoffs, format_signoff_change,
    validate_signoff_for_apply, config_snapshot_to_dict, export_signoff_to_csv_row,
    check_signoff_expired,
    detect_and_create_signoff_conflict, format_signoff_conflict_summary,
    has_unresolved_signoff_conflict, save_validation_history,
    create_execution_bundle, load_bundle_from_file, export_bundle_to_file,
    validate_bundle_for_import, import_bundle_into_store,
    create_landing_fingerprint, load_landing_from_file, export_landing_to_file,
    validate_landing_for_import, import_landing_into_store,
    verify_landing_file,
    diff_landing_fingerprints, compute_file_content_digest,
)


def _get_store(config):
    state_file = config.state_file
    if not os.path.isabs(state_file):
        state_file = os.path.join(os.getcwd(), state_file)
    return StateStore(state_file)


@click.group(help="发票文件批量整理工作流 CLI")
@click.version_option(version="1.0.0", prog_name="invoice-organizer")
def cli():
    pass


@cli.command("scan", help="按配置扫描源目录，输出文件列表")
@click.option("-c", "--config", "config_path", required=True, type=click.Path(),
              help="配置文件路径 (YAML)")
@click.option("-v", "--verbose", is_flag=True, help="显示详细信息")
def cmd_scan(config_path: str, verbose: bool):
    try:
        config = load_config(config_path)
    except Exception as e:
        click.echo(f"[错误] 加载配置失败: {e}", err=True)
        sys.exit(1)

    click.echo(f"[扫描] 源目录: {config.source_dir}")
    click.echo(f"[扫描] 递归: {'是' if config.recursive else '否'}")
    if config.file_extensions:
        click.echo(f"[扫描] 文件过滤: {', '.join(config.file_extensions)}")

    try:
        files = scan_directory(config)
    except Exception as e:
        click.echo(f"[错误] 扫描失败: {e}", err=True)
        sys.exit(1)

    store = _get_store(config)
    config_snapshot = ConfigSnapshot(
        source_dir=config.source_dir,
        dest_dir=config.dest_dir,
        rules=[r.to_dict() for r in config.rules],
        state_file=config.state_file,
        recursive=config.recursive,
        file_extensions=config.file_extensions,
        config_path=config_path,
        config_mtime=os.path.getmtime(config_path) if os.path.exists(config_path) else None,
    )
    store.save_scan(files, config_snapshot)

    matched = sum(1 for f in files if f.matched_rule)
    unmatched = len(files) - matched

    click.echo(f"\n[扫描完成] 共找到 {len(files)} 个文件")
    click.echo(f"  - 匹配规则: {matched}")
    click.echo(f"  - 未匹配规则: {unmatched}")
    click.echo(f"  - 状态文件: {store.state_file}")

    if verbose:
        click.echo("\n[文件列表]")
        for f in files:
            rule_str = click.style(f.matched_rule or "(无)", fg="green" if f.matched_rule else "yellow")
            click.echo(f"  {f.filename}  [{rule_str}]  ({f.size} bytes)  ->  {f.source_path}")


@cli.command("plan", help="生成归档预案，检测冲突")
@click.option("-c", "--config", "config_path", required=True, type=click.Path(),
              help="配置文件路径 (YAML)")
@click.option("-p", "--plan-id", "plan_id", help="指定预案 ID (默认自动生成)")
@click.option("--remark", help=f"批次备注 (最多 {MAX_REMARK_LENGTH} 字符)")
@click.option("--tag", "tags", multiple=True, help=f"标签，可多次指定 (最多 {MAX_TAGS_COUNT} 个，每个最多 {MAX_TAG_LENGTH} 字符)")
@click.option("--handler", help=f"交接人 (最多 {MAX_HANDLER_LENGTH} 字符)")
@click.option("--notes", help=f"注意事项 (最多 {MAX_NOTES_LENGTH} 字符)")
@click.option("--updated-by", default="cli", help="备注更新人 (默认: cli)")
@click.option("-v", "--verbose", is_flag=True, help="显示详细信息")
def cmd_plan(
    config_path: str,
    plan_id: Optional[str],
    remark: Optional[str],
    tags: tuple,
    handler: Optional[str],
    notes: Optional[str],
    updated_by: str,
    verbose: bool,
):
    try:
        config = load_config(config_path)
    except Exception as e:
        click.echo(f"[错误] 加载配置失败: {e}", err=True)
        sys.exit(1)

    store = _get_store(config)
    scanned = store.get_scan()
    scan_config = store.get_scan_config()

    need_rescan = False
    if scanned:
        if scan_config is None:
            click.echo(f"[提示] 旧版状态文件无配置快照，自动重新扫描...")
            need_rescan = True
        else:
            config_diff = diff_config(config, scan_config)
            if config_diff.has_diff:
                click.echo(f"[提示] 检测到配置变化，自动重新扫描...")
                need_rescan = True

    if not scanned or need_rescan:
        if not scanned:
            click.echo("[提示] 没有找到扫描结果，自动执行 scan...")
        try:
            scanned = scan_directory(config)
            config_snapshot = ConfigSnapshot(
                source_dir=config.source_dir,
                dest_dir=config.dest_dir,
                rules=[r.to_dict() for r in config.rules],
                state_file=config.state_file,
                recursive=config.recursive,
                file_extensions=config.file_extensions,
                config_path=config_path,
                config_mtime=os.path.getmtime(config_path) if os.path.exists(config_path) else None,
            )
            store.save_scan(scanned, config_snapshot)
        except Exception as e:
            click.echo(f"[错误] 自动扫描失败: {e}", err=True)
            sys.exit(1)

    moves, errors = build_plan(config, scanned)

    if errors:
        click.echo(click.style("[错误] 预案生成失败，检测到致命错误:", fg="red"))
        for err in errors:
            click.echo(click.style(f"  - {err}", fg="red"))
        click.echo(click.style("\n预案未保存，请修正配置后重试。", fg="red"))
        sys.exit(1)

    final_plan_id = plan_id or generate_id()
    conflicts = [m for m in moves if m.conflict_type is not None]
    has_conflicts = len(conflicts) > 0

    summary = build_plan_summary(config, scanned, moves)

    store.save_plan(final_plan_id, moves, has_conflicts)

    remark_obj = None
    if remark or tags or handler or notes:
        remark_obj = build_remark(
            remark=remark, tags=list(tags) if tags else None, handler=handler, notes=notes, updated_by=updated_by)
        validation = validate_remark(remark_obj)
        if validation.has_errors:
            click.echo(click.style("[错误] 备注信息验证失败：", fg="red", bold=True))
            for err in validation.errors:
                click.echo(click.style(f"  - {err}", fg="red"))
            sys.exit(1)
        if validation.warnings:
            for warn in validation.warnings:
                click.echo(click.style(f"[警告] {warn}", fg="yellow"))

    snapshot = create_batch_snapshot(
        config=config,
        scanned_files=scanned,
        moves=moves,
        plan_id=final_plan_id,
        summary=summary,
        config_path=config_path,
        remark=remark_obj,
    )
    store.save_snapshot(snapshot)

    click.echo(click.style(f"[预案生成成功] ID: {final_plan_id}", fg="green"))
    click.echo(click.style(f"[批次快照] ID: {snapshot.snapshot_id}", fg="cyan"))
    click.echo(f"  扫描文件总数: {summary.total_files}")
    click.echo(f"  匹配规则: {summary.matched_files}")
    click.echo(f"  未匹配规则: {summary.unmatched_files}")
    click.echo(f"  移动计划: {len(moves)} 条")
    if has_conflicts:
        click.echo(click.style(f"  含冲突: {len(conflicts)} 条", fg="yellow"))

    if remark_obj and not remark_obj.is_empty():
        click.echo()
        click.echo(click.style("[备注信息]", fg="cyan"))
        if remark_obj.remark:
            click.echo(f"  备注: {remark_obj.remark}")
        if remark_obj.tags:
            click.echo(f"  标签: {', '.join(remark_obj.tags)}")
        if remark_obj.handler:
            click.echo(f"  交接人: {remark_obj.handler}")
        if remark_obj.notes:
            click.echo(f"  注意事项: {remark_obj.notes}")
        click.echo(f"  更新人: {remark_obj.updated_by}")
        click.echo(f"  更新时间: {remark_obj.updated_at}")

    click.echo()
    click.echo(click.style("[摘要] 按规则分布:", fg="cyan"))
    for rule_name, count in sorted(summary.files_per_rule.items()):
        click.echo(f"  {rule_name}: {count} 个文件")

    click.echo()
    click.echo(click.style("[摘要] 按目标目录分布:", fg="cyan"))
    for tdir, count in sorted(summary.files_per_target_dir.items()):
        is_new = " (新建)" if tdir in summary.new_target_dirs else ""
        new_tag = click.style(is_new, fg="green") if is_new else ""
        click.echo(f"  {tdir}: {count} 个文件{new_tag}")

    if summary.new_target_dirs:
        click.echo()
        click.echo(click.style("[摘要] 本次新建的目标目录:", fg="green"))
        for nd in summary.new_target_dirs:
            click.echo(f"  + {nd}")

    if summary.rules_with_same_target:
        click.echo()
        click.echo(click.style("[摘要] 多条规则映射同一目录:", fg="yellow"))
        for tdir, rules in summary.rules_with_same_target.items():
            click.echo(f"  ! {tdir}  <-  {', '.join(rules)}")

    if summary.unmatched_files > 0:
        click.echo()
        click.echo(click.style(f"[摘要] 未命中规则的文件 ({summary.unmatched_files} 个):", fg="yellow"))
        unmatched = [f for f in scanned if not f.matched_rule]
        for f in unmatched:
            click.echo(f"  - {f.filename}")

    if has_conflicts:
        click.echo()
        click.echo(click.style(f"[摘要] 冲突项 ({len(conflicts)} 个):", fg="yellow"))
        for m in conflicts:
            click.echo(f"  ! {m.filename}: {m.conflict_type} - {m.conflict_detail}")

    if verbose:
        click.echo("\n[移动计划详情]")
        for m in moves:
            status = ""
            if m.conflict_type:
                status = click.style(f" [冲突: {m.conflict_type}]", fg="yellow")
                if m.conflict_detail:
                    status += click.style(f" - {m.conflict_detail}", fg="yellow")
            click.echo(f"  {m.source_path}")
            click.echo(f"    -> {m.target_path}  [{m.matched_rule}]{status}")

    if has_conflicts:
        click.echo(click.style(
            "\n[提示] 检测到冲突项，执行 apply 时这些文件将被跳过。",
            fg="yellow"
        ))


@cli.command("apply", help="执行归档预案（移动文件），支持按规则/类型/目录筛选")
@click.option("-c", "--config", "config_path", required=True, type=click.Path(),
              help="配置文件路径 (YAML)")
@click.option("-p", "--plan-id", "plan_id", help="指定预案 ID (默认使用最近的)")
@click.option("-s", "--snapshot-id", "snapshot_id", help="指定批次快照 ID (优先级高于 plan-id)")
@click.option("--dry-run", is_flag=True, help="预演模式，不实际移动文件")
@click.option("-y", "--yes", is_flag=True, help="跳过确认提示")
@click.option("-v", "--verbose", is_flag=True, help="显示详细信息")
@click.option("--force-snapshot", is_flag=True,
              help="即使配置有变更，强制按旧快照执行")
@click.option("--rule", "filter_rules", multiple=True,
              help="按规则名筛选，可多次指定 (如: --rule 增值税专用发票 --rule 电子发票PDF)")
@click.option("--type", "filter_types", multiple=True,
              help="按文件类型筛选，可多次指定 (如: --type pdf --type jpg)")
@click.option("--target", "filter_targets", multiple=True,
              help="按目标目录筛选，可多次指定 (如: --target vat_special)")
@click.option("--require-signoff", is_flag=True, default=True,
              help="要求必须有有效的签收记录才能执行 (默认开启)")
@click.option("--no-require-signoff", is_flag=True,
              help="跳过签收校验，即使没有签收也可以执行")
@click.option("--force-expired-signoff", is_flag=True,
              help="即使签收已过期也强制执行")
@click.option("--force-conflict-signoff", is_flag=True,
              help="即使存在签收冲突也强制执行（需先解决分歧）")
def cmd_apply(
    config_path: str,
    plan_id: Optional[str],
    snapshot_id: Optional[str],
    dry_run: bool,
    yes: bool,
    verbose: bool,
    force_snapshot: bool,
    filter_rules: tuple,
    filter_types: tuple,
    filter_targets: tuple,
    require_signoff: bool,
    no_require_signoff: bool,
    force_expired_signoff: bool,
    force_conflict_signoff: bool,
):
    try:
        config = load_config(config_path)
    except Exception as e:
        click.echo(f"[错误] 加载配置失败: {e}", err=True)
        sys.exit(1)

    store = _get_store(config)

    snapshot = None
    if snapshot_id:
        snapshot = store.get_snapshot(snapshot_id)
        if not snapshot:
            click.echo(f"[错误] 未找到批次快照: {snapshot_id}", err=True)
            sys.exit(1)
    elif plan_id:
        snapshot = store.get_snapshot_by_plan_id(plan_id)
        if not snapshot:
            click.echo(f"[错误] 预案 {plan_id} 没有对应的批次快照", err=True)
            sys.exit(1)
    else:
        snapshot = store.get_last_snapshot()
        if not snapshot:
            click.echo("[错误] 未找到批次快照，请先执行 plan 命令。", err=True)
            sys.exit(1)

    snapshot_id = snapshot.snapshot_id
    plan_id = snapshot.plan_id

    require_signoff = require_signoff and not no_require_signoff

    if no_require_signoff:
        click.echo(click.style("[注意] 已使用 --no-require-signoff 跳过签收校验", fg="yellow"))

    signoff_validation = validate_signoff_for_apply(
        store=store,
        snapshot=snapshot,
        current_config=config,
        require_signed=require_signoff,
    )

    triggered_by = "apply-dry-run" if dry_run else "apply"
    save_validation_history(
        store=store,
        snapshot=snapshot,
        signoff_validation=signoff_validation,
        triggered_by=triggered_by,
        lock_allowed=True,
    )

    active_signoff = signoff_validation.active_signoff

    has_conflict_error = any("未解决的签收冲突" in err or "冲突的签收记录" in err for err in signoff_validation.errors)

    if signoff_validation.is_expired and not force_expired_signoff:
        click.echo(click.style("[错误] 执行被签收过期拦截！", fg="red", bold=True))
        click.echo(click.style(f"  签收截止时间: {active_signoff.deadline if active_signoff else 'N/A'}", fg="red"))
        click.echo(click.style(f"  签收时间: {active_signoff.signed_at if active_signoff else 'N/A'}", fg="red"))
        click.echo()
        click.echo("  你可以：")
        click.echo("    1. 使用 --force-expired-signoff 强制执行已过期的签收")
        click.echo(f"       python -m invoice_organizer apply -c {config_path} -s {snapshot_id} --force-expired-signoff")
        click.echo("    2. 重新执行 sign-off 更新截止时间")
        click.echo(f"       python -m invoice_organizer sign-off -c {config_path} -s {snapshot_id} --signed-by <签收人> --deadline <新截止时间>")
        sys.exit(1)

    if signoff_validation.config_mismatch and not force_snapshot:
        click.echo(click.style("[错误] 执行被配置变化拦截！当前配置与签收时不一致", fg="red", bold=True))
        click.echo(click.style(f"  快照创建时间: {snapshot.created_at}", fg="red"))
        click.echo(click.style(f"  签收时间: {active_signoff.signed_at if active_signoff else 'N/A'}", fg="red"))
        for err in signoff_validation.errors:
            if "配置与签收时不一致" in err:
                click.echo(click.style(f"  - {err}", fg="red"))
        click.echo()
        click.echo("  你可以：")
        click.echo("    1. 使用 --force-snapshot 强制按旧快照执行")
        click.echo(f"       python -m invoice_organizer apply -c {config_path} -s {snapshot_id} --force-snapshot")
        click.echo("    2. 重新 plan 生成新快照并签收")
        click.echo(f"       python -m invoice_organizer plan -c {config_path}")
        sys.exit(1)

    if signoff_validation.snapshot_replaced:
        for warn in signoff_validation.warnings:
            if "快照已被新版本替代" in warn:
                click.echo(click.style(f"[警告] {warn}", fg="yellow", bold=True))

    if signoff_validation.conflicting_signoffs:
        for warn in signoff_validation.warnings:
            if "冲突的签收记录" in warn:
                click.echo(click.style(f"[警告] {warn}", fg="yellow", bold=True))
        for cs in signoff_validation.conflicting_signoffs:
            click.echo(click.style(f"  - {cs}", fg="yellow"))

    if signoff_validation.has_errors and not force_snapshot:
        only_expired_error = (
            signoff_validation.is_expired and
            len(signoff_validation.errors) == 1 and
            "签收已过期" in signoff_validation.errors[0]
        )
        only_conflict_error = (
            has_conflict_error and
            len(signoff_validation.errors) == sum(1 for err in signoff_validation.errors if "未解决的签收冲突" in err or "冲突的签收记录" in err)
        )
        if not (only_expired_error and force_expired_signoff) and not (only_conflict_error and force_conflict_signoff):
            click.echo(click.style("[错误] 执行被签收校验拦截！", fg="red", bold=True))
            for err in signoff_validation.errors:
                click.echo(click.style(f"  - {err}", fg="red"))
            if active_signoff:
                click.echo()
                click.echo(click.style("[当前签收信息]", fg="yellow"))
                click.echo(f"  签收 ID: {active_signoff.signoff_id}")
                status_map = {"signed": "已签收", "rejected": "已拒绝", "pending": "待处理"}
                click.echo(f"  状态: {status_map.get(active_signoff.status, active_signoff.status)}")
                click.echo(f"  签收人: {active_signoff.signed_by}")
                click.echo(f"  签收时间: {active_signoff.signed_at}")
                if active_signoff.deadline:
                    click.echo(f"  截止时间: {active_signoff.deadline}")
                if active_signoff.conflict_detail:
                    click.echo(f"  冲突详情: {active_signoff.conflict_detail}")
            click.echo()
            click.echo("  你可以：")
            click.echo(f"    1. 先执行 sign-off 签收该快照")
            click.echo(f"       python -m invoice_organizer sign-off -c {config_path} -s {snapshot_id} --signed-by <签收人>")
            if has_conflict_error:
                click.echo("    2. 使用 --force-conflict-signoff 强制执行（需先解决分歧）")
                click.echo(f"       python -m invoice_organizer apply -c {config_path} -s {snapshot_id} --force-conflict-signoff")
            click.echo("    3. 使用 --no-require-signoff 跳过签收校验")
            click.echo(f"       python -m invoice_organizer apply -c {config_path} -s {snapshot_id} --no-require-signoff")
            sys.exit(1)

    if active_signoff:
        click.echo(click.style(f"[签收信息] 已验证签收: {active_signoff.signoff_id}", fg="green"))
        status_map = {"signed": "已签收", "rejected": "已拒绝", "pending": "待处理"}
        click.echo(f"  状态: {status_map.get(active_signoff.status, active_signoff.status)}")
        click.echo(f"  签收人: {active_signoff.signed_by}")
        click.echo(f"  签收时间: {active_signoff.signed_at}")
        if active_signoff.deadline:
            click.echo(f"  截止时间: {active_signoff.deadline}")
        if signoff_validation.is_expired and force_expired_signoff:
            click.echo(click.style("  [注意] 已强制执行过期签收", fg="yellow"))
        if signoff_validation.config_mismatch and force_snapshot:
            click.echo(click.style("  [注意] 已强制使用签收时配置执行（忽略当前配置变更）", fg="yellow"))
        if has_conflict_error and force_conflict_signoff:
            click.echo(click.style("  [注意] 已强制执行（存在未解决的签收冲突）", fg="yellow"))

    allowed, reject_reason = _check_lock_before_apply(store, snapshot)

    if not allowed:
        active_lock = store.get_active_lock()
        save_validation_history(
            store=store,
            snapshot=snapshot,
            signoff_validation=signoff_validation,
            triggered_by=triggered_by,
            lock_allowed=False,
            lock_reject_reason=reject_reason,
            active_lock_snapshot_id=active_lock.snapshot_id if active_lock else None,
        )

    if not allowed:
        click.echo(click.style("[错误] 执行被版本锁定拦截！", fg="red", bold=True))
        click.echo(click.style(f"  {reject_reason}", fg="red"))
        active_lock = store.get_active_lock()
        if active_lock:
            click.echo()
            click.echo(click.style("[锁定信息]", fg="yellow"))
            click.echo(f"  锁定 ID: {active_lock.lock_id}")
            click.echo(f"  锁定快照: {active_lock.snapshot_id}")
            click.echo(f"  锁定预案: {active_lock.plan_id}")
            click.echo(f"  锁定时间: {active_lock.locked_at}")
            if active_lock.reason:
                click.echo(f"  锁定原因: {active_lock.reason}")
        click.echo()
        click.echo("  你可以：")
        click.echo("    1. 使用 -s 指定锁定的快照 ID 执行")
        click.echo(f"       python -m invoice_organizer apply -c {config_path} -s {active_lock.snapshot_id if active_lock else ''}")
        click.echo("    2. 使用 unlock-plan 释放锁定后再执行")
        click.echo(f"       python -m invoice_organizer unlock-plan -c {config_path}")
        sys.exit(1)

    diff_result = diff_config(config, snapshot.config_snapshot)
    if diff_result.has_diff and not force_snapshot:
        click.echo(click.style("[警告] 检测到配置与快照时不一致！", fg="yellow", bold=True))
        click.echo(click.style(f"  快照创建时间: {snapshot.created_at}", fg="yellow"))

        if diff_result.source_dir_changed:
            click.echo(click.style(f"  - 源目录变更: {snapshot.config_snapshot.source_dir} -> {config.source_dir}", fg="yellow"))
        if diff_result.dest_dir_changed:
            click.echo(click.style(f"  - 目标目录变更: {snapshot.config_snapshot.dest_dir} -> {config.dest_dir}", fg="yellow"))
        if diff_result.extensions_changed:
            click.echo(click.style(f"  - 文件扩展名过滤变更", fg="yellow"))
        if diff_result.recursive_changed:
            click.echo(click.style(f"  - 递归扫描变更: {snapshot.config_snapshot.recursive} -> {config.recursive}", fg="yellow"))

        if diff_result.added_rules:
            click.echo(click.style(f"  - 新增规则: {', '.join(diff_result.added_rules)}", fg="green"))
        if diff_result.removed_rules:
            click.echo(click.style(f"  - 删除规则: {', '.join(diff_result.removed_rules)}", fg="red"))
        if diff_result.modified_rules:
            click.echo(click.style(f"  - 修改规则: {', '.join(diff_result.modified_rules)}", fg="yellow"))

        click.echo()
        if not yes:
            click.echo(click.style("[提示] 继续按旧快照执行，可能导致结果与新配置不一致。", fg="yellow"))
            if not click.confirm("是否继续按旧快照执行？", default=False):
                click.echo("已取消。请重新 plan 生成新快照后再执行。")
                sys.exit(0)
        else:
            click.echo(click.style("[提示] 配置有变更，但使用 -y 跳过，将按旧快照执行。", fg="yellow"))
            click.echo()

    if snapshot.has_conflicts:
        click.echo(click.style("[提示] 该快照包含冲突项，执行时将被跳过。", fg="yellow"))

    moves = snapshot_to_planned_moves(snapshot)

    fr = list(filter_rules) if filter_rules else None
    ft = list(filter_types) if filter_types else None
    ftargets = list(filter_targets) if filter_targets else None

    has_filter = fr is not None or ft is not None or ftargets is not None
    if has_filter:
        selected, skipped_manual_preview = filter_moves(moves, fr, ft, ftargets)
        click.echo(click.style("[筛选] 当前筛选条件:", fg="cyan"))
        if fr:
            click.echo(f"  规则: {', '.join(fr)}")
        if ft:
            click.echo(f"  文件类型: {', '.join(ft)}")
        if ftargets:
            click.echo(f"  目标目录: {', '.join(ftargets)}")
        click.echo(f"  选中: {len(selected)} 条")
        click.echo(click.style(f"  人工跳过: {len(skipped_manual_preview)} 条", fg="yellow"))
        click.echo()

    click.echo(f"[执行] 快照 ID: {snapshot_id}")
    click.echo(f"[执行] 预案 ID: {plan_id}")
    click.echo(f"[执行] 模式: {'预演 (DRY-RUN)' if dry_run else '实际执行'}")
    click.echo(f"[执行] 总移动项: {len(moves)}")

    if not yes and not dry_run:
        if not click.confirm("\n确认执行归档操作？", default=False):
            click.echo("已取消。")
            sys.exit(0)

    run_id, executed, success, skipped_conflict, skipped_manual, failed = apply_plan(
        config, store, plan_id, moves,
        dry_run=dry_run,
        filter_rules=fr,
        filter_file_types=ft,
        filter_target_dirs=ftargets,
    )

    if active_signoff:
        store.update_run_signoff(run_id, active_signoff.signoff_id, snapshot_id)

    try:
        fr_list = list(filter_rules) if filter_rules else None
        ft_list = list(filter_types) if filter_types else None
        ftargs_list = list(filter_targets) if filter_targets else None
        bundle = create_execution_bundle(
            store, run_id,
            filter_rules=fr_list,
            filter_file_types=ft_list,
            filter_target_dirs=ftargs_list,
        )
        click.echo(click.style(f"\n[归档完成] Bundle ID: {bundle.bundle_id}", fg="cyan"))
        click.echo(f"  可使用 export-bundle -b {bundle.bundle_id} 导出或 list-bundles 查看")
    except Exception as be:
        click.echo(click.style(f"\n[警告] 归档包生成失败: {be}", fg="yellow"))

    try:
        landing = create_landing_fingerprint(store, run_id, config=config)
        click.echo(click.style(f"\n[落点指纹完成] Landing ID: {landing.landing_id}", fg="cyan"))
        click.echo(f"  目标目录数: {len(landing.target_dirs)}  文件指纹数: {len(landing.file_fingerprints)}")
        click.echo(f"  可使用 generate-landing/list-landings/view-landing/export-landing 管理")
    except Exception as le:
        click.echo(click.style(f"\n[警告] 落点指纹清单生成失败: {le}", fg="yellow"))

    click.echo(click.style(f"\n[执行完成] Run ID: {run_id}", fg="green"))
    click.echo(f"  成功移动: {success}")
    if skipped_conflict > 0:
        click.echo(click.style(f"  冲突跳过: {skipped_conflict}", fg="yellow"))
    if skipped_manual > 0:
        click.echo(click.style(f"  人工跳过: {skipped_manual}", fg="bright_black"))
    if failed > 0:
        click.echo(click.style(f"  执行失败: {failed}", fg="red"))

    if verbose:
        click.echo("\n[执行详情]")
        for em in executed:
            if em.status == "moved":
                tag = click.style("[移动]", fg="green")
            elif em.status == "skipped_conflict":
                tag = click.style("[冲突跳过]", fg="yellow")
            elif em.status == "skipped_manual":
                tag = click.style("[人工跳过]", fg="bright_black")
            else:
                tag = click.style("[失败]", fg="red")
            detail = f" ({em.error_message})" if em.error_message else ""
            click.echo(f"  {tag} {em.filename}")
            click.echo(f"    {em.source_path} -> {em.target_path}{detail}")


@cli.command("undo", help="撤销某次 apply 执行")
@click.option("-c", "--config", "config_path", required=True, type=click.Path(),
              help="配置文件路径 (YAML)")
@click.option("-r", "--run-id", "run_id", help="指定执行 ID (默认使用最近一次)")
@click.option("-y", "--yes", is_flag=True, help="跳过确认提示")
@click.option("-v", "--verbose", is_flag=True, help="显示详细信息")
def cmd_undo(config_path: str, run_id: Optional[str], yes: bool, verbose: bool):
    try:
        config = load_config(config_path)
    except Exception as e:
        click.echo(f"[错误] 加载配置失败: {e}", err=True)
        sys.exit(1)

    store = _get_store(config)
    runs = store.get_all_runs()

    if not runs:
        click.echo("[错误] 没有可撤销的执行记录。", err=True)
        sys.exit(1)

    if not run_id:
        runs_sorted = sorted(runs, key=lambda r: r.get("created_at", ""), reverse=True)
        target_run = runs_sorted[0]
        run_id = target_run["id"]
    else:
        target_run = store.get_run(run_id)
        if not target_run:
            click.echo(f"[错误] 执行记录不存在: {run_id}", err=True)
            sys.exit(1)

    click.echo(f"[撤销] Run ID: {run_id}")
    click.echo(f"[撤销] 创建时间: {target_run.get('created_at')}")
    click.echo(f"[撤销] 预演模式: {'是' if target_run.get('dry_run') else '否'}")
    click.echo(f"[撤销] 已撤销: {'是' if target_run.get('is_undone') else '否'}")

    total_moved = sum(1 for m in target_run.get("moves", []) if m["status"] == "moved")
    click.echo(f"[撤销] 涉及移动记录: {total_moved} 条")

    signoff = store.get_signoff_by_run(run_id)
    if signoff:
        status_map = {"signed": "已签收", "rejected": "已拒绝", "pending": "待处理"}
        click.echo(f"[撤销] 执行时签收: {status_map.get(signoff.status, signoff.status)}")
        click.echo(f"[撤销] 签收ID: {signoff.signoff_id}")
        click.echo(f"[撤销] 签收人: {signoff.signed_by}")
        click.echo(f"[撤销] 签收时间: {signoff.signed_at}")
        if signoff.deadline:
            click.echo(f"[撤销] 截止时间: {signoff.deadline}")
        if signoff.notes:
            click.echo(f"[撤销] 签收说明: {signoff.notes}")
        if signoff.forced:
            click.echo(click.style(f"[撤销] 该执行为强制签收执行", fg="yellow"))
    else:
        click.echo(f"[撤销] 执行时未使用签收记录")

    if not yes:
        if not click.confirm("\n确认执行撤销操作？", default=False):
            click.echo("已取消。")
            sys.exit(0)

    success, restored, failed, errors_detail = undo_run(store, run_id)

    try:
        store.update_bundle_undone_status(run_id, is_undone=True)
    except Exception:
        pass

    if success:
        click.echo(click.style(f"\n[撤销成功] 已恢复 {restored} 个文件", fg="green"))
    else:
        click.echo(click.style(f"\n[撤销部分完成] 恢复 {restored} 个，失败 {failed} 个", fg="yellow"))

    if verbose and errors_detail:
        click.echo("\n[撤销详情 - 错误]")
        for err in errors_detail:
            click.echo(click.style(f"  - {err}", fg="red"))

    run_data = store.get_run(run_id)
    snapshot_id = run_data.get("snapshot_id") if run_data else None
    if snapshot_id:
        snapshot = store.get_snapshot(snapshot_id)
        if snapshot:
            pre_validation = store.get_latest_validation(snapshot_id)

            store.invalidate_validation_for_snapshot(snapshot_id)

            result = validate_signoff_for_apply(
                store=store,
                snapshot=snapshot,
                current_config=config,
                require_signed=True,
            )

            save_validation_history(
                store=store,
                snapshot=snapshot,
                signoff_validation=result,
                triggered_by="undo",
                lock_allowed=True,
            )

            if pre_validation and pre_validation.status == "blocked" and not pre_validation.is_resolved:
                store.update_validation_resolution(
                    validation_id=pre_validation.validation_id,
                    resolved_by="cli",
                    resolution_note=f"撤销执行 {run_id}，状态已重置",
                    resolution_command="undo",
                )


@cli.command("export-plan", help="导出单个预案及其摘要 (JSON 或 CSV)，便于人工复核")
@click.option("-c", "--config", "config_path", required=True, type=click.Path(),
              help="配置文件路径 (YAML)")
@click.option("-p", "--plan-id", "plan_id", help="指定预案 ID (默认使用最近的)")
@click.option("-o", "--output", "output_path", required=True, type=click.Path(),
              help="导出文件路径")
@click.option("-f", "--format", "format", type=click.Choice(["json", "csv"]), default="json",
              help="导出格式 (默认: json)")
@click.option("-v", "--verbose", is_flag=True, help="显示详细信息")
def cmd_export_plan(config_path: str, plan_id: Optional[str], output_path: str, format: str, verbose: bool):
    try:
        config = load_config(config_path)
    except Exception as e:
        click.echo(f"[错误] 加载配置失败: {e}", err=True)
        sys.exit(1)

    store = _get_store(config)
    scanned = store.get_scan()

    if plan_id:
        plan = store.get_plan(plan_id)
    else:
        plan = store.get_last_plan()
        if plan:
            plan_id = store.get_last_plan_id()

    if not plan:
        click.echo("[错误] 未找到归档预案，请先执行 plan 命令。", err=True)
        sys.exit(1)

    from .models import PlannedMove
    moves_data = plan.get("moves", [])
    moves = [
        PlannedMove(
            id=m["id"],
            source_path=m["source_path"],
            target_path=m["target_path"],
            filename=m["filename"],
            matched_rule=m["matched_rule"],
            conflict_type=m.get("conflict_type"),
            conflict_detail=m.get("conflict_detail"),
        )
        for m in moves_data
    ]

    summary = build_plan_summary(config, scanned, moves)

    try:
        export_plan(plan, summary, scanned, output_path, format=format)
    except Exception as e:
        click.echo(f"[错误] 导出失败: {e}", err=True)
        sys.exit(1)

    click.echo(click.style(f"[导出成功] 预案导出文件: {output_path}", fg="green"))
    click.echo(f"  格式: {format.upper()}")
    click.echo(f"  预案ID: {plan_id}")
    click.echo(f"  摘要: {summary.total_files} 总文件, {summary.matched_files} 匹配, {summary.unmatched_files} 未命中, {summary.conflict_count} 冲突")

    snapshots = store.list_snapshots_with_signoff()
    plan_snapshots = [s for s in snapshots if s.get("plan_id") == plan_id]
    if plan_snapshots:
        status_map = {"signed": "已签收", "rejected": "已拒绝", "pending": "待处理"}
        click.echo()
        click.echo(click.style("[关联快照及签收]", fg="cyan"))
        for s in plan_snapshots:
            signoff_status = s.get("signoff_status", "未签收")
            if s.get("signoff_forced"):
                signoff_status += "*"
            if s.get("signoff_status_raw") == "signed":
                status_color = "green"
            elif s.get("signoff_status_raw") == "rejected":
                status_color = "red"
            elif s.get("signoff_status_raw") == "pending":
                status_color = "yellow"
            else:
                status_color = None
            status_display = click.style(signoff_status, fg=status_color) if status_color else signoff_status
            click.echo(f"  快照 {s['snapshot_id']}: {status_display}")
            if s.get("signed_by"):
                click.echo(f"    签收人: {s.get('signed_by')} at {s.get('signed_at', '')}")
            if s.get("signoff_deadline"):
                click.echo(f"    截止时间: {s.get('signoff_deadline')}")
            if s.get("signoff_notes"):
                notes_preview = s.get("signoff_notes", "")[:60] + "..." if len(s.get("signoff_notes", "")) > 60 else s.get("signoff_notes", "")
                click.echo(f"    说明: {notes_preview}")

    if verbose:
        click.echo(f"\n[摘要详情]")
        click.echo(f"  新建目标目录: {len(summary.new_target_dirs)} 个")
        for nd in summary.new_target_dirs:
            click.echo(f"    + {nd}")
        click.echo(f"  同目标目录规则组: {len(summary.rules_with_same_target)} 组")
        for tdir, rules in summary.rules_with_same_target.items():
            click.echo(f"    ! {tdir}: {', '.join(rules)}")


@cli.command("export", help="导出操作日志 (JSON 或 CSV)")
@click.option("-c", "--config", "config_path", required=True, type=click.Path(),
              help="配置文件路径 (YAML)")
@click.option("-o", "--output", "output_path", required=True, type=click.Path(),
              help="导出文件路径")
@click.option("-f", "--format", "format", type=click.Choice(["json", "csv"]), default="json",
              help="导出格式 (默认: json)")
@click.option("-v", "--verbose", is_flag=True, help="显示详细信息")
def cmd_export(config_path: str, output_path: str, format: str, verbose: bool):
    try:
        config = load_config(config_path)
    except Exception as e:
        click.echo(f"[错误] 加载配置失败: {e}", err=True)
        sys.exit(1)

    store = _get_store(config)

    try:
        export_logs(store, output_path, format=format)
    except Exception as e:
        click.echo(f"[错误] 导出失败: {e}", err=True)
        sys.exit(1)

    click.echo(click.style(f"[导出成功] 文件: {output_path}", fg="green"))
    click.echo(f"  格式: {format.upper()}")

    state = store.get_full_state()
    click.echo(f"  预案数: {len(state.get('plans', {}))}")
    click.echo(f"  执行数: {len(state.get('runs', {}))}")
    click.echo(f"  撤销数: {len(state.get('undo_records', []))}")

    if verbose:
        for plan_id, plan in state.get("plans", {}).items():
            created = plan.get("created_at", "N/A")
            has_conflict = "有冲突" if plan.get("has_conflicts") else "无冲突"
            click.echo(f"\n  预案 {plan_id}: {created} ({has_conflict})")

        for run in store.get_all_runs():
            rid = run.get("id", "N/A")
            created = run.get("created_at", "N/A")
            dry = "预演" if run.get("dry_run") else "实际"
            undone = "已撤销" if run.get("is_undone") else "未撤销"
            move_count = len(run.get("moves", []))
            click.echo(f"  执行 {rid}: {created} [{dry}] [{undone}] ({move_count} 条记录)")


@cli.command("list-runs", help="列出所有执行记录")
@click.option("-c", "--config", "config_path", required=True, type=click.Path(),
              help="配置文件路径 (YAML)")
def cmd_list_runs(config_path: str):
    try:
        config = load_config(config_path)
    except Exception as e:
        click.echo(f"[错误] 加载配置失败: {e}", err=True)
        sys.exit(1)

    store = _get_store(config)
    runs = store.get_all_runs()

    if not runs:
        click.echo("暂无执行记录。")
        return

    runs_sorted = sorted(runs, key=lambda r: r.get("created_at", ""), reverse=True)

    click.echo(f"{'Run ID':<14} {'创建时间':<27} {'预案ID':<14} {'模式':<6} {'状态':<8} {'记录数':<6}")
    click.echo("-" * 80)
    for r in runs_sorted:
        mode = "预演" if r.get("dry_run") else "实际"
        status = "已撤销" if r.get("is_undone") else "生效"
        created = r.get("created_at", "")[:26]
        count = len(r.get("moves", []))
        click.echo(f"{r['id']:<14} {created:<27} {r.get('plan_id',''):<14} {mode:<6} {status:<8} {count:<6}")


@cli.command("update-snapshot", help="更新批次快照的备注、标签、交接人或注意事项")
@click.option("-c", "--config", "config_path", required=True, type=click.Path(),
              help="配置文件路径 (YAML)")
@click.option("-s", "--snapshot-id", "snapshot_id", help="指定快照 ID (默认使用最近的)")
@click.option("--remark", help=f"批次备注 (最多 {MAX_REMARK_LENGTH} 字符)")
@click.option("--tag", "tags", multiple=True, help=f"标签，可多次指定 (最多 {MAX_TAGS_COUNT} 个，每个最多 {MAX_TAG_LENGTH} 字符)")
@click.option("--handler", help=f"交接人 (最多 {MAX_HANDLER_LENGTH} 字符)")
@click.option("--notes", help=f"注意事项 (最多 {MAX_NOTES_LENGTH} 字符)")
@click.option("--updated-by", default="cli", help="备注更新人 (默认: cli)")
@click.option("--append-tags", is_flag=True, help="追加标签而不是替换现有标签")
@click.option("--force", is_flag=True, help="强制覆盖已有备注（忽略冲突检测）")
@click.option("-y", "--yes", is_flag=True, help="跳过确认提示")
@click.option("-v", "--verbose", is_flag=True, help="显示详细信息")
def cmd_update_snapshot(
    config_path: str,
    snapshot_id: Optional[str],
    remark: Optional[str],
    tags: tuple,
    handler: Optional[str],
    notes: Optional[str],
    updated_by: str,
    append_tags: bool,
    force: bool,
    yes: bool,
    verbose: bool,
):
    try:
        config = load_config(config_path)
    except Exception as e:
        click.echo(f"[错误] 加载配置失败: {e}", err=True)
        sys.exit(1)

    store = _get_store(config)

    if snapshot_id:
        snapshot = store.get_snapshot(snapshot_id)
    else:
        snapshot = store.get_last_snapshot()

    if not snapshot:
        click.echo("[错误] 未找到批次快照，请先执行 plan 命令。", err=True)
        sys.exit(1)

    snapshot_id = snapshot.snapshot_id
    old_remark = snapshot.remark

    if not remark and not tags and not handler and not notes:
        click.echo("[错误] 至少需要指定 --remark、--tag、--handler 或 --notes 中的一项。", err=True)
        sys.exit(1)

    new_tags = list(tags) if tags else []
    if append_tags and old_remark.tags:
        new_tags = list(old_remark.tags) + new_tags

    final_remark = remark if remark is not None else old_remark.remark
    final_handler = handler if handler is not None else old_remark.handler
    final_notes = notes if notes is not None else old_remark.notes
    final_tags = new_tags if tags or append_tags else old_remark.tags

    new_remark = build_remark(
        remark=final_remark,
        tags=final_tags,
        handler=final_handler,
        notes=final_notes,
        updated_by=updated_by,
    )

    validation = validate_remark(new_remark)
    if validation.has_errors:
        click.echo(click.style("[错误] 备注信息验证失败：", fg="red", bold=True))
        for err in validation.errors:
            click.echo(click.style(f"  - {err}", fg="red"))
        sys.exit(1)
    if validation.warnings:
        for warn in validation.warnings:
            click.echo(click.style(f"[警告] {warn}", fg="yellow"))

    click.echo(f"[更新快照] ID: {snapshot_id}")
    click.echo()
    click.echo(click.style("[当前备注]", fg="cyan"))
    if old_remark.remark:
        click.echo(f"  备注: {old_remark.remark}")
    if old_remark.tags:
        click.echo(f"  标签: {', '.join(old_remark.tags)}")
    if old_remark.handler:
        click.echo(f"  交接人: {old_remark.handler}")
    if old_remark.notes:
        click.echo(f"  注意事项: {old_remark.notes[:100]}..." if len(old_remark.notes) > 100 else f"  注意事项: {old_remark.notes}")
    if old_remark.updated_at:
        click.echo(f"  最后更新: {old_remark.updated_at} by {old_remark.updated_by}")
    if old_remark.is_empty():
        click.echo("  (无备注信息)")

    click.echo()
    click.echo(click.style("[更新后备注]", fg="green"))
    if new_remark.remark:
        click.echo(f"  备注: {new_remark.remark}")
    if new_remark.tags:
        click.echo(f"  标签: {', '.join(new_remark.tags)}")
    if new_remark.handler:
        click.echo(f"  交接人: {new_remark.handler}")
    if new_remark.notes:
        click.echo(f"  注意事项: {new_remark.notes[:100]}..." if len(new_remark.notes) > 100 else f"  注意事项: {new_remark.notes}")
    click.echo(f"  更新人: {new_remark.updated_by}")

    field_changes = diff_remarks(old_remark, new_remark)
    if field_changes:
        click.echo()
        click.echo(click.style("[备注变更对比]", fg="cyan"))
        for fc in field_changes:
            click.echo(f"  {format_remark_change(fc)}")

    if not yes:
        if not click.confirm("\n确认更新备注信息？", default=False):
            click.echo("已取消。")
            sys.exit(0)

    success, history, errors = store.update_snapshot_remark(
        snapshot_id=snapshot_id,
        new_remark=new_remark,
        changed_by=updated_by,
        change_source="cli",
        allow_overwrite=force,
    )

    if not success:
        click.echo()
        click.echo(click.style("[错误] 备注更新失败：", fg="red", bold=True))
        for err in errors:
            click.echo(click.style(f"  - {err}", fg="red"))
        if history and history.conflict_detected:
            click.echo()
            click.echo(click.style("[提示] 备注内容存在冲突，不会自动覆盖。", fg="yellow"))
            click.echo(click.style("  使用 --force 可强制覆盖现有备注。", fg="yellow"))
        sys.exit(1)

    click.echo()
    click.echo(click.style(f"[更新成功] 快照备注已更新", fg="green"))
    if history and history.conflict_detected:
        forced_label = " (强制覆盖)" if history.forced else ""
        click.echo(click.style(f"[注意] 已覆盖原有备注{forced_label}：{history.conflict_detail}", fg="yellow"))
    if verbose and history:
        click.echo(f"  历史记录 ID: {history.history_id}")
        click.echo(f"  修改时间: {history.changed_at}")
        click.echo(f"  是否强制: {'是' if history.forced else '否'}")
        if history.changed_fields:
            click.echo(f"  变更字段: {len(history.changed_fields)} 个")
            for fc in history.changed_fields:
                click.echo(f"    {format_remark_change(fc)}")


@cli.command("list-snapshots", help="列出所有批次快照")
@click.option("-c", "--config", "config_path", required=True, type=click.Path(),
              help="配置文件路径 (YAML)")
@click.option("-v", "--verbose", is_flag=True, help="显示备注和签收详情")
def cmd_list_snapshots(config_path: str, verbose: bool):
    try:
        config = load_config(config_path)
    except Exception as e:
        click.echo(f"[错误] 加载配置失败: {e}", err=True)
        sys.exit(1)

    store = _get_store(config)
    snapshots = store.list_snapshots_with_signoff()

    if not snapshots:
        click.echo("暂无批次快照。")
        return

    if verbose:
        click.echo(f"{'快照ID':<14} {'创建时间':<27} {'预案ID':<14} {'移动项':<8} {'冲突':<6} {'来源':<10} {'签收状态':<10} {'签收人':<12} {'备注':<25}")
        click.echo("-" * 140)
        for s in snapshots:
            created = s.get("created_at", "")[:26]
            has_conflict = "是" if s.get("has_conflicts") else "否"
            source = "导入" if s.get("imported") else "本地"
            remark_display = s.get("remark", "")[:23] + "..." if len(s.get("remark", "")) > 25 else s.get("remark", "")
            tags_full = ", ".join(s.get("tags", []))
            signoff_status = s.get("signoff_status", "未签收")
            if s.get("signoff_forced"):
                signoff_status += "*"
            signed_by = s.get("signed_by", "")[:10] + "..." if len(s.get("signed_by", "")) > 10 else s.get("signed_by", "")
            if s.get("signoff_status_raw") == "signed":
                status_color = "green"
            elif s.get("signoff_status_raw") == "rejected":
                status_color = "red"
            elif s.get("signoff_status_raw") == "pending":
                status_color = "yellow"
            else:
                status_color = None
            status_display = click.style(signoff_status, fg=status_color) if status_color else signoff_status
            click.echo(
                f"{s['snapshot_id']:<14} {created:<27} {s.get('plan_id',''):<14} "
                f"{s.get('move_count',0):<8} {has_conflict:<6} {source:<10} "
                f"{status_display:<10} {signed_by:<12} {remark_display:<25}"
            )
            if tags_full:
                click.echo(f"  {'':<14} {'':<27} {'':<14} {'':<8} {'':<6} {'':<10} {'':<10} {'':<12} 标签: {tags_full}")
            if s.get("notes"):
                notes_display = s.get("notes", "")[:80] + "..." if len(s.get("notes", "")) > 80 else s.get("notes", "")
                click.echo(f"  {'':<14} {'':<27} {'':<14} {'':<8} {'':<6} {'':<10} {'':<10} {'':<12} 注意事项: {notes_display}")
            if s.get("signed_at"):
                click.echo(f"  {'':<14} {'':<27} {'':<14} {'':<8} {'':<6} {'':<10} {'':<10} {'':<12} 签收时间: {s.get('signed_at', '')}")
            if s.get("signoff_deadline"):
                click.echo(f"  {'':<14} {'':<27} {'':<14} {'':<8} {'':<6} {'':<10} {'':<10} {'':<12} 截止时间: {s.get('signoff_deadline', '')}")
            if s.get("signoff_notes"):
                signoff_notes = s.get("signoff_notes", "")[:80] + "..." if len(s.get("signoff_notes", "")) > 80 else s.get("signoff_notes", "")
                click.echo(f"  {'':<14} {'':<27} {'':<14} {'':<8} {'':<6} {'':<10} {'':<10} {'':<12} 签收说明: {signoff_notes}")
            if s.get("remark_updated_at"):
                click.echo(f"  {'':<14} {'':<27} {'':<14} {'':<8} {'':<6} {'':<10} {'':<10} {'':<12} 备注更新: {s.get('remark_updated_at', '')} by {s.get('remark_updated_by', '')}")
    else:
        click.echo(f"{'快照ID':<14} {'创建时间':<27} {'预案ID':<14} {'移动项':<8} {'冲突':<6} {'来源':<10} {'签收状态':<10} {'备注':<25}")
        click.echo("-" * 130)
        for s in snapshots:
            created = s.get("created_at", "")[:26]
            has_conflict = "是" if s.get("has_conflicts") else "否"
            source = "导入" if s.get("imported") else "本地"
            remark_display = s.get("remark", "")[:23] + "..." if len(s.get("remark", "")) > 25 else s.get("remark", "")
            signoff_status = s.get("signoff_status", "未签收")
            if s.get("signoff_forced"):
                signoff_status += "*"
            if s.get("signoff_status_raw") == "signed":
                status_color = "green"
            elif s.get("signoff_status_raw") == "rejected":
                status_color = "red"
            elif s.get("signoff_status_raw") == "pending":
                status_color = "yellow"
            else:
                status_color = None
            status_display = click.style(signoff_status, fg=status_color) if status_color else signoff_status
            click.echo(
                f"{s['snapshot_id']:<14} {created:<27} {s.get('plan_id',''):<14} "
                f"{s.get('move_count',0):<8} {has_conflict:<6} {source:<10} "
                f"{status_display:<10} {remark_display:<25}"
            )


@cli.command("export-snapshot", help="导出批次快照为 JSON 文件，可用于复核或二次执行")
@click.option("-c", "--config", "config_path", required=True, type=click.Path(),
              help="配置文件路径 (YAML)")
@click.option("-s", "--snapshot-id", "snapshot_id", help="指定快照 ID (默认使用最近的)")
@click.option("-o", "--output", "output_path", required=True, type=click.Path(),
              help="导出文件路径")
@click.option("-v", "--verbose", is_flag=True, help="显示详细信息")
def cmd_export_snapshot(config_path: str, snapshot_id: Optional[str], output_path: str, verbose: bool):
    try:
        config = load_config(config_path)
    except Exception as e:
        click.echo(f"[错误] 加载配置失败: {e}", err=True)
        sys.exit(1)

    store = _get_store(config)

    if snapshot_id:
        snapshot = store.get_snapshot(snapshot_id)
    else:
        snapshot = store.get_last_snapshot()

    if not snapshot:
        click.echo("[错误] 未找到批次快照，请先执行 plan 命令。", err=True)
        sys.exit(1)

    all_signoffs = store.get_signoffs_by_snapshot(snapshot.snapshot_id)
    snapshot.signoffs = all_signoffs

    all_signoff_conflicts = store.list_signoff_conflicts(snapshot_id=snapshot.snapshot_id)
    snapshot.signoff_conflicts = all_signoff_conflicts

    try:
        export_snapshot_to_file(snapshot, output_path)
    except Exception as e:
        click.echo(f"[错误] 导出失败: {e}", err=True)
        sys.exit(1)

    click.echo(click.style(f"[导出成功] 快照导出文件: {output_path}", fg="green"))
    click.echo(f"  快照ID: {snapshot.snapshot_id}")
    click.echo(f"  预案ID: {snapshot.plan_id}")
    click.echo(f"  创建时间: {snapshot.created_at}")
    click.echo(f"  移动计划: {len(snapshot.moves)} 条")
    click.echo(f"  未命中规则: {len(snapshot.unmatched_files)} 个")
    click.echo(f"  新建目录: {len(snapshot.new_target_dirs)} 个")

    if not snapshot.remark.is_empty():
        click.echo()
        click.echo(click.style("[备注信息]", fg="cyan"))
        if snapshot.remark.remark:
            click.echo(f"  备注: {snapshot.remark.remark}")
        if snapshot.remark.tags:
            click.echo(f"  标签: {', '.join(snapshot.remark.tags)}")
        if snapshot.remark.handler:
            click.echo(f"  交接人: {snapshot.remark.handler}")
        if snapshot.remark.notes:
            click.echo(f"  注意事项: {snapshot.remark.notes}")
        if snapshot.remark.updated_at:
            click.echo(f"  更新时间: {snapshot.remark.updated_at} by {snapshot.remark.updated_by}")

    signoff = store.get_active_signoff(snapshot.snapshot_id)
    if signoff:
        status_map = {"signed": "已签收", "rejected": "已拒绝", "pending": "待处理"}
        click.echo()
        click.echo(click.style("[签收信息]", fg="cyan"))
        click.echo(f"  签收ID: {signoff.signoff_id}")
        click.echo(f"  状态: {status_map.get(signoff.status, signoff.status)}")
        click.echo(f"  签收人: {signoff.signed_by}")
        click.echo(f"  签收时间: {signoff.signed_at}")
        if signoff.deadline:
            click.echo(f"  截止时间: {signoff.deadline}")
        if signoff.notes:
            click.echo(f"  补充说明: {signoff.notes}")
        if signoff.forced:
            click.echo(click.style(f"  [强制] 该签收为强制覆盖", fg="yellow"))

    if all_signoff_conflicts:
        pending_count = len([c for c in all_signoff_conflicts if c.status == "pending"])
        resolved_count = len(all_signoff_conflicts) - pending_count
        click.echo()
        click.echo(click.style("[签收冲突]", fg="cyan"))
        click.echo(f"  冲突记录总数: {len(all_signoff_conflicts)}")
        if pending_count:
            click.echo(click.style(f"  未解决: {pending_count}", fg="yellow"))
        if resolved_count:
            click.echo(click.style(f"  已处理: {resolved_count}", fg="green"))
        if verbose:
            for sc in all_signoff_conflicts:
                click.echo()
                click.echo(format_signoff_conflict_summary(sc))

    if verbose and snapshot.unmatched_files:
        click.echo("\n[未命中规则文件]")
        for uf in snapshot.unmatched_files:
            reason_desc = "无匹配规则" if uf.reason == "no_rule_match" else "扩展名过滤"
            click.echo(f"  - {uf.filename} ({reason_desc})")

    if verbose and snapshot.new_target_dirs:
        click.echo("\n[新建目标目录]")
        for nd in snapshot.new_target_dirs:
            click.echo(f"  + {nd.path} ({nd.file_count} 个文件)")


@cli.command("import-snapshot", help="从 JSON 文件导入批次快照，用于复核或二次执行")
@click.option("-c", "--config", "config_path", required=True, type=click.Path(),
              help="配置文件路径 (YAML)")
@click.option("-i", "--input", "input_path", required=True, type=click.Path(),
              help="快照 JSON 文件路径")
@click.option("--remark-only", is_flag=True, help="仅导入备注信息，不更新快照其他内容")
@click.option("--updated-by", default="import", help="备注更新人 (默认: import)")
@click.option("-y", "--yes", is_flag=True, help="跳过确认提示")
@click.option("-v", "--verbose", is_flag=True, help="显示详细信息")
@click.option("--force", is_flag=True, help="即使有警告/冲突也强制导入")
def cmd_import_snapshot(
    config_path: str,
    input_path: str,
    remark_only: bool,
    updated_by: str,
    yes: bool,
    verbose: bool,
    force: bool,
):
    try:
        config = load_config(config_path)
    except Exception as e:
        click.echo(f"[错误] 加载配置失败: {e}", err=True)
        sys.exit(1)

    store = _get_store(config)

    try:
        snapshot = load_snapshot_from_file(input_path)
    except Exception as e:
        click.echo(f"[错误] 加载快照文件失败: {e}", err=True)
        sys.exit(1)

    click.echo(click.style("[导入快照] 解析成功", fg="green"))
    click.echo(f"  快照ID: {snapshot.snapshot_id}")
    click.echo(f"  创建时间: {snapshot.created_at}")
    click.echo(f"  预案ID: {snapshot.plan_id}")
    click.echo(f"  移动计划: {len(snapshot.moves)} 条")
    click.echo(f"  未命中规则: {len(snapshot.unmatched_files)} 个")

    if not snapshot.remark.is_empty():
        click.echo()
        click.echo(click.style("[导入备注信息]", fg="cyan"))
        if snapshot.remark.remark:
            click.echo(f"  备注: {snapshot.remark.remark}")
        if snapshot.remark.tags:
            click.echo(f"  标签: {', '.join(snapshot.remark.tags)}")
        if snapshot.remark.handler:
            click.echo(f"  交接人: {snapshot.remark.handler}")
        if snapshot.remark.notes:
            click.echo(f"  注意事项: {snapshot.remark.notes}")

    imported_signoffs = []
    if hasattr(snapshot, 'signoffs') and snapshot.signoffs:
        imported_signoffs = snapshot.signoffs
        status_map = {"signed": "已签收", "rejected": "已拒绝", "pending": "待处理"}
        click.echo()
        click.echo(click.style("[导入签收信息]", fg="cyan"))
        click.echo(f"  签收记录数: {len(imported_signoffs)}")
        for sig in imported_signoffs:
            click.echo(f"    - {status_map.get(sig.status, sig.status)} by {sig.signed_by} at {sig.signed_at}")
            if sig.deadline:
                click.echo(f"      截止时间: {sig.deadline}")
            if sig.notes:
                notes_preview = sig.notes[:50] + "..." if len(sig.notes) > 50 else sig.notes
                click.echo(f"      说明: {notes_preview}")

    validation = validate_import_snapshot(snapshot)
    diff_result = diff_config(config, snapshot.config_snapshot)

    if not snapshot.remark.is_empty():
        remark_validation = validate_remark(snapshot.remark)
        if remark_validation.has_errors:
            click.echo(click.style("\n[错误] 备注信息验证失败：", fg="red", bold=True))
            for err in remark_validation.errors:
                click.echo(click.style(f"  - {err}", fg="red"))
            sys.exit(1)

    def _make_log(status: str, forced_flag: bool = False) -> ImportLog:
        return ImportLog(
            import_id=generate_id(),
            timestamp=now_iso(),
            status=status,
            source_file=os.path.abspath(input_path),
            snapshot_id=snapshot.snapshot_id,
            plan_id=snapshot.plan_id,
            move_count=len(snapshot.moves),
            errors=list(validation.errors),
            warnings=list(validation.warnings),
            config_diff=diff_result.to_dict() if diff_result.has_diff else None,
            forced=forced_flag,
            remark_conflict_detail=remark_conflict_detail,
        )

    if not remark_only and validation.has_errors:
        click.echo(click.style("\n[错误] 快照验证失败，无法导入：", fg="red", bold=True))
        for err in validation.errors:
            click.echo(click.style(f"  - {err}", fg="red"))

        if not force:
            store.add_import_log(_make_log("failed"))
            click.echo(click.style("\n使用 --force 可强制导入，但执行时可能出错。", fg="yellow"))
            sys.exit(1)
        else:
            click.echo(click.style("\n[警告] 强制导入，执行时可能出错。", fg="yellow"))

    if validation.warnings:
        click.echo(click.style("\n[警告] 导入时检测到以下问题：", fg="yellow"))
        for warn in validation.warnings:
            click.echo(click.style(f"  - {warn}", fg="yellow"))

    if not remark_only and diff_result.has_diff:
        click.echo(click.style("\n[提示] 当前配置与快照配置不一致：", fg="cyan"))
        if diff_result.source_dir_changed:
            click.echo(f"  - 源目录变更: {snapshot.config_snapshot.source_dir} -> {config.source_dir}")
        if diff_result.dest_dir_changed:
            click.echo(f"  - 目标目录变更: {snapshot.config_snapshot.dest_dir} -> {config.dest_dir}")
        if diff_result.added_rules:
            click.echo(f"  - 新增规则: {', '.join(diff_result.added_rules)}")
        if diff_result.removed_rules:
            click.echo(f"  - 删除规则: {', '.join(diff_result.removed_rules)}")
        if diff_result.modified_rules:
            click.echo(f"  - 修改规则: {', '.join(diff_result.modified_rules)}")

    existing = store.get_snapshot(snapshot.snapshot_id)
    remark_conflict = False
    remark_conflict_detail = ""
    remark_field_changes = []
    signoff_conflict = False
    signoff_conflict_detail = ""
    signoff_field_changes = []
    created_signoff_conflict_obj: Optional[SignoffConflictState] = None

    if imported_signoffs:
        existing_signoffs = store.get_signoffs_by_snapshot(snapshot.snapshot_id)
        if existing_signoffs:
            active_existing = [s for s in existing_signoffs if s.is_active]
            if active_existing:
                import_src = os.path.abspath(input_path)
                for imported_sig in imported_signoffs:
                    if imported_sig.is_active:
                        for active_sig in active_existing:
                            if imported_sig.signoff_id != active_sig.signoff_id:
                                signoff_field_changes = diff_signoffs(active_sig, imported_sig)
                                if signoff_field_changes:
                                    signoff_conflict = True
                                    created_signoff_conflict_obj = detect_and_create_signoff_conflict(
                                        store=store,
                                        snapshot_id=snapshot.snapshot_id,
                                        plan_id=snapshot.plan_id,
                                        local_signoff=active_sig,
                                        imported_signoff=imported_sig,
                                        import_source=import_src,
                                    )
                                    conflicts = []
                                    for fc in signoff_field_changes:
                                        conflicts.append(
                                            f"{fc.field_name} 冲突: 旧='{str(fc.old_value)[:30]}...' 新='{str(fc.new_value)[:30]}...'"
                                        )
                                    signoff_conflict_detail = "; ".join(conflicts)
                                    break
                        if signoff_conflict:
                            break

            if signoff_conflict and created_signoff_conflict_obj:
                click.echo(click.style("\n[警告] 检测到签收冲突，已创建冲突状态记录：", fg="yellow", bold=True))
                click.echo(format_signoff_conflict_summary(created_signoff_conflict_obj))
                click.echo()
                click.echo(click.style("[签收变更对比]", fg="cyan"))
                for fc in signoff_field_changes:
                    click.echo(f"  {format_signoff_change(fc)}")

                click.echo()
                click.echo(click.style("[处理方式]", fg="yellow", bold=True))
                resolution_cmd_example = (
                    f"  python -m invoice_organizer resolve-signoff-conflict "
                    f"-c {config_path} --snapshot-id {snapshot.snapshot_id} "
                    f"--resolution <keep-local|keep-imported|new-signoff> "
                    f"--by <处理人> --note \"<处理说明>\""
                )
                click.echo(resolution_cmd_example)
                click.echo()
                click.echo(click.style("  - keep-local:    保留本地原有签收，丢弃导入签收", fg="cyan"))
                click.echo(click.style("  - keep-imported: 保留导入签收，丢弃本地签收", fg="cyan"))
                click.echo(click.style("  - new-signoff:   由本地用户重新签收（覆盖前两者）", fg="cyan"))

                if not force:
                    store.add_import_log(_make_log("failed"))
                    click.echo()
                    click.echo(click.style("[提示] 未使用 --force，签收冲突状态已保存，但应用被阻止。", fg="yellow"))
                    click.echo(click.style("  使用 resolve-signoff-conflict 处理后，或使用 --force 可继续导入快照。", fg="yellow"))
                    sys.exit(1)
                else:
                    click.echo(click.style("[警告] 使用 --force：快照数据将继续导入，但签收冲突仍标记为待处理，", fg="yellow"))
                    click.echo(click.style("         check-signoff、apply --dry-run 和正式 apply 在处理前都会被拦截。", fg="yellow"))

    if existing:
        click.echo(click.style("\n[提示] 快照 ID 已存在。", fg="yellow"))

        if not existing.remark.is_empty() and not snapshot.remark.is_empty():
            remark_field_changes = diff_remarks(existing.remark, snapshot.remark)

            conflicts = []
            for fc in remark_field_changes:
                if fc.field_name == "remark" and existing.remark.remark:
                    conflicts.append(f"备注内容冲突: 旧='{existing.remark.remark[:50]}...' 新='{snapshot.remark.remark[:50]}...'")
                elif fc.field_name == "handler" and existing.remark.handler:
                    conflicts.append(f"交接人冲突: 旧='{existing.remark.handler}' 新='{snapshot.remark.handler}'")
                elif fc.field_name == "notes" and existing.remark.notes:
                    conflicts.append(f"注意事项冲突: 旧长度={len(existing.remark.notes)} 新长度={len(snapshot.remark.notes)}")
                elif fc.field_name == "tags" and existing.remark.tags:
                    old_set = set(existing.remark.tags)
                    new_set = set(snapshot.remark.tags)
                    added = new_set - old_set
                    removed = old_set - new_set
                    if added or removed:
                        conflicts.append(f"标签冲突: 新增={sorted(added)} 删除={sorted(removed)}")

            if conflicts:
                remark_conflict = True
                remark_conflict_detail = "; ".join(conflicts)
                click.echo(click.style("\n[警告] 检测到备注冲突：", fg="yellow", bold=True))
                for c in conflicts:
                    click.echo(click.style(f"  - {c}", fg="yellow"))

                click.echo()
                click.echo(click.style("[备注变更对比]", fg="cyan"))
                for fc in remark_field_changes:
                    click.echo(f"  {format_remark_change(fc)}")

                if not force:
                    new_remark = snapshot.remark
                    new_remark.updated_at = now_iso()
                    new_remark.updated_by = updated_by
                    store.update_snapshot_remark(
                        snapshot_id=snapshot.snapshot_id,
                        new_remark=new_remark,
                        changed_by=updated_by,
                        change_source="import",
                        allow_overwrite=False,
                    )

                    click.echo()
                    click.echo(click.style("[提示] 备注内容不同，不会自动覆盖。", fg="yellow"))
                    click.echo(click.style("  使用 --force 可强制覆盖现有备注。", fg="yellow"))
                    click.echo(click.style("  使用 --remark-only 可仅更新备注而不修改快照其他内容。", fg="yellow"))
                    store.add_import_log(_make_log("failed"))
                    sys.exit(1)

    if not yes:
        if remark_only:
            confirm_msg = "\n确认仅导入备注信息？"
        else:
            confirm_msg = "\n确认导入该批次快照？"
        if not click.confirm(confirm_msg, default=False):
            store.add_import_log(_make_log("cancelled"))
            click.echo("已取消。")
            sys.exit(0)

    snapshot.imported = True
    snapshot.import_source = os.path.abspath(input_path)

    if remark_only and existing:
        new_remark = snapshot.remark
        new_remark.updated_at = now_iso()
        new_remark.updated_by = updated_by

        success, history, errors = store.update_snapshot_remark(
            snapshot_id=snapshot.snapshot_id,
            new_remark=new_remark,
            changed_by=updated_by,
            change_source="import",
            allow_overwrite=force,
        )

        if not success:
            click.echo()
            click.echo(click.style("[错误] 备注导入失败：", fg="red", bold=True))
            for err in errors:
                click.echo(click.style(f"  - {err}", fg="red"))
            store.add_import_log(_make_log("failed"))
            sys.exit(1)

        click.echo(click.style(f"\n[导入成功] 备注已更新: {snapshot.snapshot_id}", fg="green"))
        if remark_conflict and force:
            click.echo(click.style(f"[注意] 已强制覆盖备注冲突：{remark_conflict_detail}", fg="yellow"))
            if remark_field_changes:
                click.echo(click.style("[备注变更对比]", fg="cyan"))
                for fc in remark_field_changes:
                    click.echo(f"  {format_remark_change(fc)}")
    else:
        if remark_conflict and force:
            snapshot.remark.updated_at = now_iso()
            snapshot.remark.updated_by = updated_by

        store.save_snapshot(snapshot)

        if remark_conflict and force:
            store.update_snapshot_remark(
                snapshot_id=snapshot.snapshot_id,
                new_remark=snapshot.remark,
                changed_by=updated_by,
                change_source="import",
                allow_overwrite=True,
            )
            history = store.get_remark_history(snapshot.snapshot_id)
            click.echo(click.style(f"\n[导入成功] 快照已保存（强制覆盖备注冲突）: {snapshot.snapshot_id}", fg="green"))
            click.echo(click.style(f"[注意] 冲突内容: {remark_conflict_detail}", fg="yellow"))
            if remark_field_changes:
                click.echo(click.style("[备注变更对比]", fg="cyan"))
                for fc in remark_field_changes:
                    click.echo(f"  {format_remark_change(fc)}")
        else:
            click.echo(click.style(f"\n[导入成功] 快照已保存: {snapshot.snapshot_id}", fg="green"))

    if imported_signoffs:
        for sig in imported_signoffs:
            sig.import_source = os.path.abspath(input_path)
            sig.forced = signoff_conflict and force
            if signoff_conflict and force:
                sig.conflict_detail = signoff_conflict_detail
            store.add_signoff(sig)
        if signoff_conflict and force:
            click.echo(click.style(f"[导入成功] 已强制导入 {len(imported_signoffs)} 条签收记录（覆盖冲突）", fg="green"))
        else:
            click.echo(click.style(f"[导入成功] 已导入 {len(imported_signoffs)} 条签收记录", fg="green"))

    snapshot_signoff_conflicts = getattr(snapshot, 'signoff_conflicts', None)
    if snapshot_signoff_conflicts:
        restored_count = 0
        skipped_pending_count = 0
        for sc in snapshot_signoff_conflicts:
            existing = store.get_signoff_conflict(sc.conflict_id)
            if existing:
                continue
            if (sc.status == "pending" and
                    store.get_pending_conflict_by_snapshot(sc.snapshot_id)):
                skipped_pending_count += 1
                continue
            store.save_signoff_conflict(sc)
            restored_count += 1
        if restored_count:
            click.echo(click.style(f"[导入成功] 已恢复 {restored_count} 条签收冲突历史记录", fg="green"))
        if skipped_pending_count:
            click.echo(click.style(f"[提示] 跳过 {skipped_pending_count} 条待处理冲突（本地已存在）", fg="yellow"))

    forced_flag = force and (validation.has_errors or remark_conflict or signoff_conflict) and not remark_only
    if remark_only and force and remark_conflict:
        forced_flag = True
    if force and signoff_conflict:
        forced_flag = True
    if forced_flag:
        store.add_import_log(_make_log("forced", forced_flag=True))
    elif validation.has_errors and force and not remark_only:
        store.add_import_log(_make_log("forced", forced_flag=True))
    else:
        store.add_import_log(_make_log("success"))

    if not remark_only:
        pre_validation = store.get_latest_validation(snapshot.snapshot_id)

        store.invalidate_validation_for_snapshot(snapshot.snapshot_id)

        result = validate_signoff_for_apply(
            store=store,
            snapshot=snapshot,
            current_config=config,
            require_signed=True,
        )

        save_validation_history(
            store=store,
            snapshot=snapshot,
            signoff_validation=result,
            triggered_by="import-snapshot",
            lock_allowed=True,
        )

        if pre_validation and pre_validation.status == "blocked" and not pre_validation.is_resolved:
            store.update_validation_resolution(
                validation_id=pre_validation.validation_id,
                resolved_by=updated_by,
                resolution_note=f"从 {input_path} 导入快照",
                resolution_command="import-snapshot",
            )

    click.echo(f"  可使用 apply -s {snapshot.snapshot_id} 执行，或 export-snapshot 导出复核。")


@cli.command("diff-plans", help="对比两版预案的差异，支持导出 JSON/CSV")
@click.option("-c", "--config", "config_path", required=True, type=click.Path(),
              help="配置文件路径 (YAML)")
@click.option("--old-snapshot", "old_snapshot_id", help="旧版快照 ID (默认使用倒数第二个)")
@click.option("--new-snapshot", "new_snapshot_id", help="新版快照 ID (默认使用最新的)")
@click.option("-o", "--output", "output_path", help="导出文件路径 (不指定则仅显示)")
@click.option("-f", "--format", "format", type=click.Choice(["json", "csv"]), default="json",
              help="导出格式 (默认: json)")
@click.option("-v", "--verbose", is_flag=True, help="显示详细信息")
@click.option("--save", "save_diff", is_flag=True, help="保存差异记录到状态文件")
def cmd_diff_plans(
    config_path: str,
    old_snapshot_id: Optional[str],
    new_snapshot_id: Optional[str],
    output_path: Optional[str],
    format: str,
    verbose: bool,
    save_diff: bool,
):
    try:
        config = load_config(config_path)
    except Exception as e:
        click.echo(f"[错误] 加载配置失败: {e}", err=True)
        sys.exit(1)

    store = _get_store(config)
    snapshots = store.list_snapshots()

    if len(snapshots) < 2:
        click.echo("[错误] 至少需要两个快照才能对比。请先执行至少两次 plan。", err=True)
        sys.exit(1)

    if new_snapshot_id:
        new_snapshot = store.get_snapshot(new_snapshot_id)
        if not new_snapshot:
            click.echo(f"[错误] 未找到新版快照: {new_snapshot_id}", err=True)
            sys.exit(1)
    else:
        new_snapshot = store.get_last_snapshot()
        if not new_snapshot:
            click.echo("[错误] 未找到快照。", err=True)
            sys.exit(1)

    if old_snapshot_id:
        old_snapshot = store.get_snapshot(old_snapshot_id)
        if not old_snapshot:
            click.echo(f"[错误] 未找到旧版快照: {old_snapshot_id}", err=True)
            sys.exit(1)
    else:
        all_snaps = sorted(snapshots, key=lambda s: s["created_at"])
        if len(all_snaps) < 2:
            click.echo("[错误] 至少需要两个快照才能对比。", err=True)
            sys.exit(1)
        old_snapshot_id = all_snaps[-2]["snapshot_id"]
        old_snapshot = store.get_snapshot(old_snapshot_id)

    diff_result = diff_plans(old_snapshot, new_snapshot)

    click.echo(click.style("[预案差异对比]", fg="cyan", bold=True))
    click.echo(f"  旧版: 预案 {diff_result.old_plan_id} / 快照 {diff_result.old_snapshot_id}")
    click.echo(f"  新版: 预案 {diff_result.new_plan_id} / 快照 {diff_result.new_snapshot_id}")
    click.echo(f"  对比时间: {diff_result.diff_timestamp}")
    click.echo()

    click.echo(click.style("[移动计划变化]", fg="cyan"))
    click.echo(f"  旧版移动项: {diff_result.old_move_count}")
    click.echo(f"  新版移动项: {diff_result.new_move_count}")
    click.echo(f"  新增: {len(diff_result.added_moves)}")
    click.echo(f"  删除: {len(diff_result.removed_moves)}")
    click.echo(f"  目标路径变化: {len(diff_result.target_changed)}")
    click.echo(f"  匹配规则变化: {len(diff_result.rule_changed)}")
    click.echo(f"  冲突状态变化: {len(diff_result.conflict_changed)}")
    click.echo(f"  未变化: {len(diff_result.unchanged_moves)}")

    if diff_result.total_changed_moves > 0:
        click.echo(click.style(f"  总计变化: {diff_result.total_changed_moves} 项", fg="yellow"))
    else:
        click.echo(click.style(f"  无变化", fg="green"))

    click.echo()
    click.echo(click.style("[未命中文件变化]", fg="cyan"))
    click.echo(f"  旧版未命中: {diff_result.old_unmatched_count}")
    click.echo(f"  新版未命中: {diff_result.new_unmatched_count}")
    click.echo(f"  新增未命中: {len(diff_result.added_unmatched)}")
    click.echo(f"  不再未命中: {len(diff_result.removed_unmatched)}")

    click.echo()
    click.echo(click.style("[规则变化]", fg="cyan"))
    if diff_result.added_rules:
        click.echo(click.style(f"  新增规则: {', '.join(diff_result.added_rules)}", fg="green"))
    if diff_result.removed_rules:
        click.echo(click.style(f"  删除规则: {', '.join(diff_result.removed_rules)}", fg="red"))
    if diff_result.modified_rules:
        click.echo(click.style(f"  修改规则: {', '.join(diff_result.modified_rules)}", fg="yellow"))
    if not diff_result.added_rules and not diff_result.removed_rules and not diff_result.modified_rules:
        click.echo(f"  规则无变化")

    if diff_result.config_diff:
        click.echo()
        click.echo(click.style("[配置差异]", fg="yellow"))
        cd = diff_result.config_diff
        if cd.get("source_dir_changed"):
            click.echo(f"  - 源目录变更")
        if cd.get("dest_dir_changed"):
            click.echo(f"  - 目标目录变更")
        if cd.get("extensions_changed"):
            click.echo(f"  - 文件扩展名过滤变更")
        if cd.get("recursive_changed"):
            click.echo(f"  - 递归扫描变更")

    if verbose:
        if diff_result.target_changed:
            click.echo()
            click.echo(click.style("[详情] 目标路径变化的文件:", fg="cyan"))
            for m in diff_result.target_changed:
                click.echo(f"  ! {m.filename}")
                click.echo(f"    旧: {m.old_target_path}")
                click.echo(f"    新: {m.new_target_path}")
                click.echo(f"    规则: {m.old_matched_rule} -> {m.new_matched_rule}")

        if diff_result.conflict_changed:
            click.echo()
            click.echo(click.style("[详情] 冲突状态变化的文件:", fg="yellow"))
            for m in diff_result.conflict_changed:
                old_status = m.old_conflict_type or "(无冲突)"
                new_status = m.new_conflict_type or "(无冲突)"
                click.echo(f"  ! {m.filename}: {old_status} -> {new_status}")

        if diff_result.added_moves:
            click.echo()
            click.echo(click.style("[详情] 新增的移动项:", fg="green"))
            for m in diff_result.added_moves:
                conflict_tag = f" [冲突: {m.new_conflict_type}]" if m.new_conflict_type else ""
                click.echo(f"  + {m.filename} -> {m.new_target_path} [{m.new_matched_rule}]{conflict_tag}")

        if diff_result.removed_moves:
            click.echo()
            click.echo(click.style("[详情] 删除的移动项:", fg="red"))
            for m in diff_result.removed_moves:
                click.echo(f"  - {m.filename} (原去向: {m.old_target_path})")

    if save_diff:
        diff_id = store.save_plan_diff(diff_result.to_dict())
        click.echo()
        click.echo(click.style(f"[差异已保存] ID: {diff_id}", fg="green"))

    if output_path:
        try:
            export_plan_diff(diff_result, output_path, format=format)
            click.echo()
            click.echo(click.style(f"[导出成功] 差异文件: {output_path}", fg="green"))
            click.echo(f"  格式: {format.upper()}")
        except Exception as e:
            click.echo(f"[错误] 导出失败: {e}", err=True)
            sys.exit(1)


@cli.command("lock-plan", help="锁定某个预案版本，apply 时只能执行该版本")
@click.option("-c", "--config", "config_path", required=True, type=click.Path(),
              help="配置文件路径 (YAML)")
@click.option("-s", "--snapshot-id", "snapshot_id", help="指定快照 ID (默认使用最新的)")
@click.option("-p", "--plan-id", "plan_id", help="指定预案 ID (优先级低于 snapshot-id)")
@click.option("--reason", help="锁定原因")
@click.option("-v", "--verbose", is_flag=True, help="显示详细信息")
def cmd_lock_plan(
    config_path: str,
    snapshot_id: Optional[str],
    plan_id: Optional[str],
    reason: Optional[str],
    verbose: bool,
):
    try:
        config = load_config(config_path)
    except Exception as e:
        click.echo(f"[错误] 加载配置失败: {e}", err=True)
        sys.exit(1)

    store = _get_store(config)

    if snapshot_id:
        snapshot = store.get_snapshot(snapshot_id)
        if not snapshot:
            click.echo(f"[错误] 未找到快照: {snapshot_id}", err=True)
            sys.exit(1)
    elif plan_id:
        snapshot = store.get_snapshot_by_plan_id(plan_id)
        if not snapshot:
            click.echo(f"[错误] 预案 {plan_id} 没有对应的快照", err=True)
            sys.exit(1)
    else:
        snapshot = store.get_last_snapshot()
        if not snapshot:
            click.echo("[错误] 未找到快照，请先执行 plan 命令。", err=True)
            sys.exit(1)

    existing_lock = store.get_active_lock()
    if existing_lock:
        click.echo(click.style(f"[提示] 已有活动锁定: {existing_lock.lock_id}", fg="yellow"))
        click.echo(f"  旧锁定将被新锁定取代。")

    lock = store.create_lock(
        snapshot_id=snapshot.snapshot_id,
        plan_id=snapshot.plan_id,
        reason=reason,
    )

    click.echo(click.style(f"[锁定成功] 锁定 ID: {lock.lock_id}", fg="green"))
    click.echo(f"  快照 ID: {lock.snapshot_id}")
    click.echo(f"  预案 ID: {lock.plan_id}")
    click.echo(f"  锁定时间: {lock.locked_at}")
    if lock.reason:
        click.echo(f"  锁定原因: {lock.reason}")

    click.echo()
    click.echo(click.style("[注意]", fg="yellow", bold=True))
    click.echo("  锁定后，如果重新 plan 或配置发生变化，")
    click.echo("  apply 命令将被拦截，不会悄悄执行最新结果。")
    click.echo("  如需解锁，请使用 unlock-plan 命令。")

    if verbose:
        click.echo()
        click.echo(f"  移动计划数: {len(snapshot.moves)}")
        click.echo(f"  未命中文件: {len(snapshot.unmatched_files)}")
        click.echo(f"  有冲突: {'是' if snapshot.has_conflicts else '否'}")


@cli.command("unlock-plan", help="释放预案版本锁定")
@click.option("-c", "--config", "config_path", required=True, type=click.Path(),
              help="配置文件路径 (YAML)")
@click.option("-l", "--lock-id", "lock_id", help="指定锁定 ID (默认释放当前活动锁定)")
@click.option("--reason", help="释放原因")
@click.option("-v", "--verbose", is_flag=True, help="显示详细信息")
def cmd_unlock_plan(
    config_path: str,
    lock_id: Optional[str],
    reason: Optional[str],
    verbose: bool,
):
    try:
        config = load_config(config_path)
    except Exception as e:
        click.echo(f"[错误] 加载配置失败: {e}", err=True)
        sys.exit(1)

    store = _get_store(config)

    if lock_id:
        lock = store.get_lock(lock_id)
        if not lock:
            click.echo(f"[错误] 未找到锁定记录: {lock_id}", err=True)
            sys.exit(1)
        if not lock.is_active:
            click.echo(click.style(f"[提示] 锁定 {lock_id} 已经是非活动状态。", fg="yellow"))
            return
        success = store.release_lock(lock_id, reason=reason)
    else:
        active_lock = store.get_active_lock()
        if not active_lock:
            click.echo("[提示] 当前没有活动锁定。")
            return
        success = store.release_active_lock(reason=reason)
        lock = active_lock

    if success:
        click.echo(click.style(f"[解锁成功] 锁定 ID: {lock.lock_id}", fg="green"))
        click.echo(f"  原快照 ID: {lock.snapshot_id}")
        click.echo(f"  原预案 ID: {lock.plan_id}")
        click.echo(f"  锁定时长: {lock.locked_at} ~ {lock.released_at}")
        if reason:
            click.echo(f"  释放原因: {reason}")
    else:
        click.echo("[错误] 解锁失败。", err=True)
        sys.exit(1)

    if verbose:
        violations = store.get_lock_violations_by_lock(lock.lock_id)
        if violations:
            click.echo()
            click.echo(click.style(f"[锁定期间违规记录: {len(violations)} 条]", fg="yellow"))
            for v in violations:
                status = "已拦截" if v.blocked else "未拦截"
                click.echo(f"  - {v.violation_timestamp}: {v.violation_type} [{status}]")
                click.echo(f"    {v.violation_detail}")

    snapshot = store.get_snapshot(lock.snapshot_id)
    if snapshot:
        pre_validation = store.get_latest_validation(snapshot.snapshot_id)

        store.invalidate_validation_for_snapshot(snapshot.snapshot_id)

        result = validate_signoff_for_apply(
            store=store,
            snapshot=snapshot,
            current_config=config,
            require_signed=True,
        )

        save_validation_history(
            store=store,
            snapshot=snapshot,
            signoff_validation=result,
            triggered_by="unlock-plan",
            lock_allowed=True,
        )

        if pre_validation and pre_validation.status == "blocked" and not pre_validation.is_resolved:
            store.update_validation_resolution(
                validation_id=pre_validation.validation_id,
                resolved_by="cli",
                resolution_note=f"释放锁定 {lock.lock_id}",
                resolution_command="unlock-plan",
            )

        click.echo()
        if result.valid:
            click.echo(click.style("[OK] 解锁后校验已通过，可以执行 apply。", fg="green"))
        else:
            click.echo(click.style("[提示] 解锁完成，但仍存在其他校验问题：", fg="yellow", bold=True))
            for err in result.errors:
                click.echo(click.style(f"  - {err}", fg="yellow"))


@cli.command("check-validation", help="查看最近的签收校验结果和当前阻塞状态")
@click.option("-c", "--config", "config_path", required=True, type=click.Path(),
              help="配置文件路径 (YAML)")
@click.option("-s", "--snapshot-id", "snapshot_id", help="指定快照 ID (默认使用最近的)")
@click.option("-p", "--plan-id", "plan_id", help="指定预案 ID (优先级低于 snapshot-id)")
@click.option("-n", "--limit", "limit", type=int, default=10, help="显示最近 N 条记录 (默认 10)")
@click.option("--all", "show_all", is_flag=True, help="显示所有快照的校验历史 (默认仅显示最近快照)")
@click.option("--blocked-only", is_flag=True, help="仅显示阻塞记录")
@click.option("-v", "--verbose", is_flag=True, help="显示详细错误和警告信息")
def cmd_check_validation(
    config_path: str,
    snapshot_id: Optional[str],
    plan_id: Optional[str],
    limit: int,
    show_all: bool,
    blocked_only: bool,
    verbose: bool,
):
    try:
        config = load_config(config_path)
    except Exception as e:
        click.echo(f"[错误] 加载配置失败: {e}", err=True)
        sys.exit(1)

    store = _get_store(config)

    if snapshot_id:
        snapshot = store.get_snapshot(snapshot_id)
        if not snapshot:
            click.echo(f"[错误] 未找到快照: {snapshot_id}", err=True)
            sys.exit(1)
        target_snapshot_id = snapshot.snapshot_id
    elif plan_id:
        snapshot = store.get_snapshot_by_plan_id(plan_id)
        if not snapshot:
            click.echo(f"[错误] 预案 {plan_id} 没有对应的快照", err=True)
            sys.exit(1)
        target_snapshot_id = snapshot.snapshot_id
    else:
        snapshot = store.get_last_snapshot()
        if not snapshot:
            click.echo("[错误] 未找到快照，请先执行 plan 命令。", err=True)
            sys.exit(1)
        target_snapshot_id = snapshot.snapshot_id

    if show_all:
        filter_snapshot_id = None
    else:
        filter_snapshot_id = target_snapshot_id

    all_records = store.get_validation_history(snapshot_id=filter_snapshot_id, limit=limit)

    if blocked_only:
        all_records = [r for r in all_records if r.status == "blocked" and not r.is_resolved]

    click.echo(click.style("[签收校验历史]", fg="cyan", bold=True))
    if not show_all:
        click.echo(f"  快照 ID: {target_snapshot_id}")
    click.echo(f"  显示最近 {len(all_records)} 条记录")
    click.echo()

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

    if not all_records:
        click.echo("  (暂无校验记录)")
        click.echo()
        click.echo(click.style("[提示] 请先执行 check-signoff、apply --dry-run 或 apply 命令生成校验记录。", fg="yellow"))
        sys.exit(0)

    latest = all_records[0]
    is_currently_blocked = (
        latest.status == "blocked" and not latest.is_resolved
    )

    click.echo(click.style("[最近一次结论]", fg="cyan", bold=True))
    if latest.status == "passed":
        status_color = "green"
        status_text = "通过"
    else:
        status_color = "red" if is_currently_blocked else "yellow"
        status_text = "阻塞" if is_currently_blocked else "已解决"

    click.echo(f"  结论: {click.style(status_text, fg=status_color, bold=True)}")
    click.echo(f"  触发命令: {latest.triggered_by}")
    click.echo(f"  触发时间: {latest.triggered_at}")
    click.echo(f"  快照 ID: {latest.snapshot_id}")
    click.echo(f"  预案 ID: {latest.plan_id}")

    if latest.block_types:
        block_types_cn = "; ".join([block_type_cn.get(bt, bt) for bt in latest.block_types])
        click.echo(f"  阻塞类型: {block_types_cn}")

    if is_currently_blocked:
        click.echo()
        click.echo(click.style("[当前状态: 阻塞中]", fg="red", bold=True))
        if latest.errors:
            click.echo(click.style("  阻塞原因:", fg="red"))
            for err in latest.errors[:5]:
                click.echo(f"    - {err}")
            if len(latest.errors) > 5:
                click.echo(f"    ... 还有 {len(latest.errors) - 5} 条错误，使用 -v 查看全部")

    elif latest.is_resolved:
        click.echo()
        click.echo(click.style("[当前状态: 已解除阻塞]", fg="green", bold=True))
        click.echo(f"  解决时间: {latest.resolved_at}")
        click.echo(f"  解决人: {latest.resolved_by or 'system'}")
        if latest.resolution_note:
            click.echo(f"  解决说明: {latest.resolution_note}")
        if latest.resolution_command:
            click.echo(f"  解决命令: {latest.resolution_command}")

    else:
        click.echo()
        click.echo(click.style("[当前状态: 可执行]", fg="green", bold=True))

    click.echo()
    click.echo(click.style("[历史记录]", fg="cyan", bold=True))

    triggered_by_cn = {
        "check-signoff": "check-signoff",
        "apply-dry-run": "apply --dry-run",
        "apply": "apply",
        "sign-off": "sign-off",
        "import-snapshot": "import-snapshot",
        "resolve-signoff-conflict": "resolve-signoff-conflict",
        "undo": "undo",
        "unlock-plan": "unlock-plan",
    }

    click.echo(
        f"{'时间':<26} {'命令':<26} {'快照ID':<14} {'状态':<10} {'阻塞类型':<20}"
    )
    click.echo("-" * 100)

    for r in all_records:
        time_str = r.triggered_at[:26] if len(r.triggered_at) > 26 else r.triggered_at
        cmd_cn = triggered_by_cn.get(r.triggered_by, r.triggered_by)

        if r.status == "passed":
            status_display = click.style("通过", fg="green")
        elif r.is_resolved:
            status_display = click.style("已解决", fg="yellow")
        else:
            status_display = click.style("阻塞", fg="red", bold=True)

        if r.block_types:
            block_types_cn = "; ".join([block_type_cn.get(bt, bt) for bt in r.block_types])
            if len(block_types_cn) > 18:
                block_types_cn = block_types_cn[:16] + "..."
        else:
            block_types_cn = "-"

        click.echo(
            f"{time_str:<26} {cmd_cn:<26} {r.snapshot_id:<14} {status_display:<10} {block_types_cn:<20}"
        )

        if verbose:
            if r.errors:
                for err in r.errors:
                    click.echo(f"  {click.style('错误:', fg='red')} {err}")
            if r.warnings:
                for warn in r.warnings:
                    click.echo(f"  {click.style('警告:', fg='yellow')} {warn}")
            if r.is_resolved:
                res_info = []
                if r.resolved_at:
                    res_info.append(f"解决于 {r.resolved_at[:26]}")
                if r.resolved_by:
                    res_info.append(f"by {r.resolved_by}")
                if r.resolution_command:
                    res_info.append(f"via {r.resolution_command}")
                if r.resolution_note:
                    res_info.append(f"- {r.resolution_note}")
                if res_info:
                    click.echo(f"  {click.style('解决:', fg='green')} {' '.join(res_info)}")

    if is_currently_blocked:
        click.echo()
        click.echo(click.style("[解除阻塞建议]", fg="yellow", bold=True))
        if "signoff_expired" in latest.block_types:
            click.echo("  签收已过期，请重新签收延长有效期：")
            click.echo(f"    python -m invoice_organizer sign-off -c {config_path} -s {target_snapshot_id} --signed-by <签收人>")
        if "config_mismatch" in latest.block_types:
            click.echo("  配置与签收时不一致，请使用 --force-snapshot 或重新 plan：")
            click.echo(f"    python -m invoice_organizer apply -c {config_path} -s {target_snapshot_id} --force-snapshot")
            click.echo(f"    python -m invoice_organizer plan -c {config_path}")
        if "unresolved_signoff_conflict" in latest.block_types:
            click.echo("  存在未解决的签收冲突，请使用 resolve-signoff-conflict 处理：")
            click.echo(f"    python -m invoice_organizer resolve-signoff-conflict -c {config_path} --snapshot-id {target_snapshot_id} --resolution <keep-local|keep-imported|new-signoff>")
        if "lock_mismatch" in latest.block_types:
            click.echo("  快照与锁定版本不一致，请使用锁定的快照或释放锁定：")
            click.echo(f"    python -m invoice_organizer unlock-plan -c {config_path}")
        if "no_signoff" in latest.block_types:
            click.echo("  未签收，请先签收：")
            click.echo(f"    python -m invoice_organizer sign-off -c {config_path} -s {target_snapshot_id} --signed-by <签收人>")

    if verbose:
        click.echo()
        click.echo(click.style("[统计信息]", fg="cyan", bold=True))
        total_count = len(store.get_validation_history(snapshot_id=filter_snapshot_id, limit=1000))
        blocked_count = len([r for r in store.get_validation_history(snapshot_id=filter_snapshot_id, limit=1000) if r.status == "blocked"])
        resolved_count = len([r for r in store.get_validation_history(snapshot_id=filter_snapshot_id, limit=1000) if r.is_resolved])
        click.echo(f"  总校验次数: {total_count}")
        click.echo(f"  阻塞次数: {blocked_count}")
        click.echo(f"  已解决次数: {resolved_count}")


@cli.command("list-locks", help="列出所有预案锁定记录")
@click.option("-c", "--config", "config_path", required=True, type=click.Path(),
              help="配置文件路径 (YAML)")
@click.option("-v", "--verbose", is_flag=True, help="显示详细信息")
def cmd_list_locks(config_path: str, verbose: bool):
    try:
        config = load_config(config_path)
    except Exception as e:
        click.echo(f"[错误] 加载配置失败: {e}", err=True)
        sys.exit(1)

    store = _get_store(config)
    locks = store.get_all_locks()

    if not locks:
        click.echo("暂无锁定记录。")
        return

    locks_sorted = sorted(locks, key=lambda l: l.locked_at, reverse=True)

    click.echo(f"{'锁定ID':<14} {'状态':<8} {'快照ID':<14} {'预案ID':<14} {'锁定时间':<27}")
    click.echo("-" * 90)
    for lock in locks_sorted:
        status = "活动" if lock.is_active else "已释放"
        locked_at = lock.locked_at[:26]
        click.echo(
            f"{lock.lock_id:<14} {status:<8} {lock.snapshot_id:<14} "
            f"{lock.plan_id:<14} {locked_at:<27}"
        )

    if verbose:
        active_lock = store.get_active_lock()
        if active_lock:
            violations = store.get_lock_violations_by_lock(active_lock.lock_id)
            if violations:
                click.echo()
                click.echo(click.style(f"[当前活动锁定违规记录: {len(violations)} 条]", fg="yellow"))
                for v in violations:
                    status = "已拦截" if v.blocked else "未拦截"
                    click.echo(f"  - {v.violation_timestamp[:26]}: {v.violation_type} [{status}]")
                    click.echo(f"    {v.violation_detail}")


@cli.command("remark-history", help="查看快照备注的修改历史，含字段级变更对比")
@click.option("-c", "--config", "config_path", required=True, type=click.Path(),
              help="配置文件路径 (YAML)")
@click.option("-s", "--snapshot-id", "snapshot_id", help="指定快照 ID (查看全部时不指定)")
@click.option("-v", "--verbose", is_flag=True, help="显示完整变更字段详情")
def cmd_remark_history(
    config_path: str,
    snapshot_id: Optional[str],
    verbose: bool,
):
    try:
        config = load_config(config_path)
    except Exception as e:
        click.echo(f"[错误] 加载配置失败: {e}", err=True)
        sys.exit(1)

    store = _get_store(config)
    histories = store.get_remark_history(snapshot_id=snapshot_id)

    if not histories:
        if snapshot_id:
            click.echo(f"快照 {snapshot_id} 暂无备注修改历史。")
        else:
            click.echo("暂无备注修改历史。")
        return

    click.echo(click.style(f"[备注修改历史] 共 {len(histories)} 条", fg="cyan", bold=True))
    if snapshot_id:
        click.echo(f"  快照 ID: {snapshot_id}")
    click.echo()

    for h in histories:
        conflict_tag = click.style(" [冲突]", fg="yellow") if h.conflict_detected else ""
        forced_tag = click.style(" [强制覆盖]", fg="red") if h.forced else ""
        source_tag = f" [{h.change_source}]" if h.change_source != "cli" else ""
        click.echo(f"  {h.changed_at[:26]}  快照: {h.snapshot_id}  修改人: {h.changed_by}{source_tag}{conflict_tag}{forced_tag}")

        if h.conflict_detail:
            click.echo(click.style(f"    冲突详情: {h.conflict_detail}", fg="yellow"))

        if verbose and h.changed_fields:
            click.echo(click.style("    变更字段:", fg="cyan"))
            for fc in h.changed_fields:
                click.echo(f"      {format_remark_change(fc)}")
        elif h.changed_fields:
            field_names = [fc.field_name for fc in h.changed_fields]
            click.echo(f"    变更字段: {', '.join(field_names)}")

        click.echo()


@cli.command("sign-off", help="签收预案/快照，记录签收状态、签收人、时间和补充说明")
@click.option("-c", "--config", "config_path", required=True, type=click.Path(),
              help="配置文件路径 (YAML)")
@click.option("-s", "--snapshot-id", "snapshot_id", help="指定快照 ID (默认使用最近的)")
@click.option("-p", "--plan-id", "plan_id", help="指定预案 ID (优先级低于 snapshot-id)")
@click.option("--status", type=click.Choice(["signed", "rejected", "pending"]),
              default="signed", show_default=True, help="签收状态")
@click.option("--signed-by", required=True, help=f"签收人 (最多 {MAX_SIGNOFF_BY_LENGTH} 字符)")
@click.option("--deadline", help="截止时间 (ISO 格式, 如: 2024-12-31T23:59:59)")
@click.option("--notes", help=f"补充说明 (最多 {MAX_SIGNOFF_NOTES_LENGTH} 字符)")
@click.option("--created-by", default="cli", help="创建人 (默认: cli)")
@click.option("--force", is_flag=True, help="即使有冲突也强制签收")
@click.option("-y", "--yes", is_flag=True, help="跳过确认提示")
@click.option("-v", "--verbose", is_flag=True, help="显示详细信息")
def cmd_sign_off(
    config_path: str,
    snapshot_id: Optional[str],
    plan_id: Optional[str],
    status: str,
    signed_by: str,
    deadline: Optional[str],
    notes: Optional[str],
    created_by: str,
    force: bool,
    yes: bool,
    verbose: bool,
):
    try:
        config = load_config(config_path)
    except Exception as e:
        click.echo(f"[错误] 加载配置失败: {e}", err=True)
        sys.exit(1)

    store = _get_store(config)

    if snapshot_id:
        snapshot = store.get_snapshot(snapshot_id)
        if not snapshot:
            click.echo(f"[错误] 未找到快照: {snapshot_id}", err=True)
            sys.exit(1)
    elif plan_id:
        snapshot = store.get_snapshot_by_plan_id(plan_id)
        if not snapshot:
            click.echo(f"[错误] 预案 {plan_id} 没有对应的快照", err=True)
            sys.exit(1)
    else:
        snapshot = store.get_last_snapshot()
        if not snapshot:
            click.echo("[错误] 未找到快照，请先执行 plan 命令。", err=True)
            sys.exit(1)

    snapshot_id = snapshot.snapshot_id
    plan_id = snapshot.plan_id

    existing_signoff = store.get_active_signoff(snapshot_id)

    config_snapshot_dict = config_snapshot_to_dict(config, config_path)

    signoff = build_signoff(
        snapshot_id=snapshot_id,
        plan_id=plan_id,
        status=status,
        signed_by=signed_by,
        deadline=deadline,
        notes=notes,
        config_snapshot=config_snapshot_dict,
        created_by=created_by,
    )

    validation = validate_signoff(signoff)
    if validation.has_errors:
        click.echo(click.style("[错误] 签收信息验证失败：", fg="red", bold=True))
        for err in validation.errors:
            click.echo(click.style(f"  - {err}", fg="red"))
        sys.exit(1)
    if validation.warnings:
        for warn in validation.warnings:
            click.echo(click.style(f"[警告] {warn}", fg="yellow"))

    status_map = {"signed": "已签收", "rejected": "已拒绝", "pending": "待处理"}
    click.echo(click.style("[签收信息]", fg="cyan", bold=True))
    click.echo(f"  快照 ID: {snapshot_id}")
    click.echo(f"  预案 ID: {plan_id}")
    click.echo(f"  状态: {status_map.get(status, status)}")
    click.echo(f"  签收人: {signed_by}")
    if deadline:
        click.echo(f"  截止时间: {deadline}")
        if check_signoff_expired(signoff):
            click.echo(click.style("  [警告] 截止时间已过期", fg="yellow"))
    if notes:
        notes_display = notes[:80] + "..." if len(notes) > 80 else notes
        click.echo(f"  补充说明: {notes_display}")

    click.echo(f"  签收时间: {signoff.signed_at}")
    click.echo(f"  创建人: {created_by}")

    if existing_signoff:
        field_changes = diff_signoffs(existing_signoff, signoff)
        if field_changes:
            click.echo()
            click.echo(click.style("[与现有签收对比]", fg="yellow"))
            for fc in field_changes:
                click.echo(f"  {format_signoff_change(fc)}")
            if not force:
                click.echo()
                click.echo(click.style("[提示] 该快照已有签收记录，使用 --force 可强制覆盖。", fg="yellow"))
                sys.exit(1)
            else:
                signoff.forced = True
                signoff.conflict_detail = "强制覆盖已有签收记录"

    if not yes:
        if not click.confirm("\n确认签收该预案/快照？", default=False):
            click.echo("已取消。")
            sys.exit(0)

    store.add_signoff(signoff)

    pre_validation = store.get_latest_validation(snapshot.snapshot_id)

    store.invalidate_validation_for_snapshot(snapshot.snapshot_id)

    result = validate_signoff_for_apply(
        store=store,
        snapshot=snapshot,
        current_config=config,
        require_signed=True,
    )

    save_validation_history(
        store=store,
        snapshot=snapshot,
        signoff_validation=result,
        triggered_by="sign-off",
        lock_allowed=True,
    )

    if pre_validation and pre_validation.status == "blocked" and not pre_validation.is_resolved:
        store.update_validation_resolution(
            validation_id=pre_validation.validation_id,
            resolved_by=signed_by,
            resolution_note=f"重新签收，签收 ID: {signoff.signoff_id}",
            resolution_command="sign-off",
        )

    click.echo()
    click.echo(click.style(f"[签收成功] 签收 ID: {signoff.signoff_id}", fg="green"))
    if signoff.forced:
        click.echo(click.style(f"[注意] 已强制覆盖原有签收记录。", fg="yellow"))

    if verbose:
        click.echo(f"  快照创建时间: {snapshot.created_at}")
        click.echo(f"  移动计划数: {len(snapshot.moves)}")
        click.echo(f"  有冲突: {'是' if snapshot.has_conflicts else '否'}")


@cli.command("check-signoff", help="检查快照的签收状态，确认是否可以执行")
@click.option("-c", "--config", "config_path", required=True, type=click.Path(),
              help="配置文件路径 (YAML)")
@click.option("-s", "--snapshot-id", "snapshot_id", help="指定快照 ID (默认使用最近的)")
@click.option("-p", "--plan-id", "plan_id", help="指定预案 ID (优先级低于 snapshot-id)")
@click.option("--require-signed", is_flag=True, default=True, help="要求必须是已签收状态")
@click.option("--no-require-signed", is_flag=True, help="不要求必须是已签收状态")
@click.option("-v", "--verbose", is_flag=True, help="显示详细信息")
def cmd_check_signoff(
    config_path: str,
    snapshot_id: Optional[str],
    plan_id: Optional[str],
    require_signed: bool,
    no_require_signed: bool,
    verbose: bool,
):
    try:
        config = load_config(config_path)
    except Exception as e:
        click.echo(f"[错误] 加载配置失败: {e}", err=True)
        sys.exit(1)

    store = _get_store(config)

    if snapshot_id:
        snapshot = store.get_snapshot(snapshot_id)
        if not snapshot:
            click.echo(f"[错误] 未找到快照: {snapshot_id}", err=True)
            sys.exit(1)
    elif plan_id:
        snapshot = store.get_snapshot_by_plan_id(plan_id)
        if not snapshot:
            click.echo(f"[错误] 预案 {plan_id} 没有对应的快照", err=True)
            sys.exit(1)
    else:
        snapshot = store.get_last_snapshot()
        if not snapshot:
            click.echo("[错误] 未找到快照，请先执行 plan 命令。", err=True)
            sys.exit(1)

    snapshot_id = snapshot.snapshot_id

    require_signed = require_signed and not no_require_signed

    result = validate_signoff_for_apply(
        store=store,
        snapshot=snapshot,
        current_config=config,
        require_signed=require_signed,
    )

    save_validation_history(
        store=store,
        snapshot=snapshot,
        signoff_validation=result,
        triggered_by="check-signoff",
        lock_allowed=True,
    )

    click.echo(click.style("[签收状态检查]", fg="cyan", bold=True))
    click.echo(f"  快照 ID: {snapshot_id}")
    click.echo(f"  预案 ID: {snapshot.plan_id}")

    if result.active_signoff:
        s = result.active_signoff
        status_map = {"signed": "已签收", "rejected": "已拒绝", "pending": "待处理"}
        click.echo(f"  签收 ID: {s.signoff_id}")
        click.echo(f"  签收状态: {status_map.get(s.status, s.status)}")
        click.echo(f"  签收人: {s.signed_by}")
        click.echo(f"  签收时间: {s.signed_at}")
        if s.deadline:
            click.echo(f"  截止时间: {s.deadline}")
            if result.is_expired:
                click.echo(click.style("  [过期] 签收已过期", fg="red"))
        if s.notes:
            click.echo(f"  补充说明: {s.notes}")
        if s.forced:
            click.echo(click.style("  [强制] 该签收为强制覆盖", fg="yellow"))
        if result.config_mismatch:
            click.echo(click.style("  [配置不一致] 当前配置与签收时不一致", fg="yellow"))
        if result.snapshot_replaced:
            click.echo(click.style("  [快照已更新] 该快照已被新版本替代", fg="yellow"))
        if result.conflicting_signoffs:
            click.echo(click.style(f"  [冲突签收] 存在冲突的签收记录:", fg="yellow"))
            for cs in result.conflicting_signoffs:
                click.echo(f"    - {cs}")
    else:
        click.echo(click.style("  签收状态: 未签收", fg="yellow"))

    only_conflict_error = all(
        "未解决的签收冲突" in err or "冲突的签收记录" in err
        for err in result.errors
    )

    if result.has_errors:
        if no_require_signed and only_conflict_error:
            click.echo()
            click.echo(click.style("[警告] 存在未解决的签收冲突（仅查看模式）：", fg="yellow", bold=True))
            for err in result.errors:
                click.echo(click.style(f"  - {err}", fg="yellow"))
        else:
            click.echo()
            click.echo(click.style("[错误] 签收校验失败：", fg="red", bold=True))
            for err in result.errors:
                click.echo(click.style(f"  - {err}", fg="red"))
            sys.exit(1)

    if result.warnings:
        click.echo()
        click.echo(click.style("[警告]", fg="yellow", bold=True))
        for warn in result.warnings:
            click.echo(click.style(f"  - {warn}", fg="yellow"))

    if not result.has_errors or (no_require_signed and only_conflict_error):
        if not result.has_errors:
            click.echo()
            click.echo(click.style("[OK] 签收校验通过，可以执行 apply。", fg="green"))
        else:
            click.echo()
            click.echo(click.style("[提示] 存在未解决的签收冲突，需先解决才能执行 apply。", fg="yellow"))

    if verbose:
        all_signoffs = store.get_signoffs_by_snapshot(snapshot_id)
        if len(all_signoffs) > 1:
            click.echo()
            click.echo(click.style(f"[历史签收记录]", fg="cyan"))
            for s in all_signoffs:
                status_map = {"signed": "已签收", "rejected": "已拒绝", "pending": "待处理"}
                status_tag = status_map.get(s.status, s.status)
                active_tag = "" if s.is_active else " (已失效)"
                forced_tag = " (强制)" if s.forced else ""
                click.echo(f"  {s.signed_at[:26]}  ID: {s.signoff_id}  状态: {status_tag}{active_tag}{forced_tag}  签收人: {s.signed_by}")


@cli.command("resolve-signoff-conflict", help="处理签收冲突，选择保留本地/导入或重新签收")
@click.option("-c", "--config", "config_path", required=True, type=click.Path(),
              help="配置文件路径 (YAML)")
@click.option("--snapshot-id", "snapshot_id", required=True,
              help="要处理冲突的快照 ID")
@click.option("--conflict-id", "conflict_id",
              help="冲突 ID（不指定时自动选择该快照 pending 状态的冲突）")
@click.option("--resolution", "resolution", required=True,
              type=click.Choice(["keep-local", "keep-imported", "new-signoff"]),
              help="处理方式：keep-local 保留本地签收 / keep-imported 保留导入签收 / new-signoff 重新签收")
@click.option("--by", "resolved_by", default="cli-user", help="处理人（默认: cli-user）")
@click.option("--note", "resolution_note", default="", help="处理说明（可选）")
@click.option("--signer", "signer", help="new-signoff 方式时的签收人姓名")
@click.option("--deadline", "deadline", help="new-signoff 方式时的签收截止时间（ISO 时间，不填默认 30 天）")
@click.option("--signoff-notes", "signoff_notes", help="new-signoff 方式时的签收补充说明（不填使用 resolution-note）")
@click.option("-y", "--yes", is_flag=True, help="跳过确认提示")
@click.option("-v", "--verbose", is_flag=True, help="显示详细信息")
def cmd_resolve_signoff_conflict(
    config_path: str,
    snapshot_id: str,
    conflict_id: Optional[str],
    resolution: str,
    resolved_by: str,
    resolution_note: str,
    signer: Optional[str],
    deadline: Optional[str],
    signoff_notes: Optional[str],
    yes: bool,
    verbose: bool,
):
    try:
        config = load_config(config_path)
    except Exception as e:
        click.echo(f"[错误] 加载配置失败: {e}", err=True)
        sys.exit(1)

    store = _get_store(config)

    snapshot = store.get_snapshot(snapshot_id)
    if not snapshot:
        click.echo(f"[错误] 未找到快照: {snapshot_id}", err=True)
        sys.exit(1)

    if conflict_id:
        conflict = store.get_signoff_conflict(conflict_id)
        if not conflict:
            click.echo(f"[错误] 未找到冲突记录: {conflict_id}", err=True)
            sys.exit(1)
        if conflict.snapshot_id != snapshot_id:
            click.echo(f"[错误] 冲突 {conflict_id} 不属于快照 {snapshot_id}", err=True)
            sys.exit(1)
    else:
        conflict = store.get_pending_conflict_by_snapshot(snapshot_id)
        if not conflict:
            click.echo(f"[错误] 快照 {snapshot_id} 没有 pending 状态的签收冲突", err=True)
            pending_count = len([
                c for c in store.list_signoff_conflicts(snapshot_id=snapshot_id)
                if c.status != "pending"
            ])
            if pending_count:
                click.echo(f"[提示] 该快照已有 {pending_count} 个已处理的冲突。", err=True)
            sys.exit(1)
        conflict_id = conflict.conflict_id

    if verbose or not yes:
        click.echo(click.style("[签收冲突详情]", fg="cyan", bold=True))
        click.echo(format_signoff_conflict_summary(conflict))
        click.echo()

    resolution_map = {
        "keep-local": "resolved_keep_local",
        "keep-imported": "resolved_keep_imported",
        "new-signoff": "resolved_new",
    }
    resolution_status = resolution_map[resolution]
    new_signoff_id: Optional[str] = None

    if resolution == "new-signoff":
        if not signer:
            if not yes:
                signer = click.prompt("请输入签收人姓名", type=str).strip()
            else:
                click.echo("[错误] new-signoff 方式需要 --signer 参数", err=True)
                sys.exit(1)
        if not resolution_note and not signoff_notes:
            click.echo("[提示] new-signoff 建议通过 --signoff-notes 提供签收说明，当前留空")

        notes_to_use = signoff_notes if signoff_notes else resolution_note
        if not deadline:
            from datetime import timedelta
            from dateutil import parser as dateparser
            try:
                base_time = dateparser.isoparse(now_iso())
            except Exception:
                base_time = datetime.utcnow()
            deadline_iso = (base_time + timedelta(days=30)).isoformat()
        else:
            deadline_iso = deadline

        new_signoff_config_snapshot = config_snapshot_to_dict(config, config_path)

        new_signoff_record = build_signoff(
            snapshot_id=snapshot_id,
            plan_id=snapshot.plan_id,
            status="signed",
            signed_by=signer,
            deadline=deadline_iso,
            notes=notes_to_use,
            config_snapshot=new_signoff_config_snapshot,
            created_by=resolved_by,
        )
        new_signoff_record.conflict_id = conflict_id
        store.add_signoff(new_signoff_record)
        new_signoff_id = new_signoff_record.signoff_id

        click.echo(click.style(f"[新建签收] ID: {new_signoff_id}", fg="green"))
        click.echo(f"  签收人: {signer}")
        click.echo(f"  签收时间: {new_signoff_record.signed_at}")
        click.echo(f"  截止时间: {new_signoff_record.deadline}")
        if notes_to_use:
            click.echo(f"  说明: {notes_to_use}")
        click.echo()

    if not yes:
        mode_cn = {
            "keep-local": "保留本地签收，丢弃导入签收",
            "keep-imported": "保留导入签收，丢弃本地签收",
            "new-signoff": f"使用新建签收(ID:{new_signoff_id})替代前两者",
        }
        confirm_msg = (
            f"\n确认使用处理方式「{mode_cn.get(resolution, resolution)}」"
            f"解决冲突 {conflict_id}？"
        )
        if not click.confirm(confirm_msg, default=False):
            if resolution == "new-signoff" and new_signoff_id:
                click.echo(f"已取消。新建签收记录 {new_signoff_id} 保留在状态中，可手动处理。")
            else:
                click.echo("已取消。")
            sys.exit(0)

    success, updated_conflict, errors = store.resolve_signoff_conflict(
        conflict_id=conflict_id,
        resolution=resolution_status,
        resolved_by=resolved_by,
        resolution_note=resolution_note,
        new_signoff_id=new_signoff_id,
    )

    if not success:
        if not updated_conflict and errors:
            click.echo(click.style("[错误] 处理失败：", fg="red", bold=True))
            for err in errors:
                click.echo(click.style(f"  - {err}", fg="red"))
        else:
            click.echo(click.style("[警告] 处理未执行：", fg="yellow", bold=True))
            for err in errors:
                click.echo(click.style(f"  - {err}", fg="yellow"))
        sys.exit(1)

    click.echo()
    click.echo(click.style("[冲突处理成功]", fg="green", bold=True))
    click.echo(format_signoff_conflict_summary(updated_conflict))
    click.echo()

    pre_validation = store.get_latest_validation(snapshot.snapshot_id)

    store.invalidate_validation_for_snapshot(snapshot.snapshot_id)

    result = validate_signoff_for_apply(
        store=store,
        snapshot=snapshot,
        current_config=config,
        require_signed=True,
    )

    save_validation_history(
        store=store,
        snapshot=snapshot,
        signoff_validation=result,
        triggered_by="resolve-signoff-conflict",
        lock_allowed=True,
    )

    if pre_validation and pre_validation.status == "blocked" and not pre_validation.is_resolved:
        store.update_validation_resolution(
            validation_id=pre_validation.validation_id,
            resolved_by=resolved_by,
            resolution_note=f"通过 resolve-signoff-conflict 解决冲突 {conflict_id}",
            resolution_command="resolve-signoff-conflict",
        )

    if result.valid:
        click.echo(click.style("[OK] 签收校验已通过，可以执行 apply。", fg="green"))
    else:
        click.echo(click.style("[警告] 冲突已解决，但仍存在其他校验问题：", fg="yellow", bold=True))
        for err in result.errors:
            click.echo(click.style(f"  - {err}", fg="yellow"))
        click.echo(click.style("  请根据上述提示调整后再执行 apply。", fg="yellow"))


@cli.command("list-bundles", help="列出所有执行批次归档包")
@click.option("-c", "--config", "config_path", required=True, type=click.Path(),
              help="配置文件路径 (YAML)")
@click.option("-v", "--verbose", is_flag=True, help="显示详细信息（签收、撤销状态等）")
@click.option("--json", "as_json", is_flag=True, help="以 JSON 格式输出")
def cmd_list_bundles(config_path: str, verbose: bool, as_json: bool):
    try:
        config = load_config(config_path)
    except Exception as e:
        click.echo(f"[错误] 加载配置失败: {e}", err=True)
        sys.exit(1)

    store = _get_store(config)
    bundles = store.list_bundles()

    if not bundles:
        if as_json:
            click.echo("[]")
        else:
            click.echo("暂无执行批次归档包。请先执行 apply 命令。")
        return

    if as_json:
        click.echo(json.dumps(bundles, ensure_ascii=False, indent=2))
        return

    click.echo(click.style(f"[执行批次归档包] 共 {len(bundles)} 个", fg="cyan", bold=True))
    click.echo()

    if verbose:
        header = (
            f"{'Bundle ID':<14} {'创建时间':<27} {'Run ID':<14} "
            f"{'快照ID':<14} {'总数':<6} {'成功':<6} {'跳过冲突':<8} {'人工跳过':<8} "
            f"{'失败':<6} {'预演':<4} {'撤销':<4} {'签收':<8}"
        )
        click.echo(header)
        click.echo("-" * 150)
        for b in bundles:
            created = b.get("created_at", "")[:26]
            dry = "是" if b.get("dry_run") else "否"
            undone = "是" if b.get("is_undone") else "否"
            signoff = ""
            if b.get("has_signoff"):
                signed_by = b.get("signed_by", "")
                if len(signed_by) > 6:
                    signed_by = signed_by[:6] + "..."
                signoff = f"已签收({signed_by})" if signed_by else "已签收"
            elif verbose:
                signoff = "未签收"
            src = "导入" if b.get("imported") else "本地"
            click.echo(
                f"{b['bundle_id']:<14} {created:<27} {b.get('run_id',''):<14} "
                f"{b.get('snapshot_id',''):<14} "
                f"{b.get('total_moves',0):<6} {b.get('success_count',0):<6} "
                f"{b.get('skipped_conflict_count',0):<8} {b.get('skipped_manual_count',0):<8} "
                f"{b.get('failed_count',0):<6} {dry:<4} {undone:<4} {signoff:<8}"
            )
            if b.get("imported"):
                click.echo(f"  {'':<14} {'':<27} {'':<14} {'':<14} 来源: {src}")
    else:
        header = (
            f"{'Bundle ID':<14} {'创建时间':<27} {'Run ID':<14} "
            f"{'移动数':<8} {'状态':<16} {'签收':<12} {'来源':<8}"
        )
        click.echo(header)
        click.echo("-" * 110)
        for b in bundles:
            created = b.get("created_at", "")[:26]
            status_parts = []
            if b.get("is_undone"):
                status_parts.append(click.style("已撤销", fg="yellow"))
            elif b.get("failed_count", 0) > 0:
                status_parts.append(click.style(f"失败{b.get('failed_count',0)}", fg="red"))
            if b.get("dry_run"):
                status_parts.append("预演")
            if not status_parts:
                status_parts.append(click.style("生效", fg="green"))
            if b.get("skipped_conflict_count", 0) > 0:
                status_parts.append(f"冲突跳过{b.get('skipped_conflict_count',0)}")
            status_display = " | ".join(status_parts)

            signoff_display = ""
            if b.get("has_signoff"):
                signed_by = b.get("signed_by", "")
                if len(signed_by) > 8:
                    signed_by = signed_by[:8] + "..."
                signoff_display = f"by {signed_by}" if signed_by else "已签收"
            else:
                signoff_display = click.style("未签收", fg="yellow")

            src_display = "导入" if b.get("imported") else "本地"

            click.echo(
                f"{b['bundle_id']:<14} {created:<27} {b.get('run_id',''):<14} "
                f"{b.get('total_moves',0):<8} {status_display:<40} {signoff_display:<14} {src_display:<8}"
            )


@cli.command("export-bundle", help="导出执行批次归档包为 JSON 文件，用于交接或备份")
@click.option("-c", "--config", "config_path", required=True, type=click.Path(),
              help="配置文件路径 (YAML)")
@click.option("-b", "--bundle-id", "bundle_id", help="指定归档包 ID (默认使用最近的)")
@click.option("-r", "--run-id", "run_id", help="按执行 ID 查找归档包 (优先级低于 bundle-id)")
@click.option("-o", "--output", "output_path", required=True, type=click.Path(),
              help="导出文件路径")
@click.option("-v", "--verbose", is_flag=True, help="显示详细信息")
def cmd_export_bundle(
    config_path: str,
    bundle_id: Optional[str],
    run_id: Optional[str],
    output_path: str,
    verbose: bool,
):
    try:
        config = load_config(config_path)
    except Exception as e:
        click.echo(f"[错误] 加载配置失败: {e}", err=True)
        sys.exit(1)

    store = _get_store(config)

    bundle = None
    if bundle_id:
        bundle = store.get_bundle(bundle_id)
        if not bundle:
            click.echo(f"[错误] 未找到归档包: {bundle_id}", err=True)
            sys.exit(1)
    elif run_id:
        bundle = store.get_bundle_by_run_id(run_id)
        if not bundle:
            click.echo(f"[错误] 执行 ID {run_id} 没有对应的归档包", err=True)
            sys.exit(1)
    else:
        bundle = store.get_last_bundle()
        if not bundle:
            click.echo("[错误] 未找到归档包，请先执行 apply 命令。", err=True)
            sys.exit(1)

    try:
        export_bundle_to_file(bundle, output_path)
    except Exception as e:
        click.echo(f"[错误] 导出失败: {e}", err=True)
        sys.exit(1)

    click.echo(click.style(f"[导出成功] 归档包导出文件: {output_path}", fg="green"))
    click.echo(f"  Bundle ID: {bundle.bundle_id}")
    click.echo(f"  版本: {bundle.bundle_version}")
    click.echo(f"  创建时间: {bundle.created_at}")
    click.echo(f"  Run ID: {bundle.run_id}")
    click.echo(f"  Plan ID: {bundle.plan_id}")
    click.echo(f"  Snapshot ID: {bundle.snapshot_id}")
    if bundle.imported:
        click.echo(f"  来源: 导入 (自 {bundle.import_source})")
        if bundle.imported_at:
            click.echo(f"  导入时间: {bundle.imported_at}")
    if bundle.checksum:
        click.echo(f"  校验和: {bundle.checksum}")

    click.echo()
    s = bundle.summary
    click.echo(click.style("[执行摘要]", fg="cyan"))
    click.echo(f"  总记录数: {s.total_moves}")
    click.echo(f"  成功移动: {s.success_count}")
    if s.skipped_conflict_count > 0:
        click.echo(click.style(f"  冲突跳过: {s.skipped_conflict_count}", fg="yellow"))
    if s.skipped_manual_count > 0:
        click.echo(click.style(f"  人工跳过: {s.skipped_manual_count}", fg="bright_black"))
    if s.failed_count > 0:
        click.echo(click.style(f"  执行失败: {s.failed_count}", fg="red"))
    click.echo(f"  预演模式: {'是' if s.dry_run else '否'}")
    click.echo(f"  已撤销: {'是' if s.is_undone else '否'}")
    if s.has_signoff:
        click.echo(f"  签收状态: {s.signoff_status} (ID: {s.signoff_id})")
        click.echo(f"  签收人: {s.signed_by}")

    if verbose:
        click.echo()
        if s.conflict_details:
            click.echo(click.style("[冲突跳过详情]", fg="yellow"))
            for d in s.conflict_details[:10]:
                click.echo(f"  - {d}")
            if len(s.conflict_details) > 10:
                click.echo(f"  ... 还有 {len(s.conflict_details) - 10} 条，请在导出文件中查看")
        if s.manual_skip_reasons:
            click.echo()
            click.echo(click.style("[人工跳过原因]", fg="bright_black"))
            for d in s.manual_skip_reasons[:10]:
                click.echo(f"  - {d}")
            if len(s.manual_skip_reasons) > 10:
                click.echo(f"  ... 还有 {len(s.manual_skip_reasons) - 10} 条，请在导出文件中查看")

        if bundle.signoffs:
            click.echo()
            click.echo(click.style(f"[归档内含签收记录: {len(bundle.signoffs)} 条]", fg="cyan"))
        if bundle.signoff_conflicts:
            click.echo(click.style(f"[归档内含签收冲突: {len(bundle.signoff_conflicts)} 条]", fg="yellow"))
        if bundle.validation_history:
            click.echo(click.style(f"[归档内含校验历史: {len(bundle.validation_history)} 条]", fg="cyan"))


@cli.command("import-bundle", help="从 JSON 文件导入执行批次归档包，用于查阅或交接")
@click.option("-c", "--config", "config_path", required=True, type=click.Path(),
              help="配置文件路径 (YAML)")
@click.option("-i", "--input", "input_path", required=True, type=click.Path(),
              help="归档包 JSON 文件路径")
@click.option("--force", is_flag=True,
              help="即使检测到冲突（重复导入/快照版本不一致等）也强制导入")
@click.option("--by", "imported_by", default="cli", help="导入人 (默认: cli)")
@click.option("--no-check-snapshot", is_flag=True,
              help="不检查本地快照与归档包快照的版本一致性（谨慎使用）")
@click.option("-y", "--yes", is_flag=True, help="跳过确认提示")
@click.option("-v", "--verbose", is_flag=True, help="显示详细信息")
def cmd_import_bundle(
    config_path: str,
    input_path: str,
    force: bool,
    imported_by: str,
    no_check_snapshot: bool,
    yes: bool,
    verbose: bool,
):
    try:
        config = load_config(config_path)
    except Exception as e:
        click.echo(f"[错误] 加载配置失败: {e}", err=True)
        sys.exit(1)

    store = _get_store(config)

    try:
        bundle = load_bundle_from_file(input_path)
    except Exception as e:
        click.echo(click.style("[错误] 加载归档包文件失败：", fg="red", bold=True), err=True)
        click.echo(click.style(f"  - {e}", fg="red"), err=True)
        sys.exit(1)

    click.echo(click.style("[导入归档包] 解析成功", fg="green"))
    click.echo(f"  Bundle ID: {bundle.bundle_id}")
    click.echo(f"  版本: {bundle.bundle_version}")
    click.echo(f"  创建时间: {bundle.created_at}")
    click.echo(f"  Run ID: {bundle.run_id}")
    click.echo(f"  Plan ID: {bundle.plan_id}")
    click.echo(f"  Snapshot ID: {bundle.snapshot_id}")

    s = bundle.summary
    click.echo()
    click.echo(click.style("[执行摘要]", fg="cyan"))
    click.echo(f"  总记录数: {s.total_moves}")
    click.echo(f"  成功移动: {s.success_count}")
    if s.skipped_conflict_count > 0:
        click.echo(click.style(f"  冲突跳过: {s.skipped_conflict_count}", fg="yellow"))
    if s.skipped_manual_count > 0:
        click.echo(click.style(f"  人工跳过: {s.skipped_manual_count}", fg="bright_black"))
    if s.failed_count > 0:
        click.echo(click.style(f"  执行失败: {s.failed_count}", fg="red"))
    click.echo(f"  预演模式: {'是' if s.dry_run else '否'}")
    click.echo(f"  已撤销: {'是' if s.is_undone else '否'}")
    if s.has_signoff:
        click.echo(f"  签收: {s.signoff_status} by {s.signed_by}")

    check_snapshot = not no_check_snapshot
    validation = validate_bundle_for_import(store, bundle, check_snapshot=check_snapshot)

    if validation.conflict_types:
        click.echo()
        click.echo(click.style("[冲突检测]", fg="yellow", bold=True))
        for ct in validation.conflict_types:
            tag = click.style(ct, fg="red", bold=True)
            click.echo(f"  冲突类型: {tag}")
        click.echo()

    if validation.warnings:
        click.echo(click.style("[警告]", fg="yellow", bold=True))
        for w in validation.warnings:
            click.echo(click.style(f"  - {w}", fg="yellow"))
        click.echo()

    if validation.has_errors:
        click.echo(click.style("[错误] 导入验证失败：", fg="red", bold=True))
        for err in validation.errors:
            click.echo(click.style(f"  - {err}", fg="red"))
        click.echo()
        if not force:
            from .models import generate_id as _gen_id, now_iso as _now_iso, BundleImportLog as _BIL
            failed_log = _BIL(
                import_log_id=_gen_id(),
                bundle_id=bundle.bundle_id,
                run_id=bundle.run_id,
                snapshot_id=bundle.snapshot_id,
                timestamp=_now_iso(),
                status="failed",
                source_file=os.path.abspath(input_path),
                errors=list(validation.errors),
                warnings=list(validation.warnings),
                conflict_details=list(validation.conflict_types),
                forced=False,
                imported_by=imported_by,
            )
            store.add_bundle_import_log(failed_log)
            click.echo(
                click.style(
                    "使用 --force 可强制导入（冲突会被覆盖并记录为 forced 状态）。",
                    fg="yellow",
                )
            )
            sys.exit(1)
        else:
            click.echo(click.style("[警告] 使用 --force 强制导入，冲突将被覆盖并标记为 forced。", fg="yellow"))
            click.echo()

    if not yes:
        if force and validation.has_errors:
            confirm_msg = "\n确认强制导入该归档包（覆盖冲突）？"
        else:
            confirm_msg = "\n确认导入该归档包？"
        if not click.confirm(confirm_msg, default=False):
            click.echo("已取消。")
            sys.exit(0)

    success, validation, import_log, info_messages = import_bundle_into_store(
        store=store,
        bundle=bundle,
        import_source=input_path,
        force=force,
        imported_by=imported_by,
        check_snapshot=check_snapshot,
    )

    if not success:
        click.echo(click.style("\n[错误] 导入失败：", fg="red", bold=True))
        for err in validation.errors:
            click.echo(click.style(f"  - {err}", fg="red"))
        if import_log.status == "failed":
            click.echo()
            click.echo(click.style(f"[导入日志] 已记录失败导入: {import_log.import_log_id}", fg="yellow"))
        sys.exit(1)

    click.echo()
    if force and validation.has_errors:
        click.echo(click.style(f"[导入成功] 已强制导入归档包: {bundle.bundle_id}", fg="green"))
        if validation.conflict_types:
            click.echo(click.style(f"  冲突类型: {', '.join(validation.conflict_types)}", fg="yellow"))
    else:
        click.echo(click.style(f"[导入成功] 归档包已导入: {bundle.bundle_id}", fg="green"))
    click.echo(f"  导入日志 ID: {import_log.import_log_id}")
    click.echo(f"  导入人: {import_log.imported_by}")
    click.echo(f"  导入时间: {import_log.timestamp}")
    click.echo(f"  状态: {import_log.status}")

    if info_messages:
        click.echo()
        click.echo(click.style("[导入详情]", fg="cyan"))
        for msg in info_messages:
            click.echo(f"  - {msg}")

    if verbose and import_log.warnings:
        click.echo()
        click.echo(click.style("[警告回顾]", fg="yellow"))
        for w in import_log.warnings:
            click.echo(f"  - {w}")

    if validation.conflict_types:
        click.echo()
        click.echo(click.style("[注意] 导入时存在冲突，建议核对：", fg="yellow"))
        for ct in validation.conflict_types:
            click.echo(f"  - {ct}")
        if import_log.status == "forced":
            click.echo(click.style("  (已使用 --force，冲突状态已记录为 forced)", fg="yellow"))

    click.echo()
    click.echo(f"  可使用 list-bundles 查看，或 export-bundle -b {bundle.bundle_id} 重新导出。")


def _check_lock_before_apply(store: StateStore, snapshot) -> Tuple[bool, Optional[str]]:
    """
    检查锁定状态，判断是否允许执行 apply

    返回: (是否允许执行, 拒绝原因)
    """
    active_lock = store.get_active_lock()
    if not active_lock:
        return True, None

    if snapshot.snapshot_id == active_lock.snapshot_id:
        return True, None

    violation_type = "wrong_snapshot"
    detail = (
        f"锁定的快照 {active_lock.snapshot_id} 与当前执行的快照 {snapshot.snapshot_id} 不一致。"
        f" 请确认是否使用了正确的版本，或先 unlock-plan 释放锁定。"
    )

    violation = LockViolation(
        violation_id=generate_id(),
        lock_id=active_lock.lock_id,
        snapshot_id=snapshot.snapshot_id,
        plan_id=snapshot.plan_id,
        violation_timestamp=now_iso(),
        violation_type=violation_type,
        violation_detail=detail,
        blocked=True,
    )
    store.add_lock_violation(violation)

    return False, detail


# ============================================================
# 落点指纹清单 CLI 命令
# ============================================================

@cli.command("generate-landing", help="为某次执行生成落点指纹清单（apply 时已自动生成，此命令可手动补录）")
@click.option("-c", "--config", "config_path", required=True, type=click.Path(),
              help="配置文件路径 (YAML)")
@click.option("-r", "--run-id", "run_id", help="指定执行 ID (默认使用最近一次)")
@click.option("-v", "--verbose", is_flag=True, help="显示详细信息")
def cmd_generate_landing(config_path: str, run_id: Optional[str], verbose: bool):
    try:
        config = load_config(config_path)
    except Exception as e:
        click.echo(f"[错误] 加载配置失败: {e}", err=True)
        sys.exit(1)

    store = _get_store(config)

    if not run_id:
        runs = store.get_all_runs()
        if not runs:
            click.echo("[错误] 没有执行记录，请先执行 apply。", err=True)
            sys.exit(1)
        runs_sorted = sorted(runs, key=lambda r: r.get("created_at", ""), reverse=True)
        run_id = runs_sorted[0]["id"]

    run = store.get_run(run_id)
    if not run:
        click.echo(f"[错误] 执行记录不存在: {run_id}", err=True)
        sys.exit(1)

    existing = store.get_landing_by_run_id(run_id)
    if existing:
        click.echo(click.style(f"[提示] 执行 {run_id} 已有落点指纹清单: {existing.landing_id}", fg="yellow"))
        if not click.confirm("是否重新生成？", default=False):
            click.echo("已取消。")
            sys.exit(0)

    try:
        landing = create_landing_fingerprint(store, run_id, config=config)
        click.echo(click.style(f"[生成成功] Landing ID: {landing.landing_id}", fg="green"))
        click.echo(f"  Run ID: {landing.run_id}")
        click.echo(f"  Snapshot ID: {landing.snapshot_id}")
        click.echo(f"  目标根目录: {landing.dest_dir}")
        click.echo(f"  目标目录数: {len(landing.target_dirs)}")
        click.echo(f"  文件指纹数: {len(landing.file_fingerprints)}")
        click.echo(f"  手工改名数: {len(landing.manual_renames)}")
        click.echo(f"  成功移动: {landing.total_moved_count}")
        click.echo(f"  冲突跳过: {landing.total_skipped_conflict_count}")
        click.echo(f"  人工跳过: {landing.total_skipped_manual_count}")
        click.echo(f"  失败: {landing.total_failed_count}")
        click.echo(f"  校验和: {landing.checksum}")

        if verbose and landing.target_dirs:
            click.echo("\n[目标目录明细]")
            for td in sorted(landing.target_dirs, key=lambda x: x.target_dir):
                click.echo(f"  {td.target_dir}  ({td.file_count} 个文件)")

        if verbose and landing.file_fingerprints:
            click.echo("\n[文件指纹明细]")
            for fp in landing.file_fingerprints:
                click.echo(f"  {fp.filename}")
                click.echo(f"    源: {fp.source_path}")
                click.echo(f"    目标: {fp.target_path}")
                click.echo(f"    大小: {fp.file_size} 字节  摘要: {fp.content_digest[:16]}...")

    except Exception as e:
        click.echo(click.style(f"[错误] 生成落点指纹清单失败: {e}", fg="red"), err=True)
        sys.exit(1)


@cli.command("list-landings", help="列出所有落点指纹清单")
@click.option("-c", "--config", "config_path", required=True, type=click.Path(),
              help="配置文件路径 (YAML)")
@click.option("--active-only", is_flag=True, help="只列出未撤销的清单")
@click.option("--undone-only", is_flag=True, help="只列出已撤销的清单")
@click.option("--json", "as_json", is_flag=True, help="以 JSON 格式输出")
def cmd_list_landings(config_path: str, active_only: bool, undone_only: bool, as_json: bool):
    try:
        config = load_config(config_path)
    except Exception as e:
        click.echo(f"[错误] 加载配置失败: {e}", err=True)
        sys.exit(1)

    store = _get_store(config)
    landings = store.list_landings(undone_only=undone_only, active_only=active_only)

    if as_json:
        click.echo(json.dumps(landings, ensure_ascii=False, indent=2))
        return

    if not landings:
        click.echo("[提示] 暂无落点指纹清单记录。")
        return

    click.echo(f"共 {len(landings)} 条落点指纹清单记录:")
    click.echo()
    click.echo(f"{'Landing ID':<20} {'创建时间':<20} {'Run ID':<12} {'目标目录':<30} {'文件数':<6} {'状态':<8}")
    click.echo("-" * 100)
    for ld in landings:
        status = "已撤销" if ld["is_undone"] else "活动"
        click.echo(
            f"{ld['landing_id']:<20} "
            f"{ld['created_at'][:19]:<20} "
            f"{ld['run_id'][:10]:<12} "
            f"{ld['dest_dir'][-28:]:<30} "
            f"{ld['file_fingerprint_count']:<6} "
            f"{status:<8}"
        )


@cli.command("view-landing", help="查看落点指纹清单的详细信息")
@click.option("-c", "--config", "config_path", required=True, type=click.Path(),
              help="配置文件路径 (YAML)")
@click.option("-l", "--landing-id", "landing_id", help="指定 Landing ID (默认使用最近一次)")
@click.option("-v", "--verbose", is_flag=True, help="显示所有文件指纹的完整信息")
def cmd_view_landing(config_path: str, landing_id: Optional[str], verbose: bool):
    try:
        config = load_config(config_path)
    except Exception as e:
        click.echo(f"[错误] 加载配置失败: {e}", err=True)
        sys.exit(1)

    store = _get_store(config)

    if not landing_id:
        landing = store.get_last_landing()
        if not landing:
            click.echo("[错误] 没有落点指纹清单记录，请先执行 apply 或 generate-landing。", err=True)
            sys.exit(1)
    else:
        landing = store.get_landing(landing_id)
        if not landing:
            click.echo(f"[错误] 落点指纹清单不存在: {landing_id}", err=True)
            sys.exit(1)

    status = "已撤销" if landing.is_undone else "活动"
    click.echo(click.style(f"[落点指纹清单] {landing.landing_id}", fg="cyan", bold=True))
    click.echo(f"  创建时间: {landing.created_at}")
    click.echo(f"  Run ID: {landing.run_id}")
    click.echo(f"  Snapshot ID: {landing.snapshot_id}")
    click.echo(f"  Plan ID: {landing.plan_id}")
    if landing.signoff_id:
        click.echo(f"  Signoff ID: {landing.signoff_id}")
    click.echo(f"  源目录: {landing.source_dir}")
    click.echo(f"  目标根目录: {landing.dest_dir}")
    click.echo(f"  状态: {status}")
    if landing.is_undone and landing.undone_at:
        click.echo(f"  撤销时间: {landing.undone_at}")
    click.echo(f"  模式: {'预演 (DRY-RUN)' if landing.is_dry_run else '实际执行'}")
    click.echo()
    click.echo(click.style("[统计]", fg="yellow"))
    click.echo(f"  成功移动: {landing.total_moved_count}")
    click.echo(f"  冲突跳过: {landing.total_skipped_conflict_count}")
    click.echo(f"  人工跳过: {landing.total_skipped_manual_count}")
    click.echo(f"  失败: {landing.total_failed_count}")
    click.echo(f"  目标目录数: {len(landing.target_dirs)}")
    click.echo(f"  文件指纹数: {len(landing.file_fingerprints)}")
    click.echo(f"  手工改名数: {len(landing.manual_renames)}")
    click.echo()
    click.echo(click.style("[摘要哈希]", fg="yellow"))
    click.echo(f"  配置快照哈希: {landing.config_snapshot_digest or '(空)'}")
    click.echo(f"  目标根目录哈希: {landing.dest_dir_digest or '(空)'}")
    click.echo(f"  Target Paths 哈希: {landing.move_target_paths_digest or '(空)'}")
    click.echo(f"  文件内容汇总哈希: {landing.file_digests_summary or '(空)'}")
    click.echo(f"  清单校验和: {landing.checksum or '(空)'}")
    if landing.imported:
        click.echo()
        click.echo(click.style("[导入信息]", fg="yellow"))
        click.echo(f"  导入来源: {landing.import_source}")
        click.echo(f"  导入时间: {landing.imported_at}")

    if landing.target_dirs:
        click.echo()
        click.echo(click.style("[目标目录明细]", fg="cyan"))
        for td in sorted(landing.target_dirs, key=lambda x: x.target_dir):
            click.echo(f"  {td.target_dir}")
            click.echo(f"    文件数: {td.file_count}  路径哈希: {td.dir_path_digest}")

    if landing.manual_renames:
        click.echo()
        click.echo(click.style("[手工改名记录]", fg="cyan"))
        for mr in landing.manual_renames:
            click.echo(f"  {mr.rename_id}")
            click.echo(f"    原路径: {mr.original_target_path}")
            click.echo(f"    终路径: {mr.final_target_path}")
            click.echo(f"    原因: {mr.rename_reason}")
            click.echo(f"    时间: {mr.renamed_at}  操作人: {mr.renamed_by}")

    if landing.file_fingerprints:
        click.echo()
        click.echo(click.style("[文件指纹明细]", fg="cyan"))
        for i, fp in enumerate(landing.file_fingerprints):
            click.echo(f"  [{i+1}] {fp.filename}")
            click.echo(f"      规则: {fp.matched_rule}")
            click.echo(f"      源: {fp.source_path}")
            click.echo(f"      目标: {fp.target_path}")
            click.echo(f"      大小: {fp.file_size} 字节  mtime: {fp.mtime}")
            if verbose:
                click.echo(f"      内容摘要: {fp.content_digest}")
            else:
                click.echo(f"      内容摘要: {fp.content_digest[:24]}..." if fp.content_digest else "      内容摘要: (空)")

    import_logs = store.get_landing_import_logs(landing.landing_id)
    if import_logs:
        click.echo()
        click.echo(click.style("[导入日志]", fg="cyan"))
        for log in import_logs:
            status_cn = {"success": "成功", "failed": "失败", "forced": "强制", "skipped": "跳过"}.get(log.status, log.status)
            click.echo(f"  {log.timestamp}  [{status_cn}]  来源: {log.source_file}")
            if log.errors:
                click.echo(f"    错误: {'; '.join(log.errors)}")
            if log.conflict_details:
                click.echo(f"    冲突类型: {', '.join(log.conflict_details)}")


@cli.command("export-landing", help="导出落点指纹清单为 JSON 文件")
@click.option("-c", "--config", "config_path", required=True, type=click.Path(),
              help="配置文件路径 (YAML)")
@click.option("-l", "--landing-id", "landing_id", help="指定 Landing ID (默认使用最近一次)")
@click.option("-o", "--output", "output_path", required=True, type=click.Path(),
              help="输出文件路径 (JSON)")
@click.option("-v", "--verbose", is_flag=True, help="显示详细信息")
def cmd_export_landing(config_path: str, landing_id: Optional[str], output_path: str, verbose: bool):
    try:
        config = load_config(config_path)
    except Exception as e:
        click.echo(f"[错误] 加载配置失败: {e}", err=True)
        sys.exit(1)

    store = _get_store(config)

    if not landing_id:
        landing = store.get_last_landing()
        if not landing:
            click.echo("[错误] 没有落点指纹清单记录，请先执行 apply 或 generate-landing。", err=True)
            sys.exit(1)
    else:
        landing = store.get_landing(landing_id)
        if not landing:
            click.echo(f"[错误] 落点指纹清单不存在: {landing_id}", err=True)
            sys.exit(1)

    try:
        result = export_landing_to_file(landing, output_path, store=store)
        click.echo(click.style(f"[导出成功] 已导出到: {output_path}", fg="green"))
        click.echo(f"  Landing ID: {landing.landing_id}")
        click.echo(f"  Run ID: {landing.run_id}")
        click.echo(f"  文件指纹数: {len(landing.file_fingerprints)}")
        click.echo(f"  校验和: {landing.checksum}")
        if landing.change_summary:
            click.echo(f"  变更摘要: {landing.change_summary}")
        if landing.export_result:
            click.echo(f"  导出结果: {landing.export_result}")

        if verbose:
            abs_path = os.path.abspath(output_path)
            file_size = os.path.getsize(abs_path)
            click.echo(f"  绝对路径: {abs_path}")
            click.echo(f"  文件大小: {file_size} 字节")
    except Exception as e:
        click.echo(click.style(f"[错误] 导出失败: {e}", fg="red"), err=True)
        sys.exit(1)


@cli.command("import-landing", help="从 JSON 文件导入落点指纹清单，进行深度比对校验")
@click.option("-c", "--config", "config_path", required=True, type=click.Path(),
              help="配置文件路径 (YAML)")
@click.option("-i", "--input", "input_path", required=True, type=click.Path(),
              help="输入文件路径 (JSON)")
@click.option("--force", is_flag=True, help="即使存在冲突也强制导入（冲突仍会记录到日志）")
@click.option("--by", "imported_by", default="cli", help="导入人标识 (默认: cli)")
@click.option("-v", "--verbose", is_flag=True, help="显示详细信息")
def cmd_import_landing(config_path: str, input_path: str, force: bool, imported_by: str, verbose: bool):
    try:
        config = load_config(config_path)
    except Exception as e:
        click.echo(f"[错误] 加载配置失败: {e}", err=True)
        sys.exit(1)

    store = _get_store(config)

    try:
        landing = load_landing_from_file(input_path)
    except Exception as e:
        click.echo(click.style(f"[错误] 加载落点指纹清单失败: {e}", fg="red"), err=True)
        sys.exit(1)

    click.echo(click.style(f"[导入清单] Landing ID: {landing.landing_id}", fg="cyan"))
    click.echo(f"  Run ID: {landing.run_id}")
    click.echo(f"  Snapshot ID: {landing.snapshot_id}")
    click.echo(f"  目标根目录: {landing.dest_dir}")
    click.echo(f"  当前配置 dest_dir: {config.dest_dir}")
    click.echo(f"  文件指纹数: {len(landing.file_fingerprints)}")
    if landing.change_summary:
        click.echo(f"  变更摘要: {landing.change_summary}")
    click.echo()

    success, validation, import_log, info_messages = import_landing_into_store(
        store, landing, import_source=input_path,
        force=force, imported_by=imported_by,
        current_config=config,
    )

    if not success:
        click.echo(click.style("[导入失败] 清单被深度比对拦截", fg="red", bold=True))
        click.echo()

        if validation.conflict_types:
            click.echo(click.style("[冲突类型]", fg="red"))
            for ct in validation.conflict_types:
                click.echo(f"  - {ct}")
            click.echo()

        if validation.errors:
            click.echo(click.style("[详细错误]", fg="red"))
            for err in validation.errors:
                click.echo(f"  - {err}")
            click.echo()

        if validation.diff_result and validation.diff_result.has_diff:
            click.echo(click.style("[差异对比]", fg="yellow"))
            dr = validation.diff_result
            if dr.dest_dir_changed:
                click.echo(f"  目标目录变化: {dr.diff_details.get('dest_dir', {})}")
            if dr.config_changed:
                click.echo(f"  配置快照变化 (config_snapshot_digest 不一致)")
            if dr.target_paths_diff:
                click.echo(f"  Target Path 差异: {len(dr.target_paths_diff)} 条")
                if verbose:
                    for tpd in dr.target_paths_diff:
                        click.echo(f"    - {tpd.get('target_path')}: {tpd.get('status')}")
            if dr.file_fingerprints_diff:
                click.echo(f"  文件指纹差异: {len(dr.file_fingerprints_diff)} 条")
                if verbose:
                    for ffd in dr.file_fingerprints_diff:
                        click.echo(f"    - {ffd.get('target_path')}: size/digest 不一致")
            if dr.file_count_mismatch:
                click.echo(f"  文件数量不一致: {dr.diff_details.get('file_count', {})}")
            if dr.target_dirs_diff and verbose:
                click.echo(f"  目标目录文件数差异: {len(dr.target_dirs_diff)} 条")
                for tdd in dr.target_dirs_diff:
                    click.echo(f"    - {tdd.get('target_dir')}: local={tdd.get('local_file_count')} imported={tdd.get('imported_file_count')}")
            click.echo()

        click.echo(f"  导入日志 ID: {import_log.import_log_id}")
        click.echo(f"  状态: {import_log.status} (已记录到状态文件)")
        click.echo()
        click.echo(click.style("[提示]", fg="yellow"))
        click.echo("  1. 确认清单来源是否正确")
        click.echo("  2. 核对本地配置 dest_dir 和 rules 是否与清单匹配")
        click.echo("  3. 检查目标目录下的文件是否被改动")
        click.echo("  4. 如需强制导入，使用 --force（冲突仍会记录）")
        sys.exit(1)

    if force and validation.conflict_types:
        click.echo(click.style(f"[强制导入成功] Landing ID: {landing.landing_id}", fg="green"))
        click.echo(click.style(f"  冲突类型: {', '.join(validation.conflict_types)}", fg="yellow"))
    else:
        click.echo(click.style(f"[导入成功] Landing ID: {landing.landing_id}", fg="green"))

    click.echo(f"  导入日志 ID: {import_log.import_log_id}")
    click.echo(f"  导入人: {import_log.imported_by}")
    click.echo(f"  导入时间: {import_log.timestamp}")
    click.echo(f"  状态: {import_log.status}")

    if info_messages:
        click.echo()
        click.echo(click.style("[导入详情]", fg="cyan"))
        for msg in info_messages:
            click.echo(f"  - {msg}")

    if verbose and import_log.warnings:
        click.echo()
        click.echo(click.style("[警告回顾]", fg="yellow"))
        for w in import_log.warnings:
            click.echo(f"  - {w}")

    if validation.conflict_types:
        click.echo()
        click.echo(click.style("[注意] 导入时存在冲突，建议核对：", fg="yellow"))
        for ct in validation.conflict_types:
            click.echo(f"  - {ct}")
        if import_log.status == "forced":
            click.echo(click.style("  (已使用 --force，冲突状态已记录为 forced)", fg="yellow"))

    click.echo()
    click.echo(f"  可使用 list-landings 查看，或 verify-landing -l {landing.landing_id} 进行核对。")


@cli.command("verify-landing", help="核对落点指纹清单：与本地状态和现场文件进行深度比对")
@click.option("-c", "--config", "config_path", required=True, type=click.Path(),
              help="配置文件路径 (YAML)")
@click.option("-l", "--landing-id", "landing_id", help="指定 Landing ID (默认使用最近一次)")
@click.option("-f", "--from-file", "from_file", type=click.Path(),
              help="从 JSON 文件加载清单进行核对（不使用本地状态）")
@click.option("--json", "as_json", is_flag=True, help="以 JSON 格式输出完整核对结果")
@click.option("-v", "--verbose", is_flag=True, help="显示详细差异信息")
def cmd_verify_landing(config_path: str, landing_id: Optional[str], from_file: Optional[str],
                        as_json: bool, verbose: bool):
    try:
        config = load_config(config_path)
    except Exception as e:
        click.echo(f"[错误] 加载配置失败: {e}", err=True)
        sys.exit(1)

    store = _get_store(config)

    if from_file:
        _check_dup = True
        verify_result = verify_landing_file(
            from_file,
            store=store,
            current_config=config,
            check_duplicate=_check_dup,
            verify_source="cli",
        )

        if as_json:
            result = {
                "status": verify_result.status,
                "valid": verify_result.is_valid,
                "landing_id": verify_result.landing_id,
                "run_id": verify_result.run_id,
                "snapshot_id": verify_result.snapshot_id,
                "plan_id": verify_result.plan_id,
                "errors": verify_result.errors,
                "warnings": verify_result.warnings,
                "conflict_types": verify_result.conflict_types,
                "diff": verify_result.diff_result.to_dict() if verify_result.diff_result else None,
                "current_config_dest_dir": verify_result.current_config_dest_dir,
                "landing_dest_dir": verify_result.landing_dest_dir,
                "verified_at": verify_result.verified_at,
            }
            click.echo(json.dumps(result, ensure_ascii=False, indent=2))
            if not verify_result.is_valid:
                sys.exit(1)
            return

        click.echo(click.style(f"[核对] 文件清单 vs 当前配置 + 本地状态", fg="cyan", bold=True))
        click.echo(f"  清单文件: {from_file}")
        click.echo(f"  Landing ID: {verify_result.landing_id}")
        click.echo(f"  Run ID: {verify_result.run_id}")

        if verify_result.landing_dest_dir and verify_result.current_config_dest_dir:
            click.echo(f"  清单 dest_dir: {verify_result.landing_dest_dir}")
            click.echo(f"  配置 dest_dir: {verify_result.current_config_dest_dir}")

        click.echo()

        status_label = {
            "valid": ("[有效] valid ✓", "green"),
            "invalid": ("[无效] invalid ✗", "red"),
            "conflict": ("[冲突] conflict ✗", "yellow"),
        }
        label, color = status_label.get(verify_result.status, (f"[{verify_result.status}]", "white"))
        click.echo(click.style(f"核对结果: {label}", fg=color, bold=True))

        if verify_result.conflict_types:
            click.echo()
            click.echo(click.style("[冲突类型]", fg="yellow"))
            for ct in verify_result.conflict_types:
                click.echo(f"  - {ct}")

        if verify_result.errors:
            click.echo()
            click.echo(click.style("[详细错误]", fg="red"))
            for err in verify_result.errors:
                click.echo(f"  - {err}")

        if verify_result.diff_result and verify_result.diff_result.has_diff:
            diff = verify_result.diff_result
            click.echo()
            click.echo(click.style("[差异明细]", fg="yellow"))
            if diff.duplicate_import:
                click.echo(f"  重复导入: {diff.diff_details.get('duplicate_import', '')}")
            if diff.dest_dir_changed:
                info = diff.diff_details.get('dest_dir', {})
                if info:
                    click.echo(f"  目标根目录变化:")
                    click.echo(f"    本地: {info.get('local', '')}")
                    click.echo(f"    导入: {info.get('imported', '')}")
                info2 = diff.diff_details.get('dest_dir_vs_current_config', {})
                if info2:
                    click.echo(f"  与当前配置目录不一致:")
                    click.echo(f"    清单: {info2.get('landing', '')}")
                    click.echo(f"    当前配置: {info2.get('current_config', '')}")
            if diff.config_changed:
                click.echo(f"  配置快照哈希不一致")
                info = diff.diff_details.get('config_snapshot_digest', {})
                click.echo(f"    本地: {info.get('local', '')}")
                click.echo(f"    导入: {info.get('imported', '')}")
            if diff.file_count_mismatch:
                info = diff.diff_details.get('file_count', {})
                click.echo(f"  文件指纹数量不一致:")
                click.echo(f"    本地: {info.get('local', 0)}")
                click.echo(f"    导入: {info.get('imported', 0)}")
            if diff.target_paths_diff:
                click.echo(f"  Target Path 差异: {len(diff.target_paths_diff)} 条")
                if verbose:
                    for tpd in diff.target_paths_diff:
                        click.echo(f"    - {tpd.get('target_path')}: {tpd.get('status')}")
            if diff.file_fingerprints_diff:
                click.echo(f"  文件指纹差异: {len(diff.file_fingerprints_diff)} 条")
                if verbose:
                    for ffd in diff.file_fingerprints_diff:
                        click.echo(f"    - {ffd.get('target_path')}:")
                        click.echo(f"        本地 size={ffd.get('local_size')}, digest={str(ffd.get('local_digest', ''))[:16]}...")
                        click.echo(f"        导入 size={ffd.get('imported_size')}, digest={str(ffd.get('imported_digest', ''))[:16]}...")
            if diff.target_dirs_diff:
                click.echo(f"  目标目录文件数差异: {len(diff.target_dirs_diff)} 条")
                if verbose:
                    for tdd in diff.target_dirs_diff:
                        click.echo(f"    - {tdd.get('target_dir')}: local={tdd.get('local_file_count')}, imported={tdd.get('imported_file_count')}")

        if verify_result.warnings:
            click.echo()
            click.echo(click.style("[警告]", fg="yellow"))
            for w in verify_result.warnings:
                click.echo(f"  - {w}")

        import_logs = store.get_landing_import_logs(verify_result.landing_id) if verify_result.landing_id != "unknown" else []
        if import_logs:
            click.echo()
            click.echo(click.style("[历史导入记录]", fg="cyan"))
            for log in import_logs:
                status_cn = {"success": "成功", "failed": "失败", "forced": "强制", "skipped": "跳过"}.get(log.status, log.status)
                tag = click.style(f"[{status_cn}]", fg="green" if log.status == "success" else "red")
                click.echo(f"  {log.timestamp} {tag} 来源: {log.source_file}")
                if log.conflict_details:
                    click.echo(f"    冲突: {', '.join(log.conflict_details)}")

        click.echo()
        if verify_result.is_valid:
            click.echo(click.style("✓ 清单有效，与当前配置和本地状态一致。", fg="green"))
        elif verify_result.is_invalid:
            click.echo(click.style("✗ 清单无效，文件本身存在问题。", fg="red"))
            sys.exit(1)
        else:
            click.echo(click.style("✗ 存在冲突，清单与当前配置或本地状态不一致。", fg="yellow"))
            sys.exit(1)

        return

    if not landing_id:
        landing = store.get_last_landing()
        if not landing:
            click.echo("[错误] 没有落点指纹清单记录，请先执行 apply 或 generate-landing。", err=True)
            sys.exit(1)
    else:
        landing = store.get_landing(landing_id)
        if not landing:
            click.echo(f"[错误] 落点指纹清单不存在: {landing_id}", err=True)
            sys.exit(1)

    imported = landing
    local = store.get_landing(landing.landing_id)
    if not as_json:
        click.echo(click.style(f"[核对] Landing ID: {landing.landing_id}", fg="cyan", bold=True))
        click.echo(f"  Run ID: {landing.run_id}")
        click.echo(f"  Snapshot ID: {landing.snapshot_id}")

    _check_dup = bool(from_file)
    validation = validate_landing_for_import(store, imported, check_duplicate=_check_dup, current_config=config)
    diff = validation.diff_result

    if as_json:
        result = {
            "landing_id": imported.landing_id,
            "run_id": imported.run_id,
            "valid": validation.valid,
            "errors": validation.errors,
            "warnings": validation.warnings,
            "conflict_types": validation.conflict_types,
            "diff": diff.to_dict() if diff else None,
            "local_exists": local is not None,
            "local_landing_id": local.landing_id if local else None,
        }
        click.echo(json.dumps(result, ensure_ascii=False, indent=2))
        if not validation.valid:
            sys.exit(1)
        return

    click.echo()

    if validation.valid:
        click.echo(click.style("[核对通过] 清单与本地状态一致 ✓", fg="green", bold=True))
    else:
        click.echo(click.style("[核对失败] 发现冲突 ✗", fg="red", bold=True))

    if validation.conflict_types:
        click.echo()
        click.echo(click.style("[冲突类型]", fg="yellow"))
        for ct in validation.conflict_types:
            click.echo(f"  - {ct}")

    if validation.errors:
        click.echo()
        click.echo(click.style("[详细错误]", fg="red"))
        for err in validation.errors:
            click.echo(f"  - {err}")

    if diff and diff.has_diff:
        click.echo()
        click.echo(click.style("[差异明细]", fg="yellow"))
        if diff.duplicate_import:
            click.echo(f"  重复导入: {diff.diff_details.get('duplicate_import', '')}")
        if diff.dest_dir_changed:
            info = diff.diff_details.get('dest_dir', {})
            if info:
                click.echo(f"  目标根目录变化:")
                click.echo(f"    本地: {info.get('local', '')}")
                click.echo(f"    导入: {info.get('imported', '')}")
            info2 = diff.diff_details.get('dest_dir_vs_current_config', {})
            if info2:
                click.echo(f"  与当前配置目录不一致:")
                click.echo(f"    清单: {info2.get('landing', '')}")
                click.echo(f"    当前配置: {info2.get('current_config', '')}")
        if diff.config_changed:
            click.echo(f"  配置快照哈希不一致")
            info = diff.diff_details.get('config_snapshot_digest', {})
            click.echo(f"    本地: {info.get('local', '')}")
            click.echo(f"    导入: {info.get('imported', '')}")
        if diff.file_count_mismatch:
            info = diff.diff_details.get('file_count', {})
            click.echo(f"  文件指纹数量不一致:")
            click.echo(f"    本地: {info.get('local', 0)}")
            click.echo(f"    导入: {info.get('imported', 0)}")
        if diff.target_paths_diff:
            click.echo(f"  Target Path 差异: {len(diff.target_paths_diff)} 条")
            if verbose:
                for tpd in diff.target_paths_diff:
                    click.echo(f"    - {tpd.get('target_path')}: {tpd.get('status')}")
        if diff.file_fingerprints_diff:
            click.echo(f"  文件指纹差异: {len(diff.file_fingerprints_diff)} 条")
            if verbose:
                for ffd in diff.file_fingerprints_diff:
                    click.echo(f"    - {ffd.get('target_path')}:")
                    click.echo(f"        本地 size={ffd.get('local_size')}, digest={str(ffd.get('local_digest', ''))[:16]}...")
                    click.echo(f"        导入 size={ffd.get('imported_size')}, digest={str(ffd.get('imported_digest', ''))[:16]}...")
        if diff.target_dirs_diff:
            click.echo(f"  目标目录文件数差异: {len(diff.target_dirs_diff)} 条")
            if verbose:
                for tdd in diff.target_dirs_diff:
                    click.echo(f"    - {tdd.get('target_dir')}: local={tdd.get('local_file_count')}, imported={tdd.get('imported_file_count')}")

    if validation.warnings:
        click.echo()
        click.echo(click.style("[警告]", fg="yellow"))
        for w in validation.warnings:
            click.echo(f"  - {w}")

    import_logs = store.get_landing_import_logs(imported.landing_id)
    if import_logs:
        click.echo()
        click.echo(click.style("[历史导入记录]", fg="cyan"))
        for log in import_logs:
            status_cn = {"success": "成功", "failed": "失败", "forced": "强制", "skipped": "跳过"}.get(log.status, log.status)
            tag = click.style(f"[{status_cn}]", fg="green" if log.status == "success" else "red")
            click.echo(f"  {log.timestamp} {tag} 来源: {log.source_file}")
            if log.conflict_details:
                click.echo(f"    冲突: {', '.join(log.conflict_details)}")

    click.echo()
    if validation.valid:
        click.echo(click.style("✓ 清单、JSON/CSV 导出和状态文件三处信息一致。", fg="green"))
    else:
        click.echo(click.style("✗ 存在冲突，建议使用 list-landings/view-landing 查看状态文件，"
                               "或 export-landing 重新导出后对比。", fg="red"))
        sys.exit(1)


def main():
    cli()


if __name__ == "__main__":
    main()
