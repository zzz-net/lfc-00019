"""回归测试：执行前校验完整链路（持久化→导出→重启→处理→放行→undo）

覆盖场景：
1. 锁定快照不一致阻塞：有效签收快照 apply 时被锁定拦截，lock_mismatch 持久化
2. 签收过期 + 未解决冲突阻塞：check-signoff / apply 拦截，signoff_expired + unresolved_signoff_conflict 持久化
3. apply --dry-run / 正式 apply 拦截时持久化校验结果
4. export JSON/CSV 包含校验历史章节
5. 模拟重启（重新加载状态）后校验历史仍然存在
6. 处理阻塞：解锁、解决冲突、重新签收，同步刷新校验状态
7. 再次 dry-run / 正式 apply 通过，校验状态更新
8. undo 后校验状态再次刷新
9. CLI 可见信息与导出内容前后对得上

关键设计：
- 先测试 lock_mismatch（snapshot1 有效签收，但锁定了 snapshot2 → apply snapshot1 时锁定拦截）
- 再制造签收过期 + 冲突（重新签收为过期 + 导入冲突签收 → check-signoff / apply 拦截）
- 这样 apply 的签收校验和锁定校验都能被独立触发和持久化
"""

import os
import sys
import json
import shutil
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import subprocess
import yaml

WORK_DIR = Path(tempfile.mkdtemp(prefix="validation_persistence_test_"))
SOURCE_DIR = WORK_DIR / "source"
DEST_DIR = WORK_DIR / "dest"
STATE_FILE = WORK_DIR / ".invoice_organizer_state.json"
CONFIG_FILE = WORK_DIR / "config.yaml"
EXPORT_DIR = WORK_DIR / "export"

PROJECT_DIR = Path(__file__).parent.parent


def run(cmd, check=True, cwd=None):
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


def setup_test_environment():
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


def get_future_date(days_ahead: int = 30) -> str:
    future = datetime.now() + timedelta(days=days_ahead)
    return future.isoformat()


def get_past_date(days_ago: int) -> str:
    past = datetime.now() - timedelta(days=days_ago)
    return past.isoformat()


def extract_snapshot_id_from_list(output: str, exclude_ids=None):
    lines = output.strip().split("\n")
    exclude_ids = exclude_ids or []
    for line in lines:
        line = line.strip()
        if not line or line.startswith("快照ID") or line.startswith("---"):
            continue
        parts = line.split()
        if parts and len(parts[0]) == 12 and parts[0] not in exclude_ids:
            return parts[0]
    return None


def step_1_setup_snapshots():
    """步骤 1：创建两个快照并签收，锁定 snapshot2"""
    print("\n" + "=" * 80)
    print("步骤 1：创建快照、签收、锁定")
    print("=" * 80)

    result = run(f'python -m invoice_organizer plan -c {CONFIG_FILE}')
    assert "预案生成成功" in result.stdout

    result = run(f'python -m invoice_organizer list-snapshots -c {CONFIG_FILE}')
    snapshot1_id = extract_snapshot_id_from_list(result.stdout)
    assert snapshot1_id, "应获取到第一个快照 ID"

    future_deadline = get_future_date(30)
    result = run(
        f'python -m invoice_organizer sign-off -c {CONFIG_FILE} -s {snapshot1_id} '
        f'--status signed --signed-by "财务-张三" '
        f'--deadline "{future_deadline}" '
        f'--notes "审核通过" -y'
    )
    assert "签收成功" in result.stdout

    result = run(f'python -m invoice_organizer plan -c {CONFIG_FILE}')
    assert "预案生成成功" in result.stdout

    result = run(f'python -m invoice_organizer list-snapshots -c {CONFIG_FILE}')
    snapshot2_id = extract_snapshot_id_from_list(result.stdout, exclude_ids=[snapshot1_id])
    assert snapshot2_id, "应获取到第二个快照 ID"

    result = run(
        f'python -m invoice_organizer sign-off -c {CONFIG_FILE} -s {snapshot2_id} '
        f'--status signed --signed-by "财务-王五" '
        f'--notes "锁定测试签收" -y'
    )
    assert "签收成功" in result.stdout

    result = run(
        f'python -m invoice_organizer lock-plan -c {CONFIG_FILE} -s {snapshot2_id} '
        f'--reason "测试锁定不一致场景"'
    )
    assert "锁定成功" in result.stdout or "已锁定" in result.stdout

    print(f"  快照1 ID: {snapshot1_id} (有效签收，但被锁定拦截)")
    print(f"  快照2 ID: {snapshot2_id} (已锁定)")
    print("  [OK] 两个快照创建、签收、锁定完成")
    return snapshot1_id, snapshot2_id


