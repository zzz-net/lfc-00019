"""回归测试：切换配置目录后仍误判有效 Bug 修复验证

Bug 描述：
  切换配置目录（修改 config 文件中的 dest_dir）后，使用 verify-landing -f
  核对从原配置导出的落点清单时，由于本地状态文件中没有对应 landing_id
  的记录，diff_landing_fingerprints 函数中所有差异检测（包括 dest_dir 不
  一致）都在 `if local is not None:` 块内，导致 dest_dir 不一致无法被检
  测出，清单被误判为 valid。

修复方案：
  在 validate_landing_for_import 中增加 current_config 参数，直接与当
  前配置的 dest_dir 比对，不依赖 local 记录是否存在。

测试场景：
  1. 准备配置 A（dest_dir=dest_a），执行 apply，生成落点清单
  2. 导出落点清单到 JSON 文件
  3. 准备配置 B（dest_dir=dest_b），使用同一状态文件
  4. 用配置 B 执行 verify-landing -f 核对原导出的清单
  5. 验证结果应为 conflict 或 invalid，不应为 valid
  6. 验证错误信息中包含 dest_dir 不一致的描述
  7. 验证三类分类（valid/invalid/conflict）正确
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

WORK_DIR = Path(tempfile.mkdtemp(prefix="landing_config_switch_test_"))
SOURCE_DIR = WORK_DIR / "source"
DEST_DIR_A = WORK_DIR / "dest_a"
DEST_DIR_B = WORK_DIR / "dest_b"
STATE_FILE = WORK_DIR / ".invoice_organizer_state.json"
CONFIG_FILE_A = WORK_DIR / "config_a.yaml"
CONFIG_FILE_B = WORK_DIR / "config_b.yaml"
EXPORT_FILE = WORK_DIR / "landing_export.json"

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

    def _safe_print(text, stream=None):
        if text is None:
            return
        stream = stream or sys.stdout
        try:
            stream.write(text + "\n")
            stream.flush()
        except UnicodeEncodeError:
            safe = text.encode(stream.encoding or "utf-8", errors="replace").decode(
                stream.encoding or "utf-8", errors="replace"
            )
            stream.write(safe + "\n")
            stream.flush()

    _safe_print(f"\n$ {cmd}")
    if stdout:
        _safe_print(stdout)
    if stderr:
        _safe_print(stderr, sys.stderr)

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


def setup_environment():
    """设置测试环境"""
    print(f"\n[设置] 测试目录: {WORK_DIR}")

    SOURCE_DIR.mkdir(parents=True, exist_ok=True)
    DEST_DIR_A.mkdir(parents=True, exist_ok=True)
    DEST_DIR_B.mkdir(parents=True, exist_ok=True)

    for i in range(5):
        f = SOURCE_DIR / f"invoice_{i:03d}.pdf"
        f.write_text(f"test invoice content {i}" * 100)

    config_a = {
        "source_dir": str(SOURCE_DIR),
        "dest_dir": str(DEST_DIR_A),
        "state_file": str(STATE_FILE),
        "recursive": False,
        "file_extensions": ["pdf"],
        "rules": [
            {
                "name": "默认分类",
                "pattern": "*.pdf",
                "target": "invoices",
            }
        ],
    }
    with open(CONFIG_FILE_A, "w", encoding="utf-8") as f:
        yaml.dump(config_a, f, allow_unicode=True)

    config_b = {
        "source_dir": str(SOURCE_DIR),
        "dest_dir": str(DEST_DIR_B),
        "state_file": str(STATE_FILE),
        "recursive": False,
        "file_extensions": ["pdf"],
        "rules": [
            {
                "name": "默认分类",
                "pattern": "*.pdf",
                "target": "invoices",
            }
        ],
    }
    with open(CONFIG_FILE_B, "w", encoding="utf-8") as f:
        yaml.dump(config_b, f, allow_unicode=True)

    print("[设置] 测试环境准备完成")


def test_config_switch_detects_dest_dir_mismatch():
    """测试：切换配置目录后能正确检测 dest_dir 不一致"""
    print("\n" + "=" * 60)
    print("测试 1: 切换配置目录后 verify-landing -f 应检测出 dest_dir 不一致")
    print("=" * 60)

    print("\n[步骤 1] 使用配置 A 执行 plan + apply")
    result = run(f'python -m invoice_organizer plan -c "{CONFIG_FILE_A}"')
    snapshot_id = None
    for line in result.stdout.splitlines():
        if "批次快照" in line and "ID" in line:
            import re
            m = re.search(r'ID[:：]\s*([A-Za-z0-9]+)', line)
            if m:
                snapshot_id = m.group(1)
                break
    if not snapshot_id:
        for line in result.stdout.splitlines():
            line = line.strip()
            parts = line.split()
            if parts and len(parts[0]) >= 8 and all(c in "0123456789abcdef" for c in parts[0]):
                snapshot_id = parts[0]
                break
    assert snapshot_id, "未能从 plan 输出中提取 snapshot_id"
    print(f"  Snapshot ID: {snapshot_id}")

    run(f'python -m invoice_organizer apply -c "{CONFIG_FILE_A}" -s {snapshot_id} -y --no-require-signoff')

    print("\n[步骤 2] 导出落点清单")
    run(f'python -m invoice_organizer export-landing -c "{CONFIG_FILE_A}" -o "{EXPORT_FILE}"')

    assert EXPORT_FILE.exists(), "导出文件应存在"

    print("\n[步骤 3] 用配置 A 核对导出的清单（应通过）")
    if STATE_FILE.exists():
        STATE_FILE.unlink()
    result = run(
        f'python -m invoice_organizer verify-landing -c "{CONFIG_FILE_A}" -f "{EXPORT_FILE}" --json',
        check=False,
    )
    data = json.loads(result.stdout)
    print(f"  状态: {data.get('status', data.get('valid'))}")
    status = data.get("status", "")
    assert status == "valid", f"配置 A 下清单状态应为 'valid'，实际为 '{status}'"
    assert result.returncode == 0, f"配置 A 下核对应通过，但返回码为 {result.returncode}"

    print("\n[步骤 4] 用配置 B 核对导出的清单（应检测出冲突）")
    result = run(
        f'python -m invoice_organizer verify-landing -c "{CONFIG_FILE_B}" -f "{EXPORT_FILE}" --json',
        check=False,
    )
    assert result.returncode != 0, "配置 B 下核对应失败，但返回码为 0（Bug 未修复！）"

    data = json.loads(result.stdout)
    status = data.get("status", "")
    print(f"  状态: {status}")
    print(f"  错误: {data.get('errors', [])}")
    print(f"  冲突类型: {data.get('conflict_types', [])}")

    assert status == "conflict", (
        f"配置 B 下清单状态应为 'conflict'，实际为 '{status}'。"
        f" 这是切换配置目录后误判有效的 Bug！"
    )

    errors = data.get("errors", [])
    has_dest_dir_error = any(
        "dest_dir" in err or "目标目录" in err or "配置目录切换" in err
        for err in errors
    )
    assert has_dest_dir_error, "错误信息中应包含 dest_dir 不一致的描述"

    conflict_types = data.get("conflict_types", [])
    has_config_switch_conflict = any(
        "配置目录切换" in ct for ct in conflict_types
    )
    assert has_config_switch_conflict, "冲突类型中应包含 '配置目录切换'"

    print("  [OK] 正确检测到配置目录切换后的 dest_dir 不一致")

    print("\n[步骤 5] 验证三类分类：invalid 场景")
    invalid_file = WORK_DIR / "invalid.json"
    invalid_file.write_text("{invalid json content")
    result = run(
        f'python -m invoice_organizer verify-landing -c "{CONFIG_FILE_A}" -f "{invalid_file}" --json',
        check=False,
    )
    data = json.loads(result.stdout)
    status = data.get("status", "")
    print(f"  无效清单状态: {status}")
    assert status == "invalid", f"无效清单状态应为 'invalid'，实际为 '{status}'"
    print("  [OK] 无效清单正确分类为 invalid")

    print("\n[步骤 6] 验证三类分类：valid 场景")
    result = run(
        f'python -m invoice_organizer verify-landing -c "{CONFIG_FILE_A}" -f "{EXPORT_FILE}" --json',
        check=False,
    )
    data = json.loads(result.stdout)
    status = data.get("status", "")
    print(f"  有效清单状态: {status}")
    assert status == "valid", f"有效清单状态应为 'valid'，实际为 '{status}'"
    print("  [OK] 有效清单正确分类为 valid")

    print("\n[通过] 测试 1 通过：切换配置目录后能正确检测 dest_dir 不一致")


def test_import_with_config_switch():
    """测试：导入时也能检测配置目录切换"""
    print("\n" + "=" * 60)
    print("测试 2: import-landing 也能检测配置目录切换")
    print("=" * 60)

    print("\n[步骤 1] 清空状态，重新用配置 A 生成并导出")
    if STATE_FILE.exists():
        STATE_FILE.unlink()

    result = run(f'python -m invoice_organizer plan -c "{CONFIG_FILE_A}"')
    snapshot_id = None
    for line in result.stdout.splitlines():
        if "批次快照" in line and "ID" in line:
            import re
            m = re.search(r'ID[:：]\s*([A-Za-z0-9]+)', line)
            if m:
                snapshot_id = m.group(1)
                break
    if not snapshot_id:
        for line in result.stdout.splitlines():
            line = line.strip()
            parts = line.split()
            if parts and len(parts[0]) >= 8 and all(c in "0123456789abcdef" for c in parts[0]):
                snapshot_id = parts[0]
                break
    assert snapshot_id, "未能从 plan 输出中提取 snapshot_id"

    run(f'python -m invoice_organizer apply -c "{CONFIG_FILE_A}" -s {snapshot_id} -y --no-require-signoff')
    run(f'python -m invoice_organizer export-landing -c "{CONFIG_FILE_A}" -o "{EXPORT_FILE}"')

    print("\n[步骤 2] 删除状态文件，模拟切换环境后导入")
    if STATE_FILE.exists():
        STATE_FILE.unlink()

    print("\n[步骤 3] 用配置 B 导入清单（应检测出冲突）")
    result = run(
        f'python -m invoice_organizer import-landing -c "{CONFIG_FILE_B}" -i "{EXPORT_FILE}"',
        check=False,
    )
    assert result.returncode != 0, "配置 B 下导入应失败（Bug 未修复！）"

    assert "目标目录" in result.stdout or "dest_dir" in result.stdout or "配置目录切换" in result.stdout, \
        "导入失败信息中应包含 dest_dir 不一致的描述"

    print("  [OK] import-landing 也能检测配置目录切换")

    print("\n[OK] 测试 2 通过：import-landing 能检测配置目录切换")


def test_verify_status_fields():
    """测试：verify-landing -f 返回结果包含必要字段"""
    print("\n" + "=" * 60)
    print("测试 3: verify-landing -f 返回结果字段完整性")
    print("=" * 60)

    result = run(
        f'python -m invoice_organizer verify-landing -c "{CONFIG_FILE_B}" -f "{EXPORT_FILE}" --json',
        check=False,
    )
    data = json.loads(result.stdout)

    required_fields = [
        "status",
        "landing_id",
        "run_id",
        "snapshot_id",
        "errors",
        "warnings",
        "conflict_types",
        "diff",
        "current_config_dest_dir",
        "landing_dest_dir",
        "verified_at",
    ]

    for field in required_fields:
        assert field in data, f"结果中缺少字段: {field}"
        print(f"  [OK] 包含字段: {field}")

    assert data["status"] in ("valid", "invalid", "conflict"), \
        f"status 必须是 valid/invalid/conflict 之一，实际为 '{data['status']}'"

    print("\n[OK] 测试 3 通过：返回结果字段完整")


def main():
    """主测试函数"""
    try:
        setup_environment()
        test_config_switch_detects_dest_dir_mismatch()
        test_import_with_config_switch()
        test_verify_status_fields()

        print("\n" + "=" * 60)
        print("所有回归测试通过！[OK]")
        print("Bug '切换配置目录后仍误判有效' 已修复。")
        print("=" * 60)
        return 0
    except AssertionError as e:
        print(f"\n[FAIL] 测试失败: {e}")
        return 1
    except Exception as e:
        print(f"\n[FAIL] 测试发生异常: {type(e).__name__}: {e}")
        import traceback
        traceback.print_exc()
        return 1
    finally:
        cleanup()


if __name__ == "__main__":
    sys.exit(main())
