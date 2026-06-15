"""验收测试：批次备注 + 交接信息完整链路

验收流程：
1. 生成快照（带备注）
2. 补备注
3. 导出快照和完整导出
4. 重启（重新加载状态文件）后查看
5. 再导入一份改过备注的快照，验证冲突检测
6. 确认 CLI 输出、状态文件、JSON/CSV 导出和后续 undo 链路都能对上
"""
import os
import sys
import json
import shutil
import subprocess
import time


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(BASE_DIR)
sys.path.insert(0, PROJECT_ROOT)

TEST_WORKSPACE = os.path.join(BASE_DIR, "remark_test_workspace")
CONFIG_PATH = os.path.join(TEST_WORKSPACE, "config.yaml")
STATE_FILE = os.path.join(TEST_WORKSPACE, ".state", "invoice_state.json")
SOURCE_DIR = os.path.join(TEST_WORKSPACE, "source")
DEST_DIR = os.path.join(TEST_WORKSPACE, "dest")
OUTPUT_DIR = os.path.join(TEST_WORKSPACE, "outputs")


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
    if os.path.exists(TEST_WORKSPACE):
        shutil.rmtree(TEST_WORKSPACE)
        print(f"  删除: {TEST_WORKSPACE}")
    print()


def setup_test_environment():
    print("\n" + "="*70)
    print("[准备] 创建测试环境...")
    print("="*70)

    os.makedirs(SOURCE_DIR, exist_ok=True)
    os.makedirs(os.path.join(TEST_WORKSPACE, ".state"), exist_ok=True)
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    sample_files = [
        ("2024年1月增值税专用发票_A001.pdf", b"%PDF-1.4 test content"),
        ("2024年2月增值税普通发票_B002.pdf", b"%PDF-1.4 test content"),
        ("电子发票_20240115_001.pdf", b"%PDF-1.4 test content"),
        ("发票扫描件_出租车发票.png", b"\x89PNG\r\n\x1a\n test content"),
        ("发票照片_餐厅发票.jpg", b"\xff\xd8\xff\xe0 test content"),
        ("出差报销单_202401.xlsx", b"PK test content"),
        ("采购合同_供应商A.docx", b"PK test content"),
        ("notes.md", b"# Test Notes\n"),
        ("未分类的文档.txt", b"test document\n"),
    ]

    for fname, content in sample_files:
        fpath = os.path.join(SOURCE_DIR, fname)
        with open(fpath, "wb") as f:
            f.write(content)
        print(f"  创建: {fname}")

    config_content = f"""source_dir: {SOURCE_DIR}
dest_dir: {DEST_DIR}
state_file: {STATE_FILE}
recursive: true

rules:
  - name: 增值税专用发票
    pattern: "*增值税专用发票*.pdf"
    target: vat_special
    description: 增值税专用发票归档

  - name: 增值税普通发票
    pattern: "*增值税普通发票*.pdf"
    target: vat/normal
    description: 增值税普通发票归档

  - name: 电子发票PDF
    pattern: "*电子发票*.pdf"
    target: electronic
    description: 电子发票PDF归档

  - name: 图片发票
    pattern: "*发票*.{{png,jpg}}"
    target: images
    description: 图片格式发票归档

  - name: 报销单据
    pattern: "*报销*.xlsx"
    target: reimbursement
    description: 报销单据归档

  - name: 合同文档
    pattern: "*合同*.docx"
    target: contracts
    description: 合同文档归档
"""

    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        f.write(config_content)
    print(f"  创建配置: {CONFIG_PATH}")
    print()