def step_2_lock_mismatch_persistence(snapshot1_id, snapshot2_id):
    """步骤 2：测试 lock_mismatch 阻塞持久化（有效签收 + 锁定不一致）"""
    print("\n" + "=" * 80)
    print("步骤 2：lock_mismatch 阻塞持久化")
    print("=" * 80)

    result = run(
        f'python -m invoice_organizer apply -c {CONFIG_FILE} -s {snapshot1_id} --dry-run -y',
        check=False
    )
    assert result.returncode != 0, "apply --dry-run 应失败（锁定不一致）"
    assert "执行被版本锁定拦截" in result.stdout, f"应被锁定拦截，实际输出: {result.stdout}"

    result = run(
        f'python -m invoice_organizer apply -c {CONFIG_FILE} -s {snapshot1_id} -y',
        check=False
    )
    assert result.returncode != 0, "正式 apply 应失败（锁定不一致）"
    assert "执行被版本锁定拦截" in result.stdout

    with open(STATE_FILE, "r", encoding="utf-8") as f:
        state = json.load(f)

    validation_history = state.get("validation_history", [])
    lock_records = [v for v in validation_history if v.get("has_lock_mismatch")]
    assert len(lock_records) >= 2, f"应有至少 2 条锁定不一致记录，实际: {len(lock_records)}"

    for lr in lock_records:
        assert "lock_mismatch" in lr.get("block_types", []), "应包含 lock_mismatch 阻塞类型"
        assert lr.get("status") == "blocked", "锁定拦截记录应为 blocked"
        assert lr.get("snapshot_id") == snapshot1_id, "快照 ID 应为 snapshot1"
        assert lr.get("lock_id") is not None, "应有锁定 ID"

    print(f"  lock_mismatch 记录数: {len(lock_records)}")
    print("  [OK] lock_mismatch 阻塞已正确持久化")
    return validation_history


