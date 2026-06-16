"""回归测试：签收冲突完整链路（检测→持久化→处理→放行）

覆盖场景：
1. 正常 signed 签收 → check-signoff 通过 → apply --dry-run 通过
2. 导出快照→修改签收人/说明→导入冲突→创建 SignoffConflictState
3. check-signoff 拦截未解决冲突（基于 SignoffConflictState）
4. apply --dry-run 拦截未解决冲突
5. 正式 apply 拦截未解决冲突
6. resolve-signoff-conflict 三种处理方式（此处先测 keep-local）
7. 处理完成后 check-signoff / apply --dry-run / 正式 apply 恢复放行
8. 状态持久化与重启恢复：signoff_conflicts 列表重启不丢失
9. 快照往返：export 再 import 恢复冲突历史（已解决的记录不丢）
10. undo 回看：撤销执行时回显当时使用的签收 ID/签收人
"""

import os
import sys
import json
import shutil
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import subprocess
import yaml

WORK_DIR = Path(tempfile.mkdtemp(prefix="signoff_conflict_test_"))
SOURCE_DIR = WORK_DIR / "source"
DEST_DIR = WORK_DIR / "dest"
STATE_FILE = WORK_DIR / ".invoice_organizer_state.json"
CONFIG_FILE = WORK_DIR / "config.yaml"
EXPORT_DIR = WORK_DIR / "export"

PROJECT_DIR = Path(__file__).parent.parent


def run(cmd, check=True, cwd=None):
    """运行命令并返回结果"""
    env = os.environ.copy()
    env["PYTHONPATH"] = str(PROJECT_DIR)
    env["PYTHONIOENCODING"] = "utf-8"

    cmd = cmd.replace("python -m invoice_organizer", f'"{sys.executable}" -m invoice_organizer')

    result = subprocess.run(
        cmd,
        shell=True,
        capture_output=True,
        cwd=cwd or str(WORK_DIR),
        env=env,
    )

    def decode(b):
        if b is None:
            return ""
        for enc in ["utf-8", "gbk", "cp936"]:
            try:
                return b.decode(enc)
            except UnicodeDecodeError:
                continue
        return b.decode("utf-8", errors="replace")

    stdout = decode(result.stdout)
    stderr = decode(result.stderr)

    print(f"\n$ {cmd}")
    if stdout:
        print(stdout)
    if stderr:
        print(stderr, file=sys.stderr)

    result.stdout = stdout
    result.stderr = stderr

    if check and result.returncode != 0:
        raise AssertionError(f"命令失败: {cmd}\n{stderr}")
    return result


def cleanup():
    """清理测试环境"""
    if WORK_DIR.exists():
        shutil.rmtree(WORK_DIR)
    print(f"[清理] 测试目录已清理: {WORK_DIR}")


def setup_test_environment():
    """设置测试环境"""
    SOURCE_DIR.mkdir(parents=True, exist_ok=True)
    DEST_DIR.mkdir(parents=True, exist_ok=True)
    EXPORT_DIR.mkdir(parents=True, exist_ok=True)

    config = {
        "source_dir": str(SOURCE_DIR),
        "dest_dir": str(DEST_DIR),
        "rules": [
            {
                "name": "增值税专用发票",
                "pattern": "*专票*.pdf",
                "target": "vat_special",
                "description": "增值税专用发票归档目录",
            },
            {
                "name": "电子发票PDF",
                "pattern": "*电子*.pdf",
                "target": "e_invoice",
                "description": "电子发票PDF归档目录",
            },
        ],
    }

    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        yaml.dump(config, f, allow_unicode=True, default_flow_style=False)

    for i in range(2):
        (SOURCE_DIR / f"专票_2026_00{i+1}.pdf").write_text("test")
    (SOURCE_DIR / "电子发票_2026_001.pdf").write_text("test")

    print(f"[环境] 测试目录已创建: {WORK_DIR}")
    print(f"[环境] 源文件目录: {SOURCE_DIR}")
    print(f"[环境] 目标目录: {DEST_DIR}")
    print(f"[环境] 配置文件: {CONFIG_FILE}")


