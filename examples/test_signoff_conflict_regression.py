"""回归测试：签收冲突拦截链路

测试场景：
1. 正常 signed 可执行
2. 导出快照、改出一份冲突但仍 signed 的签收
3. import-snapshot --force
4. check-signoff 必须拦住并给出原因
5. apply --dry-run 必须拦住并给出原因
6. 检查 CLI 提示、操作日志、状态文件
7. 重启后复查
8. undo 回看
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
    """步骤 1：正常 signed 签收可执行"""
    print("\n" + "="*80)
    print("步骤 1：正常 signed 签收可执行")
    print("="*80)

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

def step_2_export_and_modify_conflicting_signoff(snapshot_id):
    """步骤 2：导出快照，修改出冲突但仍 signed 的签收"""
    print("\n" + "="*80)
    print("步骤 2：导出快照，修改出冲突但仍 signed 的签收")
    print("="*80)

    export_file = EXPORT_DIR / f"snapshot_{snapshot_id}.json"
    result = run(
        f'python -m invoice_organizer export-snapshot -c {CONFIG_FILE} -s {snapshot_id} -o {export_file}'
    )
    assert export_file.exists(), "导出文件应存在"

    with open(export_file, "r", encoding="utf-8") as f:
        snapshot_data = json.load(f)

    assert "signoffs" in snapshot_data, "导出的快照应包含签收信息"
    assert len(snapshot_data["signoffs"]) == 1, "导出的快照应包含1条签收记录"

    original_signoff = snapshot_data["signoffs"][0]
    print(f"  原始签收人: {original_signoff['signed_by']}")
    print(f"  原始说明: {original_signoff['notes']}")

    modified_data = json.loads(json.dumps(snapshot_data))
    modified_data["signoffs"][0]["signed_by"] = "财务-李四"
    modified_data["signoffs"][0]["notes"] = "审核不通过，需要重新核对"
    modified_data["signoffs"][0]["signoff_id"] = "conflict_" + original_signoff["signoff_id"]

    modified_file = EXPORT_DIR / f"snapshot_{snapshot_id}_conflict.json"
    with open(modified_file, "w", encoding="utf-8") as f:
        json.dump(modified_data, f, ensure_ascii=False, indent=2)

    print(f"  修改后签收人: {modified_data['signoffs'][0]['signed_by']}")
    print(f"  修改后说明: {modified_data['signoffs'][0]['notes']}")
    print(f"  冲突文件: {modified_file}")

    print("  [OK] 已导出快照并创建冲突版本（状态仍是 signed，但签收人/说明不同）")
    return export_file, modified_file

def step_3_import_conflict_with_force(snapshot_id, modified_file, original_signoff_id):
    """步骤 3：import-snapshot --force 导入冲突签收"""
    print("\n" + "="*80)
    print("步骤 3：import-snapshot --force 导入冲突签收")
    print("="*80)

    result = run(
        f'python -m invoice_organizer import-snapshot -c {CONFIG_FILE} -i {modified_file} --force -y'
    )
    assert "导入成功" in result.stdout, "强制导入应成功"
    assert "冲突" in result.stdout or "force" in result.stdout.lower(), "应提示存在冲突或强制导入"

    with open(STATE_FILE, "r", encoding="utf-8") as f:
        state = json.load(f)

    signoff_records = state.get("signoff_records", [])
    print(f"  状态文件中签收记录数: {len(signoff_records)}")

    original_signoff = None
    new_signoff = None
    for s in signoff_records:
        if s["signoff_id"] == original_signoff_id:
            original_signoff = s
        elif s["signoff_id"].startswith("conflict_"):
            new_signoff = s

    assert original_signoff is not None, "原始签收应存在"
    assert new_signoff is not None, "新签收应存在"

    print(f"  原始签收: ID={original_signoff['signoff_id']}, active={original_signoff['is_active']}, forced={original_signoff.get('forced', False)}")
    print(f"  新签收: ID={new_signoff['signoff_id']}, active={new_signoff['is_active']}, forced={new_signoff.get('forced', False)}")
    print(f"  新签收冲突详情: {new_signoff.get('conflict_detail', '')}")

    assert new_signoff["is_active"] == True, "新签收应是活动的"
    assert new_signoff.get("forced", False) == True, "新签收应标记为 forced"
    assert new_signoff.get("conflict_detail", "") != "", "新签收应有冲突详情"
    assert "李四" in new_signoff["conflict_detail"] or "张三" in new_signoff["conflict_detail"], "冲突详情应包含签收人差异"

    assert original_signoff["is_active"] == False, "原始签收应已失效"
    assert original_signoff.get("superseded_by") == new_signoff["signoff_id"], "原始签收应记录被谁取代"

    print("  [OK] 强制导入成功，新签收标记为 forced 且有 conflict_detail，旧签收已失效")
    return new_signoff["signoff_id"]

def step_4_check_signoff_blocked(snapshot_id, new_signoff_id):
    """步骤 4：check-signoff 必须拦住并给出原因"""
    print("\n" + "="*80)
    print("步骤 4：check-signoff 必须拦住并给出原因")
    print("="*80)

    result = run(
        f'python -m invoice_organizer check-signoff -c {CONFIG_FILE} -s {snapshot_id}',
        check=False
    )
    assert result.returncode != 0, "check-signoff 应失败（非0退出码）"
    assert "签收校验失败" in result.stdout, "应提示签收校验失败"
    assert "未解决的签收冲突" in result.stdout, "应提示存在未解决的签收冲突"
    assert "强制导入" in result.stdout, "应提示该签收为强制导入"
    assert new_signoff_id in result.stdout, "应显示冲突的签收 ID"

    with open(STATE_FILE, "r", encoding="utf-8") as f:
        state = json.load(f)

    signoff_records = state.get("signoff_records", [])
    new_signoff = next(s for s in signoff_records if s["signoff_id"] == new_signoff_id)
    conflict_detail = new_signoff.get("conflict_detail", "")

    for part in conflict_detail.split(";"):
        if part.strip():
            assert part.strip() in result.stdout or "冲突原因" in result.stdout, f"应显示冲突原因: {part}"

    print("  [OK] check-signoff 正确拦截，显示了冲突原因和强制导入标记")

def step_5_apply_dry_run_blocked(snapshot_id):
    """步骤 5：apply --dry-run 必须拦住并给出原因"""
    print("\n" + "="*80)
    print("步骤 5：apply --dry-run 必须拦住并给出原因")
    print("="*80)

    result = run(
        f'python -m invoice_organizer apply -c {CONFIG_FILE} -s {snapshot_id} --dry-run -y',
        check=False
    )
    assert result.returncode != 0, "apply --dry-run 应失败（非0退出码）"
    assert "执行被签收校验拦截" in result.stdout, "应提示执行被拦截"
    assert "未解决的签收冲突" in result.stdout, "应提示存在未解决的签收冲突"

    assert "--force-conflict-signoff" in result.stdout, "应提示可使用 --force-conflict-signoff 绕过"
    assert "需先解决分歧" in result.stdout, "应提示需先解决分歧"

    print("  [OK] apply --dry-run 正确拦截，显示了冲突原因和绕过选项")

    result = run(
        f'python -m invoice_organizer apply -c {CONFIG_FILE} -s {snapshot_id} --dry-run -y --force-conflict-signoff',
        check=True
    )
    assert "已强制执行（存在未解决的签收冲突）" in result.stdout, "使用 --force-conflict-signoff 应显示强制提示"
    assert "执行完成" in result.stdout, "使用 --force-conflict-signoff 后 apply --dry-run 应成功"
    assert "预演 (DRY-RUN)" in result.stdout, "应显示预演模式"

    print("  [OK] 使用 --force-conflict-signoff 可绕过拦截，且有明确提示")

def step_6_check_state_and_logs(snapshot_id, new_signoff_id, original_signoff_id):
    """步骤 6：检查状态文件、操作日志"""
    print("\n" + "="*80)
    print("步骤 6：检查状态文件、操作日志")
    print("="*80)

    with open(STATE_FILE, "r", encoding="utf-8") as f:
        state = json.load(f)

    signoff_records = state.get("signoff_records", [])
    assert len(signoff_records) >= 2, "状态文件中应有至少2条签收记录"

    signoff_ids = [s["signoff_id"] for s in signoff_records]
    assert original_signoff_id in signoff_ids, "原始签收应在状态文件中"
    assert new_signoff_id in signoff_ids, "新签收应在状态文件中"

    new_signoff = next(s for s in signoff_records if s["signoff_id"] == new_signoff_id)
    assert new_signoff["forced"] == True, "状态文件中新签收应有 forced=True"
    assert new_signoff["conflict_detail"] != "", "状态文件中新签收应有 conflict_detail"
    assert new_signoff["import_source"] is not None, "状态文件中新签收应有 import_source"

    original_signoff = next(s for s in signoff_records if s["signoff_id"] == original_signoff_id)
    assert original_signoff["is_active"] == False, "状态文件中原始签收应 is_active=False"
    assert original_signoff["superseded_by"] == new_signoff_id, "状态文件中原始签收应有 superseded_by"

    export_file = EXPORT_DIR / "full_export.json"
    result = run(
        f'python -m invoice_organizer export -c {CONFIG_FILE} -o {export_file} --format json'
    )
    assert export_file.exists(), "完整导出文件应存在"

    with open(export_file, "r", encoding="utf-8") as f:
        export_data = json.load(f)

    assert "signoff_records" in export_data, "完整导出应包含 signoff_records"
    exported_signoff_ids = [s["signoff_id"] for s in export_data["signoff_records"]]
    assert new_signoff_id in exported_signoff_ids, "完整导出应包含冲突签收"

    exported_new_signoff = next(s for s in export_data["signoff_records"] if s["signoff_id"] == new_signoff_id)
    assert exported_new_signoff["forced"] == True, "导出的新签收应有 forced=True"
    assert exported_new_signoff["conflict_detail"] != "", "导出的新签收应有 conflict_detail"

    print("  [OK] 状态文件和导出文件中的签收记录完整，冲突标记正确")

def step_7_restart_and_recheck(snapshot_id, new_signoff_id):
    """步骤 7：重启后复查"""
    print("\n" + "="*80)
    print("步骤 7：重启后复查")
    print("="*80)

    import importlib
    import invoice_organizer.storage
    import invoice_organizer.workflow
    import invoice_organizer.cli
    importlib.reload(invoice_organizer.storage)
    importlib.reload(invoice_organizer.workflow)
    importlib.reload(invoice_organizer.cli)

    print("  [模拟重启] 重新加载模块...")

    result = run(
        f'python -m invoice_organizer check-signoff -c {CONFIG_FILE} -s {snapshot_id}',
        check=False
    )
    assert result.returncode != 0, "重启后 check-signoff 仍应拦截"
    assert "未解决的签收冲突" in result.stdout, "重启后仍应提示存在未解决的签收冲突"
    assert new_signoff_id in result.stdout, "重启后仍应显示冲突的签收 ID"

    with open(STATE_FILE, "r", encoding="utf-8") as f:
        state = json.load(f)

    signoff_records = state.get("signoff_records", [])
    new_signoff = next(s for s in signoff_records if s["signoff_id"] == new_signoff_id)
    assert new_signoff["forced"] == True, "重启后状态文件中 forced 标记应保留"
    assert new_signoff["conflict_detail"] != "", "重启后状态文件中 conflict_detail 应保留"

    print("  [OK] 重启后签收冲突信息完整保留，拦截行为一致")

def step_8_apply_undo_and_review(snapshot_id):
    """步骤 8：实际执行 apply 和 undo，验证 undo 回看"""
    print("\n" + "="*80)
    print("步骤 8：实际执行 apply 和 undo，验证 undo 回看")
    print("="*80)

    result = run(
        f'python -m invoice_organizer apply -c {CONFIG_FILE} -s {snapshot_id} -y --force-conflict-signoff'
    )
    assert "执行完成" in result.stdout, "apply 应成功"
    assert "已强制执行（存在未解决的签收冲突）" in result.stdout, "应显示强制执行提示"

    with open(STATE_FILE, "r", encoding="utf-8") as f:
        state = json.load(f)

    runs = state.get("runs", {})
    actual_runs = {k: v for k, v in runs.items() if not v.get("dry_run", False)}
    assert len(actual_runs) == 1, "应有1条实际执行记录"

    run_record = list(actual_runs.values())[0]
    run_id = run_record["id"]
    assert "signoff_id" in run_record, "执行记录应关联签收 ID"
    assert run_record["signoff_id"] is not None, "执行记录的签收 ID 不应为空"

    print(f"  执行记录 ID: {run_id}")
    print(f"  关联签收 ID: {run_record['signoff_id']}")

    result = run(
        f'python -m invoice_organizer undo -c {CONFIG_FILE} -r {run_id} -y'
    )
    assert "撤销成功" in result.stdout, "undo 应成功"
    assert "签收" in result.stdout or "signoff" in result.stdout.lower(), "undo 应显示签收信息"
    assert run_record["signoff_id"] in result.stdout, "undo 应显示当时使用的签收 ID"

    print("  [OK] undo 时可回看当时使用的签收信息")

    export_file = EXPORT_DIR / "after_undo_export.json"
    result = run(
        f'python -m invoice_organizer export-snapshot -c {CONFIG_FILE} -s {snapshot_id} -o {export_file}'
    )

    with open(export_file, "r", encoding="utf-8") as f:
        export_data = json.load(f)

    assert "signoffs" in export_data, "undo 后导出仍应包含签收信息"
    signoff_ids = [s["signoff_id"] for s in export_data["signoffs"]]
    assert run_record["signoff_id"] in signoff_ids, "undo 后导出仍应包含当时使用的签收记录"

    print("  [OK] undo 后重新导出，签收历史完整保留")

def main():
    try:
        cleanup()
        setup_test_environment()

        snapshot_id = step_1_normal_signed_allowed()

        with open(STATE_FILE, "r", encoding="utf-8") as f:
            state = json.load(f)
        signoff_records = state.get("signoff_records", [])
        original_signoff_id = signoff_records[0]["signoff_id"]

        export_file, modified_file = step_2_export_and_modify_conflicting_signoff(snapshot_id)
        new_signoff_id = step_3_import_conflict_with_force(snapshot_id, modified_file, original_signoff_id)
        step_4_check_signoff_blocked(snapshot_id, new_signoff_id)
        step_5_apply_dry_run_blocked(snapshot_id)
        step_6_check_state_and_logs(snapshot_id, new_signoff_id, original_signoff_id)
        step_7_restart_and_recheck(snapshot_id, new_signoff_id)
        step_8_apply_undo_and_review(snapshot_id)

        print("\n" + "="*80)
        print("[OK] 所有回归测试通过！")
        print("="*80)
        print("\n验证要点总结：")
        print("  [OK] 正常 signed 签收可通过 check-signoff 和 apply --dry-run")
        print("  [OK] 强制导入冲突签收后，新签收标记为 forced=True 且有 conflict_detail")
        print("  [OK] 原始签收被标记为 is_active=False，记录 superseded_by")
        print("  [OK] check-signoff 正确拦截冲突签收，显示冲突原因")
        print("  [OK] apply --dry-run 正确拦截冲突签收，显示绕过选项")
        print("  [OK] --force-conflict-signoff 可绕过拦截，且有明确提示")
        print("  [OK] 状态文件、JSON 导出中的签收记录完整，冲突标记正确")
        print("  [OK] 重启后冲突信息完整保留，拦截行为一致")
        print("  [OK] undo 时可回看当时使用的签收信息")
        print("  [OK] undo 后重新导出，签收历史完整保留")

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