def step_1_plan_with_remark():
    print("\n" + "="*70)
    print("[步骤 1] 生成快照并写入备注")
    print("="*70)

    result = run(
        f"python -m invoice_organizer plan -c {CONFIG_PATH} "
        f"--remark \"2024年1月发票批次，待财务审核\" "
        f"--tag 2024-01 --tag 财务审核 --tag 增值税 "
        f"--handler \"张三\" "
        f"--notes \"请在1月31日前完成审核，注意核对发票真伪。\" "
        f"--updated-by \"财务系统\" "
        f"-v"
    )
    assert result.returncode == 0, "plan 命令失败"
    assert "2024年1月发票批次，待财务审核" in result.stdout, "备注未在输出中显示"
    assert "张三" in result.stdout, "交接人未在输出中显示"
    assert "2024-01" in result.stdout, "标签未在输出中显示"

    with open(STATE_FILE, "r", encoding="utf-8") as f:
        state = json.load(f)

    snapshots = state.get("snapshots", {})
    assert len(snapshots) == 1, "应该有1个快照"

    snapshot_data = list(snapshots.values())[0]
    remark = snapshot_data.get("remark", {})
    assert remark["remark"] == "2024年1月发票批次，待财务审核", "备注内容错误"
    assert set(remark["tags"]) == {"2024-01", "财务审核", "增值税"}, "标签内容错误"
    assert remark["handler"] == "张三", "交接人错误"
    assert "1月31日前完成审核" in remark["notes"], "注意事项错误"
    assert remark["updated_by"] == "财务系统", "更新人错误"

    return snapshot_data["snapshot_id"], snapshot_data["plan_id"]


def step_2_update_remark(snapshot_id):
    print("\n" + "="*70)
    print("[步骤 2] 补充修改备注信息")
    print("="*70)

    result = run(
        f"python -m invoice_organizer update-snapshot -c {CONFIG_PATH} "
        f"-s {snapshot_id} "
        f"--remark \"2024年1月发票批次，已完成初审\" "
        f"--tag 已初审 --append-tags "
        f"--notes \"已完成初审，发现2张发票需要补充材料，详见附件。\" "
        f"--updated-by \"李四\" "
        f"-y -v"
    )
    assert result.returncode == 0, "update-snapshot 命令失败"
    assert "已完成初审" in result.stdout, "更新后的备注未显示"

    with open(STATE_FILE, "r", encoding="utf-8") as f:
        state = json.load(f)

    snapshot_data = state["snapshots"][snapshot_id]
    remark = snapshot_data["remark"]
    assert remark["remark"] == "2024年1月发票批次，已完成初审", "备注更新错误"
    assert set(remark["tags"]) == {"2024-01", "财务审核", "增值税", "已初审"}, "标签追加错误"
    assert "补充材料" in remark["notes"], "注意事项更新错误"
    assert remark["updated_by"] == "李四", "更新人错误"

    histories = state.get("remark_histories", [])
    assert len(histories) >= 1, "应该有备注修改历史"
    history = histories[-1]
    assert history["snapshot_id"] == snapshot_id, "历史记录快照ID错误"
    assert history["changed_by"] == "李四", "历史记录修改人错误"

    return remark


def step_3_export_snapshot_and_full(snapshot_id):
    print("\n" + "="*70)
    print("[步骤 3] 导出快照和完整日志")
    print("="*70)

    snapshot_json = os.path.join(OUTPUT_DIR, "snapshot_export.json")
    full_json = os.path.join(OUTPUT_DIR, "full_export.json")
    full_csv = os.path.join(OUTPUT_DIR, "full_export.csv")

    result = run(
        f"python -m invoice_organizer export-snapshot -c {CONFIG_PATH} "
        f"-s {snapshot_id} -o {snapshot_json} -v"
    )
    assert result.returncode == 0, "export-snapshot 命令失败"
    assert "2024年1月发票批次，已完成初审" in result.stdout, "导出时未显示备注"

    with open(snapshot_json, "r", encoding="utf-8") as f:
        export_data = json.load(f)
    remark = export_data["remark"]
    assert remark["remark"] == "2024年1月发票批次，已完成初审", "导出的JSON备注错误"
    assert set(remark["tags"]) == {"2024-01", "财务审核", "增值税", "已初审"}, "导出的JSON标签错误"
    assert remark["handler"] == "张三", "导出的JSON交接人错误"

    result = run(
        f"python -m invoice_organizer export -c {CONFIG_PATH} "
        f"-o {full_json} -f json -v"
    )
    assert result.returncode == 0, "export JSON 命令失败"

    with open(full_json, "r", encoding="utf-8") as f:
        full_state = json.load(f)
    snapshot_data = full_state["snapshots"][snapshot_id]
    assert snapshot_data["remark"]["remark"] == "2024年1月发票批次，已完成初审", "完整导出JSON备注错误"
    assert "remark_histories" in full_state, "完整导出应包含备注历史"

    result = run(
        f"python -m invoice_organizer export -c {CONFIG_PATH} "
        f"-o {full_csv} -f csv"
    )
    assert result.returncode == 0, "export CSV 命令失败"

    with open(full_csv, "r", encoding="utf-8-sig") as f:
        csv_content = f.read()
    assert "2024年1月发票批次，已完成初审" in csv_content, "CSV导出应包含备注"
    assert "张三" in csv_content, "CSV导出应包含交接人"
    assert "批次快照" in csv_content, "CSV导出应包含批次快照章节"
    assert "备注修改历史" in csv_content, "CSV导出应包含备注修改历史章节"

    return snapshot_json


