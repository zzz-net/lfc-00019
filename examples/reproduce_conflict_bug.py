"""稳定复现冲突链路问题：同一个文件同时被记成 skipped_conflict 和 failed"""
import os
import sys
import shutil
import json

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.dirname(BASE_DIR)
sys.path.insert(0, PROJECT_DIR)

CONFIG_PATH = os.path.join(BASE_DIR, "config_normal.yaml")
SAMPLE_DIR = os.path.join(BASE_DIR, "sample_invoices")
ARCHIVE_DIR = os.path.join(BASE_DIR, "archived")
STATE_DIR = os.path.join(BASE_DIR, ".state")


def clean_env():
    """清理测试环境"""
    print("=== 清理环境 ===")
    if os.path.exists(STATE_DIR):
        shutil.rmtree(STATE_DIR)
    if os.path.exists(ARCHIVE_DIR):
        shutil.rmtree(ARCHIVE_DIR)
    os.makedirs(SAMPLE_DIR, exist_ok=True)

    # 重新创建测试文件
    test_files = [
        "电子发票_20240115_001.pdf",
        "电子发票_20240116_002.pdf",
    ]
    for f in test_files:
        fpath = os.path.join(SAMPLE_DIR, f)
        if not os.path.exists(fpath):
            with open(fpath, "w", encoding="utf-8") as fp:
                fp.write(f"test content for {f}")
    print(f"  清理完成，测试文件: {test_files}")


def run_cmd(cmd):
    """运行命令并返回结果"""
    import subprocess
    print(f"\n$ {cmd}")
    result = subprocess.run(
        cmd,
        shell=True,
        cwd=PROJECT_DIR,
        capture_output=True,
        text=True,
    )
    print(result.stdout)
    if result.stderr:
        print("STDERR:", result.stderr, file=sys.stderr)
    return result


