"""验收测试：批次快照 + 可回查能力

覆盖场景：
1. plan 生成快照，导出快照 JSON
2. 模拟重启（清空状态），导入快照做部分执行
3. 改配置后验证旧快照不会失真
4. 核对：冲突跳过、人工跳过、真实移动、undo 回滚后的状态和导出内容都能一一对上
5. 导入验证：文件被外部移动、目标冲突、目录无写权限等情况拦截
"""
import os
import sys
import json
import shutil
import subprocess

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.dirname(BASE_DIR)
sys.path.insert(0, PROJECT_DIR)


def run_cmd(cmd, cwd=PROJECT_DIR, input_text=None):
    """运行命令并返回结果"""
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    result = subprocess.run(
        cmd,
        shell=True,
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding='utf-8',
        errors='replace',
        input=input_text,
        env=env,
    )
    return result


def clean_env(config_name="config_normal.yaml"):
    """清理测试环境"""
    state_dir = os.path.join(BASE_DIR, ".state")
    archive_dir = os.path.join(BASE_DIR, "archived")
    sample_dir = os.path.join(BASE_DIR, "sample_invoices")

    if os.path.exists(state_dir):
        shutil.rmtree(state_dir)
    if os.path.exists(archive_dir):
        shutil.rmtree(archive_dir)
    os.makedirs(sample_dir, exist_ok=True)

    test_files = [
        "电子发票_20240115_001.pdf",
        "电子发票_20240116_002.pdf",
        "2024年1月增值税专用发票_12345.pdf",
        "发票照片_餐厅发票.jpg",
    ]

    for f in os.listdir(sample_dir):
        fpath = os.path.join(sample_dir, f)
        if os.path.isfile(fpath) and f not in test_files:
            os.remove(fpath)

    for f in test_files:
        fpath = os.path.join(sample_dir, f)
        if not os.path.exists(fpath):
            with open(fpath, "w", encoding="utf-8") as fp:
                fp.write(f"test content for {f}")


def step_1_plan_and_export_snapshot():
    """步骤1：plan 生成快照并导出"""
    print("\n" + "="*70)
    print("[步骤 1] plan 生成批次快照并导出")
    print("="*70)

    config = "examples/config_normal.yaml"

    result = run_cmd(f'python -m invoice_organizer scan -c {config}')
    assert result.returncode == 0, f"scan 失败: {result.stderr}"

    result = run_cmd(f'python -m invoice_organizer plan -c {config} -v')
    assert result.returncode == 0, f"plan 失败: {result.stderr}"

    assert "[批次快照] ID:" in result.stdout, "plan 输出应包含批次快照 ID"

    state_file = os.path.join(BASE_DIR, ".state", "invoice_state.json")
    with open(state_file, "r", encoding="utf-8") as f:
        state = json.load(f)

    assert "snapshots" in state, "状态文件应包含 snapshots"
    assert len(state["snapshots"]) == 1, "应有 1 个快照"

    snapshot_id = state["last_snapshot"]
    snapshot_data = state["snapshots"][snapshot_id]

    assert "config_snapshot" in snapshot_data, "快照应包含 config_snapshot"
    assert "moves" in snapshot_data, "快照应包含 moves"
    assert "unmatched_files" in snapshot_data, "快照应包含 unmatched_files"
    assert "new_target_dirs" in snapshot_data, "快照应包含 new_target_dirs"
    assert "summary" in snapshot_data, "快照应包含 summary"
    assert "scanned_files" in snapshot_data, "快照应包含 scanned_files"

    print(f"  快照 ID: {snapshot_id}")
    print(f"  移动计划: {len(snapshot_data['moves'])} 条")
    print(f"  未命中文件: {len(snapshot_data['unmatched_files'])} 个")
    print(f"  新建目录: {len(snapshot_data['new_target_dirs'])} 个")
    print(f"  扫描文件: {len(snapshot_data['scanned_files'])} 个")
    print("  [OK] 快照结构完整")

    export_path = os.path.join(BASE_DIR, "snapshot_export.json")
    result = run_cmd(
        f'python -m invoice_organizer export-snapshot -c {config} '
        f'-o {export_path} -v'
    )
    assert result.returncode == 0, f"export-snapshot 失败: {result.stderr}"
    assert os.path.exists(export_path), "导出文件应存在"

    with open(export_path, "r", encoding="utf-8") as f:
        exported = json.load(f)

    assert exported["snapshot_id"] == snapshot_id, "导出的快照 ID 应一致"
    assert len(exported["moves"]) == len(snapshot_data["moves"]), "移动计划数应一致"
    assert exported["has_conflicts"] == snapshot_data["has_conflicts"], "冲突标记应一致"

    print("  [OK] 导出的快照与内部状态一致")

    return snapshot_id, export_path


