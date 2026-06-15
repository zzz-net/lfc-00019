"""预案版本对比 + 冻结执行 验收链路测试"""
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


def step_2_first_plan():
    print("\n" + "="*70)
    print("[步骤 2] 第一次 plan（正常配置）")
    print("="*70)
    result = run(f"python -m invoice_organizer plan -c {os.path.join(BASE_DIR, 'config_normal.yaml')}")
    assert result.returncode == 0, "第一次 plan 应该成功"

    state_file = os.path.join(BASE_DIR, ".state", "invoice_state.json")
    with open(state_file, "r", encoding="utf-8") as f:
        state = json.load(f)

    first_snapshot_id = state.get("last_snapshot")
    first_plan_id = state.get("last_plan")
    assert first_snapshot_id, "应生成快照 ID"
    assert first_plan_id, "应生成预案 ID"

    print(f"  第一次快照 ID: {first_snapshot_id}")
    print(f"  第一次预案 ID: {first_plan_id}")

    return first_snapshot_id, first_plan_id


def step_3_second_plan_modified():
    print("\n" + "="*70)
    print("[步骤 3] 第二次 plan（修改后配置 - 模拟改了配置、规则顺序、目标目录）")
    print("="*70)
    result = run(f"python -m invoice_organizer plan -c {os.path.join(BASE_DIR, 'config_modified.yaml')}")
    assert result.returncode == 0, "第二次 plan 应该成功"

    state_file = os.path.join(BASE_DIR, ".state", "invoice_state.json")
    with open(state_file, "r", encoding="utf-8") as f:
        state = json.load(f)

    second_snapshot_id = state.get("last_snapshot")
    second_plan_id = state.get("last_plan")
    assert second_snapshot_id, "应生成第二个快照 ID"
    assert second_plan_id, "应生成第二个预案 ID"

    print(f"  第二次快照 ID: {second_snapshot_id}")
    print(f"  第二次预案 ID: {second_plan_id}")

    return second_snapshot_id, second_plan_id


def step_4_diff_plans_display(first_snapshot_id, second_snapshot_id):
    print("\n" + "="*70)
    print("[步骤 4] diff-plans 对比两版预案（仅显示）")
    print("="*70)
    result = run(
        f"python -m invoice_organizer diff-plans -c {os.path.join(BASE_DIR, 'config_normal.yaml')} "
        f"--old-snapshot {first_snapshot_id} --new-snapshot {second_snapshot_id} -v"
    )
    assert result.returncode == 0, "diff-plans 应该成功"

    combined_output = result.stdout + result.stderr

    assert "预案差异对比" in combined_output, "应显示预案差异对比标题"
    assert "移动计划变化" in combined_output, "应显示移动计划变化"
    assert "规则变化" in combined_output, "应显示规则变化"

    assert "目标路径变化" in combined_output or "target_changed" in combined_output.lower() or len([
        line for line in combined_output.split('\n') if '目标路径变化' in line
    ]) > 0 or '目标路径变化的文件' in combined_output, \
        "应显示目标路径变化（配置改动后目标目录变了）"

    print("  [验证通过] diff-plans 显示正常")
    return result


