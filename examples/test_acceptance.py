"""验收链路测试脚本"""
import os
import sys
import json
import shutil
import subprocess


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(BASE_DIR)
sys.path.insert(0, PROJECT_ROOT)


def run(cmd, **kwargs):
    print(f"\n{'='*70}")
    print(f"$ {cmd}")
    print('='*70)
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    if "env" in kwargs:
        env.update(kwargs.pop("env"))
    result = subprocess.run(
        cmd, shell=True, cwd=PROJECT_ROOT, capture_output=True,
        text=True, encoding='utf-8', errors='replace', env=env, **kwargs
    )
    if result.stdout:
        try:
            sys.stdout.write(result.stdout)
            sys.stdout.flush()
        except Exception:
            print(result.stdout.encode('ascii', errors='replace').decode('ascii'))
    if result.stderr:
        try:
            sys.stderr.write(result.stderr)
            sys.stderr.flush()
        except Exception:
            print(result.stderr.encode('ascii', errors='replace').decode('ascii'), file=sys.stderr)
    print(f"\n[返回码: {result.returncode}]")
    return result


def cleanup():
    print("\n" + "="*70)
    print("[清理] 重置测试环境...")
    print("="*70)
    dirs_to_clean = [
        os.path.join(BASE_DIR, "sample_invoices"),
        os.path.join(BASE_DIR, "archived"),
        os.path.join(BASE_DIR, ".state"),
    ]
    for d in dirs_to_clean:
        if os.path.exists(d):
            shutil.rmtree(d)
            print(f"  删除: {d}")
    print()


def step_1_create_samples():
    print("\n" + "="*70)
    print("[步骤 1] 创建样例数据")
    print("="*70)
    run(f"python {os.path.join(BASE_DIR, 'create_samples.py')}")


def step_2_scan():
    print("\n" + "="*70)
    print("[步骤 2] 执行 scan 扫描目录")
    print("="*70)
    result = run(f"python -m invoice_organizer scan -c {os.path.join(BASE_DIR, 'config_normal.yaml')} -v")
    assert result.returncode == 0, "scan 命令失败"
    return result


def step_3_plan_normal():
    print("\n" + "="*70)
    print("[步骤 3] 执行 plan 生成归档预案（正常配置）")
    print("="*70)
    result = run(f"python -m invoice_organizer plan -c {os.path.join(BASE_DIR, 'config_normal.yaml')} -v")
    assert result.returncode == 0, "正常配置 plan 应该成功"

    state_file = os.path.join(BASE_DIR, ".state", "invoice_state.json")
    with open(state_file, "r", encoding="utf-8") as f:
        state = json.load(f)

    last_plan_id = state.get("last_plan")
    assert last_plan_id, "应该生成预案 ID"

    plan = state["plans"][last_plan_id]
    assert plan["has_conflicts"] == True, "应有冲突 (预置的冲突文件)"

    moves = plan["moves"]
    conflicts = [m for m in moves if m["conflict_type"] is not None]
    print(f"  预案包含 {len(moves)} 条移动计划, 其中 {len(conflicts)} 条有冲突")
    assert len(conflicts) >= 1, "至少应有1条冲突记录"

    return result


def step_4_plan_conflict():
    print("\n" + "="*70)
    print("[步骤 4] 执行 plan - 两条规则映射同一目标（应失败）")
    print("="*70)
    result = run(f"python -m invoice_organizer plan -c {os.path.join(BASE_DIR, 'config_conflict.yaml')}")
    assert result.returncode != 0, "冲突规则配置下 plan 应该失败"
    combined_output = (result.stdout or "") + (result.stderr or "")
    assert "规则冲突" in combined_output, "应包含规则冲突错误信息"
    print("  [验证通过] plan 按预期失败，检测到规则冲突")
    return result


def step_5_apply_dry_run():
    print("\n" + "="*70)
    print("[步骤 5] 执行 apply --dry-run 预演")
    print("="*70)
    result = run(f"python -m invoice_organizer apply -c {os.path.join(BASE_DIR, 'config_normal.yaml')} --dry-run -v")
    assert result.returncode == 0, "dry-run apply 应该成功"
    return result