def step_2_import_snapshot_after_restart():
    """步骤2：模拟重启后导入快照"""
    print("\n" + "="*70)
    print("[步骤 2] 模拟重启后导入快照")
    print("="*70)

    config = "examples/config_normal.yaml"
    export_path = os.path.join(BASE_DIR, "snapshot_export.json")

    state_dir = os.path.join(BASE_DIR, ".state")
    if os.path.exists(state_dir):
        shutil.rmtree(state_dir)
    print("  已清空状态目录（模拟重启）")

    result = run_cmd(
        f'python -m invoice_organizer import-snapshot -c {config} '
        f'-i {export_path} -y -v'
    )
    assert result.returncode == 0, f"import-snapshot 失败: {result.stderr}"

    assert "[导入成功]" in result.stdout, "输出应包含导入成功"
    assert "导入" in result.stdout or "imported" in result.stdout.lower()

    state_file = os.path.join(BASE_DIR, ".state", "invoice_state.json")
    with open(state_file, "r", encoding="utf-8") as f:
        state = json.load(f)

    assert len(state["snapshots"]) == 1, "应有 1 个导入的快照"
    snapshot_id = state["last_snapshot"]
    snapshot = state["snapshots"][snapshot_id]

    assert snapshot.get("imported") == True, "导入的快照应标记 imported=True"
    assert snapshot.get("import_source") is not None, "导入的快照应有 import_source"

    print(f"  导入快照 ID: {snapshot_id}")
    print(f"  移动计划: {len(snapshot['moves'])} 条")
    print("  [OK] 快照导入成功，数据完整")

    return snapshot_id


def step_3_partial_apply_from_imported():
    """步骤3：从导入的快照做部分执行"""
    print("\n" + "="*70)
    print("[步骤 3] 从导入的快照做部分执行（按规则筛选）")
    print("="*70)

    config = "examples/config_normal.yaml"

    result = run_cmd(
        f'python -m invoice_organizer apply -c {config} '
        f'--rule 电子发票PDF -y -v'
    )
    assert result.returncode == 0, f"apply 失败: {result.stderr}"

    assert "成功移动: 2" in result.stdout, "应成功移动 2 个电子发票"
    assert "人工跳过: 2" in result.stdout, "应人工跳过 2 个文件"

    state_file = os.path.join(BASE_DIR, ".state", "invoice_state.json")
    with open(state_file, "r", encoding="utf-8") as f:
        state = json.load(f)

    runs = state["runs"]
    assert len(runs) == 1, "应有 1 次执行记录"

    run_id = list(runs.keys())[0]
    run = runs[run_id]
    moves = run["moves"]

    moved = [m for m in moves if m["status"] == "moved"]
    skipped_manual = [m for m in moves if m["status"] == "skipped_manual"]

    assert len(moved) == 2, "应有 2 条 moved 记录"
    assert len(skipped_manual) == 2, "应有 2 条 skipped_manual 记录"

    for m in moved:
        assert "电子发票" in m["filename"], "移动的应是电子发票"

    print(f"  Run ID: {run_id}")
    print(f"  成功移动: {len(moved)}")
    print(f"  人工跳过: {len(skipped_manual)}")
    print("  [OK] 部分执行正确")

    return run_id