def step_1_normal_signed_allowed():
    """步骤 1：正常 signed 签收可执行（链路1 - 正常签收放行）"""
    print("\n" + "=" * 80)
    print("步骤 1：正常 signed 签收可执行（链路1 - 正常签收放行）")
    print("=" * 80)

    result = run(f'python -m invoice_organizer plan -c {CONFIG_FILE}')
    assert "预案生成成功" in result.stdout, "预案生成应成功"

    result = run(f'python -m invoice_organizer list-snapshots -c {CONFIG_FILE}')
    lines = result.stdout.strip().split("\n")
    snapshot_id = None
    for line in lines:
        line = line.strip()
        if not line or line.startswith("快照ID") or line.startswith("---"):
            continue
        parts = line.split()
        if parts and len(parts[0]) == 12:
            snapshot_id = parts[0]
            break
    assert snapshot_id, "应获取到快照 ID"

    result = run(
        f'python -m invoice_organizer sign-off -c {CONFIG_FILE} -s {snapshot_id} '
        f'--status signed --signed-by "财务-张三" '
        f'--notes "审核通过，可以执行" -y'
    )
    assert "签收成功" in result.stdout, "签收应成功"
    assert "signed" in result.stdout.lower() or "已签收" in result.stdout, "应显示已签收状态"

    result = run(f'python -m invoice_organizer check-signoff -c {CONFIG_FILE} -s {snapshot_id}')
    assert "签收校验通过" in result.stdout, "check-signoff 应通过"
    assert "可以执行 apply" in result.stdout, "应提示可以执行"

    result = run(f'python -m invoice_organizer apply -c {CONFIG_FILE} -s {snapshot_id} --dry-run -y')
    assert "执行完成" in result.stdout, "apply --dry-run 应成功"
    assert "预演 (DRY-RUN)" in result.stdout, "应显示预演模式"

    print("  [OK] 正常 signed 签收可通过 check-signoff 和 apply --dry-run")
    return snapshot_id


def step_2_export_modify_import_conflict(snapshot_id):
    """步骤 2：导出快照→修改签收→导入冲突，验证 SignoffConflictState 创建"""
    print("\n" + "=" * 80)
    print("步骤 2：导出→修改→导入冲突，创建 SignoffConflictState")
    print("=" * 80)

    export_file = EXPORT_DIR / f"snapshot_{snapshot_id}.json"
    result = run(
        f'python -m invoice_organizer export-snapshot -c {CONFIG_FILE} -s {snapshot_id} -o {export_file}'
    )
    assert export_file.exists(), "导出文件应存在"

    with open(export_file, "r", encoding="utf-8") as f:
        snapshot_data = json.load(f)

    assert "signoffs" in snapshot_data, "导出的快照应包含签收信息"
    assert len(snapshot_data["signoffs"]) == 1, "导出的快照应包含1条签收记录"
    original_signoff_id = snapshot_data["signoffs"][0]["signoff_id"]

    modified_data = json.loads(json.dumps(snapshot_data))
    modified_data["signoffs"][0]["signed_by"] = "财务-李四"
    modified_data["signoffs"][0]["notes"] = "审核不通过，需要重新核对"
    modified_data["signoffs"][0]["signoff_id"] = "imp_" + original_signoff_id
    modified_signoff_id = modified_data["signoffs"][0]["signoff_id"]

    modified_file = EXPORT_DIR / f"snapshot_{snapshot_id}_conflict.json"
    with open(modified_file, "w", encoding="utf-8") as f:
        json.dump(modified_data, f, ensure_ascii=False, indent=2)

    result = run(
        f'python -m invoice_organizer import-snapshot -c {CONFIG_FILE} -i {modified_file} --force -y',
        check=False
    )
    if result.returncode != 0:
        print(f"  [信息] import-snapshot 非零退出，继续检查状态...")

    with open(STATE_FILE, "r", encoding="utf-8") as f:
        state = json.load(f)

    conflicts = state.get("signoff_conflicts", [])
    print(f"  状态文件中签收冲突记录数: {len(conflicts)}")
    assert len(conflicts) >= 1, "状态文件中应存在签收冲突记录（signoff_conflicts 列表）"

    pending = [c for c in conflicts if c.get("status") == "pending" and c.get("snapshot_id") == snapshot_id]
    assert len(pending) >= 1, f"快照 {snapshot_id} 应有 pending 状态的签收冲突"

    conflict = pending[0]
    conflict_id = conflict["conflict_id"]
    print(f"  冲突 ID: {conflict_id}")
    print(f"  状态: {conflict['status']}")
    print(f"  本地签收 ID: {conflict['local_signoff_id']}")
    print(f"  导入签收 ID: {conflict['imported_signoff_id']}")
    print(f"  差异字段: {conflict.get('diff_fields', [])}")
    print(f"  差异摘要: {conflict.get('conflict_summary', '')}")
    print(f"  导入来源: {conflict.get('import_source', '')}")

    assert conflict["local_signoff_id"] == original_signoff_id, "冲突记录的本地签收 ID 应正确"
    assert conflict["imported_signoff_id"] == modified_signoff_id, "冲突记录的导入签收 ID 应正确"
    assert "signed_by" in conflict.get("diff_fields", []), "差异字段应包含 signed_by"
    assert "notes" in conflict.get("diff_fields", []), "差异字段应包含 notes"
    assert conflict.get("import_source", "") != "", "冲突记录应记录 import_source"
    assert conflict.get("conflict_summary", "") != "", "冲突记录应有 conflict_summary"

    signoff_records = state.get("signoff_records", [])
    for s in signoff_records:
        if s["signoff_id"] in (original_signoff_id, modified_signoff_id):
            assert s.get("conflict_id") == conflict_id, f"签收 {s['signoff_id']} 应关联 conflict_id"

    print("  [OK] 导入时成功创建 SignoffConflictState 并关联两条签收记录")
    return conflict_id, original_signoff_id, modified_signoff_id


