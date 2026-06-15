"""回归测试：reviewer_lockseq 旧 state 场景完整链路

验证点：
1. 旧 state 无 scan_config 时自动重扫，不漏 notes.md / .txt / .jpg
2. diff-plans 能看到版本差异（文件去向、规则增删改）
3. export 的 JSON/CSV 都有锁定、违规、差异字段
4. 重启后再导出也一致
5. 锁定后 apply 只认指定快照
6. undo 只回滚实际移动项
"""
import os
import sys
import json
import csv
import subprocess
import shutil

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EXAMPLES_DIR = os.path.join(BASE, "examples")
REVIEW_DIR = os.path.join(EXAMPLES_DIR, "reviewer_lockseq")
SOURCE_DIR = os.path.join(REVIEW_DIR, "source")
DEST_DIR = os.path.join(REVIEW_DIR, "dest")
STATE_DIR = os.path.join(REVIEW_DIR, ".state")
OUTPUTS_DIR = os.path.join(REVIEW_DIR, "outputs")
CONFIG_A = "examples/reviewer_lockseq/config_a.yaml"
CONFIG_B = "examples/reviewer_lockseq/config_b.yaml"
STATE_FILE = os.path.join(STATE_DIR, "review_state.json")

SOURCE_FILES = [
    "2024年1月增值税专用发票_A001.pdf",
    "2024年2月增值税普通发票_B002.pdf",
    "出差报销单_202401.xlsx",
    "发票扫描件_出租车发票.png",
    "发票照片_餐厅发票.jpg",
    "电子发票_20240115_001.pdf",
    "采购合同_供应商A.docx",
    "未分类的文档.txt",
    "notes.md",
]

passed = 0
failed = 0


def run(cmd, expect_exit=0):
    result = subprocess.run(cmd, shell=True, capture_output=True, text=True, cwd=BASE)
    exit_ok = result.returncode == expect_exit
    if not exit_ok:
        print(f"    [返回码: {result.returncode}] 期望: {expect_exit}")
    return result, exit_ok


def check(label, condition, detail=""):
    global passed, failed
    if condition:
        print(f"  [验证通过] {label}")
        passed += 1
    else:
        print(f"  [验证失败] {label} {detail}")
        failed += 1


def reset_env():
    if os.path.exists(DEST_DIR):
        for d in os.listdir(DEST_DIR):
            p = os.path.join(DEST_DIR, d)
            if os.path.isdir(p):
                shutil.rmtree(p)
            else:
                os.remove(p)
    os.makedirs(os.path.join(DEST_DIR, "vat_special"), exist_ok=True)
    with open(os.path.join(DEST_DIR, "vat_special", "2024年1月增值税专用发票_A001.pdf"), "w", encoding="utf-8") as f:
        f.write("conflict")
    for fname in SOURCE_FILES:
        p = os.path.join(SOURCE_DIR, fname)
        if not os.path.exists(p):
            with open(p, "w", encoding="utf-8") as f:
                f.write("test")
    if os.path.exists(STATE_DIR):
        shutil.rmtree(STATE_DIR)
    os.makedirs(STATE_DIR, exist_ok=True)
    if os.path.exists(OUTPUTS_DIR):
        shutil.rmtree(OUTPUTS_DIR)
    os.makedirs(OUTPUTS_DIR, exist_ok=True)


def step(num, title):
    print(f"\n{'='*70}\n[步骤 {num}] {title}\n{'='*70}")


# ===== 初始化 =====
reset_env()

# ===== 步骤 1: 第一次 plan (config_a) =====
step(1, "第一次 plan (config_a)")
r, ok = run(f'python -m invoice_organizer plan -c "{CONFIG_A}" -v')
print(f"$ python -m invoice_organizer plan -c config_a.yaml -v")
print(r.stdout)
snapshot_a = None
plan_a = None
for line in r.stdout.splitlines():
    if "批次快照] ID:" in line:
        snapshot_a = line.split("ID:")[-1].strip()
    if "预案生成成功] ID:" in line:
        plan_a = line.split("ID:")[-1].strip()
check("第一次 plan 成功", ok and snapshot_a and plan_a, f"snapshot={snapshot_a}, plan={plan_a}")
check("扫描文件总数 7 (config_a 不含 txt/md)", "扫描文件总数: 7" in r.stdout)

# ===== 步骤 2: 第二次 plan (config_b) - 旧 state 无 scan_config =====
step(2, "第二次 plan (config_b) - 应检测到旧 state 无配置快照并重扫")
r, ok = run(f'python -m invoice_organizer plan -c "{CONFIG_B}" -v')
print(f"$ python -m invoice_organizer plan -c config_b.yaml -v")
print(r.stdout)
snapshot_b = None
plan_b = None
for line in r.stdout.splitlines():
    if "批次快照] ID:" in line:
        snapshot_b = line.split("ID:")[-1].strip()
    if "预案生成成功] ID:" in line:
        plan_b = line.split("ID:")[-1].strip()
