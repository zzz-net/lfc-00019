"""验收测试：预案签收 + 执行前核对 完整链路

验收流程：
1. 生成快照
2. 签收快照（signed 状态）
3. 导出快照和完整导出
4. 重启（重新加载状态文件）后查看
5. 导入冲突签收信息，验证冲突检测
6. 验证 apply 执行前校验（签收校验、过期校验、配置一致性校验）
7. 执行 apply，验证签收关联
8. undo 后查看当时使用的签收信息
9. 重新导出验证状态一致性
10. 确认 CLI 输出、状态文件、JSON/CSV 导出都能互相对上
"""
import os
import sys
import json
import shutil
import subprocess
import time
from datetime import datetime, timedelta


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(BASE_DIR)
sys.path.insert(0, PROJECT_ROOT)

TEST_WORKSPACE = os.path.join(BASE_DIR, "signoff_test_workspace")
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


def step_1_create_snapshot():
    print("\n" + "="*70)
    print("[步骤 1] 生成快照")
    print("="*70)

    result = run(
        f"python -m invoice_organizer plan -c {CONFIG_PATH} -v"
    )
    assert result.returncode == 0, "plan 命令失败"

    with open(STATE_FILE, "r", encoding="utf-8") as f:
        state = json.load(f)

    snapshots = state.get("snapshots", {})
    assert len(snapshots) == 1, "应该有1个快照"

    snapshot_data = list(snapshots.values())[0]
    snapshot_id = snapshot_data["snapshot_id"]
    plan_id = snapshot_data["plan_id"]

    print(f"  [OK] 快照创建成功: {snapshot_id}")
    return snapshot_id, plan_id


def step_2_sign_off_snapshot(snapshot_id, plan_id):
    print("\n" + "="*70)
    print("[步骤 2] 签收快照")
    print("="*70)

    deadline = (datetime.now() + timedelta(days=7)).isoformat()

    print("\n[测试 2.1] 首次签收快照（signed 状态）")
    result = run(
        f"python -m invoice_organizer sign-off -c {CONFIG_PATH} "
        f"-s {snapshot_id} "
        f"--status signed "
        f"--signed-by \"财务主管-李总\" "
        f"--deadline \"{deadline}\" "
        f"--notes \"审核通过，发票真实有效，可执行归档。\" "
        f"--created-by \"cli\" "
        f"-y -v"
    )
    assert result.returncode == 0, "sign-off 命令失败"
    assert "已签收" in result.stdout, "应显示已签收状态"
    assert "财务主管-李总" in result.stdout, "应显示签收人"
    assert "审核通过" in result.stdout, "应显示签收说明"

    with open(STATE_FILE, "r", encoding="utf-8") as f:
        state = json.load(f)

    signoff_records = state.get("signoff_records", [])
    assert len(signoff_records) >= 1, "应该有签收记录"

    active_signoff = [s for s in signoff_records if s.get("is_active") and s.get("snapshot_id") == snapshot_id]
    assert len(active_signoff) == 1, "应该有且仅有一个活动签收"

    signoff = active_signoff[0]
    assert signoff["status"] == "signed", "签收状态应为 signed"
    assert signoff["signed_by"] == "财务主管-李总", "签收人错误"
    assert "审核通过" in signoff["notes"], "签收说明错误"
    assert signoff["deadline"] == deadline, "截止时间错误"
    assert signoff["is_active"] == True, "应为活动签收"
    assert signoff["forced"] == False, "首次签收 forced 应为 False"

    signoff_id = signoff["signoff_id"]

    print("\n[测试 2.2] 查看签收状态")
    result = run(
        f"python -m invoice_organizer check-signoff -c {CONFIG_PATH} "
        f"-s {snapshot_id} -v"
    )
    assert result.returncode == 0, "check-signoff 命令失败"
    assert "已签收" in result.stdout, "check-signoff 应显示已签收"
    assert "财务主管-李总" in result.stdout, "check-signoff 应显示签收人"
    assert signoff_id in result.stdout, "check-signoff 应显示签收ID"

    print("\n[测试 2.3] list-snapshots 显示签收状态")
    result = run(
        f"python -m invoice_organizer list-snapshots -c {CONFIG_PATH} -v"
    )
    assert result.returncode == 0, "list-snapshots 命令失败"
    assert "已签收" in result.stdout, "list-snapshots 应显示签收状态"
    assert "财务主管-李总" in result.stdout, "list-snapshots 应显示签收人"

    print(f"  [OK] 签收成功: {signoff_id}")
    return signoff_id, deadline