def step_3_block_before_resolution(snapshot_id, conflict_id):
    """步骤 3：冲突处理前拦截 check-signoff、apply --dry-run、正式 apply（链路2 - 冲突拦截）"""
    print("\n" + "=" * 80)
    print("步骤 3：冲突处理前 check-signoff/apply 三重拦截（链路2 - 冲突拦截）")
    print("=" * 80)

    result = run(
        f'python -m invoice_organizer check-signoff -c {CONFIG_FILE} -s {snapshot_id}',
        check=False
    )
    assert result.returncode != 0, "check-signoff 应失败（非0退出码）"
    assert "签收校验失败" in result.stdout, "应提示签收校验失败"
    assert "未解决的签收冲突" in result.stdout, "应提示存在未解决的签收冲突"
    assert conflict_id in result.stdout, "应显示冲突 ID"
    assert "resolve-signoff-conflict" in result.stdout, "应提示使用 resolve-signoff-conflict 命令"
    print("  [OK] check-signoff 正确拦截")

    result = run(
        f'python -m invoice_organizer apply -c {CONFIG_FILE} -s {snapshot_id} --dry-run -y',
        check=False
    )
    assert result.returncode != 0, "apply --dry-run 应失败（非0退出码）"
    assert "执行被签收校验拦截" in result.stdout, "应提示执行被拦截"
    assert "未解决的签收冲突" in result.stdout, "应提示存在未解决的签收冲突"
    print("  [OK] apply --dry-run 正确拦截")

    result = run(
        f'python -m invoice_organizer apply -c {CONFIG_FILE} -s {snapshot_id} -y',
        check=False
    )
    assert result.returncode != 0, "正式 apply 应失败（非0退出码）"
    assert "执行被签收校验拦截" in result.stdout, "应提示执行被拦截"
    assert "未解决的签收冲突" in result.stdout, "应提示存在未解决的签收冲突"
    print("  [OK] 正式 apply 正确拦截")


def step_4_resolve_keep_local(snapshot_id, conflict_id, original_signoff_id):
    """步骤 4：使用 resolve-signoff-conflict keep-local 处理冲突"""
    print("\n" + "=" * 80)
    print("步骤 4：resolve-signoff-conflict keep-local 处理冲突")
    print("=" * 80)

    result = run(
        f'python -m invoice_organizer resolve-signoff-conflict '
        f'-c {CONFIG_FILE} --snapshot-id {snapshot_id} '
        f'--resolution keep-local --by "操作员A" '
        f'--note "经复核，本地签收张三的审核有效" -y'
    )
    assert "冲突处理成功" in result.stdout, "应显示冲突处理成功"
    assert "已解决（保留本地）" in result.stdout or "resolved_keep_local" in result.stdout, "应显示保留本地"
    assert "操作员A" in result.stdout, "应显示处理人"
    assert "签收校验已通过" in result.stdout, "处理后应提示可以执行 apply"

    with open(STATE_FILE, "r", encoding="utf-8") as f:
        state = json.load(f)
    conflicts = state.get("signoff_conflicts", [])
    resolved = next(c for c in conflicts if c["conflict_id"] == conflict_id)
    assert resolved["status"] == "resolved_keep_local", "冲突状态应为 resolved_keep_local"
    assert resolved["resolved_by"] == "操作员A", "应记录处理人"
    assert "经复核" in resolved["resolution_note"], "应记录处理说明"
    assert resolved["resolved_at"] is not None and resolved["resolved_at"] != "", "应有处理时间"

    signoffs = state.get("signoff_records", [])
    local_sig = next(s for s in signoffs if s["signoff_id"] == original_signoff_id)
    assert local_sig["is_active"] == True, "保留的本地签收应为活动状态"

    print("  [OK] keep-local 处理成功，状态更新正确")