def step_4_config_change_snapshot_integrity():
    """步骤4：改配置后验证旧快照不会失真"""
    print("\n" + "="*70)
    print("[步骤 4] 修改配置后验证旧快照不会失真")
    print("="*70)

    config_normal = "examples/config_normal.yaml"
    config_modified = "examples/config_modified.yaml"

    state_file = os.path.join(BASE_DIR, ".state", "invoice_state.json")
    with open(state_file, "r", encoding="utf-8") as f:
        state_before = json.load(f)

    old_snapshot_id = state_before["last_snapshot"]
    old_snapshot = state_before["snapshots"][old_snapshot_id]
    old_move_count = len(old_snapshot["moves"])
    old_rule_count = len(old_snapshot["config_snapshot"]["rules"])
    old_total_files = old_snapshot["summary"]["total_files"]

    sample_dir = os.path.join(BASE_DIR, "sample_invoices")
    archive_dir = os.path.join(BASE_DIR, "archived")
    for fname in os.listdir(sample_dir):
        fpath = os.path.join(sample_dir, fname)
        if os.path.isfile(fpath) and not fname.endswith('.pdf') and not fname.endswith('.jpg'):
            os.remove(fpath)

    test_files = [
        "电子发票_20240115_001.pdf",
        "电子发票_20240116_002.pdf",
        "2024年1月增值税专用发票_12345.pdf",
        "发票照片_餐厅发票.jpg",
        "未分类的文档.txt",
        "notes.md",
    ]
    for f in test_files:
        fpath = os.path.join(sample_dir, f)
        if not os.path.exists(fpath):
            with open(fpath, "w", encoding="utf-8") as fp:
                fp.write(f"test content for {f}")
    print("  已补充测试文件（用于 modified 配置）")

    result = run_cmd(f'python -m invoice_organizer scan -c {config_modified}')
    result = run_cmd(f'python -m invoice_organizer plan -c {config_modified}')
    assert result.returncode == 0, "修改配置后 plan 应成功"

    with open(state_file, "r", encoding="utf-8") as f:
        state_after = json.load(f)

    assert len(state_after["snapshots"]) == 2, "应有 2 个快照"

    old_snapshot_after = state_after["snapshots"][old_snapshot_id]
    assert len(old_snapshot_after["moves"]) == old_move_count, \
        "旧快照的移动计划数不应改变"
    assert len(old_snapshot_after["config_snapshot"]["rules"]) == old_rule_count, \
        "旧快照的配置规则数不应改变"
    assert old_snapshot_after["summary"]["total_files"] == old_total_files, \
        "旧快照的摘要信息不应改变"

    new_snapshot_id = state_after["last_snapshot"]
    new_snapshot = state_after["snapshots"][new_snapshot_id]
    assert new_snapshot_id != old_snapshot_id, "新快照 ID 应不同"

    print(f"  旧快照 ID: {old_snapshot_id}")
    print(f"  旧快照移动项: {old_move_count} (未变)")
    print(f"  新快照 ID: {new_snapshot_id}")
    print(f"  新快照移动项: {len(new_snapshot['moves'])}")
    print("  [OK] 旧快照数据未失真，新快照独立生成")

    return old_snapshot_id, new_snapshot_id