def step_4_restart_and_check():
    print("\n" + "="*70)
    print("[步骤 4] 模拟重启，验证备注信息持久化")
    print("="*70)

    print("  重新加载状态文件...")
    with open(STATE_FILE, "r", encoding="utf-8") as f:
        state_before = json.load(f)
    snapshot_before = list(state_before["snapshots"].values())[0]
    remark_before = snapshot_before["remark"]

    result = run(
        f"python -m invoice_organizer list-snapshots -c {CONFIG_PATH} -v"
    )
    assert result.returncode == 0, "list-snapshots 命令失败"
    assert "2024年1月发票批次，已完成初审" in result.stdout, "重启后list-snapshots应显示备注"
    assert "张三" in result.stdout, "重启后list-snapshots应显示交接人"
    assert "已初审" in result.stdout, "list-snapshots -v 应完整展示末尾标签（不截断）"

    with open(STATE_FILE, "r", encoding="utf-8") as f:
        state_after = json.load(f)
    snapshot_after = list(state_after["snapshots"].values())[0]
    remark_after = snapshot_after["remark"]

    assert remark_before == remark_after, "重启后备注信息不应改变"
    assert "已初审" in remark_after["tags"], "重启后标签应包含'已初审'"
    assert set(remark_after["tags"]) == {"2024-01", "财务审核", "增值税", "已初审"}, "重启后标签应完整"

    print("  [OK] 重启后备注信息完整保留")