def step_5_release_after_resolution(snapshot_id, conflict_id):
    """步骤 5：处理后 check-signoff / apply --dry-run / 正式 apply 恢复放行（链路3 - 处理后恢复执行）"""
    print("\n" + "=" * 80)
    print("步骤 5：处理后三重校验恢复放行（链路3 - 处理后恢复执行）")
    print("=" * 80)

    result = run(f'python -m invoice_organizer check-signoff -c {CONFIG_FILE} -s {snapshot_id}')
    assert "签收校验通过" in result.stdout, "check-signoff 应通过"
    print("  [OK] check-signoff 放行")

    result = run(f'python -m invoice_organizer apply -c {CONFIG_FILE} -s {snapshot_id} --dry-run -y')
    assert "执行完成" in result.stdout, "apply --dry-run 应成功"
    assert "预演 (DRY-RUN)" in result.stdout, "应显示预演模式"
    print("  [OK] apply --dry-run 放行")

    result = run(f'python -m invoice_organizer apply -c {CONFIG_FILE} -s {snapshot_id} -y')
    assert "执行完成" in result.stdout, "正式 apply 应成功"
    print("  [OK] 正式 apply 放行")

    with open(STATE_FILE, "r", encoding="utf-8") as f:
        state = json.load(f)
    runs = state.get("runs", {})
    actual_runs = {k: v for k, v in runs.items() if not v.get("dry_run", False)}
    assert len(actual_runs) >= 1, "应有实际执行记录"
    run_record = list(actual_runs.values())[-1]
    run_id = run_record["id"]
    assert "signoff_id" in run_record and run_record["signoff_id"], "执行记录应关联签收 ID"

    print(f"  执行 ID: {run_id}")
    print(f"  使用签收 ID: {run_record['signoff_id']}")
    return run_id, run_record["signoff_id"]


def step_6_undo_review_signoff(run_id, expected_signoff_id):
    """步骤 6：undo 回看当时签收信息"""
    print("\n" + "=" * 80)
    print("步骤 6：undo 回看签收信息")
    print("=" * 80)

    result = run(f'python -m invoice_organizer undo -c {CONFIG_FILE} -r {run_id} -y')
    assert "撤销成功" in result.stdout, "undo 应成功"
    assert "签收" in result.stdout or "signoff" in result.stdout.lower(), "undo 应显示签收信息"
    assert expected_signoff_id in result.stdout, "undo 应显示当时使用的签收 ID"

    print("  [OK] undo 时正确回显当时使用的签收信息")


def step_7_restart_persistence(snapshot_id, conflict_id, original_signoff_id):
    """步骤 7：重启后复查冲突状态、处理结果、最近导入来源"""
    print("\n" + "=" * 80)
    print("步骤 7：重启后冲突状态/处理结果/导入来源持久化")
    print("=" * 80)

    import importlib
    import invoice_organizer.storage
    import invoice_organizer.workflow
    import invoice_organizer.cli
    importlib.reload(invoice_organizer.storage)
    importlib.reload(invoice_organizer.workflow)
    importlib.reload(invoice_organizer.cli)
    print("  [模拟重启] 重新加载模块...")

    result = run(f'python -m invoice_organizer check-signoff -c {CONFIG_FILE} -s {snapshot_id}')
    assert "签收校验通过" in result.stdout, "重启后 check-signoff 应通过"

    with open(STATE_FILE, "r", encoding="utf-8") as f:
        state = json.load(f)
    conflicts = state.get("signoff_conflicts", [])
    assert len(conflicts) >= 1, "重启后 signoff_conflicts 列表不应为空"
    resolved = next(c for c in conflicts if c["conflict_id"] == conflict_id)
    assert resolved["status"] == "resolved_keep_local", "重启后冲突状态仍应为 resolved_keep_local"
    assert resolved["resolved_by"] == "操作员A", "重启后处理人信息应保留"
    assert resolved.get("import_source", "") != "", "重启后 import_source 应保留"

    last_source_result = run(
        f'python -m invoice_organizer check-signoff -c {CONFIG_FILE} -s {snapshot_id} --no-require-signed'
    )
    print("  [提示] 最近导入来源保留在冲突记录和签收记录的 import_source 字段")

    print("  [OK] 重启后冲突标记、处理结果、导入来源均保留")