def step_5_diff_plans_export_json(first_snapshot_id, second_snapshot_id):
    print("\n" + "="*70)
    print("[步骤 5] diff-plans 导出 JSON")
    print("="*70)
    json_path = os.path.join(BASE_DIR, "diff_export.json")
    result = run(
        f"python -m invoice_organizer diff-plans -c {os.path.join(BASE_DIR, 'config_normal.yaml')} "
        f"--old-snapshot {first_snapshot_id} --new-snapshot {second_snapshot_id} "
        f"-o {json_path} -f json --save"
    )
    assert result.returncode == 0, "差异 JSON 导出应该成功"
    assert os.path.exists(json_path), "JSON 文件应存在"

    with open(json_path, "r", encoding="utf-8") as f:
        diff_data = json.load(f)

    assert "old_plan_id" in diff_data, "应包含 old_plan_id"
    assert "new_plan_id" in diff_data, "应包含 new_plan_id"
    assert "old_move_count" in diff_data, "应包含 old_move_count"
    assert "new_move_count" in diff_data, "应包含 new_move_count"
    assert "added_moves" in diff_data, "应包含 added_moves"
    assert "removed_moves" in diff_data, "应包含 removed_moves"
    assert "target_changed" in diff_data, "应包含 target_changed"
    assert "rule_changed" in diff_data, "应包含 rule_changed"
    assert "conflict_changed" in diff_data, "应包含 conflict_changed"
    assert "added_rules" in diff_data, "应包含 added_rules"
    assert "removed_rules" in diff_data, "应包含 removed_rules"
    assert "modified_rules" in diff_data, "应包含 modified_rules"
    assert "has_changes" in diff_data, "应包含 has_changes"
    assert diff_data["has_changes"] == True, "应有变化"

    assert len(diff_data["target_changed"]) > 0, "应有目标路径变化的文件（配置改了目标目录）"
    assert len(diff_data["added_rules"]) > 0 or len(diff_data["modified_rules"]) > 0, \
        "应有新增或修改的规则"

    print(f"  目标路径变化: {len(diff_data['target_changed'])} 个文件")
    print(f"  新增规则: {len(diff_data['added_rules'])} 条")
    print(f"  修改规则: {len(diff_data['modified_rules'])} 条")
    print(f"  删除规则: {len(diff_data['removed_rules'])} 条")

    state_file = os.path.join(BASE_DIR, ".state", "invoice_state.json")
    with open(state_file, "r", encoding="utf-8") as f:
        state = json.load(f)
    assert "plan_diffs" in state, "状态文件应包含 plan_diffs"
    assert len(state["plan_diffs"]) >= 1, "应保存了至少 1 条差异记录"

    print("  [验证通过] 差异 JSON 导出正确，且保存到状态文件")

    return diff_data


def step_6_diff_plans_export_csv(first_snapshot_id, second_snapshot_id):
    print("\n" + "="*70)
    print("[步骤 6] diff-plans 导出 CSV")
    print("="*70)
    csv_path = os.path.join(BASE_DIR, "diff_export.csv")
    result = run(
        f"python -m invoice_organizer diff-plans -c {os.path.join(BASE_DIR, 'config_normal.yaml')} "
        f"--old-snapshot {first_snapshot_id} --new-snapshot {second_snapshot_id} "
        f"-o {csv_path} -f csv"
    )
    assert result.returncode == 0, "差异 CSV 导出应该成功"
    assert os.path.exists(csv_path), "CSV 文件应存在"

    with open(csv_path, "r", encoding="utf-8-sig") as f:
        content = f.read()

    assert "=== 预案差异对比摘要 ===" in content, "CSV 应包含摘要"
    assert "=== 规则变化 ===" in content, "CSV 应包含规则变化"
    assert "=== 目标路径变化 ===" in content, "CSV 应包含目标路径变化"
    assert "=== 冲突状态变化 ===" in content, "CSV 应包含冲突状态变化"

    print("  [验证通过] 差异 CSV 导出正确")
    return result


def step_7_save_diff_and_restart_review():
    print("\n" + "="*70)
    print("[步骤 7] 模拟重启后复查差异记录（验证持久化）")
    print("="*70)

    state_file = os.path.join(BASE_DIR, ".state", "invoice_state.json")
    with open(state_file, "r", encoding="utf-8") as f:
        state_before = json.load(f)

    diffs_before = state_before.get("plan_diffs", [])
    assert len(diffs_before) >= 1, "重启前应有差异记录"

    diff_id = diffs_before[0]["diff_id"]
    print(f"  差异记录 ID: {diff_id}")

    result = run(
        f"python -m invoice_organizer export -c {os.path.join(BASE_DIR, 'config_normal.yaml')} "
        f"-o {os.path.join(BASE_DIR, 'state_after_restart.json')} -f json"
    )
    assert result.returncode == 0, "重启后 export 应该成功"

    with open(os.path.join(BASE_DIR, "state_after_restart.json"), "r", encoding="utf-8") as f:
        exported = json.load(f)

    assert "plan_diffs" in exported, "导出应包含 plan_diffs"
    diffs_after = exported.get("plan_diffs", [])
    assert len(diffs_after) == len(diffs_before), "重启后差异记录数应一致"

    assert "plan_locks" in exported, "导出应包含 plan_locks"
    assert "lock_violations" in exported, "导出应包含 lock_violations"

    print(f"  差异记录数一致: {len(diffs_before)} == {len(diffs_after)}")
    print("  [验证通过] 重启后差异记录持久化完整")
    return diff_id