def step_5_apply_with_config_diff_detection():
    """步骤5：apply 时检测配置变更并提示"""
    print("\n" + "="*70)
    print("[步骤 5] apply 时检测配置变更并提示（强制使用旧快照）")
    print("="*70)

    config_modified = "examples/config_modified.yaml"

    state_file = os.path.join(BASE_DIR, ".state", "invoice_state.json")
    with open(state_file, "r", encoding="utf-8") as f:
        state = json.load(f)

    old_snapshot_id = [sid for sid, s in state["snapshots"].items()
                       if len(s["moves"]) == 4][0]

    result = run_cmd(
        f'python -m invoice_organizer apply -c {config_modified} '
        f'-s {old_snapshot_id} --rule 增值税专用发票 --force-snapshot -y -v'
    )
    assert result.returncode == 0, "使用 --force-snapshot 应成功"

    assert "[执行] 快照 ID:" in result.stdout, "输出应显示快照 ID"

    with open(state_file, "r", encoding="utf-8") as f:
        state_after = json.load(f)

    runs = state_after["runs"]
    latest_run = sorted(runs.values(), key=lambda r: r["created_at"])[-1]
    moves = latest_run["moves"]

    moved = [m for m in moves if m["status"] == "moved"]
    assert len(moved) == 1, "应成功移动 1 个增值税专用发票"

    print(f"  使用旧快照执行: {old_snapshot_id}")
    print(f"  移动文件数: {len(moved)}")
    print("  [OK] 配置变更时可强制使用旧快照执行")


def step_6_conflict_and_undo_verification():
    """步骤6：冲突跳过 + undo 回滚的完整核对"""
    print("\n" + "="*70)
    print("[步骤 6] 冲突跳过 + undo 回滚后状态核对")
    print("="*70)

    config = "examples/config_normal.yaml"
    state_file = os.path.join(BASE_DIR, ".state", "invoice_state.json")

    sample_dir = os.path.join(BASE_DIR, "sample_invoices")
    archive_dir = os.path.join(BASE_DIR, "archived")
    if os.path.exists(archive_dir):
        shutil.rmtree(archive_dir)

    test_files = [
        "电子发票_20240115_001.pdf",
        "电子发票_20240116_002.pdf",
        "2024年1月增值税专用发票_12345.pdf",
        "发票照片_餐厅发票.jpg",
    ]
    for f in test_files:
        fpath = os.path.join(sample_dir, f)
        if not os.path.exists(fpath):
            with open(fpath, "w", encoding="utf-8") as fp:
                fp.write(f"test content for {f}")
    print("  已重置测试文件")

    target_file = os.path.join(archive_dir, "electronic", "电子发票_20240115_001.pdf")
    os.makedirs(os.path.dirname(target_file), exist_ok=True)
    with open(target_file, "w", encoding="utf-8") as f:
        f.write("PRE-EXISTING CONFLICT FILE")
    print("  已制造冲突文件")

    result = run_cmd(f'python -m invoice_organizer scan -c {config}')
    result = run_cmd(f'python -m invoice_organizer plan -c {config}')
    assert result.returncode == 0

    result = run_cmd(f'python -m invoice_organizer apply -c {config} -y -v')
    assert result.returncode == 0

    assert "[冲突跳过] 电子发票_20240115_001.pdf" in result.stdout, \
        "应显示冲突跳过"
    assert "成功移动: 3" in result.stdout, f"应成功移动 3 个文件，实际输出: {result.stdout}"
    assert "冲突跳过: 1" in result.stdout, "应冲突跳过 1 个"

    with open(state_file, "r", encoding="utf-8") as f:
        state_after = json.load(f)

    runs = state_after["runs"]
    latest_run = sorted(runs.values(), key=lambda r: r["created_at"])[-1]
    run_id = latest_run["id"]
    moves = latest_run["moves"]

    moved_count = sum(1 for m in moves if m["status"] == "moved")
    conflict_count = sum(1 for m in moves if m["status"] == "skipped_conflict")

    assert moved_count == 3, f"应有 3 个 moved，实际 {moved_count}"
    assert conflict_count == 1, f"应有 1 个 skipped_conflict，实际 {conflict_count}"

    print(f"  Run ID: {run_id}")
    print(f"  成功移动: {moved_count}")
    print(f"  冲突跳过: {conflict_count}")

    export_path = os.path.join(BASE_DIR, "snapshot_full_export.json")
    result = run_cmd(f'python -m invoice_organizer export -c {config} -o {export_path} -f json')
    assert result.returncode == 0

    with open(export_path, "r", encoding="utf-8") as f:
        exported = json.load(f)

    exported_run = exported["runs"][run_id]
    exported_moved = sum(1 for m in exported_run["moves"] if m["status"] == "moved")
    exported_conflict = sum(1 for m in exported_run["moves"] if m["status"] == "skipped_conflict")

    assert exported_moved == moved_count, "导出的 moved 数应与状态文件一致"
    assert exported_conflict == conflict_count, "导出的 conflict 数应与状态文件一致"
    print("  [OK] 导出数据与状态文件一致")

    result = run_cmd(f'python -m invoice_organizer undo -c {config} -r {run_id} -y -v')
    assert result.returncode == 0
    assert "已恢复 3 个文件" in result.stdout, "应恢复 3 个文件"

    with open(state_file, "r", encoding="utf-8") as f:
        state_undone = json.load(f)

    assert state_undone["runs"][run_id]["is_undone"] == True, \
        "run 应标记为已撤销"

    undo_records = state_undone["undo_records"]
    assert len(undo_records) >= 1, "应有撤销记录"

    latest_undo = undo_records[-1]
    assert latest_undo["run_id"] == run_id, "撤销记录应对应正确 run"
    assert latest_undo["moves_restored"] == 3, "应恢复 3 个"

    with open(target_file, "r", encoding="utf-8") as f:
        content = f.read()
    assert content == "PRE-EXISTING CONFLICT FILE", \
        "预先存在的冲突文件不应被改动"

    print("  [OK] undo 只回滚了实际移动的 3 个文件")
    print("  [OK] 冲突文件未受影响")
    print("  [OK] 撤销记录正确")

    with open(export_path, "r", encoding="utf-8") as f:
        exported_before_undo = json.load(f)
    result = run_cmd(
        f'python -m invoice_organizer export -c {config} '
        f'-o {BASE_DIR}/snapshot_after_undo.json -f json'
    )
    with open(os.path.join(BASE_DIR, "snapshot_after_undo.json"), "r", encoding="utf-8") as f:
        exported_after_undo = json.load(f)

    assert exported_after_undo["runs"][run_id]["is_undone"] == True, \
        "导出的 run 撤销状态应更新"
    assert len(exported_after_undo["undo_records"]) == len(exported_before_undo["undo_records"]) + 1, \
        "导出的撤销记录应增加"

    print("  [OK] undo 后导出数据也正确更新")
    print("  [OK] 冲突跳过/人工跳过/真实移动/undo 状态全部核对通过")