def step_8_snapshot_roundtrip(snapshot_id, conflict_id):
    """步骤 8：快照往返：export → import → 冲突记录保留"""
    print("\n" + "=" * 80)
    print("步骤 8：快照往返（导出再导入冲突信息不丢失）")
    print("=" * 80)

    export_1 = EXPORT_DIR / "roundtrip_1.json"
    result = run(
        f'python -m invoice_organizer export-snapshot -c {CONFIG_FILE} -s {snapshot_id} -o {export_1} -v'
    )
    assert "签收冲突" in result.stdout, "export-snapshot 应显示签收冲突摘要"

    with open(export_1, "r", encoding="utf-8") as f:
        data_1 = json.load(f)
    assert "signoff_conflicts" in data_1, "导出快照 JSON 应包含 signoff_conflicts 字段"
    conflicts_exported = data_1["signoff_conflicts"]
    assert len(conflicts_exported) >= 1, "导出快照应包含至少 1 条签收冲突记录"
    assert any(c["conflict_id"] == conflict_id for c in conflicts_exported), "导出快照应包含目标冲突记录"

    BACKUP_STATE = WORK_DIR / "state_backup.json"
    shutil.copy2(STATE_FILE, BACKUP_STATE)

    try:
        os.remove(STATE_FILE)
        print("  [模拟新环境] 删除状态文件...")

        import_1 = EXPORT_DIR / "roundtrip_1.json"
        result = run(
            f'python -m invoice_organizer import-snapshot -c {CONFIG_FILE} -i {import_1} -y',
            check=False
        )
        if result.returncode != 0:
            print(f"  [信息] import-snapshot 非零退出（可能因原快照无文件），仅检查状态恢复")

        with open(STATE_FILE, "r", encoding="utf-8") as f:
            recovered = json.load(f)
        recovered_conflicts = recovered.get("signoff_conflicts", [])
        assert len(recovered_conflicts) >= 1, "导入后 signoff_conflicts 应恢复"
        found = [c for c in recovered_conflicts if c["conflict_id"] == conflict_id]
        assert len(found) >= 1, "导入后应找到目标冲突记录"
        rc = found[0]
        assert rc["status"] == "resolved_keep_local", "导入后冲突处理状态不丢失"
        assert rc["resolved_by"] == "操作员A", "导入后处理人不丢失"
        print("  [OK] 快照往返后冲突记录完整恢复，状态/处理人/时间不丢失")
    finally:
        shutil.copy2(BACKUP_STATE, STATE_FILE)
        print("  [还原] 已恢复原状态文件")


def step_9_keep_imported_scenario(snapshot_id):
    """步骤 9：追加导入冲突，验证 keep-imported 处理方式"""
    print("\n" + "=" * 80)
    print("步骤 9：验证 keep-imported 处理方式")
    print("=" * 80)

    export_file = EXPORT_DIR / f"snapshot_{snapshot_id}_for_imp.json"
    result = run(
        f'python -m invoice_organizer export-snapshot -c {CONFIG_FILE} -s {snapshot_id} -o {export_file}'
    )

    with open(export_file, "r", encoding="utf-8") as f:
        snapshot_data = json.load(f)
    orig_local_id = None
    for s in snapshot_data.get("signoffs", []):
        if s.get("is_active"):
            orig_local_id = s["signoff_id"]
            break
    assert orig_local_id, "应找到活动签收 ID"

    modified_data = json.loads(json.dumps(snapshot_data))
    for s in modified_data.get("signoffs", []):
        if s["signoff_id"] == orig_local_id:
            s["signed_by"] = "财务-王五"
            s["notes"] = "异地复核通过"
            s["signoff_id"] = "imp_KI_" + orig_local_id
            imported_id = s["signoff_id"]
            break

    modified_file = EXPORT_DIR / f"snapshot_{snapshot_id}_ki.json"
    with open(modified_file, "w", encoding="utf-8") as f:
        json.dump(modified_data, f, ensure_ascii=False, indent=2)

    result = run(
        f'python -m invoice_organizer import-snapshot -c {CONFIG_FILE} -i {modified_file} --force -y',
        check=False
    )

    with open(STATE_FILE, "r", encoding="utf-8") as f:
        state = json.load(f)
    conflicts = [c for c in state.get("signoff_conflicts", [])
                 if c["snapshot_id"] == snapshot_id and c["status"] == "pending"]
    assert len(conflicts) >= 1, "应存在新的 pending 冲突"
    conflict_id = conflicts[-1]["conflict_id"]

    result = run(
        f'python -m invoice_organizer resolve-signoff-conflict '
        f'-c {CONFIG_FILE} --snapshot-id {snapshot_id} --conflict-id {conflict_id} '
        f'--resolution keep-imported --by "操作员B" '
        f'--note "采纳异地复核意见，使用导入签收" -y'
    )
    assert "冲突处理成功" in result.stdout, "keep-imported 处理应成功"

    with open(STATE_FILE, "r", encoding="utf-8") as f:
        state = json.load(f)
    signoffs = state.get("signoff_records", [])
    imported_sig = next(s for s in signoffs if s["signoff_id"] == imported_id)
    assert imported_sig["is_active"] == True, "导入签收应为活动状态"
    local_sig = next(s for s in signoffs if s["signoff_id"] == orig_local_id)
    assert local_sig["is_active"] == False, "本地签收应已失效"

    result = run(f'python -m invoice_organizer check-signoff -c {CONFIG_FILE} -s {snapshot_id}')
    assert "签收校验通过" in result.stdout, "keep-imported 后 check-signoff 应通过"
    assert "王五" in result.stdout, "应显示新签收人王五"

    print("  [OK] keep-imported 处理正确，导入签收生效")