def main():
    clean_env()

    # 1. scan
    print("\n" + "="*70)
    print("[步骤 1] 扫描文件")
    print("="*70)
    result = run_cmd('python -m invoice_organizer scan -c examples/config_normal.yaml')
    assert result.returncode == 0, "scan 失败"

    # 2. plan
    print("\n" + "="*70)
    print("[步骤 2] 生成预案")
    print("="*70)
    result = run_cmd('python -m invoice_organizer plan -c examples/config_normal.yaml -v')
    assert result.returncode == 0, "plan 失败"

    # 3. 关键：人为制造冲突 - 在目标目录预先放一个同名文件
    print("\n" + "="*70)
    print("[步骤 3] 人为制造冲突：预先在目标目录创建同名文件")
    print("="*70)
    target_file = os.path.join(ARCHIVE_DIR, "electronic", "电子发票_20240115_001.pdf")
    os.makedirs(os.path.dirname(target_file), exist_ok=True)
    with open(target_file, "w", encoding="utf-8") as f:
        f.write("PRE-EXISTING CONFLICT FILE")
    print(f"  已预先创建冲突文件: {target_file}")
    print(f"  内容: 'PRE-EXISTING CONFLICT FILE'")
    print(f"  大小: {os.path.getsize(target_file)} bytes")

    # 4. apply - 这一步应该触发 bug
    print("\n" + "="*70)
    print("[步骤 4] 执行 apply（应触发冲突 bug）")
    print("="*70)
    result = run_cmd('python -m invoice_organizer apply -c examples/config_normal.yaml -y -v')
    # 即使有内部错误，CLI 也可能返回 0，所以不检查 returncode

    # 5. 检查状态文件中的记录
    print("\n" + "="*70)
    print("[步骤 5] 检查状态文件中的执行记录")
    print("="*70)
    state_file = os.path.join(STATE_DIR, "invoice_state.json")
    with open(state_file, "r", encoding="utf-8") as f:
        state = json.load(f)

    runs = state.get("runs", {})
    assert len(runs) == 1, f"应有 1 次执行记录，实际有 {len(runs)}"

    run_id = list(runs.keys())[0]
    run = runs[run_id]
    moves = run.get("moves", [])

    print(f"  Run ID: {run_id}")
    print(f"  执行记录数: {len(moves)}")
    print(f"\n  各文件的状态记录:")
    statuses_per_file = {}
    for m in moves:
        fname = m["filename"]
        status = m["status"]
        err = m.get("error_message", "")
        if fname not in statuses_per_file:
            statuses_per_file[fname] = []
        statuses_per_file[fname].append((status, err))
        print(f"    - {fname}: {status} | {err}")

    # 提取纯状态用于比较
    pure_statuses_per_file = {
        fname: [s[0] for s in statuses]
        for fname, statuses in statuses_per_file.items()
    }

    # 6. 验证问题：同一个文件是否有两条记录
    print("\n" + "="*70)
    print("[步骤 6] 验证重复记录问题")
    print("="*70)
    bug_found = False
    for fname, statuses in statuses_per_file.items():
        print(f"  {fname}: {len(statuses)} 条记录 -> {[s[0] for s in statuses]}")
        if len(statuses) > 1:
            print(f"    [X] 发现重复记录！")
            bug_found = True
            for s in statuses:
                print(f"       - {s[0]}: {s[1]}")

    # 检查冲突文件的具体情况
    conflict_file = "电子发票_20240115_001.pdf"
    if conflict_file in statuses_per_file:
        statuses = statuses_per_file[conflict_file]
        status_types = [s[0] for s in statuses]
        print(f"\n  冲突文件 '{conflict_file}' 的状态: {status_types}")
        if "skipped_conflict" in status_types and "failed" in status_types:
            print("    [X] 确认 Bug：同时有 skipped_conflict 和 failed 两条记录！")
            bug_found = True
        elif len(statuses) == 1 and statuses[0][0] == "skipped_conflict":
            print("    [OK] 正确：只有一条 skipped_conflict 记录")
        else:
            print(f"    [WARN] 异常状态: {status_types}")

    # 7. 检查正常文件
    normal_file = "电子发票_20240116_002.pdf"
    if normal_file in statuses_per_file:
        statuses = statuses_per_file[normal_file]
        print(f"\n  正常文件 '{normal_file}' 的状态: {[s[0] for s in statuses]}")
        if len(statuses) == 1 and statuses[0][0] == "moved":
            print("    [OK] 正确：只有一条 moved 记录")
        else:
            print(f"    [WARN] 异常状态: {[s[0] for s in statuses]}")

    # 8. 导出并检查 JSON
    print("\n" + "="*70)
    print("[步骤 7] 导出 JSON 并检查")
    print("="*70)
    export_json = os.path.join(BASE_DIR, "conflict_bug_export.json")
    result = run_cmd(f'python -m invoice_organizer export -c examples/config_normal.yaml -o examples/conflict_bug_export.json -f json')

    with open(export_json, "r", encoding="utf-8") as f:
        export_data = json.load(f)

    export_moves = []
    for rid, r in export_data.get("runs", {}).items():
        export_moves.extend(r.get("moves", []))

    export_statuses = {}
    for m in export_moves:
        fname = m["filename"]
        if fname not in export_statuses:
            export_statuses[fname] = []
        export_statuses[fname].append(m["status"])

    print(f"\n  导出 JSON 中的状态:")
    for fname, statuses in export_statuses.items():
        print(f"    {fname}: {statuses}")

    # 验证 JSON 导出和状态文件一致
    assert export_statuses == pure_statuses_per_file, \
        f"JSON 导出与状态文件不一致: {export_statuses} vs {pure_statuses_per_file}"
    print("  [OK] JSON 导出与状态文件一致")

    # 9. 导出并检查 CSV
    print("\n" + "="*70)
    print("[步骤 8] 导出 CSV 并检查")
    print("="*70)
    export_csv = os.path.join(BASE_DIR, "conflict_bug_export.csv")
    result = run_cmd(f'python -m invoice_organizer export -c examples/config_normal.yaml -o examples/conflict_bug_export.csv -f csv')

    # 读取 CSV 检查
    import csv
    csv_statuses = {}
    with open(export_csv, "r", encoding="utf-8-sig") as f:
        reader = csv.reader(f)
        in_execution_section = False
        for row in reader:
            if not row:
                continue
            if row[0] == "=== 执行记录 ===":
                in_execution_section = True
                continue
            if row[0].startswith("==="):
                in_execution_section = False
                continue
            if in_execution_section and len(row) >= 9 and row[0] != "执行ID":
                fname = row[6]
                status = row[8]
                if fname not in csv_statuses:
                    csv_statuses[fname] = []
                csv_statuses[fname].append(status)

    print(f"\n  导出 CSV 中的状态:")
    for fname, statuses in csv_statuses.items():
        print(f"    {fname}: {statuses}")

    # 验证 CSV 导出也一致
    assert csv_statuses == pure_statuses_per_file, \
        f"CSV 导出与状态文件不一致: {csv_statuses} vs {pure_statuses_per_file}"
    print("  [OK] CSV 导出与状态文件一致")

    # 10. 验证 CLI 输出中的计数
    print("\n" + "="*70)
    print("[步骤 9] 验证 CLI 输出计数正确")
    print("="*70)
    # 重新执行一次 apply 捕获输出（但要先 undo 回滚）
    undo_result = run_cmd(f'python -m invoice_organizer undo -c examples/config_normal.yaml -r {run_id} -y')
    assert undo_result.returncode == 0, "undo 失败"

    # 重新制造冲突（因为 undo 会移动回来，但冲突文件还在）
    # 检查冲突文件还在
    assert os.path.exists(target_file), "冲突文件应该还在"

    # 重新 apply
    apply_result = run_cmd(f'python -m invoice_organizer apply -c examples/config_normal.yaml -y')
    assert "成功移动: 1" in apply_result.stdout, "CLI 应显示成功移动: 1"
    assert "冲突跳过: 1" in apply_result.stdout, "CLI 应显示冲突跳过: 1"
    assert "执行失败" not in apply_result.stdout, "CLI 不应显示执行失败"
    print("  [OK] CLI 输出计数正确: 成功移动=1, 冲突跳过=1, 无执行失败")

    if bug_found:
        print("\n" + "="*70)
        print("[X] Bug 复现成功！同一个文件同时有 skipped_conflict 和 failed 记录")
        print("="*70)
        return False
    else:
        print("\n" + "="*70)
        print("[OK] 未发现重复记录问题，三方（CLI/状态文件/导出）完全一致")
        print("="*70)
        return True


if __name__ == "__main__":
    success = main()
    sys.exit(0 if success else 1)