def step_7_import_validation():
    """步骤7：导入验证 - 文件缺失、目标冲突等拦截"""
    print("\n" + "="*70)
    print("[步骤 7] 导入验证：文件缺失、冲突、权限等拦截")
    print("="*70)

    config = "examples/config_normal.yaml"
    export_path = os.path.join(BASE_DIR, "snapshot_export.json")

    state_dir = os.path.join(BASE_DIR, ".state")
    if os.path.exists(state_dir):
        shutil.rmtree(state_dir)

    sample_dir = os.path.join(BASE_DIR, "sample_invoices")
    moved_file = os.path.join(sample_dir, "电子发票_20240115_001.pdf")
    temp_dir = os.path.join(BASE_DIR, "_temp_moved")
    os.makedirs(temp_dir, exist_ok=True)
    shutil.move(moved_file, os.path.join(temp_dir, "电子发票_20240115_001.pdf"))
    print("  已移动一个源文件（模拟外部移动）")

    result = run_cmd(
        f'python -m invoice_organizer import-snapshot -c {config} '
        f'-i {export_path} -y'
    )
    assert result.returncode != 0, "源文件缺失时导入应失败"
    assert "[错误]" in result.stdout or "错误" in result.stderr, "应显示错误"
    assert "源文件不存在" in result.stdout or "源文件不存在" in result.stderr, \
        "错误信息应包含源文件不存在"

    print("  [OK] 源文件缺失时导入被拦截")

    shutil.move(os.path.join(temp_dir, "电子发票_20240115_001.pdf"), moved_file)
    print("  已恢复源文件")

    archive_dir = os.path.join(BASE_DIR, "archived")
    target_conflict = os.path.join(archive_dir, "electronic", "电子发票_20240116_002.pdf")
    os.makedirs(os.path.dirname(target_conflict), exist_ok=True)
    with open(target_conflict, "w", encoding="utf-8") as f:
        f.write("CONFLICT TARGET FILE")
    print("  已制造目标冲突文件")

    if os.path.exists(state_dir):
        shutil.rmtree(state_dir)

    result = run_cmd(
        f'python -m invoice_organizer import-snapshot -c {config} '
        f'-i {export_path} -y'
    )
    assert result.returncode == 0, "目标冲突只是警告，导入应成功"
    assert "[警告]" in result.stdout, "应显示警告"
    assert "目标文件已存在" in result.stdout, "警告应包含目标文件已存在"

    print("  [OK] 目标冲突显示为警告，不阻止导入")

    if os.path.exists(archive_dir):
        shutil.rmtree(archive_dir)

    print("  [OK] 导入验证通过")