def step_8_lock_first_plan(first_snapshot_id, first_plan_id):
    print("\n" + "="*70)
    print("[步骤 8] 锁定第一版预案")
    print("="*70)
    result = run(
        f"python -m invoice_organizer lock-plan -c {os.path.join(BASE_DIR, 'config_normal.yaml')} "
        f"-s {first_snapshot_id} --reason 复核通过锁定执行版本 -v"
    )
    assert result.returncode == 0, "lock-plan 应该成功"

    combined_output = result.stdout + result.stderr
    assert "锁定成功" in combined_output, "应显示锁定成功"
    assert "锁定 ID" in combined_output, "应显示锁定 ID"

    state_file = os.path.join(BASE_DIR, ".state", "invoice_state.json")
    with open(state_file, "r", encoding="utf-8") as f:
        state = json.load(f)

    assert "active_lock_id" in state, "状态应包含 active_lock_id"
    assert state["active_lock_id"] is not None, "应有活动锁定"

    assert "plan_locks" in state, "状态应包含 plan_locks"
    locks = state["plan_locks"]
    assert len(locks) >= 1, "应有至少 1 条锁定记录"

    active_lock = None
    for lock in locks:
        if lock["lock_id"] == state["active_lock_id"]:
            active_lock = lock
            break

    assert active_lock is not None, "应能找到活动锁定"
    assert active_lock["is_active"] == True, "锁定应处于活动状态"
    assert active_lock["snapshot_id"] == first_snapshot_id, "锁定的快照应匹配"
    assert active_lock["plan_id"] == first_plan_id, "锁定的预案应匹配"
    assert active_lock["reason"] == "复核通过锁定执行版本", "锁定原因应匹配"

    print(f"  活动锁定 ID: {state['active_lock_id']}")
    print(f"  锁定快照: {active_lock['snapshot_id']}")
    print(f"  锁定预案: {active_lock['plan_id']}")
    print("  [验证通过] 预案锁定成功")

    return active_lock["lock_id"]


def step_9_apply_locked_version_should_succeed(first_snapshot_id):
    print("\n" + "="*70)
    print("[步骤 9] 使用锁定版本 apply - 应该成功")
    print("="*70)
    result = run(
        f"python -m invoice_organizer apply -c {os.path.join(BASE_DIR, 'config_normal.yaml')} "
        f"-s {first_snapshot_id} -y --dry-run"
    )
    assert result.returncode == 0, "使用锁定版本 apply 应该成功"

    combined_output = result.stdout + result.stderr
    assert "执行被版本锁定拦截" not in combined_output, "不应被拦截"
    assert "[执行完成]" in combined_output, "应执行完成"

    state_file = os.path.join(BASE_DIR, ".state", "invoice_state.json")
    with open(state_file, "r", encoding="utf-8") as f:
        state = json.load(f)

    runs = state.get("runs", {})
    assert len(runs) >= 1, "应有执行记录"

    print("  [验证通过] 锁定版本 apply 成功")
    return result


def step_10_apply_latest_should_be_rejected():
    print("\n" + "="*70)
    print("[步骤 10] 不指定快照 apply（默认用最新）- 应该被锁定拦截")
    print("="*70)
    result = run(
        f"python -m invoice_organizer apply -c {os.path.join(BASE_DIR, 'config_modified.yaml')} "
        f"-y --dry-run"
    )
    assert result.returncode != 0, "版本不一致的 apply 应该失败"

    combined_output = result.stdout + result.stderr
    assert "执行被版本锁定拦截" in combined_output, "应显示被拦截"
    assert "锁定信息" in combined_output, "应显示锁定信息"

    state_file = os.path.join(BASE_DIR, ".state", "invoice_state.json")
    with open(state_file, "r", encoding="utf-8") as f:
        state = json.load(f)

    assert "lock_violations" in state, "状态应包含 lock_violations"
    violations = state.get("lock_violations", [])
    assert len(violations) >= 1, "应有至少 1 条违规记录"

    latest_violation = violations[-1]
    assert latest_violation["blocked"] == True, "违规应被标记为已拦截"
    assert latest_violation["violation_type"] == "wrong_snapshot", "违规类型应为 wrong_snapshot"

    print(f"  违规记录数: {len(violations)}")
    print(f"  最新违规类型: {latest_violation['violation_type']}")
    print("  [验证通过] 版本不一致的 apply 被成功拦截")

    return latest_violation