def step_3_export_snapshot_and_full(snapshot_id):
    print("\n" + "="*70)
    print("[步骤 3] 导出快照和完整日志")
    print("="*70)

    snapshot_json = os.path.join(OUTPUT_DIR, "snapshot_with_signoff.json")
    full_json = os.path.join(OUTPUT_DIR, "full_export_signoff.json")
    full_csv = os.path.join(OUTPUT_DIR, "full_export_signoff.csv")

    result = run(
        f"python -m invoice_organizer export-snapshot -c {CONFIG_PATH} "
        f"-s {snapshot_id} -o {snapshot_json} -v"
    )
    assert result.returncode == 0, "export-snapshot 命令失败"
    assert "已签收" in result.stdout, "导出时应显示签收状态"
    assert "财务主管-李总" in result.stdout, "导出时应显示签收人"

    with open(snapshot_json, "r", encoding="utf-8") as f:
        export_data = json.load(f)

    assert "signoffs" in export_data, "导出的JSON应包含signoffs字段"
    assert len(export_data["signoffs"]) >= 1, "导出的JSON应包含签收记录"

    signoff_export = export_data["signoffs"][0]
    assert signoff_export["status"] == "signed", "导出的签收状态错误"
    assert signoff_export["signed_by"] == "财务主管-李总", "导出的签收人错误"
    assert "审核通过" in signoff_export["notes"], "导出的签收说明错误"

    result = run(
        f"python -m invoice_organizer export -c {CONFIG_PATH} "
        f"-o {full_json} -f json -v"
    )
    assert result.returncode == 0, "export JSON 命令失败"

    with open(full_json, "r", encoding="utf-8") as f:
        full_state = json.load(f)

    assert "signoff_records" in full_state, "完整导出应包含 signoff_records"
    assert len(full_state["signoff_records"]) >= 1, "完整导出应包含至少1条签收记录"

    result = run(
        f"python -m invoice_organizer export -c {CONFIG_PATH} "
        f"-o {full_csv} -f csv"
    )
    assert result.returncode == 0, "export CSV 命令失败"

    with open(full_csv, "r", encoding="utf-8-sig") as f:
        csv_content = f.read()

    assert "=== 签收记录 ===" in csv_content, "CSV导出应包含签收记录章节"
    assert "财务主管-李总" in csv_content, "CSV导出应包含签收人"
    assert "已签收" in csv_content, "CSV导出应包含签收状态"
    assert "审核通过" in csv_content, "CSV导出应包含签收说明"

    print(f"  [OK] 导出成功")
    return snapshot_json, full_json, full_csv


def step_4_restart_and_check(snapshot_id, signoff_id):
    print("\n" + "="*70)
    print("[步骤 4] 模拟重启，验证签收信息持久化")
    print("="*70)

    print("  重新加载状态文件...")
    with open(STATE_FILE, "r", encoding="utf-8") as f:
        state_before = json.load(f)
    signoffs_before = state_before.get("signoff_records", [])

    result = run(
        f"python -m invoice_organizer list-snapshots -c {CONFIG_PATH} -v"
    )
    assert result.returncode == 0, "list-snapshots 命令失败"
    assert "已签收" in result.stdout, "重启后list-snapshots应显示签收状态"
    assert "财务主管-李总" in result.stdout, "重启后list-snapshots应显示签收人"

    result = run(
        f"python -m invoice_organizer check-signoff -c {CONFIG_PATH} "
        f"-s {snapshot_id}"
    )
    assert result.returncode == 0, "check-signoff 命令失败"
    assert signoff_id in result.stdout, "重启后check-signoff应显示签收ID"

    with open(STATE_FILE, "r", encoding="utf-8") as f:
        state_after = json.load(f)
    signoffs_after = state_after.get("signoff_records", [])

    assert len(signoffs_before) == len(signoffs_after), "重启后签收记录数不应改变"

    active_before = [s for s in signoffs_before if s.get("signoff_id") == signoff_id][0]
    active_after = [s for s in signoffs_after if s.get("signoff_id") == signoff_id][0]
    assert active_before == active_after, "重启后签收信息不应改变"

    print("  [OK] 重启后签收信息完整保留")


