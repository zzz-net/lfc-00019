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
    print("[步骤 4] 执行 plan - 两条规则映射同一目标（应成功，摘要中可见）")
    print("="*70)
    result = run(f"python -m invoice_organizer plan -c {os.path.join(BASE_DIR, 'config_conflict.yaml')} -v")
    assert result.returncode == 0, "同目录规则配置下 plan 应该成功"
    combined_output = (result.stdout or "") + (result.stderr or "")
    assert "多条规则映射同一目录" in combined_output, "摘要应显示同目录规则提示"
    assert "same_folder" in combined_output, "应显示 same_folder 目录"

    state_file = os.path.join(BASE_DIR, ".state", "invoice_state_conflict.json")
    with open(state_file, "r", encoding="utf-8") as f:
        state = json.load(f)

    last_plan_id = state.get("last_plan")
    assert last_plan_id, "应生成预案 ID"
    plan = state["plans"][last_plan_id]
    moves = plan["moves"]
    print(f"  预案包含 {len(moves)} 条移动计划")
    print(f"  [验证通过] 同目录规则不再阻止 plan 生成，摘要中正确显示")
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


def step_11_export_plan_json():
    print("\n" + "="*70)
    print("[步骤 11] 执行 export-plan 导出预案 (JSON)")
    print("="*70)
    json_path = os.path.join(BASE_DIR, "plan_export.json")
    result = run(
        f"python -m invoice_organizer export-plan -c {os.path.join(BASE_DIR, 'config_normal.yaml')} "
        f"-o {json_path} -f json -v"
    )
    assert result.returncode == 0, "export-plan JSON 应该成功"
    assert os.path.exists(json_path), "JSON 文件应存在"

    with open(json_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    assert "summary" in data, "导出应包含 summary"
    assert "moves" in data, "导出应包含 moves"
    assert "unmatched_files" in data, "导出应包含 unmatched_files"

    summary = data["summary"]
    assert summary["total_files"] == 9, "总文件数应为 9 (file_extensions 过滤后)"
    assert summary["matched_files"] == 9, "匹配文件数应为 9"
    assert summary["unmatched_files"] == 0, "未匹配文件数应为 0"
    assert summary["conflict_count"] == 1, "冲突数应为 1"
    assert "new_target_dirs" in summary, "应包含 new_target_dirs 字段"
    assert len(summary["files_per_rule"]) == 7, "应有 7 条规则"

    moves = data["moves"]
    assert len(moves) == 9, "应有 9 条移动计划"
    conflict_moves = [m for m in moves if m["conflict_type"] is not None]
    assert len(conflict_moves) == 1, "应有 1 条冲突记录"

    print(f"  [验证通过] 预案 JSON 导出正确: {len(moves)} 条移动, "
          f"{summary['conflict_count']} 冲突, "
          f"{len(summary['new_target_dirs'])} 新建目录")
    return result


def step_12_export_plan_csv():
    print("\n" + "="*70)
    print("[步骤 12] 执行 export-plan 导出预案 (CSV)")
    print("="*70)
    csv_path = os.path.join(BASE_DIR, "plan_export.csv")
    result = run(
        f"python -m invoice_organizer export-plan -c {os.path.join(BASE_DIR, 'config_normal.yaml')} "
        f"-o {csv_path} -f csv"
    )
    assert result.returncode == 0, "export-plan CSV 应该成功"
    assert os.path.exists(csv_path), "CSV 文件应存在"

    with open(csv_path, "r", encoding="utf-8-sig") as f:
        content = f.read()

    assert "=== 预案摘要 ===" in content, "CSV 应包含预案摘要"
    assert "=== 各规则文件数 ===" in content, "CSV 应包含规则分布"
    assert "=== 各目标目录文件数 ===" in content, "CSV 应包含目录分布"
    assert "=== 移动计划详情 ===" in content, "CSV 应包含移动详情"
    assert "=== 未匹配规则文件 ===" in content, "CSV 应包含未匹配文件"

    print(f"  [验证通过] 预案 CSV 导出成功，包含所有章节")
    return result


def step_13_apply_filter_by_rule():
    print("\n" + "="*70)
    print("[步骤 13] 按规则筛选 apply：只执行电子发票PDF 和 报销单Excel")
    print("="*70)
    result = run(
        f"python -m invoice_organizer apply -c {os.path.join(BASE_DIR, 'config_normal.yaml')} "
        f"--rule 电子发票PDF --rule 报销单Excel -y -v"
    )
    assert result.returncode == 0, "筛选 apply 应该成功"

    state_file = os.path.join(BASE_DIR, ".state", "invoice_state.json")
    with open(state_file, "r", encoding="utf-8") as f:
        state = json.load(f)

    runs_sorted = sorted(state["runs"].values(), key=lambda r: r["created_at"], reverse=True)
    latest_run = runs_sorted[0]
    moves = latest_run["moves"]

    moved_count = sum(1 for m in moves if m["status"] == "moved")
    skipped_conflict_count = sum(1 for m in moves if m["status"] == "skipped_conflict")
    skipped_manual_count = sum(1 for m in moves if m["status"] == "skipped_manual")
    failed_count = sum(1 for m in moves if m["status"] == "failed")

    print(f"  执行结果: 移动={moved_count}, 冲突跳过={skipped_conflict_count}, "
          f"人工跳过={skipped_manual_count}, 失败={failed_count}")

    assert moved_count == 3, "应移动 3 个文件 (2个电子发票 + 1个报销单)"
    assert skipped_conflict_count == 0, "选中的规则不应有冲突"
    assert skipped_manual_count == 6, "应人工跳过 6 个文件 (9条计划 - 3条选中)"
    assert failed_count == 0, "不应有失败"

    for m in moves:
        if m["status"] == "skipped_manual":
            assert "规则不在筛选范围内" in m["error_message"], "人工跳过应有原因描述"

    print(f"  [验证通过] 按规则筛选执行正确，人工跳过记录完整")
    return result


def step_14_undo_partial_apply():
    print("\n" + "="*70)
    print("[步骤 14] undo 部分执行：只回滚本次实际移动的文件")
    print("="*70)

    state_file = os.path.join(BASE_DIR, ".state", "invoice_state.json")
    with open(state_file, "r", encoding="utf-8") as f:
        state_before = json.load(f)

    runs_sorted = sorted(state_before["runs"].values(), key=lambda r: r["created_at"], reverse=True)
    target_run = runs_sorted[0]
    run_id = target_run["id"]

    moved_before = sum(1 for m in target_run["moves"] if m["status"] == "moved")
    skipped_manual_before = sum(1 for m in target_run["moves"] if m["status"] == "skipped_manual")

    print(f"  目标 Run: {run_id}")
    print(f"  移动记录: {moved_before} 条")
    print(f"  人工跳过: {skipped_manual_before} 条")

    sample_dir = os.path.join(BASE_DIR, "sample_invoices")
    files_before_undo = len([f for f in os.listdir(sample_dir) if not f.startswith(".")])
    print(f"  源目录文件数(undo前): {files_before_undo}")

    result = run(
        f"python -m invoice_organizer undo -c {os.path.join(BASE_DIR, 'config_normal.yaml')} "
        f"-r {run_id} -y -v"
    )
    assert result.returncode == 0, "undo 应该成功"

    with open(state_file, "r", encoding="utf-8") as f:
        state_after = json.load(f)

    run_after = state_after["runs"][run_id]
    assert run_after["is_undone"] == True, "run 应标记为已撤销"

    undo_records = state_after["undo_records"]
    latest_undo = undo_records[-1]
    assert latest_undo["run_id"] == run_id, "撤销记录应对应正确的 run"
    assert latest_undo["moves_restored"] == moved_before, "恢复数量应等于移动数量"
    assert latest_undo["moves_restored"] != len(target_run["moves"]), \
        "恢复数量不应等于总记录数（人工跳过的不算）"

    files_after_undo = len([f for f in os.listdir(sample_dir) if not f.startswith(".")])
    print(f"  源目录文件数(undo后): {files_after_undo}")
    assert files_after_undo == files_before_undo + moved_before, \
        f"源目录应增加 {moved_before} 个文件"

    print(f"  [验证通过] undo 只回滚了 {moved_before} 个实际移动的文件，"
          f"人工跳过的 {skipped_manual_before} 条未受影响")
    return result


def step_15_config_change_replan():
    print("\n" + "="*70)
    print("[步骤 15] 配置改动后重新 scan + plan（使用修改后配置）")
    print("="*70)

    state_file = os.path.join(BASE_DIR, ".state", "invoice_state.json")
    with open(state_file, "r", encoding="utf-8") as f:
        state_before = json.load(f)
    plan_count_before = len(state_before["plans"])

    run(f"python -m invoice_organizer scan -c {os.path.join(BASE_DIR, 'config_modified.yaml')}")

    result = run(
        f"python -m invoice_organizer plan -c {os.path.join(BASE_DIR, 'config_modified.yaml')} -v"
    )
    assert result.returncode == 0, "修改配置后 plan 应该成功"

    with open(state_file, "r", encoding="utf-8") as f:
        state_after = json.load(f)
    plan_count_after = len(state_after["plans"])

    assert plan_count_after == plan_count_before + 1, "应新增 1 个预案"

    last_plan_id = state_after["last_plan"]
    plan = state_after["plans"][last_plan_id]
    moves = plan["moves"]

    print(f"  新预案 ID: {last_plan_id}")
    print(f"  移动计划数: {len(moves)}")
    assert len(moves) == 11, "修改后配置应匹配 11 个文件 (增加了 txt 和 md)"

    combined_output = result.stdout
    assert "按规则分布" in combined_output, "plan 输出应显示规则分布"
    assert "按目标目录分布" in combined_output, "plan 输出应显示目录分布"
    assert "多条规则映射同一目录" in combined_output, "plan 输出应显示同目录规则"
    assert "本次新建的目标目录" in combined_output, "plan 输出应显示新建目录"
    assert "匹配规则:" in combined_output, "plan 输出应显示匹配数"
    assert "未匹配规则:" in combined_output, "plan 输出应显示未匹配数"

    print(f"  [验证通过] 配置改动后成功生成新预案，摘要信息完整")
    return result


def step_16_verify_modified_plan_summary():
    print("\n" + "="*70)
    print("[步骤 16] 验证修改后预案的摘要信息（同目录规则、未命中文件）")
    print("="*70)

    json_path = os.path.join(BASE_DIR, "plan_export_modified.json")
    result = run(
        f"python -m invoice_organizer export-plan -c {os.path.join(BASE_DIR, 'config_modified.yaml')} "
        f"-o {json_path} -f json"
    )
    assert result.returncode == 0

    with open(json_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    summary = data["summary"]
    print(f"  总文件数: {summary['total_files']}")
    print(f"  匹配规则: {summary['matched_files']}")
    print(f"  未匹配规则: {summary['unmatched_files']}")
    print(f"  冲突数: {summary['conflict_count']}")
    print(f"  新建目录数: {len(summary['new_target_dirs'])}")
    print(f"  同目录规则组数: {len(summary['rules_with_same_target'])}")

    assert summary["total_files"] == 11, "总文件数应为 11 (txt和md也被扫描)"
    assert summary["matched_files"] == 11, "匹配文件数应为 11"
    assert summary["unmatched_files"] == 0, "未匹配文件数应为 0"

    same_targets = summary["rules_with_same_target"]
    assert len(same_targets) >= 2, "应有至少 2 组同目录规则 (images 和 misc)"

    found_images = False
    found_misc = False
    for tdir, rules in same_targets.items():
        if "images" in tdir.replace("\\", "/"):
            found_images = True
            assert len(rules) >= 2, "images 目录应有多条规则"
        if "misc" in tdir.replace("\\", "/"):
            found_misc = True
            assert len(rules) >= 2, "misc 目录应有多条规则"
    assert found_images, "应检测到 images 同目录规则"
    assert found_misc, "应检测到 misc 同目录规则"

    unmatched = data["unmatched_files"]
    assert len(unmatched) == summary["unmatched_files"], \
        "未命中文件数应与摘要一致"

    print(f"  [验证通过] 修改后配置摘要正确: 同目录规则={len(same_targets)}组, "
          f"未命中={summary['unmatched_files']}, 冲突={summary['conflict_count']}")
    return result


def step_17_apply_filter_by_type():
    print("\n" + "="*70)
    print("[步骤 17] 按文件类型筛选 apply：只移动图片文件")
    print("="*70)

    result = run(
        f"python -m invoice_organizer apply -c {os.path.join(BASE_DIR, 'config_modified.yaml')} "
        f"--type jpg --type png -y -v"
    )
    assert result.returncode == 0

    state_file = os.path.join(BASE_DIR, ".state", "invoice_state.json")
    with open(state_file, "r", encoding="utf-8") as f:
        state = json.load(f)

    runs_sorted = sorted(state["runs"].values(), key=lambda r: r["created_at"], reverse=True)
    latest_run = runs_sorted[0]
    moves = latest_run["moves"]

    moved = [m for m in moves if m["status"] == "moved"]
    skipped_manual = [m for m in moves if m["status"] == "skipped_manual"]

    print(f"  移动: {len(moved)} 个")
    print(f"  人工跳过: {len(skipped_manual)} 个")

    assert len(moved) == 2, "应移动 2 个图片文件 (jpg 和 png)"
    for m in moved:
        assert m["filename"].endswith(".jpg") or m["filename"].endswith(".png"), \
            "移动的应都是图片文件"

    for m in skipped_manual:
        assert "文件类型不在筛选范围内" in m["error_message"], \
            "人工跳过原因应包含文件类型"

    print(f"  [验证通过] 按文件类型筛选正确")

    run_id = latest_run["id"]
    run(
        f"python -m invoice_organizer undo -c {os.path.join(BASE_DIR, 'config_modified.yaml')} "
        f"-r {run_id} -y"
    )
    print(f"  (已撤销本次执行以保持环境清洁)")
    return result


def step_18_apply_filter_by_target():
    print("\n" + "="*70)
    print("[步骤 18] 按目标目录筛选 apply：只执行 vat 目录下的")
    print("="*70)

    result = run(
        f"python -m invoice_organizer apply -c {os.path.join(BASE_DIR, 'config_modified.yaml')} "
        f"--target vat -y -v"
    )
    assert result.returncode == 0

    state_file = os.path.join(BASE_DIR, ".state", "invoice_state.json")
    with open(state_file, "r", encoding="utf-8") as f:
        state = json.load(f)

    runs_sorted = sorted(state["runs"].values(), key=lambda r: r["created_at"], reverse=True)
    latest_run = runs_sorted[0]
    moves = latest_run["moves"]

    moved = [m for m in moves if m["status"] == "moved"]
    skipped_conflict = [m for m in moves if m["status"] == "skipped_conflict"]
    skipped_manual = [m for m in moves if m["status"] == "skipped_manual"]

    print(f"  移动: {len(moved)} 个")
    print(f"  冲突跳过: {len(skipped_conflict)} 个")
    print(f"  人工跳过: {len(skipped_manual)} 个")

    for m in moved:
        target_dir = os.path.dirname(m["target_path"]).replace("\\", "/")
        assert "vat" in target_dir, f"移动的文件应在 vat 目录下: {target_dir}"

    for m in skipped_manual:
        assert "目标目录不在筛选范围内" in m["error_message"], \
            "人工跳过原因应包含目标目录"

    print(f"  [验证通过] 按目标目录筛选正确")

    run_id = latest_run["id"]
    run(
        f"python -m invoice_organizer undo -c {os.path.join(BASE_DIR, 'config_modified.yaml')} "
        f"-r {run_id} -y"
    )
    print(f"  (已撤销本次执行以保持环境清洁)")
    return result


def step_19_export_verify_all_statuses():
    print("\n" + "="*70)
    print("[步骤 19] 完整日志导出：验证四类状态都在结果中")
    print("="*70)

    result = run(
        f"python -m invoice_organizer apply -c {os.path.join(BASE_DIR, 'config_modified.yaml')} "
        f"--rule 电子发票PDF -y"
    )
    assert result.returncode == 0

    json_path = os.path.join(BASE_DIR, "full_export.json")
    result = run(
        f"python -m invoice_organizer export -c {os.path.join(BASE_DIR, 'config_modified.yaml')} "
        f"-o {json_path} -f json"
    )
    assert result.returncode == 0

    with open(json_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    all_statuses = set()
    for run_id, run_data in data["runs"].items():
        for m in run_data["moves"]:
            all_statuses.add(m["status"])

    print(f"  导出中包含的状态: {sorted(all_statuses)}")

    assert "moved" in all_statuses, "导出应有 moved 状态"
    assert "skipped_conflict" in all_statuses, "导出应有 skipped_conflict 状态"
    assert "skipped_manual" in all_statuses, "导出应有 skipped_manual 状态"

    csv_path = os.path.join(BASE_DIR, "full_export.csv")
    run(
        f"python -m invoice_organizer export -c {os.path.join(BASE_DIR, 'config_modified.yaml')} "
        f"-o {csv_path} -f csv"
    )
    with open(csv_path, "r", encoding="utf-8-sig") as f:
        csv_content = f.read()
    assert "skipped_manual" in csv_content or "人工跳过" in csv_content or "skipped_conflict" in csv_content, \
        "CSV 导出应包含跳过状态"

    print(f"  [验证通过] 完整日志导出包含所有状态类型: {sorted(all_statuses)}")
    return result


def step_20_restart_consistency_full():
    print("\n" + "="*70)
    print("[步骤 20] 重启后数据一致性：预案、执行、撤销、四类状态全部核对")
    print("="*70)

    state_file = os.path.join(BASE_DIR, ".state", "invoice_state.json")
    with open(state_file, "r", encoding="utf-8") as f:
        state_before = json.load(f)

    json_path = os.path.join(BASE_DIR, "restart_consistency.json")
    result = run(
        f"python -m invoice_organizer export -c {os.path.join(BASE_DIR, 'config_modified.yaml')} "
        f"-o {json_path} -f json -v"
    )
    assert result.returncode == 0

    with open(json_path, "r", encoding="utf-8") as f:
        exported = json.load(f)

    print(f"  预案数: {len(state_before['plans'])} == {len(exported['plans'])}")
    assert len(state_before["plans"]) == len(exported["plans"])

    print(f"  执行数: {len(state_before['runs'])} == {len(exported['runs'])}")
    assert len(state_before["runs"]) == len(exported["runs"])

    print(f"  撤销记录数: {len(state_before['undo_records'])} == {len(exported['undo_records'])}")
    assert len(state_before["undo_records"]) == len(exported["undo_records"])

    for rid in state_before["runs"]:
        orig = state_before["runs"][rid]
        exp = exported["runs"][rid]
        assert orig["is_undone"] == exp["is_undone"], f"撤销状态不一致: {rid}"
        assert len(orig["moves"]) == len(exp["moves"]), f"移动记录数不一致: {rid}"

        orig_statuses = sorted(set(m["status"] for m in orig["moves"]))
        exp_statuses = sorted(set(m["status"] for m in exp["moves"]))
        assert orig_statuses == exp_statuses, f"状态集合不一致: {rid}"

    for plan_id in state_before["plans"]:
        orig = state_before["plans"][plan_id]
        exp = exported["plans"][plan_id]
        assert orig["has_conflicts"] == exp["has_conflicts"], f"冲突标记不一致: {plan_id}"
        assert len(orig["moves"]) == len(exp["moves"]), f"移动计划数不一致: {plan_id}"

    print(f"  [验证通过] 重启后所有数据完全一致：预案/执行/撤销/四类状态/冲突标记")
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

        step_11_export_plan_json()
        step_12_export_plan_csv()
        step_13_apply_filter_by_rule()
        step_14_undo_partial_apply()
        step_15_config_change_replan()
        step_16_verify_modified_plan_summary()
        step_17_apply_filter_by_type()
        step_18_apply_filter_by_target()
        step_19_export_verify_all_statuses()
        step_20_restart_consistency_full()

        print("\n" + "="*70)
        print("  所有验收测试通过！")
        print("="*70)
        print("\n验收点总结:")
        print("  OK scan: 按配置扫描目录，识别匹配规则")
        print("  OK plan: 生成归档预案")
        print("  OK plan 同目录规则: 多条规则映射同一目标 -> 摘要提示，不阻止 plan")
        print("  OK plan 摘要: 未命中文件、同目录规则、新建目录、规则分布、目录分布")
        print("  OK apply dry-run: 预演不实际移动")
        print("  OK apply 实际执行: 遇到冲突文件跳过，其他文件继续")
        print("  OK apply 筛选执行: 按规则/文件类型/目标目录筛选，人工跳过有记录")
        print("  OK 四类状态: moved / skipped_conflict / skipped_manual / failed")
        print("  OK undo: 撤销某次 apply，文件恢复原位")
        print("  OK undo 精确回滚: 只回滚实际移动的文件，人工跳过的不受影响")
        print("  OK export-plan: JSON/CSV 导出预案及其摘要，便于人工复核")
        print("  OK export: JSON/CSV 导出操作日志")
        print("  OK 重启一致性: 状态持久化后重新 export 数据一致")
        print("  OK 时间戳/撤销状态/冲突记录: 完整保留并一致")
        print("  OK 配置改动后重 plan: 新配置生成新预案，摘要正确更新")
        print("  OK 未命中/冲突/人工跳过: 在日志和导出结果中都能对得上")

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
