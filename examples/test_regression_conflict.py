"""回归测试：apply 冲突链路 - 目标已存在时只记 skipped_conflict，不再重复记 failed

覆盖场景：
1. 正常路径：无冲突时 apply 正常，状态正确
2. 冲突路径：plan 后人为制造目标文件冲突，apply 后验证：
   - CLI 输出只显示 [冲突跳过]，不显示 [失败]
   - 状态文件中每个文件只有一条记录
   - JSON/CSV 导出与状态文件一致
   - undo 只回滚实际移动的文件
   - 计数正确：成功移动=N, 冲突跳过=M, 执行失败=0
"""
import os
import sys
import shutil
import json
import csv
import subprocess

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.dirname(BASE_DIR)
sys.path.insert(0, PROJECT_DIR)


def run_cmd(cmd, cwd=PROJECT_DIR):
    """运行命令并返回结果"""
    result = subprocess.run(
        cmd,
        shell=True,
        cwd=cwd,
        capture_output=True,
        text=True,
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

    # 只保留测试需要的文件
    test_files = [
        "电子发票_20240115_001.pdf",
        "电子发票_20240116_002.pdf",
    ]

    # 删除其他文件，只保留这两个
    for f in os.listdir(sample_dir):
        fpath = os.path.join(sample_dir, f)
        if os.path.isfile(fpath) and f not in test_files:
            os.remove(fpath)

    # 确保测试文件存在
    for f in test_files:
        fpath = os.path.join(sample_dir, f)
        if not os.path.exists(fpath):
            with open(fpath, "w", encoding="utf-8") as fp:
                fp.write(f"test content for {f}")


def test_conflict_link():
    """测试冲突链路：plan 后制造冲突，apply 验证"""
    print("\n" + "="*70)
    print("[回归测试] 冲突链路：目标已存在时只记 skipped_conflict")
    print("="*70)

    clean_env()
    config = "examples/config_normal.yaml"

    # 1. scan
    result = run_cmd(f'python -m invoice_organizer scan -c {config}')
    assert result.returncode == 0, f"scan 失败: {result.stderr}"

    # 2. plan
    result = run_cmd(f'python -m invoice_organizer plan -c {config}')
    assert result.returncode == 0, f"plan 失败: {result.stderr}"

    # 3. 人为制造冲突
    archive_dir = os.path.join(BASE_DIR, "archived")
    target_file = os.path.join(archive_dir, "electronic", "电子发票_20240115_001.pdf")
    os.makedirs(os.path.dirname(target_file), exist_ok=True)
    with open(target_file, "w", encoding="utf-8") as f:
        f.write("PRE-EXISTING CONFLICT FILE")

    # 4. apply
    result = run_cmd(f'python -m invoice_organizer apply -c {config} -y -v')
    stdout = result.stdout

    # 5. 验证 CLI 输出
    assert "[冲突跳过] 电子发票_20240115_001.pdf" in stdout, \
        "CLI 应显示冲突跳过"
    assert "[失败] 电子发票_20240115_001.pdf" not in stdout, \
        "CLI 不应显示失败"
    assert "成功移动: 1" in stdout, "应显示成功移动: 1"
    assert "冲突跳过: 1" in stdout, "应显示冲突跳过: 1"
    assert "执行失败: 0" in stdout or "执行失败" not in stdout, \
        "执行失败应为 0"
    print("  [OK] CLI 输出正确：无重复记录，计数正确")

    # 6. 验证状态文件
    state_file = os.path.join(BASE_DIR, ".state", "invoice_state.json")
    with open(state_file, "r", encoding="utf-8") as f:
        state = json.load(f)

    runs = state.get("runs", {})
    assert len(runs) == 1, "应有 1 次执行"

    run_id = list(runs.keys())[0]
    run = runs[run_id]
    moves = run.get("moves", [])

    # 检查每个文件只有一条记录
    statuses_per_file = {}
    for m in moves:
        fname = m["filename"]
        if fname not in statuses_per_file:
            statuses_per_file[fname] = []
        statuses_per_file[fname].append(m["status"])

    for fname, statuses in statuses_per_file.items():
        assert len(statuses) == 1, f"{fname} 应有 1 条记录，实际有 {len(statuses)}: {statuses}"

    assert statuses_per_file["电子发票_20240115_001.pdf"] == ["skipped_conflict"], \
        "冲突文件状态应为 skipped_conflict"
    assert statuses_per_file["电子发票_20240116_002.pdf"] == ["moved"], \
        "正常文件状态应为 moved"
    print("  [OK] 状态文件正确：每个文件只有一条记录，状态正确")

    # 7. 验证 JSON 导出
    export_json = os.path.join(BASE_DIR, "regression_conflict.json")
    result = run_cmd(f'python -m invoice_organizer export -c {config} -o examples/regression_conflict.json -f json')
    assert result.returncode == 0, "JSON 导出失败"

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

    assert export_statuses == statuses_per_file, \
        f"JSON 导出与状态文件不一致: {export_statuses} vs {statuses_per_file}"
    print("  [OK] JSON 导出与状态文件一致")

    # 8. 验证 CSV 导出
    export_csv = os.path.join(BASE_DIR, "regression_conflict.csv")
    result = run_cmd(f'python -m invoice_organizer export -c {config} -o examples/regression_conflict.csv -f csv')
    assert result.returncode == 0, "CSV 导出失败"

    csv_statuses = {}
    with open(export_csv, "r", encoding="utf-8-sig") as f:
        reader = csv.reader(f)
        in_exec = False
        for row in reader:
            if not row:
                continue
            if row[0] == "=== 执行记录 ===":
                in_exec = True
                continue
            if row[0].startswith("==="):
                in_exec = False
                continue
            if in_exec and len(row) >= 9 and row[0] != "执行ID":
                fname = row[6]
                status = row[8]
                if fname not in csv_statuses:
                    csv_statuses[fname] = []
                csv_statuses[fname].append(status)

    assert csv_statuses == statuses_per_file, \
        f"CSV 导出与状态文件不一致: {csv_statuses} vs {statuses_per_file}"
    print("  [OK] CSV 导出与状态文件一致")

    # 9. 验证 undo 只回滚实际移动的
    result = run_cmd(f'python -m invoice_organizer undo -c {config} -r {run_id} -y')
    assert result.returncode == 0, "undo 失败"
    assert "已恢复 1 个文件" in result.stdout, "undo 应只恢复 1 个文件"
    print("  [OK] undo 只回滚了实际移动的 1 个文件")

    # 10. 验证冲突文件还在（不是我们移动的，不应该被动）
    with open(target_file, "r", encoding="utf-8") as f:
        content = f.read()
    assert content == "PRE-EXISTING CONFLICT FILE", \
        "预先存在的冲突文件不应被改动"
    print("  [OK] 预先存在的冲突文件未被改动")

    print("\n[通过] 冲突链路回归测试全部通过")
    return True


def test_normal_path():
    """测试正常路径：无冲突时 apply 正常"""
    print("\n" + "="*70)
    print("[回归测试] 正常路径：无冲突时 apply 正常")
    print("="*70)

    clean_env()
    config = "examples/config_normal.yaml"

    # 1. scan + plan + apply（无冲突）
    run_cmd(f'python -m invoice_organizer scan -c {config}')
    run_cmd(f'python -m invoice_organizer plan -c {config}')
    result = run_cmd(f'python -m invoice_organizer apply -c {config} -y -v')

    stdout = result.stdout

    assert "成功移动: 2" in stdout, f"应成功移动 2 个文件，实际输出: {stdout}"
    # 计数为 0 时 CLI 不显示该行，所以只要没显示>0的就行
    assert "冲突跳过: " not in stdout or "冲突跳过: 0" in stdout, \
        f"应无冲突跳过，实际输出: {stdout}"
    assert "执行失败" not in stdout, \
        f"应无执行失败，实际输出: {stdout}"
    assert "人工跳过" not in stdout, \
        f"应无人工跳过，实际输出: {stdout}"

    # 检查状态
    state_file = os.path.join(BASE_DIR, ".state", "invoice_state.json")
    with open(state_file, "r", encoding="utf-8") as f:
        state = json.load(f)

    runs = state.get("runs", {})
    run_id = list(runs.keys())[0]
    moves = runs[run_id].get("moves", [])

    statuses = [m["status"] for m in moves]
    assert all(s == "moved" for s in statuses), "所有文件状态应为 moved"
    assert len(statuses) == 2, "应有 2 条记录"

    # undo
    run_cmd(f'python -m invoice_organizer undo -c {config} -r {run_id} -y')
    print("  [OK] 正常路径通过")
    return True


def test_plan_time_conflict():
    """测试 plan 阶段就检测到的冲突（目标已存在）"""
    print("\n" + "="*70)
    print("[回归测试] plan 阶段冲突：apply 时正确处理")
    print("="*70)

    clean_env()
    config = "examples/config_normal.yaml"

    # scan 后先制造冲突，再 plan（这样 plan 阶段就能检测到）
    archive_dir = os.path.join(BASE_DIR, "archived")
    target_file = os.path.join(archive_dir, "electronic", "电子发票_20240115_001.pdf")
    os.makedirs(os.path.dirname(target_file), exist_ok=True)
    with open(target_file, "w", encoding="utf-8") as f:
        f.write("PRE-EXISTING CONFLICT FILE")

    run_cmd(f'python -m invoice_organizer scan -c {config}')
    result = run_cmd(f'python -m invoice_organizer plan -c {config} -v')
    assert "冲突项 (1 个)" in result.stdout, f"plan 应检测到 1 个冲突，实际输出: {result.stdout}"

    # apply
    result = run_cmd(f'python -m invoice_organizer apply -c {config} -y -v')
    stdout = result.stdout

    assert "成功移动: 1" in stdout, "应成功移动 1 个"
    assert "冲突跳过: 1" in stdout, "应冲突跳过 1 个"
    assert "[失败]" not in stdout, "不应有失败"

    # 检查状态文件
    state_file = os.path.join(BASE_DIR, ".state", "invoice_state.json")
    with open(state_file, "r", encoding="utf-8") as f:
        state = json.load(f)

    runs = state.get("runs", {})
    run_id = list(runs.keys())[0]
    moves = runs[run_id].get("moves", [])

    statuses_per_file = {}
    for m in moves:
        fname = m["filename"]
        if fname not in statuses_per_file:
            statuses_per_file[fname] = []
        statuses_per_file[fname].append(m["status"])

    for fname, statuses in statuses_per_file.items():
        assert len(statuses) == 1, f"{fname} 不应有重复记录"

    assert statuses_per_file["电子发票_20240115_001.pdf"] == ["skipped_conflict"]
    assert statuses_per_file["电子发票_20240116_002.pdf"] == ["moved"]

    print("  [OK] plan 阶段冲突也正确处理")
    return True


def main():
    try:
        test_normal_path()
        test_plan_time_conflict()
        test_conflict_link()
        print("\n" + "="*70)
        print("  所有回归测试通过！")
        print("="*70)
        return 0
    except AssertionError as e:
        print(f"\n[X] 断言失败: {e}", file=sys.stderr)
        return 1
    except Exception as e:
        print(f"\n[X] 异常: {e}", file=sys.stderr)
        import traceback
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())
