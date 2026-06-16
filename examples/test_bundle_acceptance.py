"""验收测试：执行批次归档包（ExecutionBundle）完整链路

验收流程：
1. 准备工作空间（清空状态，拷贝示例发票）
2. 生成快照、签收（signed 状态）、筛选部分规则后 apply 执行
3. list-bundles 验证归档包存在、字段正确
4. export-bundle 导出为 JSON，确认包含 snapshot/signoffs/moves/conflict_details 等关键字段
5. 删除 .state/invoice_state.json → 调用 import-bundle 重新导入
6. 重新 list-bundles / 状态文件确认三处一致
7. 导出 JSON 和 CSV，核对内容
8. 构造三类冲突场景分别验证：
   - 重复导入同一 bundle（应报"同一批次重复导入"，退出码 1）
   - 篡改 bundle JSON 的 snapshot 内容（应报"快照版本对不上"）
   - 删去 bundle 的 run_details.moves[0].filename 字段（应报"日志缺字段"）
9. undo 执行 → 再次 list-bundles 验证 is_undone 字段为 true → 仍可成功 export-bundle 查看当时执行上下文
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

TEST_WORKSPACE = os.path.join(BASE_DIR, "bundle_test_workspace")
CONFIG_PATH = os.path.join(TEST_WORKSPACE, "config.yaml")
STATE_FILE = os.path.join(TEST_WORKSPACE, ".state", "invoice_state.json")
SOURCE_DIR = os.path.join(TEST_WORKSPACE, "source")
DEST_DIR = os.path.join(TEST_WORKSPACE, "dest")
OUTPUT_DIR = os.path.join(TEST_WORKSPACE, "outputs")


def run(cmd, expect_fail=False, capture_output_flag=False):
    print(f"\n{'='*70}")
    print(f"$ {cmd}")
    print('='*70)
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    result = subprocess.run(
        cmd, shell=True, cwd=PROJECT_ROOT, capture_output=True,
        text=True, encoding='utf-8', errors='replace', env=env
    )
    if result.stdout and not capture_output_flag:
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
    if expect_fail:
        assert result.returncode != 0, f"预期失败但命令成功！返回码: {result.returncode}"
    else:
        assert result.returncode == 0, f"命令执行失败！返回码: {result.returncode}"
    return result


def read_state():
    with open(STATE_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def prepare_workspace():
    print("\n" + "="*70)
    print("【步骤 1】准备测试工作空间")
    print("="*70)

    if os.path.exists(TEST_WORKSPACE):
        shutil.rmtree(TEST_WORKSPACE)
    os.makedirs(os.path.join(TEST_WORKSPACE, ".state"), exist_ok=True)
    os.makedirs(SOURCE_DIR, exist_ok=True)
    os.makedirs(DEST_DIR, exist_ok=True)
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    sample_dir = os.path.join(BASE_DIR, "sample_invoices")
    for fn in os.listdir(sample_dir):
        src = os.path.join(sample_dir, fn)
        if os.path.isfile(src):
            shutil.copy(src, os.path.join(SOURCE_DIR, fn))
    print(f"  已拷贝示例发票文件到: {SOURCE_DIR}")
    print(f"    文件列表: {sorted(os.listdir(SOURCE_DIR))}")

    config_content = (
        f"source_dir: {SOURCE_DIR}\n"
        f"dest_dir: {DEST_DIR}\n"
        f"state_file: {STATE_FILE}\n"
        f"recursive: true\n"
        f"\n"
        f"rules:\n"
        f"  - name: 增值税专用发票\n"
        f"    pattern: \"*增值税专用发票*.pdf\"\n"
        f"    target: vat_special\n"
        f"\n"
        f"  - name: 增值税普通发票\n"
        f"    pattern: \"*增值税普通发票*.pdf\"\n"
        f"    target: vat/normal\n"
        f"\n"
        f"  - name: 电子发票\n"
        f"    pattern: \"*电子发票*.pdf\"\n"
        f"    target: electronic\n"
        f"\n"
        f"  - name: 图片发票扫描件\n"
        f"    pattern: \"*发票*.png\"\n"
        f"    target: images\n"
        f"\n"
        f"  - name: 图片发票照片\n"
        f"    pattern: \"*发票*.jpg\"\n"
        f"    target: images/jpg\n"
        f"\n"
        f"  - name: 报销单据\n"
        f"    pattern: \"*报销*.xlsx\"\n"
        f"    target: reimbursement\n"
        f"\n"
        f"  - name: 合同文件\n"
        f"    pattern: \"*合同*.docx\"\n"
        f"    target: contracts\n"
        f"\n"
        f"  - name: 未分类文档\n"
        f"    pattern: \"*.md\"\n"
        f"    target: documents\n"
    )
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        f.write(config_content)
    print(f"  已生成配置文件: {CONFIG_PATH}")


def plan_signoff_and_apply():
    print("\n" + "="*70)
    print("【步骤 2】执行 plan → signoff → apply（带筛选）")
    print("="*70)

    run(f'python -m invoice_organizer.cli plan -c "{CONFIG_PATH}"')

    state = read_state()
    snapshots = state.get("snapshots", {})
    assert len(snapshots) >= 1, "plan 后应有快照"
    snap_data = list(snapshots.values())[0]
    snapshot_id = snap_data["snapshot_id"]
    plan_id = snap_data["plan_id"]
    print(f"  捕获快照 ID: {snapshot_id}")
    print(f"  捕获预案 ID: {plan_id}")

    deadline_str = (datetime.now() + timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%S")
    run(
        f'python -m invoice_organizer.cli sign-off -c "{CONFIG_PATH}" '
        f'-s {snapshot_id} '
        f'--signed-by "验收测试员" --deadline "{deadline_str}" '
        f'--notes "完整验收测试 - 批次归档包" -y'
    )

    run(
        f'python -m invoice_organizer.cli apply -c "{CONFIG_PATH}" '
        f'-s {snapshot_id} '
        f'--rule "增值税专用发票" --rule "增值税普通发票" --rule "电子发票" '
        f'--rule "图片发票扫描件" --rule "图片发票照片" --rule "报销单据" --rule "合同文件" '
        f'-y'
    )

    return snapshot_id, plan_id


def step3_verify_bundle_exists():
    print("\n" + "="*70)
    print("【步骤 3】验证归档包存在并 list-bundles 正确")
    print("="*70)

    run(f'python -m invoice_organizer.cli list-bundles -c "{CONFIG_PATH}"')
    run(f'python -m invoice_organizer.cli list-bundles -c "{CONFIG_PATH}" -v')

    result = run(
        f'python -m invoice_organizer.cli list-bundles -c "{CONFIG_PATH}" --json',
        capture_output_flag=True
    )
    bundles = json.loads(result.stdout.strip())
    assert len(bundles) == 1, f"应有 1 个归档包，实际有 {len(bundles)} 个"
    bundle = bundles[0]
    bundle_id = bundle["bundle_id"]
    print(f"  捕获 Bundle ID: {bundle_id}")

    required_fields = [
        "bundle_id", "created_at", "run_id", "snapshot_id",
        "total_moves", "success_count",
        "skipped_conflict_count", "skipped_manual_count", "failed_count",
        "dry_run", "is_undone", "has_signoff",
    ]
    for f in required_fields:
        assert f in bundle, f"list-bundles 输出缺少字段: {f}"

    assert bundle["has_signoff"] is True, "归档包应关联签收信息"
    assert bundle["signed_by"] == "验收测试员", f'signed_by 应为"验收测试员"，实际为 {bundle["signed_by"]}'
    assert bundle["total_moves"] > 0, "归档包应有移动记录"
    assert bundle["is_undone"] is False, "归档包撤销状态应为 False"

    return bundle_id, bundle["run_id"]


def step4_export_and_verify_bundle(bundle_id, snapshot_id, run_id):
    print("\n" + "="*70)
    print("【步骤 4】导出归档包并验证内容完整性")
    print("="*70)

    bundle_export_path = os.path.join(OUTPUT_DIR, "original_bundle.json")
    run(
        f'python -m invoice_organizer.cli export-bundle -c "{CONFIG_PATH}" '
        f'-b {bundle_id} -o "{bundle_export_path}" -v'
    )

    assert os.path.exists(bundle_export_path), "归档包导出文件不存在"
    with open(bundle_export_path, "r", encoding="utf-8") as f:
        bundle_data = json.load(f)

    top_fields = [
        "bundle_id", "bundle_version", "created_at",
        "run_id", "plan_id", "snapshot_id",
        "summary", "snapshot", "run_details",
    ]
    for f in top_fields:
        assert f in bundle_data, f"归档包顶层缺少必填字段: {f}"

    assert bundle_data["bundle_id"] == bundle_id, "bundle_id 不匹配"
    assert bundle_data["snapshot_id"] == snapshot_id, "snapshot_id 不匹配"
    assert bundle_data["run_id"] == run_id, "run_id 不匹配"

    s = bundle_data["summary"]
    sum_fields = [
        "total_moves", "success_count", "skipped_conflict_count",
        "skipped_manual_count", "failed_count", "dry_run", "is_undone",
        "has_signoff", "signoff_status", "signed_by", "signoff_id",
    ]
    for f in sum_fields:
        assert f in s, f"summary 缺少字段: {f}"

    snap = bundle_data["snapshot"]
    snap_fields = ["snapshot_id", "plan_id", "config_snapshot", "moves"]
    for f in snap_fields:
        assert f in snap, f"snapshot 缺少字段: {f}"

    rd = bundle_data["run_details"]
    rd_fields = ["moves", "created_at", "completed_at"]
    for f in rd_fields:
        assert f in rd, f"run_details 缺少字段: {f}"

    move_required = ["filename", "source_path", "target_path", "status"]
    for i, m in enumerate(rd["moves"]):
        for f in move_required:
            assert f in m, f"run_details.moves[{i}] 缺少字段: {f}"

    assert "signoffs" in bundle_data, "归档包应包含 signoffs 字段"
    assert len(bundle_data["signoffs"]) >= 1, "归档包应至少包含 1 条签收记录"
    signoff = bundle_data["signoffs"][0]
    assert signoff["signed_by"] == "验收测试员", "签收人信息丢失"
    assert signoff["status"] == "signed", "签收状态应为 signed"

    assert "checksum" in bundle_data and len(bundle_data["checksum"]) >= 8, "归档包应有校验和"

    print(f"  [OK] 归档包结构完整，共 {s['total_moves']} 条移动记录，校验和: {bundle_data['checksum']}")
    print(f"  [OK] 签收信息完整: {signoff['signed_by']} - {signoff['signoff_id']}")
    print(f"  [OK] 快照 ID: {snap['snapshot_id']}, 移动数: {len(snap.get('moves', []))}")
    print(f"  [OK] 归档包文件: {bundle_export_path}")
    return bundle_export_path


def step5_delete_state_and_import(bundle_export_path):
    print("\n" + "="*70)
    print("【步骤 5】删除运行现场（状态文件）后重新导入")
    print("="*70)

    assert os.path.exists(STATE_FILE), f"状态文件应存在: {STATE_FILE}"
    os.remove(STATE_FILE)
    print(f"  已删除状态文件: {STATE_FILE}")
    assert not os.path.exists(STATE_FILE), "状态文件删除失败"

    run(
        f'python -m invoice_organizer.cli import-bundle -c "{CONFIG_PATH}" '
        f'-i "{bundle_export_path}" -y -v'
    )


def step6_verify_consistency_after_import(
    original_bundle_id, _original_snapshot_id, original_run_id
):
    print("\n" + "="*70)
    print("【步骤 6】核对 CLI、JSON/CSV 导出、状态文件三处一致")
    print("="*70)

    cli_bundles_result = run(
        f'python -m invoice_organizer.cli list-bundles -c "{CONFIG_PATH}" --json',
        capture_output_flag=True
    )
    cli_bundles = json.loads(cli_bundles_result.stdout.strip())
    assert len(cli_bundles) == 1, "导入后应有 1 个归档包"
    cli_bundle = cli_bundles[0]
    assert cli_bundle["bundle_id"] == original_bundle_id, "导入后 bundle_id 不匹配"
    assert cli_bundle["snapshot_id"] == _original_snapshot_id, "导入后 snapshot_id 不匹配"
    assert cli_bundle["run_id"] == original_run_id, "导入后 run_id 不匹配"
    assert cli_bundle["has_signoff"] is True, "导入后签收状态丢失"
    assert cli_bundle["imported"] is True, "imported 标记应为 True"
    print("  [OK] CLI list-bundles 输出正确")

    state_data = read_state()

    assert "snapshots" in state_data, "状态文件应有 snapshots 字段"
    assert _original_snapshot_id in state_data["snapshots"], "状态文件中快照缺失"
    print("  [OK] 状态文件 (JSON) 快照内容正确")

    assert "runs" in state_data, "状态文件应有 runs 字段"
    assert original_run_id in state_data["runs"], "状态文件中执行记录缺失"
    print("  [OK] 状态文件 (JSON) 执行记录内容正确")

    assert "execution_bundles" in state_data, "状态文件应有 execution_bundles 字段"
    assert original_bundle_id in state_data["execution_bundles"], "状态文件中 bundle_id 缺失"
    state_bundle = state_data["execution_bundles"][original_bundle_id]
    assert state_bundle["snapshot_id"] == _original_snapshot_id, "状态文件中 snapshot_id 不匹配"
    assert state_bundle["run_id"] == original_run_id, "状态文件中 run_id 不匹配"
    assert state_bundle["imported"] is True, "状态文件 imported 应为 True"
    print("  [OK] 状态文件 (JSON) 归档包内容正确")

    assert "bundle_import_logs" in state_data, "状态文件应有 bundle_import_logs 字段"
    assert len(state_data["bundle_import_logs"]) >= 1, "状态文件导入日志应为非空"
    import_log = state_data["bundle_import_logs"][0]
    assert import_log["bundle_id"] == original_bundle_id, "导入日志 bundle_id 不匹配"
    assert import_log["status"] == "success", f"导入日志 status 应为 success，实际为 {import_log['status']}"
    assert import_log["imported_by"] == "cli", f"导入日志 imported_by 应为 cli"
    print("  [OK] 状态文件 (JSON) 导入日志内容正确")

    json_export_path = os.path.join(OUTPUT_DIR, "after_import_export.json")
    run(
        f'python -m invoice_organizer.cli export -c "{CONFIG_PATH}" '
        f'-o "{json_export_path}" -f json'
    )
    with open(json_export_path, "r", encoding="utf-8") as f:
        export_json = json.load(f)
    assert "execution_bundles" in export_json, "JSON 导出缺少 execution_bundles 字段"
    assert original_bundle_id in export_json["execution_bundles"], "JSON 导出缺少该 bundle"
    export_bundle = export_json["execution_bundles"][original_bundle_id]
    assert export_bundle["snapshot_id"] == _original_snapshot_id, "JSON 导出 snapshot_id 不匹配"
    assert export_bundle["imported"] is True, "JSON 导出 imported 标记丢失"
    assert "bundle_import_logs" in export_json, "JSON 导出缺少 bundle_import_logs 字段"
    assert len(export_json["bundle_import_logs"]) >= 1, "JSON 导出导入日志应为非空"
    print("  [OK] JSON 导出 (export-logs) 内容正确")

    csv_export_path = os.path.join(OUTPUT_DIR, "after_import_export.csv")
    run(
        f'python -m invoice_organizer.cli export -c "{CONFIG_PATH}" '
        f'-o "{csv_export_path}" -f csv'
    )
    with open(csv_export_path, "r", encoding="utf-8-sig") as f:
        csv_content = f.read()
    assert "=== 执行批次归档包 ===" in csv_content, "CSV 导出缺少执行批次归档包章节"
    assert original_bundle_id in csv_content, f"CSV 导出缺少 bundle_id {original_bundle_id}"
    assert "=== 归档包导入日志 ===" in csv_content, "CSV 导出缺少归档包导入日志章节"
    print("  [OK] CSV 导出 (export-logs) 内容正确")

    re_export_path = os.path.join(OUTPUT_DIR, "after_import_re_bundle.json")
    run(
        f'python -m invoice_organizer.cli export-bundle -c "{CONFIG_PATH}" '
        f'-b {original_bundle_id} -o "{re_export_path}"'
    )
    with open(re_export_path, "r", encoding="utf-8") as f:
        re_bundle = json.load(f)
    assert re_bundle["bundle_id"] == original_bundle_id, "重新导出 bundle_id 不匹配"
    assert re_bundle["snapshot_id"] == _original_snapshot_id, "重新导出 snapshot_id 不匹配"
    assert len(re_bundle["run_details"]["moves"]) > 0, "重新导出移动明细为空"
    assert re_bundle["summary"]["has_signoff"] is True, "重新导出签收信息丢失"
    print("  [OK] 重新 export-bundle 内容正确（可交接）")


def step7_conflict_tests(original_bundle_export_path, _original_snapshot_id):
    print("\n" + "="*70)
    print("【步骤 7】验证三类冲突导入会被明确拒绝")
    print("="*70)

    print("\n--- 冲突场景 1: 同一批次重复导入 ---")
    dup_result = run(
        f'python -m invoice_organizer.cli import-bundle -c "{CONFIG_PATH}" '
        f'-i "{original_bundle_export_path}" -y --by "冲突测试"',
        expect_fail=True, capture_output_flag=True
    )
    combined_output = dup_result.stdout + dup_result.stderr
    assert "同一批次重复导入" in combined_output or "重复导入" in combined_output, \
        f"应提示'同一批次重复导入'或类似文案。输出: {combined_output}"
    assert dup_result.returncode != 0, "重复导入应返回非零退出码"

    state_data = read_state()
    any_failed = any(
        l["status"] == "failed"
        for l in state_data.get("bundle_import_logs", [])
    )
    assert any_failed, "状态文件中应记录一次失败的导入尝试"
    print("  [OK] 冲突 1: 重复导入被明确拒绝，并记录失败导入日志")

    print("\n--- 冲突场景 2: 快照版本对不上 ---")
    with open(original_bundle_export_path, "r", encoding="utf-8") as f:
        tamper_bundle = json.load(f)
    if tamper_bundle["snapshot"].get("move_count", 0) > 0:
        tamper_bundle["snapshot"]["move_count"] += 999
    else:
        tamper_bundle["snapshot"]["move_count"] = 999
    tamper_bundle["snapshot"]["has_conflicts"] = True
    tamper_bundle["bundle_id"] = f"TP{int(time.time())%100000000:08d}"
    tamper_path = os.path.join(OUTPUT_DIR, "tamper_snapshot_bundle.json")
    with open(tamper_path, "w", encoding="utf-8") as f:
        json.dump(tamper_bundle, f, ensure_ascii=False, indent=2)

    snap_result = run(
        f'python -m invoice_organizer.cli import-bundle -c "{CONFIG_PATH}" '
        f'-i "{tamper_path}" -y --by "冲突测试2"',
        expect_fail=True, capture_output_flag=True
    )
    combined_snap = snap_result.stdout + snap_result.stderr
    assert "快照版本对不上" in combined_snap or "快照" in combined_snap, \
        f"应提示'快照版本对不上'或类似文案。输出: {combined_snap}"
    assert snap_result.returncode != 0, "快照篡改导入应返回非零退出码"
    print("  [OK] 冲突 2: 快照版本对不上被明确拒绝")

    print("\n--- 冲突场景 3: 日志缺字段 ---")
    with open(original_bundle_export_path, "r", encoding="utf-8") as f:
        missing_bundle = json.load(f)
    missing_bundle["bundle_id"] = f"MP{int(time.time())%100000000:08d}"
    if len(missing_bundle["run_details"]["moves"]) > 0:
        if "filename" in missing_bundle["run_details"]["moves"][0]:
            del missing_bundle["run_details"]["moves"][0]["filename"]
    missing_path = os.path.join(OUTPUT_DIR, "missing_field_bundle.json")
    with open(missing_path, "w", encoding="utf-8") as f:
        json.dump(missing_bundle, f, ensure_ascii=False, indent=2)

    missing_result = run(
        f'python -m invoice_organizer.cli import-bundle -c "{CONFIG_PATH}" '
        f'-i "{missing_path}" -y --by "冲突测试3"',
        expect_fail=True, capture_output_flag=True
    )
    combined_missing = missing_result.stdout + missing_result.stderr
    assert "日志缺字段" in combined_missing or "缺字段" in combined_missing or "缺少" in combined_missing, \
        f"应提示'日志缺字段'或类似文案。输出: {combined_missing}"
    assert missing_result.returncode != 0, "缺字段导入应返回非零退出码"
    print("  [OK] 冲突 3: 日志缺字段被明确拒绝")


def step8_undo_and_verify_context(original_run_id, original_bundle_id, _original_snapshot_id):
    print("\n" + "="*70)
    print("【步骤 8】执行 undo，验证撤销后仍可回看执行上下文")
    print("="*70)

    run(f'python -m invoice_organizer.cli undo -c "{CONFIG_PATH}" -r {original_run_id} -y')

    bundles_result = run(
        f'python -m invoice_organizer.cli list-bundles -c "{CONFIG_PATH}" --json',
        capture_output_flag=True
    )
    bundles = json.loads(bundles_result.stdout.strip())
    assert len(bundles) == 1, "撤销后归档包应仍存在"
    bundle = bundles[0]
    assert bundle["bundle_id"] == original_bundle_id, "撤销后 bundle_id 不匹配"
    assert bundle["is_undone"] is True, "撤销后归档包 is_undone 应为 True"
    print("  [OK] undo 后 list-bundles 中 is_undone=True，归档包仍存在")

    undo_export_path = os.path.join(OUTPUT_DIR, "after_undo_bundle.json")
    run(
        f'python -m invoice_organizer.cli export-bundle -c "{CONFIG_PATH}" '
        f'-b {original_bundle_id} -o "{undo_export_path}" -v'
    )

    with open(undo_export_path, "r", encoding="utf-8") as f:
        undo_bundle = json.load(f)
    assert undo_bundle["bundle_id"] == original_bundle_id, "撤销后导出 bundle_id 不匹配"
    assert undo_bundle["summary"]["is_undone"] is True, "撤销后导出 summary.is_undone 应为 True"
    assert len(undo_bundle["run_details"]["moves"]) > 0, "撤销后导出移动明细不应为空"
    assert undo_bundle["summary"]["has_signoff"] is True, "撤销后导出签收信息丢失"
    assert undo_bundle["snapshot"]["snapshot_id"] == _original_snapshot_id, \
        "撤销后导出快照信息丢失"
    print("  [OK] undo 后 export-bundle 仍可导出完整执行上下文")
    print(f"    - 总移动数: {undo_bundle['summary']['total_moves']}")
    print(f"    - 成功数: {undo_bundle['summary']['success_count']}")
    print(f"    - 签收状态: {undo_bundle['summary']['signoff_status']}")
    print(f"    - 签收人: {undo_bundle['summary']['signed_by']}")
    print(f"    - 是否撤销: {undo_bundle['summary']['is_undone']}")

    run(
        f'python -m invoice_organizer.cli list-bundles -c "{CONFIG_PATH}" -v'
    )
    print("  [OK] undo 后归档包上下文完整保留，可随时查阅或再次导出")


def main():
    print("\n" + "="*70)
    print("执行批次归档包（ExecutionBundle）完整验收测试")
    print("="*70)

    prepare_workspace()

    snapshot_id, plan_id = plan_signoff_and_apply()

    bundle_id, run_id = step3_verify_bundle_exists()

    bundle_export_path = step4_export_and_verify_bundle(bundle_id, snapshot_id, run_id)

    step5_delete_state_and_import(bundle_export_path)

    step6_verify_consistency_after_import(bundle_id, snapshot_id, run_id)

    step7_conflict_tests(bundle_export_path, snapshot_id)

    step8_undo_and_verify_context(run_id, bundle_id, snapshot_id)

    GREEN = "\033[92m"
    BOLD = "\033[1m"
    RESET = "\033[0m"
    print("\n" + "="*70)
    print(f"{GREEN}{BOLD}[PASS] 所有验收步骤全部通过！{RESET}")
    print("="*70)
    print("\n验收覆盖项：")
    print("  1. [PASS] apply 后自动生成归档包（含快照/签收/校验/移动明细）")
    print("  2. [PASS] list-bundles 正确列出，字段完整")
    print("  3. [PASS] export-bundle 导出 JSON，结构符合要求")
    print("  4. [PASS] 删除现场后 import-bundle 可恢复完整状态")
    print("  5. [PASS] CLI 输出 / 状态文件 / JSON 导出 / CSV 导出 / 重导出 五处一致")
    print("  6. [PASS] 同一批次重复导入被拒绝，退出码非零")
    print("  7. [PASS] 快照版本对不上被拒绝，退出码非零")
    print("  8. [PASS] 日志缺字段被拒绝，退出码非零")
    print("  9. [PASS] undo 后归档包 is_undone=True，仍可回看完整上下文")
    print("  10. [PASS] 导入日志（成功/失败）全部写入状态文件")
    print(f"\n测试输出目录: {OUTPUT_DIR}")
    print("="*70)


if __name__ == "__main__":
    main()