check("第二次 plan 成功", ok and snapshot_b and plan_b)
check("自动重扫触发 (配置变化或旧版无快照)", "旧版状态文件无配置快照" in r.stdout or "检测到配置变化" in r.stdout)
check("扫描文件总数 9 (config_b 含 txt/md)", "扫描文件总数: 9" in r.stdout)
check("移动计划 9 条 (含 notes.md / .txt / .jpg)", "移动计划: 9 条" in r.stdout)
check("notes.md 在结果中", "notes.md" in r.stdout)
check("未分类的文档.txt 在结果中", "未分类的文档.txt" in r.stdout)
check("发票照片_餐厅发票.jpg 在结果中", "发票照片_餐厅发票.jpg" in r.stdout)
check("发票照片匹配新规则名'发票图片'", "发票图片]" in r.stdout and "发票照片_餐厅发票.jpg" in r.stdout)

# ===== 步骤 3: diff-plans 对比 =====
step(3, "diff-plans 对比两版预案")
r, ok = run(f'python -m invoice_organizer diff-plans -c "{CONFIG_A}" --old-snapshot {snapshot_a} --new-snapshot {snapshot_b} -v')
print(f"$ python -m invoice_organizer diff-plans --old-snapshot {snapshot_a} --new-snapshot {snapshot_b} -v")
print(r.stdout)
check("diff-plans 成功", ok)
check("新增移动项 2 (notes.md + .txt)", "新增: 2" in r.stdout)
check("目标路径变化", "目标路径变化:" in r.stdout)
check("规则增删改", "新增规则" in r.stdout or "删除规则" in r.stdout or "修改规则" in r.stdout)

# ===== 步骤 4: diff-plans 导出 JSON + CSV =====
step(4, "diff-plans 导出 JSON + CSV (保存到状态)")
diff_json = os.path.join(OUTPUTS_DIR, "diff.json")
diff_csv = os.path.join(OUTPUTS_DIR, "diff.csv")
r1, ok1 = run(f'python -m invoice_organizer diff-plans -c "{CONFIG_A}" --old-snapshot {snapshot_a} --new-snapshot {snapshot_b} -o "{diff_json}" -f json --save')
r2, ok2 = run(f'python -m invoice_organizer diff-plans -c "{CONFIG_A}" --old-snapshot {snapshot_a} --new-snapshot {snapshot_b} -o "{diff_csv}" -f csv')
check("差异 JSON 导出", ok1 and os.path.exists(diff_json))
check("差异 CSV 导出", ok2 and os.path.exists(diff_csv))
if os.path.exists(diff_json):
    with open(diff_json, "r", encoding="utf-8") as f:
        diff_data = json.load(f)
    check("JSON 中有 target_changed", "target_changed" in diff_data or "moves" in diff_data)

# ===== 步骤 5: export JSON =====
step(5, "export JSON - 包含锁定、违规、差异字段")
export_json = os.path.join(OUTPUTS_DIR, "export.json")
r, ok = run(f'python -m invoice_organizer export -c "{CONFIG_A}" -o "{export_json}" -f json')
check("export JSON 成功", ok and os.path.exists(export_json))
if os.path.exists(export_json):
    with open(export_json, "r", encoding="utf-8") as f:
        data = json.load(f)
    check("JSON 包含 plan_locks", "plan_locks" in data)
    check("JSON 包含 lock_violations", "lock_violations" in data)
    check("JSON 包含 plan_diffs", "plan_diffs" in data)

# ===== 步骤 6: export CSV =====
step(6, "export CSV - 包含锁定、违规、差异段落")
export_csv = os.path.join(OUTPUTS_DIR, "export.csv")
r, ok = run(f'python -m invoice_organizer export -c "{CONFIG_A}" -o "{export_csv}" -f csv')
check("export CSV 成功", ok and os.path.exists(export_csv))
if os.path.exists(export_csv):
    with open(export_csv, "r", encoding="utf-8-sig") as f:
        content = f.read()
    check("CSV 包含版本锁定记录段落", "版本锁定记录" in content)
    check("CSV 包含锁定违规记录段落", "锁定违规记录" in content)
    check("CSV 包含预案差异记录段落", "预案差异记录" in content)

