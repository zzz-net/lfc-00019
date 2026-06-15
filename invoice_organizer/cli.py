"""CLI 入口 - 发票文件批量整理工作流"""
from __future__ import annotations

import os
import sys
from typing import Optional

import click

from .models import load_config, generate_id
from .storage import StateStore
from .workflow import (
    scan_directory, build_plan, apply_plan, undo_run, export_logs,
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

    store.save_plan(final_plan_id, moves, has_conflicts)

    click.echo(click.style(f"[预案生成成功] ID: {final_plan_id}", fg="green"))
    click.echo(f"  总移动项: {len(moves)}")
    click.echo(f"  无冲突项: {len(moves) - len(conflicts)}")
    click.echo(f"  有冲突项: {len(conflicts)}")

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
            "\n[警告] 检测到冲突项，执行 apply 时这些文件将被跳过。",
            fg="yellow"
        ))


@cli.command("apply", help="执行归档预案（移动文件）")
@click.option("-c", "--config", "config_path", required=True, type=click.Path(),
              help="配置文件路径 (YAML)")
@click.option("-p", "--plan-id", "plan_id", help="指定预案 ID (默认使用最近的)")
@click.option("--dry-run", is_flag=True, help="预演模式，不实际移动文件")
@click.option("-y", "--yes", is_flag=True, help="跳过确认提示")
@click.option("-v", "--verbose", is_flag=True, help="显示详细信息")
def cmd_apply(config_path: str, plan_id: Optional[str], dry_run: bool, yes: bool, verbose: bool):
    try:
        config = load_config(config_path)
    except Exception as e:
        click.echo(f"[错误] 加载配置失败: {e}", err=True)
        sys.exit(1)

    store = _get_store(config)

    if plan_id:
        plan = store.get_plan(plan_id)
    else:
        plan = store.get_last_plan()
        if plan:
            plan_id = store.get_last_plan_id()

    if not plan:
        click.echo("[错误] 未找到归档预案，请先执行 plan 命令。", err=True)
        sys.exit(1)

    if plan.get("has_conflicts"):
        click.echo(click.style("[警告] 该预案包含冲突项，执行时将被跳过。", fg="yellow"))

    moves_data = plan.get("moves", [])
    from .models import PlannedMove
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

    click.echo(f"[执行] 预案 ID: {plan_id}")
    click.echo(f"[执行] 模式: {'预演 (DRY-RUN)' if dry_run else '实际执行'}")
    click.echo(f"[执行] 待处理项: {len(moves)}")

    if not yes and not dry_run:
        if not click.confirm("\n确认执行归档操作？", default=False):
            click.echo("已取消。")
            sys.exit(0)

    run_id, executed, success, skipped, failed = apply_plan(
        config, store, plan_id, moves, dry_run=dry_run
    )

    click.echo(click.style(f"\n[执行完成] Run ID: {run_id}", fg="green"))
    click.echo(f"  成功移动: {success}")
    if skipped > 0:
        click.echo(click.style(f"  跳过冲突: {skipped}", fg="yellow"))
    if failed > 0:
        click.echo(click.style(f"  执行失败: {failed}", fg="red"))

    if verbose:
        click.echo("\n[执行详情]")
        for em in executed:
            if em.status == "moved":
                tag = click.style("[移动]", fg="green")
            elif em.status == "skipped_conflict":
                tag = click.style("[跳过]", fg="yellow")
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


def main():
    cli()


if __name__ == "__main__":
    main()