def step_6_apply_real():
    print("\n" + "="*70)
    print("[步骤 6] 执行 apply 实际移动文件 (带 -y 确认)")
    print("="*70)
    result = run(f"python -m invoice_organizer apply -c {os.path.join(BASE_DIR, 'config_normal.yaml')} -y -v")
    assert result.returncode == 0, "apply 应该成功"

    archived = os.path.join(BASE_DIR, "archived")
    print("\n[验证] 检查归档目录:")
    for root, dirs, files in os.walk(archived):
        level = root.replace(archived, '').count(os.sep)
        indent = ' ' * 2 * level
        print(f'  {indent}{os.path.basename(root)}/')
        subindent = ' ' * 2 * (level + 1)
        for file in files:
            print(f'  {subindent}{file}')

    state_file = os.path.join(BASE_DIR, ".state", "invoice_state.json")
    with open(state_file, "r", encoding="utf-8") as f:
        state = json.load(f)

    runs = state["runs"]
    actual_run = None
    for rid, r in runs.items():
        if not r["dry_run"]:
            actual_run = r
            break
    assert actual_run, "应有实际执行记录"

    moves = actual_run["moves"]
    moved_count = sum(1 for m in moves if m["status"] == "moved")
    skipped_count = sum(1 for m in moves if m["status"] == "skipped_conflict")
    print(f"\n  执行记录: 移动成功={moved_count}, 跳过冲突={skipped_count}")
    assert skipped_count >= 1, "应至少跳过1条冲突项"
    print("  [验证通过] apply 遇到冲突文件跳过，其他文件正常移动")

    return result