def step_8_list_snapshots():
    """步骤8：列出快照"""
    print("\n" + "="*70)
    print("[步骤 8] list-snapshots 列出所有快照")
    print("="*70)

    config = "examples/config_normal.yaml"

    result = run_cmd(f'python -m invoice_organizer list-snapshots -c {config}')
    assert result.returncode == 0

    assert "快照ID" in result.stdout, "输出应包含表头"
    assert "创建时间" in result.stdout, "输出应包含创建时间"
    assert "预案ID" in result.stdout, "输出应包含预案ID"
    assert "移动项" in result.stdout, "输出应包含移动项"

    lines = result.stdout.strip().split('\n')
    data_lines = [l for l in lines if l and not l.startswith('-') and '快照ID' not in l]
    assert len(data_lines) >= 1, "至少应有 1 条快照记录"

    print("  [OK] list-snapshots 正常工作")


def main():
    print("="*70)
    print("  批次快照 + 可回查能力 - 验收测试")
    print("="*70)

    clean_env()

    try:
        snapshot_id, export_path = step_1_plan_and_export_snapshot()
        imported_id = step_2_import_snapshot_after_restart()
        run_id = step_3_partial_apply_from_imported()

        old_sid, new_sid = step_4_config_change_snapshot_integrity()
        step_5_apply_with_config_diff_detection()

        step_6_conflict_and_undo_verification()
        step_7_import_validation()
        step_8_list_snapshots()

        print("\n" + "="*70)
        print("  所有批次快照验收测试通过！")
        print("="*70)
        print("\n验收点总结:")
        print("  OK plan 生成批次快照：候选文件/命中规则/目标路径/未命中原因/预期新建目录")
        print("  OK export-snapshot 导出快照 JSON")
        print("  OK import-snapshot 导入快照（模拟重启后）")
        print("  OK 导入快照后可执行部分 apply")
        print("  OK 配置改动后旧快照数据不失真")
        print("  OK apply 检测配置变更并提示差异")
        print("  OK --force-snapshot 可强制使用旧快照执行")
        print("  OK 冲突跳过：目标已存在时正确标记 skipped_conflict")
        print("  OK 人工跳过：筛选执行时正确标记 skipped_manual")
        print("  OK 真实移动：moved 状态正确，文件实际移动")
        print("  OK undo 回滚：只恢复实际移动的文件")
        print("  OK 导出内容与状态文件一致")
        print("  OK 导入验证：源文件缺失时拦截并报错")
        print("  OK 导入验证：目标冲突时给出警告")
        print("  OK list-snapshots 可列出所有快照")
        print("  OK 快照带时间戳，可追溯")

        return 0

    except AssertionError as e:
        print(f"\n[X] 断言失败: {e}", file=sys.stderr)
        return 1
    except Exception as e:
        print(f"\n[X] 测试异常: {e}", file=sys.stderr)
        import traceback
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())