def step_3_signoff_blocks_persistence(snapshot1_id):
    print("\n" + "=" * 80)
    print("步骤 3：签收过期 + 冲突阻塞持久化")
    print("=" * 80)

    past_deadline = get_past_date(10)
    result = run(
        f'python -m invoice_organizer sign-off -c {CONFIG_FILE} -s {snapshot1_id} '
        f'--status signed --signed-by "财务-张三" --force '
        f'--deadline "{past_deadline}" '
        f'--notes "重新签收，设置为过期" -y'
    )
    assert "签收成功" in result.stdout
    print(f"  已将 snapshot1 签收设为过期")

    result = run(
        f'python -m invoice_organizer check-signoff -c {CONFIG_FILE} -s {snapshot1_id}',
        check=False
    )
    assert result.returncode != 0, "check-signoff 应失败（签收过期）"
    assert "签收校验失败" in result.stdout
    print(f"  check-signoff 检测到签收过期阻塞")

    export_file = EXPORT_DIR / f"snapshot_{snapshot1_id}.json"
    run(f'python -m invoice_organizer export-snapshot -c {CONFIG_FILE} -s {snapshot1_id} -o {export_file}')

    with open(export_file, "r", encoding="utf-8") as f:
        snapshot_data = json.load(f)

    active_idx = None
    for i, s in enumerate(snapshot_data["signoffs"]):
        if s.get("is_active", True):
            active_idx = i
            break
    assert active_idx is not None, "应有活动签收记录"

    modified_data = json.loads(json.dumps(snapshot_data))
    modified_data["signoffs"][active_idx]["signed_by"] = "财务-李四"
    modified_data["signoffs"][active_idx]["notes"] = "审核不通过"
    modified_data["signoffs"][active_idx]["signoff_id"] = "imp_" + modified_data["signoffs"][active_idx]["signoff_id"]

    modified_file = EXPORT_DIR / f"snapshot_{snapshot1_id}_conflict.json"
    with open(modified_file, "w", encoding="utf-8") as f:
        json.dump(modified_data, f, ensure_ascii=False, indent=2)

    import_result = run(
        f'python -m invoice_organizer import-snapshot -c {CONFIG_FILE} -i {modified_file} --force -y',
        check=False
    )

    with open(STATE_FILE, "r", encoding="utf-8") as f:
        state = json.load(f)
    conflicts = state.get("signoff_conflicts", [])
    pending_conflicts = [c for c in conflicts if c.get("status") == "pending" and c.get("snapshot_id") == snapshot1_id]
    assert len(pending_conflicts) >= 1, "应有 pending 状态的签收冲突"
    conflict_id = pending_conflicts[0]["conflict_id"]
    print(f"  冲突 ID: {conflict_id}")

    result = run(
        f'python -m invoice_organizer check-signoff -c {CONFIG_FILE} -s {snapshot1_id}',
        check=False
    )
    assert result.returncode != 0, "check-signoff 应失败（签收冲突）"

    result = run(
        f'python -m invoice_organizer apply -c {CONFIG_FILE} -s {snapshot1_id} --dry-run -y',
        check=False
    )
    assert result.returncode != 0, "apply --dry-run 应失败（签收冲突）"

    with open(STATE_FILE, "r", encoding="utf-8") as f:
        state = json.load(f)

    validation_history = state.get("validation_history", [])

    expired_records = [v for v in validation_history if "signoff_expired" in v.get("block_types", [])]
    assert len(expired_records) >= 1, "应有 signoff_expired 阻塞记录"
    expired_rec = expired_records[0]
    assert expired_rec.get("is_expired") == True
    assert expired_rec.get("snapshot_id") == snapshot1_id
    print(f"  [OK] signoff_expired 阻塞已持久化")

    conflict_records = [v for v in validation_history if "unresolved_signoff_conflict" in v.get("block_types", [])]
    assert len(conflict_records) >= 1, "应有 unresolved_signoff_conflict 阻塞记录"
    conflict_rec = conflict_records[0]
    assert conflict_rec.get("has_unresolved_conflict") == True
    assert conflict_rec.get("conflict_id") == conflict_id
    print(f"  [OK] unresolved_signoff_conflict 阻塞已持久化")

    apply_blocked = [v for v in validation_history if v.get("triggered_by") == "apply-dry-run" and v.get("status") == "blocked"]
    assert len(apply_blocked) >= 1, "应有 apply-dry-run 签收阻塞记录"

    for record in validation_history:
        assert record.get("triggered_at"), "每条记录应有触发时间"
        assert record.get("triggered_by"), "每条记录应有触发命令"
        assert record.get("validation_id"), "每条记录应有校验 ID"
        assert record.get("plan_id"), "每条记录应有预案 ID"

    print("  [OK] 签收过期 + 冲突阻塞全部持久化验证通过")
    return conflict_id, validation_history


def step_4_export_contains_validation(validation_history_before):
    """步骤 4：导出 JSON/CSV 包含校验历史"""
    print("\n" + "=" * 80)
    print("步骤 4：导出 JSON/CSV 包含校验历史")
    print("=" * 80)

    json_file = EXPORT_DIR / "export_logs.json"
    run(f'python -m invoice_organizer export -c {CONFIG_FILE} -o {json_file} --format json')
    assert json_file.exists()

    with open(json_file, "r", encoding="utf-8") as f:
        exported_state = json.load(f)

    exported_validation = exported_state.get("validation_history", [])
    print(f"  JSON 导出校验历史记录数: {len(exported_validation)}")
    assert len(exported_validation) == len(validation_history_before), \
        f"JSON 导出记录数不一致: {len(exported_validation)} vs {len(validation_history_before)}"

    for exp, orig in zip(exported_validation, validation_history_before):
        assert exp["validation_id"] == orig["validation_id"]
        assert exp["triggered_by"] == orig["triggered_by"]
        assert exp["status"] == orig["status"]

    csv_file = EXPORT_DIR / "export_logs.csv"
    run(f'python -m invoice_organizer export -c {CONFIG_FILE} -o {csv_file} --format csv')
    assert csv_file.exists()

    with open(csv_file, "r", encoding="utf-8-sig") as f:
        csv_content = f.read()

    assert "=== 签收校验历史 ===" in csv_content
    assert "校验ID" in csv_content
    assert "阻塞类型" in csv_content
    assert "未解决签收冲突" in csv_content
    assert "签收过期" in csv_content
    assert "锁定快照不一致" in csv_content

    for record in validation_history_before:
        assert record["validation_id"] in csv_content, \
            f"CSV 中应包含校验 ID {record['validation_id']}"

    print("  [OK] JSON/CSV 导出均包含校验历史，数据一致")