# ===== 步骤 7: 锁定 config_a 快照 =====
step(7, "锁定 config_a 快照")
r, ok = run(f'python -m invoice_organizer lock-plan -c "{CONFIG_A}" -s {snapshot_a} --reason reviewer_ok -v')
print(r.stdout)
check("锁定成功", ok)
lock_id = None
for line in r.stdout.splitlines():
    if "锁定成功] 锁定 ID:" in line:
        lock_id = line.split("锁定 ID:")[-1].strip()
check("锁定 ID 存在", lock_id is not None)

# ===== 步骤 8: 锁定版本 apply 成功 =====
step(8, "锁定版本 apply (dry-run) - 应成功")
r, ok = run(f'python -m invoice_organizer apply -c "{CONFIG_A}" -s {snapshot_a} -y --dry-run')
print(r.stdout)
check("锁定版本 apply 成功", ok)

# ===== 步骤 9: 非锁定版本 apply 被拦截 =====
step(9, "非锁定版本 apply - 应被拦截")
r, ok = run(f'python -m invoice_organizer apply -c "{CONFIG_B}" -y --dry-run', expect_exit=1)
print(r.stdout[:500])
check("非锁定版本被拦截 (exit=1)", ok)
check("拦截原因包含版本锁定", "版本锁定拦截" in r.stderr + r.stdout)

# ===== 步骤 10: 实际锁定版本 apply + undo =====
step(10, "实际 apply (锁定版本) + undo 精确回滚")
source_count_before = len([f for f in os.listdir(SOURCE_DIR) if os.path.isfile(os.path.join(SOURCE_DIR, f))])
r, ok = run(f'python -m invoice_organizer apply -c "{CONFIG_A}" -s {snapshot_a} -y')
print(r.stdout)
run_id = None
for line in r.stdout.splitlines():
    if "执行完成] Run ID:" in line:
        run_id = line.split("Run ID:")[-1].strip()
check("apply 成功", ok and run_id)
moved_count = None
for line in r.stdout.splitlines():
    if "成功移动:" in line:
        moved_count = int(line.split("成功移动:")[-1].strip())
source_count_after = len([f for f in os.listdir(SOURCE_DIR) if os.path.isfile(os.path.join(SOURCE_DIR, f))])
check(f"apply 后源目录文件减少 ({moved_count} 个)", source_count_after == source_count_before - (moved_count or 0))

r, ok = run(f'python -m invoice_organizer undo -c "{CONFIG_A}" -r {run_id} -y -v')
print(r.stdout)
check("undo 成功", ok)
source_count_undo = len([f for f in os.listdir(SOURCE_DIR) if os.path.isfile(os.path.join(SOURCE_DIR, f))])
check(f"undo 后源目录恢复到 {source_count_before} 个文件", source_count_undo == source_count_before)

# ===== 步骤 11: 重启后导出一致性 =====
step(11, "重启后导出一致性")
export_after_restart = os.path.join(OUTPUTS_DIR, "after_restart.json")
r, ok = run(f'python -m invoice_organizer export -c "{CONFIG_A}" -o "{export_after_restart}" -f json')
check("重启后导出成功", ok)
if os.path.exists(export_after_restart) and os.path.exists(export_json):
    with open(export_after_restart, "r", encoding="utf-8") as f:
        restart_data = json.load(f)
    check("重启后 plans 一致", len(restart_data.get("plans", {})) == len(data.get("plans", {})))
    check("重启后 runs 只增不减", len(restart_data.get("runs", {})) >= len(data.get("runs", {})))
    check("重启后 plan_locks 一致", len(restart_data.get("plan_locks", [])) >= len(data.get("plan_locks", [])))
    check("重启后 lock_violations 一致", len(restart_data.get("lock_violations", [])) >= len(data.get("lock_violations", [])))
    check("重启后 plan_diffs 一致", len(restart_data.get("plan_diffs", [])) >= len(data.get("plan_diffs", [])))

# ===== 步骤 12: CSV 重启后一致性 =====
step(12, "重启后 CSV 导出一致性")
csv_after_restart = os.path.join(OUTPUTS_DIR, "after_restart.csv")
r, ok = run(f'python -m invoice_organizer export -c "{CONFIG_A}" -o "{csv_after_restart}" -f csv')
check("重启后 CSV 导出成功", ok)
if os.path.exists(csv_after_restart):
    with open(csv_after_restart, "r", encoding="utf-8-sig") as f:
        content = f.read()
    check("重启后 CSV 仍包含版本锁定记录", "版本锁定记录" in content)
    check("重启后 CSV 仍包含锁定违规记录", "锁定违规记录" in content)
    check("重启后 CSV 仍包含预案差异记录", "预案差异记录" in content)

# ===== 总结 =====
print(f"\n{'='*70}")
print(f"回归测试完成：{passed} 通过，{failed} 失败")
print(f"{'='*70}")
if failed > 0:
    sys.exit(1)
