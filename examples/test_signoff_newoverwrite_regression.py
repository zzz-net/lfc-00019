"""专项回归测试：resolve-signoff-conflict --resolution new-signoff 链路

覆盖场景：
1. 复现 TypeError 崩溃场景（修复前）
2. 构造冲突快照 → 导入 → new-signoff 重新签收
3. 冲突处理后 check-signoff 通过
4. 冲突处理后 apply --dry-run 通过
5. 冲突处理后正式 apply 通过
6. 冲突状态 persist（resolved_new）
7. 验证保留的签收记录已清除 forced/conflict_detail
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

WORK_DIR = Path(tempfile.mkdtemp(prefix="signoff_newoverwrite_"))
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
    if WORK_DIR.exists():
        shutil.rmtree(WORK_DIR)
    print(f"[清理] 测试目录已清理: {WORK_DIR}")


def setup():
    SOURCE_DIR.mkdir(parents=True, exist_ok=True)
    DEST_DIR.mkdir(parents=True, exist_ok=True)
    EXPORT_DIR.mkdir(parents=True, exist_ok=True)

    config = {
        "source_dir": str(SOURCE_DIR),
        "dest_dir": str(DEST_DIR),
        "rules": [
            {"name": "专票", "pattern": "*专票*.pdf", "target": "vat"},
            {"name": "电子票", "pattern": "*电子*.pdf", "target": "einv"},
        ],
    }
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        yaml.dump(config, f, allow_unicode=True, default_flow_style=False)

    for i in range(2):
        (SOURCE_DIR / f"专票_2026_0{i+1}.pdf").write_text("x")
    (SOURCE_DIR / "电子发票_2026_01.pdf").write_text("x")

    print(f"[环境] {WORK_DIR}")


def step_1_build_base_signed():
    """步骤1：生成预案+正常签收"""
    print("\n" + "=" * 80)
    print("步骤1：生成预案+正常签收（财务-张三）")
    print("=" * 80)

    run(f'python -m invoice_organizer plan -c {CONFIG_FILE}')
    result = run(f'python -m invoice_organizer list-snapshots -c {CONFIG_FILE}')
    snapshot_id = None
    for line in result.stdout.strip().split("\n"):
        parts = line.strip().split()
        if parts and len(parts[0]) == 12:
            snapshot_id = parts[0]
            break
    assert snapshot_id

    run(
        f'python -m invoice_organizer sign-off -c {CONFIG_FILE} -s {snapshot_id} '
        f'--status signed --signed-by "财务-张三" --notes "审核通过" -y'
    )
    print(f"  快照ID: {snapshot_id}")
    return snapshot_id


def step_2_export_and_modify_conflict(snapshot_id):
    """步骤2：导出快照，修改签收人/说明构造冲突"""
    print("\n" + "=" * 80)
    print("步骤2：导出→修改签收（财务-李四，说明变了）构造冲突")
    print("=" * 80)

    export_f = EXPORT_DIR / f"snap_{snapshot_id}.json"
    run(f'python -m invoice_organizer export-snapshot -c {CONFIG_FILE} -s {snapshot_id} -o {export_f}')

    with open(export_f, "r", encoding="utf-8") as f:
        data = json.load(f)
    orig_id = data["signoffs"][0]["signoff_id"]

    data["signoffs"][0]["signed_by"] = "财务-李四"
    data["signoffs"][0]["notes"] = "异地复核，需二次确认"
    data["signoffs"][0]["signoff_id"] = "imp_ns_" + orig_id

    conflict_f = EXPORT_DIR / f"snap_{snapshot_id}_ns_conflict.json"
    with open(conflict_f, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    print(f"  原签收人: 财务-张三")
    print(f"  改后签收人: 财务-李四")
    print(f"  冲突文件: {conflict_f}")
    return conflict_f, orig_id


def step_3_import_conflict(snapshot_id, conflict_f):
    """步骤3：导入冲突快照，创建 pending 冲突"""
    print("\n" + "=" * 80)
    print("步骤3：导入冲突快照，创建 pending 冲突状态")
    print("=" * 80)

    result = run(
        f'python -m invoice_organizer import-snapshot -c {CONFIG_FILE} -i {conflict_f} --force -y',
        check=False
    )

    with open(STATE_FILE, "r", encoding="utf-8") as f:
        state = json.load(f)
    pending = [
        c for c in state.get("signoff_conflicts", [])
        if c["snapshot_id"] == snapshot_id and c["status"] == "pending"
    ]
    assert len(pending) >= 1, "应创建 pending 冲突"
    conflict_id = pending[-1]["conflict_id"]
    print(f"  冲突ID: {conflict_id}")
    print(f"  差异: {pending[-1]['conflict_summary']}")
    return conflict_id


def step_4_new_signoff_resolution(snapshot_id, conflict_id):
    """步骤4：resolve-signoff-conflict --resolution new-signoff

    这是崩溃复现点：修复前这里会抛 TypeError。
    修复后应成功创建新签收并标记冲突为 resolved_new。
    """
    print("\n" + "=" * 80)
    print("步骤4：resolve-signoff-conflict --resolution new-signoff（原崩溃点）")
    print("=" * 80)

    result = run(
        f'python -m invoice_organizer resolve-signoff-conflict '
        f'-c {CONFIG_FILE} --snapshot-id {snapshot_id} --conflict-id {conflict_id} '
        f'--resolution new-signoff --by "操作员C" --signer "财务-赵六" '
        f'--signoff-notes "综合三方意见，最终审核通过" '
        f'--note "new-signoff 方式覆盖前两者" -y'
    )

    assert "冲突处理成功" in result.stdout, "new-signoff 应成功（修复前抛 TypeError）"
    assert "已解决（新建签收）" in result.stdout or "resolved_new" in result.stdout, "应显示 resolved_new"
    assert "财务-赵六" in result.stdout, "应显示新签收人"
    assert "签收校验已通过" in result.stdout, "处理后应提示可执行 apply"

    with open(STATE_FILE, "r", encoding="utf-8") as f:
        state = json.load(f)
    conflicts = state.get("signoff_conflicts", [])
    resolved = next(c for c in conflicts if c["conflict_id"] == conflict_id)
    assert resolved["status"] == "resolved_new", f"状态应为 resolved_new，实际: {resolved['status']}"
    assert resolved["resolved_by"] == "操作员C", "应记录处理人"
    assert resolved["new_signoff_id"] is not None, "应记录新签收 ID"
    new_signoff_id = resolved["new_signoff_id"]

    signoffs = state.get("signoff_records", [])
    new_sig = next(s for s in signoffs if s["signoff_id"] == new_signoff_id)
    assert new_sig["is_active"] == True, "新签收应为活动状态"
    assert new_sig["signed_by"] == "财务-赵六", "新签收人应为赵六"
    assert new_sig["forced"] == False, f"新签收 forced 应为 False，实际: {new_sig['forced']}"
    assert new_sig.get("conflict_detail", "") == "", f"新签收 conflict_detail 应清空，实际: {new_sig.get('conflict_detail')}"
    assert new_sig.get("conflict_id") == conflict_id, "新签收应关联 conflict_id"

    print(f"  新签收ID: {new_signoff_id}")
    print(f"  新签收人: {new_sig['signed_by']}")
    print(f"  冲突状态: {resolved['status']}")
    print(f"  forced 标记: {new_sig['forced']}")
    print(f"  conflict_detail: '{new_sig.get('conflict_detail', '')}'")
    return new_signoff_id


def step_5_triple_validation_after_resolution(snapshot_id, expected_signer):
    """步骤5：冲突处理后三重校验全部放行"""
    print("\n" + "=" * 80)
    print("步骤5：处理后 check-signoff / apply --dry-run / 正式 apply 全部放行")
    print("=" * 80)

    result = run(f'python -m invoice_organizer check-signoff -c {CONFIG_FILE} -s {snapshot_id}')
    assert "签收校验通过" in result.stdout, "check-signoff 应通过"
    assert expected_signer in result.stdout, f"应显示新签收人 {expected_signer}"
    print("  [OK] check-signoff 放行")

    result = run(f'python -m invoice_organizer apply -c {CONFIG_FILE} -s {snapshot_id} --dry-run -y')
    assert "执行完成" in result.stdout, "apply --dry-run 应成功"
    print("  [OK] apply --dry-run 放行")

    result = run(f'python -m invoice_organizer apply -c {CONFIG_FILE} -s {snapshot_id} -y')
    assert "执行完成" in result.stdout, "正式 apply 应成功"
    print("  [OK] 正式 apply 放行")

    with open(STATE_FILE, "r", encoding="utf-8") as f:
        state = json.load(f)
    runs = state.get("runs", {})
    actual = [v for v in runs.values() if not v.get("dry_run")]
    assert len(actual) >= 1
    run_id = actual[-1]["id"]
    assert actual[-1].get("signoff_id"), "执行记录应关联签收 ID"
    return run_id, actual[-1]["signoff_id"]


def step_6_undo_review(run_id, expected_signoff_id, expected_signer):
    """步骤6：undo 回看签收信息"""
    print("\n" + "=" * 80)
    print("步骤6：undo 回看新签收信息")
    print("=" * 80)

    result = run(f'python -m invoice_organizer undo -c {CONFIG_FILE} -r {run_id} -y')
    assert "撤销成功" in result.stdout
    assert expected_signoff_id in result.stdout, "undo 应回显签收 ID"
    assert expected_signer in result.stdout, "undo 应回显签收人"
    print("  [OK] undo 回显正确")


def step_7_persistence(snapshot_id, conflict_id, new_signoff_id):
    """步骤7：重启持久化验证"""
    print("\n" + "=" * 80)
    print("步骤7：重启后冲突状态/新签收不丢失")
    print("=" * 80)

    import importlib
    import invoice_organizer.storage, invoice_organizer.workflow, invoice_organizer.cli
    importlib.reload(invoice_organizer.storage)
    importlib.reload(invoice_organizer.workflow)
    importlib.reload(invoice_organizer.cli)

    result = run(f'python -m invoice_organizer check-signoff -c {CONFIG_FILE} -s {snapshot_id}')
    assert "签收校验通过" in result.stdout, "重启后 check-signoff 仍应通过"

    with open(STATE_FILE, "r", encoding="utf-8") as f:
        state = json.load(f)
    resolved = next(c for c in state["signoff_conflicts"] if c["conflict_id"] == conflict_id)
    assert resolved["status"] == "resolved_new", "重启后状态仍为 resolved_new"
    assert resolved["new_signoff_id"] == new_signoff_id, "重启后 new_signoff_id 不丢"
    assert resolved["import_source"] != "", "重启后 import_source 不丢"

    new_sig = next(s for s in state["signoff_records"] if s["signoff_id"] == new_signoff_id)
    assert new_sig["is_active"] == True, "重启后新签收仍为活动状态"
    assert new_sig["forced"] == False, "重启后 forced 仍为 False"
    assert new_sig.get("conflict_detail", "") == "", "重启后 conflict_detail 仍清空"

    print("  [OK] 重启后所有状态正确保留")


def main():
    try:
        cleanup()
        setup()

        snapshot_id = step_1_build_base_signed()
        conflict_f, orig_signoff_id = step_2_export_and_modify_conflict(snapshot_id)
        conflict_id = step_3_import_conflict(snapshot_id, conflict_f)

        # ===== 核心修复验证点 =====
        new_signoff_id = step_4_new_signoff_resolution(snapshot_id, conflict_id)

        run_id, used_signoff_id = step_5_triple_validation_after_resolution(
            snapshot_id, "财务-赵六"
        )
        assert used_signoff_id == new_signoff_id, "执行应使用新签收"

        step_6_undo_review(run_id, new_signoff_id, "财务-赵六")
        step_7_persistence(snapshot_id, conflict_id, new_signoff_id)

        print("\n" + "=" * 80)
        print("[OK] new-signoff 专项回归测试全部通过！")
        print("=" * 80)
        print("\n用户可见变化：")
        print("  [已修复] resolve-signoff-conflict --resolution new-signoff 不再抛 TypeError")
        print("  [新体验] new-signoff 方式成功后，自动清除保留签收的 forced 标记和 conflict_detail")
        print("  [新体验] 新签收记录关联 conflict_id，便于追溯")
        print("  [新体验] 冲突状态变为 resolved_new，记录 new_signoff_id")
        print("  [新体验] 处理后 check-signoff、apply --dry-run、正式 apply 全部正常放行")

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