def step_5_restart_review(snapshot1_id, conflict_id, prev_count):
    """步骤 5：模拟重启后复查校验历史"""
    print("\n" + "=" * 80)
    print("步骤 5：模拟重启后复查校验历史")
    print("=" * 80)

    import importlib
    import invoice_organizer.storage
    import invoice_organizer.workflow
    import invoice_organizer.cli
    importlib.reload(invoice_organizer.storage)
    importlib.reload(invoice_organizer.workflow)
    importlib.reload(invoice_organizer.cli)

    with open(STATE_FILE, "r", encoding="utf-8") as f:
        state_after_restart = json.load(f)

    validation_after_restart = state_after_restart.get("validation_history", [])
    print(f"  重启后校验历史记录数: {len(validation_after_restart)}")
    assert len(validation_after_restart) >= prev_count, "重启后校验历史不应丢失"

    blocked_after = [v for v in validation_after_restart if v.get("status") == "blocked"]
    assert len(blocked_after) >= 3, f"重启后阻塞记录不应丢失，实际: {len(blocked_after)}"

    result = run(
        f'python -m invoice_organizer check-signoff -c {CONFIG_FILE} -s {snapshot1_id}',
        check=False
    )
    assert result.returncode != 0
    assert "未解决的签收冲突" in result.stdout

    with open(STATE_FILE, "r", encoding="utf-8") as f:
        state_after_check = json.load(f)
    validation_after_check = state_after_check.get("validation_history", [])
    assert len(validation_after_check) > len(validation_after_restart), "应新增一条校验记录"

    print("  [OK] 重启后校验历史完整保留，且可继续追加")


def step_6_resolve_blockages(snapshot1_id, conflict_id, snapshot2_id):
    """步骤 6：处理所有阻塞并刷新校验状态"""
    print("\n" + "=" * 80)
    print("步骤 6：处理所有阻塞并刷新校验状态")
    print("=" * 80)

    result = run(f'python -m invoice_organizer unlock-plan -c {CONFIG_FILE}')
    assert "解锁成功" in result.stdout or "已释放" in result.stdout
    print("  [解锁] 锁定已释放")

    result = run(
        f'python -m invoice_organizer resolve-signoff-conflict '
        f'-c {CONFIG_FILE} --snapshot-id {snapshot1_id} '
        f'--resolution keep-local --by "操作员A" '
        f'--note "保留本地张三的审核意见" -y'
    )
    assert "冲突处理成功" in result.stdout
    print("  [冲突处理] 冲突已解决")

    result = run(
        f'python -m invoice_organizer sign-off -c {CONFIG_FILE} -s {snapshot1_id} '
        f'--status signed --signed-by "财务-张三" --force '
        f'--notes "重新审核，延长有效期" -y'
    )
    assert "签收成功" in result.stdout
    print("  [重新签收] 已更新签收（非过期）")

    with open(STATE_FILE, "r", encoding="utf-8") as f:
        state = json.load(f)

    validation_history = state.get("validation_history", [])

    newly_resolved = [v for v in validation_history if v.get("resolved_at") and v.get("resolved_by") == "操作员A"]
    assert len(newly_resolved) >= 1, "应有记录被标记为已解决"

    signoff_records = [v for v in validation_history if v.get("triggered_by") == "sign-off"]
    assert len(signoff_records) >= 1
    latest_signoff = signoff_records[-1]
    assert latest_signoff.get("status") == "passed"
    assert latest_signoff.get("is_expired") == False
    assert latest_signoff.get("has_unresolved_conflict") == False
    assert latest_signoff.get("has_lock_mismatch") == False

    print("  [OK] 所有阻塞已处理，校验状态已刷新")