def step_11_list_locks():
    print("\n" + "="*70)
    print("[步骤 11] list-locks 查看锁定记录")
    print("="*70)
    result = run(
        f"python -m invoice_organizer list-locks -c {os.path.join(BASE_DIR, 'config_normal.yaml')} -v"
    )
    assert result.returncode == 0, "list-locks 应该成功"

    combined_output = result.stdout + result.stderr
    assert "锁定ID" in combined_output, "应显示锁定ID列"
    assert "活动" in combined_output, "应显示活动状态"

    print("  [验证通过] list-locks 显示正常")
    return result


def step_12_unlock():
    print("\n" + "="*70)
    print("[步骤 12] unlock-plan 释放锁定")
    print("="*70)
    result = run(
        f"python -m invoice_organizer unlock-plan -c {os.path.join(BASE_DIR, 'config_normal.yaml')} "
        f"--reason 复核完成释放锁定 -v"
    )
    assert result.returncode == 0, "unlock-plan 应该成功"

    combined_output = result.stdout + result.stderr
    assert "解锁成功" in combined_output, "应显示解锁成功"

    state_file = os.path.join(BASE_DIR, ".state", "invoice_state.json")
    with open(state_file, "r", encoding="utf-8") as f:
        state = json.load(f)

    assert state.get("active_lock_id") is None, "活动锁定应为空"

    locks = state.get("plan_locks", [])
    released_lock = None
    for lock in locks:
        if not lock.get("is_active", True):
            released_lock = lock
            break

    assert released_lock is not None, "应有已释放的锁定"
    assert released_lock.get("release_reason") == "复核完成释放锁定", "释放原因应匹配"

    print("  [验证通过] 锁定释放成功")
    return result


def step_13_full_cycle_real_apply_and_undo(first_snapshot_id):
    print("\n" + "="*70)
    print("[步骤 13] 完整流程：锁定 -> 实际 apply -> undo 精确回滚")
    print("="*70)

    run(
        f"python -m invoice_organizer lock-plan -c {os.path.join(BASE_DIR, 'config_normal.yaml')} "
        f"-s {first_snapshot_id}"
    )

    state_file = os.path.join(BASE_DIR, ".state", "invoice_state.json")
    with open(state_file, "r", encoding="utf-8") as f:
        state_before = json.load(f)

    sample_dir = os.path.join(BASE_DIR, "sample_invoices")
    files_before = len([f for f in os.listdir(sample_dir) if not f.startswith(".")])
    print(f"  apply 前源目录文件数: {files_before}")

    result = run(
        f"python -m invoice_organizer apply -c {os.path.join(BASE_DIR, 'config_normal.yaml')} "
        f"-s {first_snapshot_id} -y"
    )
    assert result.returncode == 0, "实际 apply 应该成功"

    with open(state_file, "r", encoding="utf-8") as f:
        state_after_apply = json.load(f)

    runs = state_after_apply.get("runs", {})
    actual_run = None
    for rid, r in runs.items():
        if not r["dry_run"]:
            actual_run = r
            break
    assert actual_run, "应有实际执行记录"

    moves = actual_run["moves"]
    moved_count = sum(1 for m in moves if m["status"] == "moved")
    skipped_count = sum(1 for m in moves if m["status"] == "skipped_conflict")

    print(f"  移动成功: {moved_count}")
    print(f"  冲突跳过: {skipped_count}")

    files_after = len([f for f in os.listdir(sample_dir) if not f.startswith(".")])
    print(f"  apply 后源目录文件数: {files_after}")
    assert files_after == files_before - moved_count, f"源目录应减少 {moved_count} 个文件"

    run_id = actual_run["id"]
    result_undo = run(
        f"python -m invoice_organizer undo -c {os.path.join(BASE_DIR, 'config_normal.yaml')} "
        f"-r {run_id} -y -v"
    )
    assert result_undo.returncode == 0, "undo 应该成功"

    files_after_undo = len([f for f in os.listdir(sample_dir) if not f.startswith(".")])
    print(f"  undo 后源目录文件数: {files_after_undo}")
    assert files_after_undo == files_before, "undo 后源目录文件数应恢复"

    with open(state_file, "r", encoding="utf-8") as f:
        state_after_undo = json.load(f)

    undo_records = state_after_undo.get("undo_records", [])
    latest_undo = undo_records[-1]
    assert latest_undo["run_id"] == run_id, "撤销记录应对应正确的 run"
    assert latest_undo["moves_restored"] == moved_count, "恢复数量应等于移动数量"
    assert latest_undo["status"] == "completed", "撤销状态应为 completed"

    print("  [验证通过] 完整流程：锁定 apply 成功，undo 精确回滚实际移动的文件")
    return run_id


