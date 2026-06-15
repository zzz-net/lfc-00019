"""CLI 入口 - 发票文件批量整理工作流"""
from __future__ import annotations

import os
import sys
from typing import Optional

import click

from .models import load_config, generate_id, ImportLog, now_iso
from .storage import StateStore
from .workflow import (
    scan_directory, build_plan, build_plan_summary, apply_plan,
    undo_run, export_logs, export_plan, filter_moves,
    create_batch_snapshot, diff_config, validate_import_snapshot,
    load_snapshot_from_file, export_snapshot_to_file,
    snapshot_to_planned_moves,
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
    store.save_scan(files)

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
@click.option("-v", "--verbose", is_flag=True, help="显示详细信息")
def cmd_plan(config_path: str, plan_id: Optional[str], verbose: bool):
    try:
        config = load_config(config_path)
    except Exception as e:
        click.echo(f"[错误] 加载配置失败: {e}", err=True)
        sys.exit(1)

    store = _get_store(config)
    scanned = store.get_scan()

    if not scanned:
        click.echo("[提示] 没有找到扫描结果，自动执行 scan...")
        try:
            scanned = scan_directory(config)
            store.save_scan(scanned)
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

    snapshot = create_batch_snapshot(
        config=config,
        scanned_files=scanned,
        moves=moves,
        plan_id=final_plan_id,
        summary=summary,
        config_path=config_path,
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

    if not yes:
        if not click.confirm("\n确认执行撤销操作？", default=False):
            click.echo("已取消。")
            sys.exit(0)

    success, restored, failed, errors_detail = undo_run(store, run_id)

    if success:
        click.echo(click.style(f"\n[撤销成功] 已恢复 {restored} 个文件", fg="green"))
    else:
        click.echo(click.style(f"\n[撤销部分完成] 恢复 {restored} 个，失败 {failed} 个", fg="yellow"))

    if verbose and errors_detail:
        click.echo("\n[撤销详情 - 错误]")
        for err in errors_detail:
            click.echo(click.style(f"  - {err}", fg="red"))


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


@cli.command("list-snapshots", help="列出所有批次快照")
@click.option("-c", "--config", "config_path", required=True, type=click.Path(),
              help="配置文件路径 (YAML)")
def cmd_list_snapshots(config_path: str):
    try:
        config = load_config(config_path)
    except Exception as e:
        click.echo(f"[错误] 加载配置失败: {e}", err=True)
        sys.exit(1)

    store = _get_store(config)
    snapshots = store.list_snapshots()

    if not snapshots:
        click.echo("暂无批次快照。")
        return

    click.echo(f"{'快照ID':<14} {'创建时间':<27} {'预案ID':<14} {'移动项':<8} {'冲突':<6} {'来源':<10}")
    click.echo("-" * 90)
    for s in snapshots:
        created = s.get("created_at", "")[:26]
        has_conflict = "是" if s.get("has_conflicts") else "否"
        source = "导入" if s.get("imported") else "本地"
        click.echo(
            f"{s['snapshot_id']:<14} {created:<27} {s.get('plan_id',''):<14} "
            f"{s.get('move_count',0):<8} {has_conflict:<6} {source:<10}"
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
@click.option("-y", "--yes", is_flag=True, help="跳过确认提示")
@click.option("-v", "--verbose", is_flag=True, help="显示详细信息")
@click.option("--force", is_flag=True, help="即使有警告也强制导入")
def cmd_import_snapshot(config_path: str, input_path: str, yes: bool, verbose: bool, force: bool):
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

    validation = validate_import_snapshot(snapshot)
    diff_result = diff_config(config, snapshot.config_snapshot)

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
        )

    if validation.has_errors:
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

    if diff_result.has_diff:
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
    if existing:
        click.echo(click.style("\n[提示] 快照 ID 已存在，将更新已有快照。", fg="yellow"))

    if not yes:
        if not click.confirm("\n确认导入该批次快照？", default=False):
            store.add_import_log(_make_log("cancelled"))
            click.echo("已取消。")
            sys.exit(0)

    snapshot.imported = True
    snapshot.import_source = os.path.abspath(input_path)

    store.save_snapshot(snapshot)

    if validation.has_errors and force:
        store.add_import_log(_make_log("forced", forced_flag=True))
    else:
        store.add_import_log(_make_log("success"))

    click.echo(click.style(f"\n[导入成功] 快照已保存: {snapshot.snapshot_id}", fg="green"))
    click.echo(f"  可使用 apply -s {snapshot.snapshot_id} 执行，或 export-snapshot 导出复核。")


def main():
    cli()


if __name__ == "__main__":
    main()