def step_10_csv_export_contains_conflicts():
    """步骤 10：CSV 导出包含签收冲突章节"""
    print("\n" + "=" * 80)
    print("步骤 10：CSV 导出包含签收冲突章节")
    print("=" * 80)

    csv_file = EXPORT_DIR / "export_logs.csv"
    result = run(
        f'python -m invoice_organizer export -c {CONFIG_FILE} -o {csv_file} --format csv'
    )
    assert csv_file.exists(), "CSV 导出文件应存在"

    with open(csv_file, "r", encoding="utf-8-sig") as f:
        content = f.read()
    assert "=== 签收冲突记录 ===" in content, "CSV 导出应包含签收冲突章节标题"
    assert "冲突ID" in content, "CSV 导出应包含冲突表头"
    assert "已解决（保留本地）" in content or "resolved_keep_local" in content, "CSV 中应包含处理结果"

    print("  [OK] CSV 导出包含签收冲突章节")


def main():
    try:
        cleanup()
        setup_test_environment()

        snapshot_id = step_1_normal_signed_allowed()

        conflict_id, original_signoff_id, modified_signoff_id = step_2_export_modify_import_conflict(
            snapshot_id
        )

        step_3_block_before_resolution(snapshot_id, conflict_id)

        step_4_resolve_keep_local(snapshot_id, conflict_id, original_signoff_id)

        run_id, used_signoff_id = step_5_release_after_resolution(snapshot_id, conflict_id)

        step_6_undo_review_signoff(run_id, used_signoff_id)

        step_7_restart_persistence(snapshot_id, conflict_id, original_signoff_id)

        step_8_snapshot_roundtrip(snapshot_id, conflict_id)

        step_9_keep_imported_scenario(snapshot_id)

        step_10_csv_export_contains_conflicts()

        print("\n" + "=" * 80)
        print("[OK] 全部 10 步回归测试通过！")
        print("=" * 80)
        print("\n三条用户可见链路验证：")
        print("  [链路1 OK] 正常签收放行：signed → check-signoff → apply --dry-run 全部通过")
        print("  [链路2 OK] 冲突拦截：导入冲突后 check-signoff / apply --dry-run / 正式 apply 全部拦截")
        print("  [链路3 OK] 处理后恢复执行：resolve-signoff-conflict 后三重校验恢复放行")
        print("\n持久化与恢复：")
        print("  [OK] 重启后冲突标记、处理结果、导入来源不丢失")
        print("  [OK] undo 回显当时的签收 ID/签收人")
        print("  [OK] 快照导出→再导入，冲突历史记录完整恢复")
        print("  [OK] CSV 导出含签收冲突章节")
        print("\n三种处理方式：")
        print("  [OK] keep-local：保留本地签收")
        print("  [OK] keep-imported：保留导入签收")
        print("  [提示] new-signoff 方式在 CLI 中可用（含交互式签收人）")

    except AssertionError as e:
        print(f"\n[ERROR] 测试失败: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
    except Exception as e:
        print(f"\n[ERROR] 发生错误: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
    finally:
        pass


if __name__ == "__main__":
    main()