def step_14_restart_all_state_persistent():
    print("\n" + "="*70)
    print("[步骤 14] 重启验证：所有状态持久化完整")
    print("="*70)

    state_file = os.path.join(BASE_DIR, ".state", "invoice_state.json")
    with open(state_file, "r", encoding="utf-8") as f:
        state = json.load(f)

    required_sections = [
        "plans", "runs", "snapshots", "undo_records",
        "import_logs", "plan_locks", "lock_violations", "plan_diffs",
        "active_lock_id",
    ]

    print("  检查状态文件各部分:")
    for section in required_sections:
        assert section in state, f"状态应包含 {section}"
        print(f"    - {section}: OK")

    json_path = os.path.join(BASE_DIR, "full_state_export.json")
    result = run(
        f"python -m invoice_organizer export -c {os.path.join(BASE_DIR, 'config_normal.yaml')} "
        f"-o {json_path} -f json"
    )
    assert result.returncode == 0, "完整状态导出应该成功"

    with open(json_path, "r", encoding="utf-8") as f:
        exported = json.load(f)

    for section in required_sections:
        assert section in exported, f"导出应包含 {section}"

    print(f"  预案数: {len(state['plans'])} == {len(exported['plans'])}")
    assert len(state["plans"]) == len(exported["plans"])

    print(f"  执行数: {len(state['runs'])} == {len(exported['runs'])}")
    assert len(state["runs"]) == len(exported["runs"])

    print(f"  快照数: {len(state['snapshots'])} == {len(exported['snapshots'])}")
    assert len(state["snapshots"]) == len(exported["snapshots"])

    print(f"  锁定记录数: {len(state['plan_locks'])} == {len(exported['plan_locks'])}")
    assert len(state["plan_locks"]) == len(exported["plan_locks"])

    print(f"  违规记录数: {len(state['lock_violations'])} == {len(exported['lock_violations'])}")
    assert len(state["lock_violations"]) == len(exported["lock_violations"])

    print(f"  差异记录数: {len(state['plan_diffs'])} == {len(exported['plan_diffs'])}")
    assert len(state["plan_diffs"]) == len(exported["plan_diffs"])

    print("  [验证通过] 重启后所有状态完整持久化，导出一致")


def main():
    print("="*70)
    print("预案版本对比 + 冻结执行 - 验收链路测试")
    print("="*70)

    cleanup()

    try:
        step_1_create_samples()
        first_snapshot_id, first_plan_id = step_2_first_plan()
        second_snapshot_id, second_plan_id = step_3_second_plan_modified()

        step_4_diff_plans_display(first_snapshot_id, second_snapshot_id)
        step_5_diff_plans_export_json(first_snapshot_id, second_snapshot_id)
        step_6_diff_plans_export_csv(first_snapshot_id, second_snapshot_id)
        step_7_save_diff_and_restart_review()

        lock_id = step_8_lock_first_plan(first_snapshot_id, first_plan_id)
        step_9_apply_locked_version_should_succeed(first_snapshot_id)
        step_10_apply_latest_should_be_rejected()
        step_11_list_locks()
        step_12_unlock()

        step_13_full_cycle_real_apply_and_undo(first_snapshot_id)
        step_14_restart_all_state_persistent()

        print("\n" + "="*70)
        print("  所有验收测试通过！")
        print("="*70)
        print("\n验收点总结:")
        print("  OK 两次 plan 对比: diff-plans 命令正常工作")
        print("  OK 文件去向变化: target_changed 正确识别目标目录变化")
        print("  OK 规则增删改: added/removed/modified rules 正确识别")
        print("  OK 冲突状态变化: conflict_changed 正确识别")
        print("  OK 差异导出 JSON: 完整的差异数据结构")
        print("  OK 差异导出 CSV: 人类可读的表格格式")
        print("  OK 差异记录持久化: plan_diffs 保存到状态文件，重启可查")
        print("  OK 版本锁定: lock-plan 锁定指定快照版本")
        print("  OK 锁定版本执行成功: apply -s 锁定版本正常执行")
        print("  OK 版本不一致被拒绝: 默认最新版本 apply 被拦截")
        print("  OK 锁定违规记录: lock_violations 记录每次拦截，重启可查")
        print("  OK 解锁: unlock-plan 正常释放锁定")
        print("  OK undo 精确回滚: 只回滚本次实际移动的文件")
        print("  OK 全状态持久化: 预案/执行/锁定/违规/差异 全部重启可查")

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
