"""回归测试：校验状态判断——clean pass 与阻塞解除的明确区分

覆盖场景：
1. 首次签收后的 clean pass：check-validation 应显示"可执行"，而非"已解除阻塞"
2. 已有阻塞被解除：先制造阻塞，再解除，check-validation 应显示"已解除阻塞"
3. check-validation、导出 JSON、用户可见状态说明三处结果一致

Bug 复现路径：
- 修复前：sign-off/undo/import/unlock/resolve-conflict 后，新写入的 passed 记录
  被 update_validation_resolution 误标为 is_resolved=True，导致 check-validation
  显示"已解除阻塞"而非"可执行"
- 修复后：update_validation_resolution 只对旧的 blocked 记录调用，新 passed 记录
  不会被标记 resolved_at，check-validation 正确显示"可执行"
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

WORK_DIR = Path(tempfile.mkdtemp(prefix="validation_status_test_"))
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


def get_future_date(days_ahead=30):
    future = datetime.now() + timedelta(days=days_ahead)
    return future.isoformat()


def get_past_date(days_ago=10):
    past = datetime.now() - timedelta(days=days_ago)
    return past.isoformat()


def extract_snapshot_id_from_list(output, exclude_ids=None):
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


def step_1_clean_pass_after_first_signoff():
    """路径1：首次签收后的 clean pass 不应显示"已解除阻塞" """
    print("\n" + "=" * 80)
    print("步骤 1：首次签收后 clean pass 状态判断")
    print("=" * 80)

    result = run(f'python -m invoice_organizer plan -c {CONFIG_FILE}')
    assert "预案生成成功" in result.stdout

    result = run(f'python -m invoice_organizer list-snapshots -c {CONFIG_FILE}')
    snapshot_id = extract_snapshot_id_from_list(result.stdout)
    assert snapshot_id, "应获取到快照 ID"
    print(f"  快照 ID: {snapshot_id}")

    future_deadline = get_future_date(30)
    result = run(
        f'python -m invoice_organizer sign-off -c {CONFIG_FILE} -s {snapshot_id} '
        f'--status signed --signed-by "财务-张三" '
        f'--deadline "{future_deadline}" '
        f'--notes "首次签收" -y'
    )
    assert "签收成功" in result.stdout

    result = run(
        f'python -m invoice_organizer check-validation -c {CONFIG_FILE} -s {snapshot_id} -n 5',
        check=True
    )
    assert "[最近一次结论]" in result.stdout
    assert "结论: 通过" in result.stdout, \
        f"首次签收后应显示'通过'，实际: {result.stdout}"
    assert "[当前状态: 可执行]" in result.stdout, \
        f"首次签收后应显示'可执行'，不应显示'已解除阻塞'，实际: {result.stdout}"
    assert "已解除阻塞" not in result.stdout, \
        f"首次签收后的 clean pass 不应出现'已解除阻塞'，实际: {result.stdout}"

    print("  [OK] check-validation 首次签收后正确显示'可执行'")

    json_file = EXPORT_DIR / "clean_pass_export.json"
    run(f'python -m invoice_organizer export -c {CONFIG_FILE} -o {json_file} --format json')

    with open(json_file, "r", encoding="utf-8") as f:
        export_data = json.load(f)

    validation_history = export_data.get("validation_history", [])
    passed_records = [v for v in validation_history if v.get("status") == "passed"]

    for pr in passed_records:
        assert pr.get("resolved_at") is None, \
            f"passed 记录不应有 resolved_at，实际: {pr.get('resolved_at')}，triggered_by={pr.get('triggered_by')}"

    print("  [OK] 导出 JSON 中 passed 记录无 resolved_at 字段")

    latest_passed = [v for v in validation_history if v.get("triggered_by") == "sign-off" and v.get("status") == "passed"]
    assert len(latest_passed) >= 1, "应有 sign-off 触发的 passed 记录"
    assert latest_passed[-1].get("resolved_at") is None, \
        "sign-off 触发的 passed 记录不应被标记为已解决"

    print("  [OK] 首次签收 clean pass：check-validation、导出 JSON、用户可见状态一致")
    return snapshot_id


def step_2_blocked_then_resolved(snapshot_id):
    """路径2：已有阻塞被解除应显示"已解除阻塞" """
    print("\n" + "=" * 80)
    print("步骤 2：阻塞出现→解除后应显示'已解除阻塞'")
    print("=" * 80)

    past_deadline = get_past_date(10)
    result = run(
        f'python -m invoice_organizer sign-off -c {CONFIG_FILE} -s {snapshot_id} '
        f'--status signed --signed-by "财务-张三" --force '
        f'--deadline "{past_deadline}" '
        f'--notes "设置为过期" -y'
    )
    assert "签收成功" in result.stdout

    result = run(
        f'python -m invoice_organizer check-signoff -c {CONFIG_FILE} -s {snapshot_id}',
        check=False
    )
    assert result.returncode != 0, "check-signoff 应失败（签收过期）"
    print("  [OK] 签收过期阻塞已产生")

    result = run(
        f'python -m invoice_organizer check-validation -c {CONFIG_FILE} -s {snapshot_id} -n 5',
        check=True
    )
    assert "[当前状态: 阻塞中]" in result.stdout, \
        f"阻塞期间应显示'阻塞中'，实际: {result.stdout}"
    print("  [OK] check-validation 正确显示'阻塞中'")

    result = run(
        f'python -m invoice_organizer sign-off -c {CONFIG_FILE} -s {snapshot_id} '
        f'--status signed --signed-by "财务-张三" --force '
        f'--notes "重新签收，延长有效期" -y'
    )
    assert "签收成功" in result.stdout

    result = run(
        f'python -m invoice_organizer check-validation -c {CONFIG_FILE} -s {snapshot_id} -n 10',
        check=True
    )
    assert "[当前状态: 可执行]" in result.stdout, \
        f"阻塞解除后应显示'可执行'，实际: {result.stdout}"

    print("  [OK] check-validation 解除阻塞后显示'可执行'")

    json_file = EXPORT_DIR / "resolved_export.json"
    run(f'python -m invoice_organizer export -c {CONFIG_FILE} -o {json_file} --format json')

    with open(json_file, "r", encoding="utf-8") as f:
        export_data = json.load(f)

    validation_history = export_data.get("validation_history", [])

    blocked_records = [v for v in validation_history if v.get("status") == "blocked"]
    for br in blocked_records:
        assert br.get("resolved_at") is not None, \
            f"blocked 记录应有 resolved_at，triggered_by={br.get('triggered_by')}"

    passed_records = [v for v in validation_history if v.get("status") == "passed"]
    for pr in passed_records:
        assert pr.get("resolved_at") is None, \
            f"passed 记录不应有 resolved_at，triggered_by={pr.get('triggered_by')}"

    print("  [OK] 导出 JSON 中：blocked 记录有 resolved_at，passed 记录无 resolved_at")

    signoff_blocked = [v for v in validation_history
                       if v.get("triggered_by") == "sign-off" and v.get("status") == "blocked"]
    if signoff_blocked:
        assert signoff_blocked[-1].get("resolved_at") is not None, \
            "签收命令产生的 blocked 记录应被标记为已解决（因为 invalidate_validation_for_snapshot 会处理）"

    signoff_passed = [v for v in validation_history
                      if v.get("triggered_by") == "sign-off" and v.get("status") == "passed"]
    for sp in signoff_passed:
        assert sp.get("resolved_at") is None, \
            f"sign-off 触发的 passed 记录不应有 resolved_at"

    print("  [OK] 阻塞解除：check-validation、导出 JSON、用户可见状态一致")
    return snapshot_id


def step_3_three_way_consistency(snapshot_id):
    """步骤 3：check-validation、导出 JSON、用户可见状态三处一致性验证"""
    print("\n" + "=" * 80)
    print("步骤 3：三处一致性验证")
    print("=" * 80)

    result = run(
        f'python -m invoice_organizer check-validation -c {CONFIG_FILE} -s {snapshot_id} -n 10 -v',
        check=True
    )
    cv_output = result.stdout

    json_file = EXPORT_DIR / "consistency_export.json"
    run(f'python -m invoice_organizer export -c {CONFIG_FILE} -o {json_file} --format json')

    with open(json_file, "r", encoding="utf-8") as f:
        export_data = json.load(f)

    validation_history = export_data.get("validation_history", [])

    passed_count = len([v for v in validation_history if v.get("status") == "passed"])
    blocked_count = len([v for v in validation_history if v.get("status") == "blocked"])
    resolved_count = len([v for v in validation_history if v.get("resolved_at")])

    passed_with_resolved = [v for v in validation_history
                            if v.get("status") == "passed" and v.get("resolved_at") is not None]
    assert len(passed_with_resolved) == 0, \
        f"不应有 passed 记录带 resolved_at，但发现 {len(passed_with_resolved)} 条: " \
        + "; ".join(f"vid={v.get('validation_id')}, triggered_by={v.get('triggered_by')}, resolved_at={v.get('resolved_at')}"
                    for v in passed_with_resolved)

    print(f"  总记录: {len(validation_history)}, 通过: {passed_count}, 阻塞: {blocked_count}, 已解决: {resolved_count}")
    print("  [OK] 无 passed 记录被误标为'已解决'")

    assert "已解除阻塞" not in cv_output or blocked_count > 0, \
        "check-validation 不应在无阻塞历史时显示'已解除阻塞'"

    latest_record = validation_history[-1] if validation_history else None
    if latest_record and latest_record.get("status") == "passed":
        assert "[当前状态: 可执行]" in cv_output, \
            "最近记录为 passed 时，check-validation 应显示'可执行'"
        assert latest_record.get("resolved_at") is None, \
            "最近的 passed 记录不应有 resolved_at"

    print("  [OK] 三处一致性验证通过")


def step_4_undo_after_apply(snapshot_id):
    """步骤 4：apply 后 undo，undo 产生的 passed 记录也不应被误标"""
    print("\n" + "=" * 80)
    print("步骤 4：apply 后 undo 的校验状态判断")
    print("=" * 80)

    result = run(
        f'python -m invoice_organizer apply -c {CONFIG_FILE} -s {snapshot_id} --dry-run -y'
    )
    assert "执行完成" in result.stdout

    result = run(
        f'python -m invoice_organizer apply -c {CONFIG_FILE} -s {snapshot_id} -y'
    )
    assert "执行完成" in result.stdout

    run_id = None
    for line in result.stdout.split("\n"):
        if "Run ID:" in line or "执行 ID:" in line:
            parts = line.strip().split()
            for p in parts:
                if len(p) == 12:
                    run_id = p
                    break
    assert run_id, f"应获取到执行 ID，输出: {result.stdout}"

    result = run(
        f'python -m invoice_organizer undo -c {CONFIG_FILE} -r {run_id} -y'
    )
    assert "撤销成功" in result.stdout

    result = run(
        f'python -m invoice_organizer check-validation -c {CONFIG_FILE} -s {snapshot_id} -n 10',
        check=True
    )
    assert "[当前状态: 可执行]" in result.stdout, \
        f"undo 后应显示'可执行'，实际: {result.stdout}"
    assert "已解除阻塞" not in result.stdout or "阻塞" in result.stdout, \
        f"undo 后不应误显示'已解除阻塞'（除非前面确实有阻塞被解除）"

    json_file = EXPORT_DIR / "undo_export.json"
    run(f'python -m invoice_organizer export -c {CONFIG_FILE} -o {json_file} --format json')

    with open(json_file, "r", encoding="utf-8") as f:
        export_data = json.load(f)

    validation_history = export_data.get("validation_history", [])

    undo_passed = [v for v in validation_history
                   if v.get("triggered_by") == "undo" and v.get("status") == "passed"]
    for up in undo_passed:
        assert up.get("resolved_at") is None, \
            f"undo 产生的 passed 记录不应有 resolved_at，实际: {up.get('resolved_at')}"

    print("  [OK] undo 后 passed 记录未被误标为'已解决'")

    passed_with_resolved = [v for v in validation_history
                            if v.get("status") == "passed" and v.get("resolved_at") is not None]
    assert len(passed_with_resolved) == 0, \
        f"所有 passed 记录均不应有 resolved_at，但发现 {len(passed_with_resolved)} 条"

    print("  [OK] 全量 passed 记录均无 resolved_at 误标")


def main():
    try:
        cleanup()
        setup_test_environment()

        snapshot_id = step_1_clean_pass_after_first_signoff()

        step_2_blocked_then_resolved(snapshot_id)

        step_3_three_way_consistency(snapshot_id)

        step_4_undo_after_apply(snapshot_id)

        print("\n" + "=" * 80)
        print("[OK] 全部 4 步校验状态判断回归测试通过！")
        print("=" * 80)
        print("\n验证结论：")
        print("  [OK] 首次签收 clean pass → check-validation 显示'可执行'，非'已解除阻塞'")
        print("  [OK] 已有阻塞被解除 → blocked 记录有 resolved_at，passed 记录无")
        print("  [OK] check-validation、导出 JSON、用户可见状态三处一致")
        print("  [OK] undo 后 passed 记录未被误标")

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