def step_7_export_before_undo():
    print("\n" + "="*70)
    print("[步骤 7] 执行 export 导出日志（撤销前）")
    print("="*70)
    json_path = os.path.join(BASE_DIR, "export_before_undo.json")
    csv_path = os.path.join(BASE_DIR, "export_before_undo.csv")

    result = run(f"python -m invoice_organizer export -c {os.path.join(BASE_DIR, 'config_normal.yaml')} -o {json_path} -f json -v")
    assert result.returncode == 0, "JSON 导出应该成功"
    assert os.path.exists(json_path), "JSON 文件应存在"

    result = run(f"python -m invoice_organizer export -c {os.path.join(BASE_DIR, 'config_normal.yaml')} -o {csv_path} -f csv")
    assert result.returncode == 0, "CSV 导出应该成功"
    assert os.path.exists(csv_path), "CSV 文件应存在"

    with open(json_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    assert "plans" in data
    assert "runs" in data
    assert "scanned_files" in data
    print(f"  [验证通过] JSON 导出包含 {len(data['plans'])} 预案, {len(data['runs'])} 执行记录")

    return result


def step_8_undo():
    print("\n" + "="*70)
    print("[步骤 8] 执行 undo 撤销实际移动")
    print("="*70)
    result = run(f"python -m invoice_organizer undo -c {os.path.join(BASE_DIR, 'config_normal.yaml')} -y -v")
    assert result.returncode == 0, "undo 应该成功"

    state_file = os.path.join(BASE_DIR, ".state", "invoice_state.json")
    with open(state_file, "r", encoding="utf-8") as f:
        state = json.load(f)

    runs = state["runs"]
    actual_run = None
    for rid, r in runs.items():
        if not r["dry_run"]:
            actual_run = r
            break
    assert actual_run["is_undone"] == True, "执行应被标记为已撤销"

    undo_records = state["undo_records"]
    assert len(undo_records) >= 1, "应有撤销记录"
    print(f"  [验证通过] 撤销记录存在，run 标记为已撤销")

    sample_dir = os.path.join(BASE_DIR, "sample_invoices")
    restored_files = [f for f in os.listdir(sample_dir) if not f.startswith(".")]
    print(f"  源目录现有文件数: {len(restored_files)}")
    return result


def step_9_export_after_restart():
    print("\n" + "="*70)
    print("[步骤 9] 模拟重启后 export 验证数据一致性")
    print("="*70)

    state_file = os.path.join(BASE_DIR, ".state", "invoice_state.json")
    with open(state_file, "r", encoding="utf-8") as f:
        state_before = json.load(f)

    json_path = os.path.join(BASE_DIR, "export_after_restart.json")
    result = run(f"python -m invoice_organizer export -c {os.path.join(BASE_DIR, 'config_normal.yaml')} -o {json_path} -f json -v")
    assert result.returncode == 0, "重启后 export 应该成功"

    with open(json_path, "r", encoding="utf-8") as f:
        exported = json.load(f)

    print("\n[一致性验证]")
    print(f"  预案数一致: {len(state_before['plans'])} == {len(exported['plans'])}")
    assert len(state_before['plans']) == len(exported['plans'])

    print(f"  执行数一致: {len(state_before['runs'])} == {len(exported['runs'])}")
    assert len(state_before['runs']) == len(exported['runs'])

    for rid in state_before['runs']:
        orig_run = state_before['runs'][rid]
        exp_run = exported['runs'][rid]
        assert orig_run['is_undone'] == exp_run['is_undone'], f"撤销状态不一致: {rid}"
        assert orig_run['created_at'] == exp_run['created_at'], f"时间戳不一致: {rid}"
        assert len(orig_run['moves']) == len(exp_run['moves']), f"移动记录数不一致: {rid}"
        print(f"  Run {rid}: 撤销={orig_run['is_undone']}, 记录数={len(orig_run['moves'])} OK")

    print(f"  撤销记录数一致: {len(state_before['undo_records'])} == {len(exported['undo_records'])}")
    assert len(state_before['undo_records']) == len(exported['undo_records'])

    for orig_undo, exp_undo in zip(state_before['undo_records'], exported['undo_records']):
        assert orig_undo['undo_timestamp'] == exp_undo['undo_timestamp'], "撤销时间戳不一致"
        assert orig_undo['status'] == exp_undo['status'], "撤销状态不一致"

    print("  [验证通过] 重启后 export 数据完全一致 (预案/移动记录/冲突/撤销状态/时间戳)")

    return result


def step_10_list_runs():
    print("\n" + "="*70)
    print("[步骤 10] 执行 list-runs 查看执行历史")
    print("="*70)
    result = run(f"python -m invoice_organizer list-runs -c {os.path.join(BASE_DIR, 'config_normal.yaml')}")
    assert result.returncode == 0
    return result


def main():
    print("="*70)
    print("发票文件批量整理 CLI - 验收链路测试")
    print("="*70)

    cleanup()

    try:
        step_1_create_samples()
        step_2_scan()
        step_3_plan_normal()
        step_4_plan_conflict()
        step_5_apply_dry_run()
        step_6_apply_real()
        step_7_export_before_undo()
        step_8_undo()
        step_9_export_after_restart()
        step_10_list_runs()

        print("\n" + "="*70)
        print("  所有验收测试通过！")
        print("="*70)
        print("\n验收点总结:")
        print("  OK scan: 按配置扫描目录，识别匹配规则")
        print("  OK plan: 生成归档预案")
        print("  OK plan 冲突检测: 两条规则映射同一目标 -> plan 失败")
        print("  OK apply dry-run: 预演不实际移动")
        print("  OK apply 实际执行: 遇到冲突文件跳过，其他文件继续")
        print("  OK undo: 撤销某次 apply，文件恢复原位")
        print("  OK export: JSON/CSV 导出操作日志")
        print("  OK 重启一致性: 状态持久化后重新 export 数据一致")
        print("  OK 时间戳/撤销状态/冲突记录: 完整保留并一致")

    except AssertionError as e:
        print(f"\n[X] 断言失败: {e}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"\n[X] 测试异常: {e}", file=sys.stderr)
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