def step_5_import_modified_remark(snapshot_json, original_snapshot_id):
    print("\n" + "="*70)
    print("[步骤 5] 导入修改过备注的快照，验证冲突检测")
    print("="*70)

    with open(snapshot_json, "r", encoding="utf-8") as f:
        data = json.load(f)

    data["remark"]["remark"] = "2024年1月发票批次，审核通过"
    data["remark"]["tags"] = ["2024-01", "财务审核", "已完成"]
    data["remark"]["handler"] = "王五"
    data["remark"]["notes"] = "审核通过，可进行下一步账务处理。"

    modified_snapshot = os.path.join(OUTPUT_DIR, "snapshot_modified.json")
    with open(modified_snapshot, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    print("\n[测试 5.1] 首次导入，应检测到备注冲突并拒绝")
    result = run(
        f"python -m invoice_organizer import-snapshot -c {CONFIG_PATH} "
        f"-i {modified_snapshot} -y"
    )
    assert result.returncode != 0, "检测到备注冲突时应失败"
    assert "备注内容冲突" in result.stdout, "应提示备注内容冲突"
    assert "交接人冲突" in result.stdout, "应提示交接人冲突"
    assert "标签冲突" in result.stdout, "应提示标签冲突"
    assert "--force" in result.stdout, "应提示使用--force参数"

    with open(STATE_FILE, "r", encoding="utf-8") as f:
        state = json.load(f)
    import_logs = state.get("import_logs", [])
    assert len(import_logs) >= 1, "应记录导入失败日志"
    last_log = import_logs[-1]
    assert last_log["status"] == "failed", "导入状态应为failed"

    remark_histories = state.get("remark_histories", [])
    conflict_history = [h for h in remark_histories if h.get("conflict_detected")]
    assert len(conflict_history) >= 1, "应记录备注冲突历史"

    print("\n[测试 5.2] 使用 --force 强制导入备注冲突")
    with open(STATE_FILE, "r", encoding="utf-8") as f:
        state_before_force = json.load(f)
    n_rh_before = len(state_before_force.get("remark_histories", []))
    n_il_before = len(state_before_force.get("import_logs", []))

    result = run(
        f"python -m invoice_organizer import-snapshot -c {CONFIG_PATH} "
        f"-i {modified_snapshot} --force -y -v"
    )
    assert result.returncode == 0, "使用--force应成功导入"
    assert "强制覆盖备注冲突" in result.stdout, "应提示已强制覆盖冲突"

    with open(STATE_FILE, "r", encoding="utf-8") as f:
        state = json.load(f)
    snapshot_data = state["snapshots"][original_snapshot_id]
    remark = snapshot_data["remark"]
    assert remark["remark"] == "2024年1月发票批次，审核通过", "强制导入后备注应更新"
    assert remark["handler"] == "王五", "强制导入后交接人应更新"

    n_rh_after = len(state.get("remark_histories", []))
    assert n_rh_after > n_rh_before, f"--force 后 remark_histories 应新增记录 ({n_rh_before} -> {n_rh_after})"
    forced_logs = [l for l in state.get("import_logs", [])[n_il_before:] if l.get("forced")]
    assert len(forced_logs) >= 1, "--force 后 import_logs 中应有 forced=true 的记录"
    last_log = state["import_logs"][-1]
    assert last_log["forced"] == True, "最后一条 import_log 应标记 forced=true"
    assert last_log["status"] == "forced", "最后一条 import_log 状态应为 forced"

    print("\n[测试 5.2b] 重启后复查 --force 导入的历史和 import log")
    result = run(
        f"python -m invoice_organizer export -c {CONFIG_PATH} "
        f"-o {os.path.join(OUTPUT_DIR, 'check_after_force.json')} -f json"
    )
    assert result.returncode == 0, "导出不应失败"
    with open(os.path.join(OUTPUT_DIR, "check_after_force.json"), "r", encoding="utf-8") as f:
        exported = json.load(f)
    exp_rh = exported.get("remark_histories", [])
    exp_il = exported.get("import_logs", [])
    assert len(exp_rh) >= 2, "导出文件应包含 remark_histories (至少冲突+强制两条)"
    forced_in_exp = [l for l in exp_il if l.get("forced")]
    assert len(forced_in_exp) >= 1, "导出文件的 import_logs 中应有 forced=true"
    exp_snap = list(exported.get("snapshots", {}).values())[0]
    assert exp_snap["remark"]["remark"] == "2024年1月发票批次，审核通过", "导出快照备注应与 --force 导入后一致"
    assert exp_snap["remark"]["handler"] == "王五", "导出快照交接人应与 --force 导入后一致"

    print("\n[测试 5.3] 使用 --remark-only 仅导入备注")
    data["remark"]["remark"] = "2024年1月发票批次，已归档"
    data["remark"]["handler"] = "赵六"

    modified_snapshot2 = os.path.join(OUTPUT_DIR, "snapshot_modified2.json")
    with open(modified_snapshot2, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    result = run(
        f"python -m invoice_organizer import-snapshot -c {CONFIG_PATH} "
        f"-i {modified_snapshot2} --remark-only --force -y -v"
    )
    assert result.returncode == 0, "--remark-only 应成功"
    assert "备注已更新" in result.stdout, "应提示备注已更新"

    with open(STATE_FILE, "r", encoding="utf-8") as f:
        state = json.load(f)
    snapshot_data = state["snapshots"][original_snapshot_id]
    remark = snapshot_data["remark"]
    assert remark["remark"] == "2024年1月发票批次，已归档", "--remark-only 备注应更新"
    assert remark["handler"] == "赵六", "--remark-only 交接人应更新"


def step_6_input_validation():
    print("\n" + "="*70)
    print("[步骤 6] 验证输入错误处理")
    print("="*70)

    print("\n[测试 6.1] 备注超长")
    long_remark = "A" * 600
    result = run(
        f"python -m invoice_organizer plan -c {CONFIG_PATH} "
        f"--remark \"{long_remark}\" "
        f"--handler \"测试\""
    )
    assert result.returncode != 0, "备注超长应失败"
    assert "备注内容过长" in result.stdout, "应提示备注过长"

    print("\n[测试 6.2] 标签重复")
    result = run(
        f"python -m invoice_organizer plan -c {CONFIG_PATH} "
        f"--tag 测试 --tag 测试 "
        f"--handler \"测试\""
    )
    assert result.returncode != 0, "标签重复应失败"
    assert "标签重复" in result.stdout, "应提示标签重复"

    print("\n[测试 6.3] 标签数量过多")
    tags_cmd = " ".join([f"--tag 标签{i}" for i in range(15)])
    result = run(
        f"python -m invoice_organizer plan -c {CONFIG_PATH} "
        f"{tags_cmd} "
        f"--handler \"测试\""
    )
    assert result.returncode != 0, "标签过多应失败"
    assert "标签数量过多" in result.stdout, "应提示标签数量过多"

    print("\n[测试 6.4] 交接人超长")
    long_handler = "A" * 60
    result = run(
        f"python -m invoice_organizer plan -c {CONFIG_PATH} "
        f"--handler \"{long_handler}\""
    )
    assert result.returncode != 0, "交接人超长应失败"
    assert "交接人过长" in result.stdout, "应提示交接人过长"


def step_7_undo_chain():
    print("\n" + "="*70)
    print("[步骤 7] 验证 undo 链路正常工作（备注不影响 undo）")
    print("="*70)

    result = run(
        f"python -m invoice_organizer apply -c {CONFIG_PATH} "
        f"--dry-run -y -v"
    )
    assert result.returncode == 0, "apply (dry-run) 命令失败"

    result = run(
        f"python -m invoice_organizer list-runs -c {CONFIG_PATH}"
    )
    assert result.returncode == 0, "list-runs 命令失败"

    result = run(
        f"python -m invoice_organizer undo -c {CONFIG_PATH} -y -v"
    )
    assert result.returncode == 0, "undo 命令失败"
    assert "撤销成功" in result.stdout, "undo 应成功"

    with open(STATE_FILE, "r", encoding="utf-8") as f:
        state = json.load(f)
    snapshot_data = list(state["snapshots"].values())[0]
    remark = snapshot_data["remark"]
    assert remark["remark"] == "2024年1月发票批次，已归档", "undo 不应影响备注信息"
    print("  [OK] undo 操作不影响备注信息")


def main():
    print("\n" + "="*70)
    print("批次备注 + 交接信息 完整链路验收测试")
    print("="*70)

    try:
        cleanup()
        setup_test_environment()

        snapshot_id, plan_id = step_1_plan_with_remark()
        step_2_update_remark(snapshot_id)
        snapshot_json = step_3_export_snapshot_and_full(snapshot_id)
        step_4_restart_and_check()
        step_5_import_modified_remark(snapshot_json, snapshot_id)
        step_6_input_validation()
        step_7_undo_chain()

        print("\n" + "="*70)
        print("[OK] 所有验收测试通过！")
        print("="*70)
        print("\n验证要点总结：")
        print("  [OK] plan 时写入备注、标签、交接人、注意事项")
        print("  [OK] update-snapshot 补充/修改备注，支持追加标签")
        print("  [OK] list-snapshots 展示备注信息")
        print("  [OK] export-snapshot JSON 导出包含备注")
        print("  [OK] export JSON/CSV 完整导出包含备注和历史")
        print("  [OK] 重启后备注信息持久化保存")
        print("  [OK] import-snapshot 检测备注冲突并拒绝覆盖")
        print("  [OK] --force 强制覆盖备注冲突并记录")
        print("  [OK] --remark-only 仅导入备注信息")
        print("  [OK] 输入验证：超长、重复标签、数量限制")
        print("  [OK] undo 链路正常，备注信息不受影响")
        print()

    except AssertionError as e:
        print(f"\n[FAIL] 验收测试失败: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
    except Exception as e:
        print(f"\n[ERROR] 发生未知错误: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
    finally:
        print("\n" + "="*70)
        print("[清理] 保留测试环境用于人工检查...")
        print(f"  工作目录: {TEST_WORKSPACE}")
        print("="*70)


if __name__ == "__main__":
    main()