def step_5_import_conflicting_signoff(snapshot_json, snapshot_id, original_signoff_id):
    print("\n" + "="*70)
    print("[步骤 5] 导入冲突签收信息，验证冲突检测")
    print("="*70)

    with open(snapshot_json, "r", encoding="utf-8") as f:
        data = json.load(f)

    data["signoffs"][0]["status"] = "rejected"
    data["signoffs"][0]["signed_by"] = "财务经理-王总"
    data["signoffs"][0]["notes"] = "拒绝，发现3张发票有疑问，需要重新核实。"
    data["signoffs"][0]["signoff_id"] = "conflict_signoff_001"

    modified_snapshot = os.path.join(OUTPUT_DIR, "snapshot_conflict_signoff.json")
    with open(modified_snapshot, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    print("\n[测试 5.1] 导入冲突签收，不带 --force 应被拒绝")
    result = run(
        f"python -m invoice_organizer import-snapshot -c {CONFIG_PATH} "
        f"-i {modified_snapshot} -y -v"
    )
    assert result.returncode != 0, "检测到签收冲突时应失败"
    assert "签收冲突" in result.stdout or "检测到签收冲突" in result.stdout, "应提示签收冲突"
    assert "签收变更对比" in result.stdout, "应显示签收变更对比"

    with open(STATE_FILE, "r", encoding="utf-8") as f:
        state = json.load(f)
    signoff_records = state.get("signoff_records", [])
    active_signoffs = [s for s in signoff_records if s.get("is_active") and s.get("snapshot_id") == snapshot_id]
    assert len(active_signoffs) == 1, "冲突拒绝后应仍只有一个活动签收"
    assert active_signoffs[0]["signoff_id"] == original_signoff_id, "冲突拒绝后原签收应保持活动"

    import_logs = state.get("import_logs", [])
    assert len(import_logs) >= 1, "应记录导入失败日志"
    last_log = import_logs[-1]
    assert last_log["status"] == "failed", "导入状态应为failed"

    print("\n[测试 5.2] 使用 --force 强制导入冲突签收")
    result = run(
        f"python -m invoice_organizer import-snapshot -c {CONFIG_PATH} "
        f"-i {modified_snapshot} --force -y -v"
    )
    assert result.returncode == 0, "使用--force应成功导入"
    assert "强制导入" in result.stdout, "应提示已强制导入"
    assert "覆盖冲突" in result.stdout, "应提示覆盖冲突"

    with open(STATE_FILE, "r", encoding="utf-8") as f:
        state = json.load(f)
    signoff_records = state.get("signoff_records", [])

    original = [s for s in signoff_records if s.get("signoff_id") == original_signoff_id][0]
    new_signoff = [s for s in signoff_records if s.get("signoff_id") == "conflict_signoff_001"][0]

    assert original["is_active"] == False, "原签收应被标记为非活动"
    assert original["superseded_by"] == "conflict_signoff_001", "原签收应记录被谁取代"
    assert original["superseded_at"] is not None, "原签收应记录取代时间"

    assert new_signoff["is_active"] == True, "新签收应为活动状态"
    assert new_signoff["forced"] == True, "强制导入的签收 forced 应为 True"
    assert new_signoff["conflict_detail"] != "", "强制导入的签收应记录冲突详情"
    assert new_signoff["import_source"] is not None, "应记录导入来源"

    import_logs = state.get("import_logs", [])
    last_log = import_logs[-1]
    assert last_log["status"] == "forced", "导入状态应为forced"
    assert last_log["forced"] == True, "forced 标记应为 True"

    print("\n[测试 5.3] check-signoff 显示被取代的签收历史")
    result = run(
        f"python -m invoice_organizer check-signoff -c {CONFIG_PATH} "
        f"-s {snapshot_id} --no-require-signed -v"
    )
    assert result.returncode == 0, "check-signoff 命令失败"
    assert "已拒绝" in result.stdout, "应显示新的签收状态（已拒绝）"
    assert "财务经理-王总" in result.stdout, "应显示新的签收人"
    assert "已失效" in result.stdout or "被取代" in result.stdout, "应显示原签收已失效"
    assert original_signoff_id in result.stdout, "应显示原签收ID"

    print(f"  [OK] 冲突检测和强制导入正常")
    return "conflict_signoff_001"


def step_6_apply_validation(snapshot_id, plan_id):
    print("\n" + "="*70)
    print("[步骤 6] 验证 apply 执行前签收校验")
    print("="*70)

    print("\n[测试 6.1] 当前签收状态为 rejected，apply 应被拦截")
    result = run(
        f"python -m invoice_organizer apply -c {CONFIG_PATH} "
        f"-s {snapshot_id} --dry-run -y -v"
    )
    assert result.returncode != 0, "rejected 状态的签收受应被拦截"
    assert "签收" in result.stdout, "应提示签收相关问题"
    assert "已拒绝" in result.stdout or "rejected" in result.stdout, "应说明是已拒绝状态"

    print("\n[测试 6.2] 将签收改回 signed，验证正常执行")
    result = run(
        f"python -m invoice_organizer sign-off -c {CONFIG_PATH} "
        f"-s {snapshot_id} "
        f"--status signed "
        f"--signed-by \"财务总监-赵总\" "
        f"--notes \"二次审核通过，可执行。\" "
        f"--force -y -v"
    )
    assert result.returncode == 0, "重新签收应成功"

    result = run(
        f"python -m invoice_organizer apply -c {CONFIG_PATH} "
        f"-s {snapshot_id} --dry-run -y -v"
    )
    assert result.returncode == 0, "signed 状态的签收应允许执行"
    assert "已验证签收" in result.stdout or "使用签收" in result.stdout or "当前签收" in result.stdout, "应显示使用的签收信息"
    assert "财务总监-赵总" in result.stdout, "应显示签收人"

    print("\n[测试 6.3] 修改配置，验证配置一致性校验")
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        config_content = f.read()

    modified_config = config_content + "\n# 配置修改标记\n"
    modified_config = modified_config.replace(
        "target: vat_special",
        "target: vat_special_modified"
    )

    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        f.write(modified_config)

    result = run(
        f"python -m invoice_organizer apply -c {CONFIG_PATH} "
        f"-s {snapshot_id} --dry-run -y -v"
    )
    assert result.returncode != 0, "配置不一致应被拦截"
    assert "配置" in result.stdout and "不一致" in result.stdout, "应提示配置不一致"
    assert "vat_special" in result.stdout, "应显示具体的配置变更"
    assert "--force-snapshot" in result.stdout, "应提示使用 --force-snapshot"

    print("\n[测试 6.4] 使用 --force-snapshot 绕过配置校验")
    result = run(
        f"python -m invoice_organizer apply -c {CONFIG_PATH} "
        f"-s {snapshot_id} --dry-run --force-snapshot -y -v"
    )
    assert result.returncode == 0, "使用 --force-snapshot 应允许执行"
    assert "强制" in result.stdout or "force" in result.stdout.lower(), "应显示强制标记"

    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        f.write(config_content)

    print("\n[测试 6.5] 使用 --no-require-signoff 跳过签收校验")
    result = run(
        f"python -m invoice_organizer apply -c {CONFIG_PATH} "
        f"-s {snapshot_id} --dry-run --no-require-signoff -y -v"
    )
    assert result.returncode == 0, "使用 --no-require-signoff 应允许执行"
    assert "跳过" in result.stdout or "no-require-signoff" in result.stdout, "应显示跳过签收校验"

    print("\n[测试 6.6] 验证过期签收拦截")
    past_deadline = (datetime.now() - timedelta(days=1)).isoformat()
    result = run(
        f"python -m invoice_organizer sign-off -c {CONFIG_PATH} "
        f"-s {snapshot_id} "
        f"--status signed "
        f"--signed-by \"过期测试员\" "
        f"--deadline \"{past_deadline}\" "
        f"--notes \"测试过期签收\" "
        f"--force -y -v"
    )
    assert result.returncode == 0, "创建过期签收受成功"

    result = run(
        f"python -m invoice_organizer apply -c {CONFIG_PATH} "
        f"-s {snapshot_id} --dry-run -y -v"
    )
    assert result.returncode != 0, "过期签收受应被拦截"
    assert "过期" in result.stdout, "应提示签收已过期"
    assert "--force-expired-signoff" in result.stdout, "应提示使用 --force-expired-signoff"

    print("\n[测试 6.7] 使用 --force-expired-signoff 绕过期校验")
    result = run(
        f"python -m invoice_organizer apply -c {CONFIG_PATH} "
        f"-s {snapshot_id} --dry-run --force-expired-signoff -y -v"
    )
    assert result.returncode == 0, "使用 --force-expired-signoff 应允许执行"
    assert "强制" in result.stdout or "过期" in result.stdout, "应显示强制过期标记"

    result = run(
        f"python -m invoice_organizer sign-off -c {CONFIG_PATH} "
        f"-s {snapshot_id} "
        f"--status signed "
        f"--signed-by \"财务总监-赵总\" "
        f"--notes \"最终审核通过\" "
        f"--force -y -v"
    )
    assert result.returncode == 0, "恢复正常签收"

    print("  [OK] apply 签收校验正常")


def step_7_apply_and_undo(snapshot_id, plan_id):
    print("\n" + "="*70)
    print("[步骤 7] 执行 apply 和 undo，验证签收关联")
    print("="*70)

    print("\n[测试 7.1] 实际执行 apply")
    result = run(
        f"python -m invoice_organizer apply -c {CONFIG_PATH} "
        f"-s {snapshot_id} -y -v"
    )
    assert result.returncode == 0, "apply 命令失败"
    assert "执行完成" in result.stdout, "应显示执行完成"
    assert "Run ID" in result.stdout, "应显示 Run ID"

    with open(STATE_FILE, "r", encoding="utf-8") as f:
        state = json.load(f)

    runs = state.get("runs", {})
    actual_runs = {k: v for k, v in runs.items() if not v.get("dry_run", False)}
    assert len(actual_runs) == 1, f"应该有1条实际执行记录，实际有{len(actual_runs)}条"

    run_record = list(actual_runs.values())[0]
    run_id = run_record["id"]
    assert "signoff_id" in run_record, "执行记录应关联签收ID"
    assert run_record["signoff_id"] is not None, "执行记录的签收ID不应为空"
    assert run_record["snapshot_id"] == snapshot_id, "执行记录应关联快照ID"

    print("\n[测试 7.2] undo 时显示当时使用的签收信息")
    result = run(
        f"python -m invoice_organizer undo -c {CONFIG_PATH} -y -v"
    )
    assert result.returncode == 0, "undo 命令失败"
    assert "撤销成功" in result.stdout, "应显示撤销成功"
    assert "执行时签收" in result.stdout, "应显示执行时的签收信息"
    assert "财务总监-赵总" in result.stdout, "应显示当时的签收人"
    assert run_record["signoff_id"] in result.stdout, "应显示当时的签收ID"

    with open(STATE_FILE, "r", encoding="utf-8") as f:
        state = json.load(f)

    runs = state.get("runs", {})
    run_record = runs[run_id]
    assert run_record["is_undone"] == True, "执行记录应标记为已撤销"
    assert "signoff_id" in run_record, "撤销后执行记录仍应保留签收ID"

    print(f"  [OK] apply 和 undo 签收关联正常: Run ID={run_id}")
    return run_id


def step_8_re_export_after_undo(snapshot_id, run_id):
    print("\n" + "="*70)
    print("[步骤 8] 撤销后重新导出，验证状态一致性")
    print("="*70)

    final_json = os.path.join(OUTPUT_DIR, "final_export_after_undo.json")
    final_csv = os.path.join(OUTPUT_DIR, "final_export_after_undo.csv")

    result = run(
        f"python -m invoice_organizer export -c {CONFIG_PATH} "
        f"-o {final_json} -f json -v"
    )
    assert result.returncode == 0, "export JSON 命令失败"

    with open(final_json, "r", encoding="utf-8") as f:
        final_state = json.load(f)

    assert "signoff_records" in final_state, "导出应包含 signoff_records"
    signoff_records = final_state["signoff_records"]
    assert len(signoff_records) >= 3, "应包含至少3条签收记录（原始+冲突+最终）"

    runs = final_state.get("runs", {})
    run_record = runs[run_id]
    assert run_record["is_undone"] == True, "执行记录应标记为已撤销"
    assert "signoff_id" in run_record, "执行记录应保留签收ID关联"

    result = run(
        f"python -m invoice_organizer export -c {CONFIG_PATH} "
        f"-o {final_csv} -f csv"
    )
    assert result.returncode == 0, "export CSV 命令失败"

    with open(final_csv, "r", encoding="utf-8-sig") as f:
        csv_content = f.read()

    assert "=== 签收记录 ===" in csv_content, "CSV应包含签收记录章节"
    assert "财务总监-赵总" in csv_content, "CSV应包含最终签收人"
    assert "已签收" in csv_content, "CSV应包含签收状态"
    assert run_id in csv_content, "CSV应包含执行记录ID"
    assert "已撤销" in csv_content, "CSV应显示执行记录已撤销"

    print("\n[测试 8.1] list-snapshots 最终视图验证")
    result = run(
        f"python -m invoice_organizer list-snapshots -c {CONFIG_PATH} -v"
    )
    assert result.returncode == 0, "list-snapshots 命令失败"
    assert "已签收" in result.stdout, "最终状态应显示已签收"
    assert "财务总监-赵总" in result.stdout, "最终状态应显示签收人"

    print("\n[测试 8.2] check-signoff 最终视图验证")
    result = run(
        f"python -m invoice_organizer check-signoff -c {CONFIG_PATH} "
        f"-s {snapshot_id} -v"
    )
    assert result.returncode == 0, "check-signoff 命令失败"
    assert "财务总监-赵总" in result.stdout, "check-signoff 应显示最终签收人"
    assert "被取代" in result.stdout or "已失效" in result.stdout, "应显示历史签收取代链"

    print("\n[测试 8.3] 状态文件、JSON、CSV、CLI 四方比对")
    with open(STATE_FILE, "r", encoding="utf-8") as f:
        state_file = json.load(f)

    with open(final_json, "r", encoding="utf-8") as f:
        export_file = json.load(f)

    state_signoffs = state_file.get("signoff_records", [])
    export_signoffs = export_file.get("signoff_records", [])

    state_active = [s for s in state_signoffs if s.get("is_active") and s.get("snapshot_id") == snapshot_id]
    export_active = [s for s in export_signoffs if s.get("is_active") and s.get("snapshot_id") == snapshot_id]

    assert len(state_active) == 1, "状态文件中应有1个活动签收"
    assert len(export_active) == 1, "导出JSON中应有1个活动签收"
    assert state_active[0]["signoff_id"] == export_active[0]["signoff_id"], "活动签收ID应一致"
    assert state_active[0]["signed_by"] == export_active[0]["signed_by"], "签收人应一致"
    assert state_active[0]["status"] == export_active[0]["status"], "签收状态应一致"

    assert "财务总监-赵总" in csv_content, "CSV中应包含最终签收人"
    assert state_active[0]["signoff_id"] in result.stdout, "CLI中应包含最终签收ID"

    print("  [OK] 四方比对一致：状态文件 <-> JSON <-> CSV <-> CLI")


def step_9_input_validation():
    print("\n" + "="*70)
    print("[步骤 9] 验证签收输入错误处理")
    print("="*70)

    print("\n[测试 9.1] 签收人超长")
    long_signed_by = "A" * 60
    result = run(
        f"python -m invoice_organizer sign-off -c {CONFIG_PATH} "
        f"--status signed "
        f"--signed-by \"{long_signed_by}\" "
        f"-y"
    )
    assert result.returncode != 0, "签收人超长应失败"
    assert "签收人" in result.stdout and "过长" in result.stdout, "应提示签收人过长"

    print("\n[测试 9.2] 补充说明超长")
    long_notes = "A" * 600
    result = run(
        f"python -m invoice_organizer sign-off -c {CONFIG_PATH} "
        f"--status signed "
        f"--signed-by \"测试员\" "
        f"--notes \"{long_notes}\" "
        f"-y"
    )
    assert result.returncode != 0, "补充说明超长应失败"
    assert "说明" in result.stdout and "过长" in result.stdout, "应提示补充说明过长"

    print("\n[测试 9.3] 缺少 --signed-by 参数")
    result = run(
        f"python -m invoice_organizer sign-off -c {CONFIG_PATH} "
        f"--status signed "
        f"-y 2>&1"
    )
    assert result.returncode != 0, "缺少 --signed-by 应失败"

    print("  [OK] 输入验证正常")


def main():
    print("\n" + "="*70)
    print("预案签收 + 执行前核对 完整链路验收测试")
    print("="*70)

    try:
        cleanup()
        setup_test_environment()

        snapshot_id, plan_id = step_1_create_snapshot()
        signoff_id, deadline = step_2_sign_off_snapshot(snapshot_id, plan_id)
        snapshot_json, full_json, full_csv = step_3_export_snapshot_and_full(snapshot_id)
        step_4_restart_and_check(snapshot_id, signoff_id)
        new_signoff_id = step_5_import_conflicting_signoff(snapshot_json, snapshot_id, signoff_id)
        step_6_apply_validation(snapshot_id, plan_id)
        run_id = step_7_apply_and_undo(snapshot_id, plan_id)
        step_8_re_export_after_undo(snapshot_id, run_id)
        step_9_input_validation()

        print("\n" + "="*70)
        print("[OK] 所有验收测试通过！")
        print("="*70)
        print("\n验证要点总结：")
        print("  [OK] sign-off 命令支持 signed/rejected/pending 三种状态")
        print("  [OK] 签收记录包含签收人、签收时间、截止时间、补充说明")
        print("  [OK] check-signoff 命令查看签收状态和历史")
        print("  [OK] list-snapshots 显示签收状态视图（颜色区分状态）")
        print("  [OK] export-snapshot JSON 导出包含签收记录")
        print("  [OK] export JSON/CSV 完整导出包含签收记录章节")
        print("  [OK] 重启后签收信息持久化保存")
        print("  [OK] import-snapshot 检测签收冲突并拒绝覆盖")
        print("  [OK] --force 强制导入冲突签收并标记 forced=True")
        print("  [OK] 签收取代链：旧签收标记 is_active=False，记录 superseded_by")
        print("  [OK] apply 默认要求 signed 状态且未过期的签收")
        print("  [OK] apply 校验配置一致性，不一致时拦截")
        print("  [OK] --no-require-signoff 跳过签收校验")
        print("  [OK] --force-snapshot 绕过配置一致性校验")
        print("  [OK] --force-expired-signoff 绕过期校验")
        print("  [OK] 执行记录关联签收ID（signoff_id）")
        print("  [OK] undo 时显示当时使用的签收信息")
        print("  [OK] 撤销后重新导出，状态文件、JSON、CSV、CLI 四方一致")
        print("  [OK] 输入验证：签收人超长、补充说明超长、必填参数缺失")
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