def step_7_dry_run_and_apply(snapshot1_id):
    """步骤 7：dry-run 和正式 apply 通过，校验状态更新"""
    print("\n" + "=" * 80)
    print("步骤 7：dry-run 和正式 apply 通过，校验状态更新")
    print("=" * 80)

    result = run(f'python -m invoice_organizer check-signoff -c {CONFIG_FILE} -s {snapshot1_id}')
    assert "签收校验通过" in result.stdout

    result = run(f'python -m invoice_organizer apply -c {CONFIG_FILE} -s {snapshot1_id} --dry-run -y')
    assert "执行完成" in result.stdout
    assert "预演 (DRY-RUN)" in result.stdout

    result = run(f'python -m invoice_organizer apply -c {CONFIG_FILE} -s {snapshot1_id} -y')
    assert "执行完成" in result.stdout

    with open(STATE_FILE, "r", encoding="utf-8") as f:
        state = json.load(f)

    validation_history = state.get("validation_history", [])
    check_pass = [v for v in validation_history
                  if v.get("triggered_by") == "check-signoff" and v.get("status") == "passed"]
    apply_dry_pass = [v for v in validation_history
                      if v.get("triggered_by") == "apply-dry-run" and v.get("status") == "passed"]
    apply_pass = [v for v in validation_history
                  if v.get("triggered_by") == "apply" and v.get("status") == "passed"]

    assert len(check_pass) >= 1, "应有 check-signoff 通过记录"
    assert len(apply_dry_pass) >= 1, "应有 apply-dry-run 通过记录"
    assert len(apply_pass) >= 1, "应有 apply 通过记录"

    latest = validation_history[-1]
    assert latest.get("triggered_by") == "apply"
    assert latest.get("status") == "passed"
    hard_blocks = [b for b in latest.get("block_types", [])
                   if b not in ("snapshot_replaced",)]
    assert hard_blocks == [], f"不应有硬性阻塞，实际: {latest.get('block_types')}"
    assert latest.get("is_expired") == False
    assert latest.get("has_unresolved_conflict") == False
    assert latest.get("has_lock_mismatch") == False

    runs = state.get("runs", {})
    actual_runs = {k: v for k, v in runs.items() if not v.get("dry_run", False)}
    assert len(actual_runs) >= 1
    run_id = list(actual_runs.values())[-1]["id"]
    print(f"  执行 ID: {run_id}")
    print("  [OK] dry-run 和正式 apply 均通过")
    return run_id


def step_8_undo_and_refresh(snapshot1_id, run_id):
    """步骤 8：undo 后校验状态再次刷新"""
    print("\n" + "=" * 80)
    print("步骤 8：undo 后校验状态再次刷新")
    print("=" * 80)

    result = run(f'python -m invoice_organizer undo -c {CONFIG_FILE} -r {run_id} -y')
    assert "撤销成功" in result.stdout

    with open(STATE_FILE, "r", encoding="utf-8") as f:
        state = json.load(f)

    validation_history = state.get("validation_history", [])

    latest = validation_history[-1]
    assert latest.get("triggered_by") == "undo"
    assert latest.get("status") == "passed"
    assert latest.get("resolved_at") is not None and latest["resolved_at"] != ""
    assert latest.get("resolution_command") == "undo"

    json_file = EXPORT_DIR / "export_after_undo.json"
    run(f'python -m invoice_organizer export -c {CONFIG_FILE} -o {json_file} --format json')

    with open(json_file, "r", encoding="utf-8") as f:
        exported = json.load(f)

    exported_validation = exported.get("validation_history", [])
    assert len(exported_validation) == len(validation_history)

    exported_latest = exported_validation[-1]
    assert exported_latest["validation_id"] == latest["validation_id"]
    assert exported_latest["triggered_by"] == "undo"
    assert exported_latest["resolution_command"] == "undo"

    print("  [OK] undo 后校验状态已刷新，导出数据一致")


def step_9_final_verification(snapshot1_id):
    """步骤 9：最终验证 CLI 可见信息与导出内容前后对得上"""
    print("\n" + "=" * 80)
    print("步骤 9：最终验证 CLI 与导出内容一致")
    print("=" * 80)

    result = run(f'python -m invoice_organizer check-signoff -c {CONFIG_FILE} -s {snapshot1_id}')
    assert "签收校验通过" in result.stdout

    json_file = EXPORT_DIR / "export_final.json"
    csv_file = EXPORT_DIR / "export_final.csv"
    run(f'python -m invoice_organizer export -c {CONFIG_FILE} -o {json_file} --format json')
    run(f'python -m invoice_organizer export -c {CONFIG_FILE} -o {csv_file} --format csv')

    with open(json_file, "r", encoding="utf-8") as f:
        final_export = json.load(f)

    validation_history = final_export.get("validation_history", [])
    blocked_count = len([v for v in validation_history if v.get("status") == "blocked"])
    passed_count = len([v for v in validation_history if v.get("status") == "passed"])
    resolved_count = len([v for v in validation_history if v.get("resolved_at")])

    print(f"  总校验历史记录数: {len(validation_history)}")
    print(f"  阻塞记录数: {blocked_count}")
    print(f"  通过记录数: {passed_count}")
    print(f"  已解决记录数: {resolved_count}")

    assert len(validation_history) >= 8, f"校验历史记录数不足: {len(validation_history)}"
    assert blocked_count >= 3, f"阻塞记录不足: {blocked_count}"
    assert passed_count >= 4, f"通过记录不足: {passed_count}"

    has_signoff_expired = any("signoff_expired" in v.get("block_types", []) for v in validation_history)
    has_unresolved_conflict = any("unresolved_signoff_conflict" in v.get("block_types", []) for v in validation_history)
    has_lock_mismatch = any("lock_mismatch" in v.get("block_types", []) for v in validation_history)
    assert has_signoff_expired, "校验历史中应有 signoff_expired"
    assert has_unresolved_conflict, "校验历史中应有 unresolved_signoff_conflict"
    assert has_lock_mismatch, "校验历史中应有 lock_mismatch"

    with open(csv_file, "r", encoding="utf-8-sig") as f:
        csv_content = f.read()

    assert "=== 签收校验历史 ===" in csv_content
    assert "通过" in csv_content
    assert "阻塞" in csv_content
    assert "check-signoff" in csv_content
    assert "apply-dry-run" in csv_content
    assert "apply" in csv_content
    assert "resolve-signoff-conflict" in csv_content
    assert "sign-off" in csv_content
    assert "undo" in csv_content
    assert "签收过期" in csv_content
    assert "未解决签收冲突" in csv_content
    assert "锁定快照不一致" in csv_content

    print("  [OK] 最终验证通过：CLI 可见信息与导出内容完全一致")


def main():
    try:
        cleanup()
        setup_test_environment()

        snapshot1_id, snapshot2_id = step_1_setup_snapshots()

        step_2_lock_mismatch_persistence(snapshot1_id, snapshot2_id)

        conflict_id, validation_history = step_3_signoff_blocks_persistence(snapshot1_id)

        step_4_export_contains_validation(validation_history)

        step_5_restart_review(snapshot1_id, conflict_id, len(validation_history))

        step_6_resolve_blockages(snapshot1_id, conflict_id, snapshot2_id)

        run_id = step_7_dry_run_and_apply(snapshot1_id)

        step_8_undo_and_refresh(snapshot1_id, run_id)

        step_9_final_verification(snapshot1_id)

        print("\n" + "=" * 80)
        print("[OK] 全部 9 步回归测试通过！")
        print("=" * 80)
        print("\n校验持久化链路验证：")
        print("  [OK] lock_mismatch 阻塞（有效签收 + 锁定不一致）持久化")
        print("  [OK] signoff_expired + unresolved_signoff_conflict 阻塞持久化")
        print("  [OK] 时间、触发命令、快照 ID、处理前后状态完整记录")
        print("  [OK] JSON/CSV 导出包含校验历史")
        print("  [OK] 导入快照、解决冲突、重新签收、undo 后同步刷新")
        print("  [OK] 重启后校验历史完整保留，可继续追加")
        print("  [OK] CLI 可见信息与导出内容前后对得上")

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
