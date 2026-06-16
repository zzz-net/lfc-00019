"""交接包重构回归测试

覆盖 4 个重构要求的回归场景：
  1. fresh workspace 导入后空目录判 invalid
  2. 原始目录信息不被覆盖
  3. 重启后复查还能看清真实落点
  4. 非零退出码会让测试直接失败

设计原则：
- 不依赖任何状态值，全部以实际文件系统检查 + CLI 退出码裁决
- 不写只看 verify_status 的断言（已被重构收敛）
- 测试结束后清理临时文件（不保留泄漏进程）
"""
from __future__ import annotations

import json
import os
import re as _re
import subprocess
import sys
import tempfile
import shutil
from pathlib import Path
from typing import Any, Dict

import pytest


_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _PROJECT_ROOT)

CLI_MOD = "invoice_organizer.cli"
PYTHON_EXEC = sys.executable


def _run_cli(args: str, cwd: str | None = None, check: bool = False
             ) -> subprocess.CompletedProcess:
    """封装 CLI 调用（与验收脚本保持一致，带 PYTHONPATH + 强制 UTF-8）。"""
    env = os.environ.copy()
    existing_pp = env.get("PYTHONPATH", "")
    sep = ";" if os.name == "nt" else ":"
    env["PYTHONPATH"] = _PROJECT_ROOT + (sep + existing_pp if existing_pp else "")
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"

    cmd = f'{PYTHON_EXEC} -m {CLI_MOD} {args}'
    return subprocess.run(
        cmd, shell=True, cwd=cwd, env=env,
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        check=check,  # 让 subprocess 原生抛 CalledProcessError（场景 4 D 部分依赖它）
    )


def _make_workspace(root: Path, name: str, source_files: list, rules: list,
                    dest_subdirs: list = None) -> dict:
    """创建一个工作区（与验收脚本保持一致）。"""
    ws = root / name
    source_dir = ws / "source"
    dest_dir = ws / "dest"
    state_file = ws / "state.json"
    config_path = ws / "config.yaml"

    source_dir.mkdir(parents=True, exist_ok=True)
    dest_dir.mkdir(parents=True, exist_ok=True)
    if dest_subdirs:
        for d in dest_subdirs:
            (dest_dir / d).mkdir(parents=True, exist_ok=True)

    for rel, content in source_files:
        fp = source_dir / rel
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_text(content, encoding="utf-8")

    yaml_rules = []
    for r in rules:
        yaml_rules.append(
            f"  - name: {r['name']}\n"
            f"    pattern: '{r['pattern']}'\n"
            f"    target: '{r['target']}'\n"
        )
    cfg_text = (
        f"source_dir: {source_dir.as_posix()}\n"
        f"dest_dir: {dest_dir.as_posix()}\n"
        f"state_file: {state_file.as_posix()}\n"
        f"recursive: true\n"
        f"require_signoff: false\n"
        f"signoff_expiry_days: 0\n"
        f"rules:\n"
        f"{''.join(yaml_rules)}\n"
    )
    config_path.write_text(cfg_text, encoding="utf-8")
    return {
        "ws_dir": ws,
        "source_dir": source_dir,
        "dest_dir": dest_dir,
        "config_path": config_path,
        "state_file": state_file,
    }


def _run_plan_and_apply(ws: dict):
    """在工作区执行 plan + apply（与验收脚本一致）。"""
    cwd = str(ws["ws_dir"])
    cfg = str(ws["config_path"])

    plan_r = _run_cli(f'plan -c "{cfg}"', cwd=cwd, check=True)

    all_ids = _re.findall(r'ID[:：]\s*([a-zA-Z0-9]{8,})', plan_r.stdout)
    plan_id = all_ids[0] if len(all_ids) >= 1 else None
    snapshot_id = all_ids[1] if len(all_ids) >= 2 else plan_id

    if not snapshot_id:
        for line in plan_r.stdout.splitlines():
            line = line.strip()
            parts = line.split()
            if parts and 8 <= len(parts[0]) <= 36 and all(c in "0123456789abcdef" for c in parts[0]):
                snapshot_id = parts[0]
                plan_id = plan_id or snapshot_id
                break
    assert snapshot_id, f"未能从 plan 输出提取 snapshot_id: {plan_r.stdout[-2000:]}"

    apply_r = _run_cli(
        f'apply -c "{cfg}" -s {snapshot_id} -y --no-require-signoff',
        cwd=cwd,
        check=True,
    )

    apply_ids = _re.findall(r'ID[:：]\s*([a-zA-Z0-9]{8,})', apply_r.stdout)
    run_id = apply_ids[0] if apply_ids else None
    if not plan_id:
        plan_id = "unknown_plan"
    return plan_id, run_id, snapshot_id


def _create_origin_workspace_and_handover(root: Path) -> Dict[str, Any]:
    """构造源工作区 + 生成交接包，返回与验收脚本一致的结构。"""
    source_files = [
        ("inv_0.pdf", "invoice-0"),
        ("inv_1.pdf", "invoice-1"),
        ("inv_2.pdf", "invoice-2"),
        ("receipt_0.jpg", "receipt-0"),
        ("receipt_1.jpg", "receipt-1"),
        ("contract_0.docx", "contract-0"),
        ("contract_1.docx", "contract-1"),
    ]
    rules = [
        {"name": "inv", "pattern": "inv_*.pdf", "target": "invoices_2024"},
        {"name": "rec", "pattern": "receipt_*.jpg", "target": "receipts"},
        {"name": "con", "pattern": "contract_*.docx", "target": "contracts"},
    ]

    wsA = _make_workspace(root, "env_A", source_files, rules,
                          dest_subdirs=["invoices_2024", "receipts", "contracts"])
    _run_plan_and_apply(wsA)

    handover_json = root / "env_A_handover.json"
    r = _run_cli(
        f'generate-handover -c "{wsA["config_path"]}" '
        f'-o "{handover_json}" --notes "regression test origin" --json-output',
        cwd=str(wsA["ws_dir"]),
        check=True,
    )
    data = json.loads(r.stdout)
    handover_id = data["handover_id"]

    target_dirs_full = {
        "invoices_2024": wsA["dest_dir"] / "invoices_2024",
        "receipts": wsA["dest_dir"] / "receipts",
        "contracts": wsA["dest_dir"] / "contracts",
    }
    return {
        "handover_json": handover_json,
        "handover_id": handover_id,
        "orig_dest": wsA["dest_dir"],
        "origin_ws": wsA,
        "target_dirs": target_dirs_full,
    }


# ============================================================
# 回归场景 1：fresh workspace 导入后空目录判 invalid
# ============================================================
def test_regression_1_empty_fresh_workspace_invalid():
    """场景 1：fresh workspace 是空目录（没真的复制文件过来），
    导入后 unified_validation.is_valid 必须是 False。"""
    tmp = Path(tempfile.mkdtemp(prefix="handover_reg1_"))
    try:
        origin = _create_origin_workspace_and_handover(tmp)
        handover_json = origin["handover_json"]
        orig_dest = origin["orig_dest"]

        # ---- fresh workspace：空的 dest，没有复制文件 ----
        fresh_ws_info = _make_workspace(tmp, "env_B", [], [
            {"name": "noop", "pattern": "noop", "target": "dummy"},
        ])
        fresh_cfg = fresh_ws_info["config_path"]
        fresh_dest = fresh_ws_info["dest_dir"]
        fresh_state = fresh_ws_info["state_file"]

        # 导入（fresh_dest 空目录）：用 --dest-dir 强制指定确保重绑生效，
        # 不回退到交接包里的原始路径
        r = _run_cli(
            f'import-handover -f "{handover_json}" -c "{fresh_cfg}" '
            f'--dest-dir "{fresh_dest}" --json-output',
            cwd=str(fresh_ws_info["ws_dir"]),
            check=False,
        )
        out = json.loads(r.stdout)

        # 关键断言 1：空目录 → actual_file_check.total_found == 0
        afc = out.get("actual_file_check")
        assert afc is not None, "actual_file_check 字段未输出"
        assert afc["total_expected"] > 0, "total_expected 应为正值"
        assert afc["total_found"] == 0, f"total_found 应为 0，实际 {afc['total_found']}"
        assert afc["all_files_present"] is False, "all_files_present 空目录必须 False"

        # 关键断言 2：统一校验 is_valid=False
        uv = out.get("unified_validation")
        assert uv is not None, "unified_validation 字段未输出"
        assert uv["is_valid"] is False, f"is_valid 必须 False, 实际 {uv['is_valid']}"
        assert uv["overall_status"] == "invalid", f"overall_status={uv['overall_status']}"
        assert len(uv["block_reasons"]) > 0, "block_reasons 不应为空"

        # 关键断言 3：is_valid 快捷字段必须 False
        assert out.get("is_valid") is False, "is_valid 快捷字段必须 False"

        # 关键断言 4：CLI 退出码非零（关键回归：不要让测试误判通过）
        assert r.returncode != 0, f"空目录导入 CLI 必须非零退出, 实际 returncode={r.returncode}"

        print(f"[场景1 通过] 空目录 total_expected={afc['total_expected']} "
              f"total_found={afc['total_found']} CLI_exit={r.returncode}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ============================================================
# 回归场景 2：原始目录信息不被覆盖
# ============================================================
def test_regression_2_original_dir_not_overwritten():
    """场景 2：original_target_dir_info 永远保持源 workspace 的路径，
    导入后 rebound 和 original 必须是两条独立记录。"""
    tmp = Path(tempfile.mkdtemp(prefix="handover_reg2_"))
    try:
        origin = _create_origin_workspace_and_handover(tmp)
        handover_json = origin["handover_json"]
        orig_dest = origin["orig_dest"]

        # 先从文件里直接读取 original（校验生成阶段就写了 original_dest_dir_record）
        bundle_raw = json.loads(handover_json.read_text(encoding="utf-8"))
        orig_from_bundle = bundle_raw.get("original_dest_dir_record")
        assert orig_from_bundle is not None, "generate-handover 时 original_dest_dir_record 未写入"
        assert orig_from_bundle["dest_root"] == str(orig_dest), (
            f"original dest_root 生成阶段即应写入: 期望 {orig_dest}, "
            f"实际 {orig_from_bundle.get('dest_root')}"
        )
        # 同时应存在 rebound_dest_dir_record（创建时和 original 相同）
        reb_from_bundle = bundle_raw.get("rebound_dest_dir_record")
        assert reb_from_bundle is not None, "generate-handover 时 rebound_dest_dir_record 未写入"

        # 导入到全新路径（用 --dest-dir 强制指定，不依赖 config 解析）
        fresh_ws_info = _make_workspace(tmp, "env_B", [], [
            {"name": "noop", "pattern": "noop", "target": "dummy"},
        ])
        fresh_state = fresh_ws_info["state_file"]
        fresh_cfg = fresh_ws_info["config_path"]
        # 覆盖成一个完全不同的 dest 路径，确保重绑
        fresh_dest = tmp / "totally_different_path" / "completely_new_dest"
        fresh_dest.mkdir(parents=True, exist_ok=True)

        r = _run_cli(
            f'import-handover -f "{handover_json}" -c "{fresh_cfg}" '
            f'--dest-dir "{fresh_dest}" --json-output',
            cwd=str(fresh_ws_info["ws_dir"]),
            check=False,
        )
        out = json.loads(r.stdout)

        # 关键断言：original 仍保持旧路径，rebound 是新路径
        orig_after = out.get("original_target_dir_info")
        reb_after = out.get("rebound_target_dir_info")
        assert orig_after is not None and reb_after is not None, "三个独立字段缺失"
        assert orig_after["dest_root"] == str(orig_dest), (
            f"original_target_dir_info.dest_root 被覆盖了! "
            f"期望 {orig_dest}, 实际 {orig_after['dest_root']}"
        )
        assert reb_after["dest_root"] != orig_after["dest_root"], (
            "rebound_target_dir_info 应与 original 不同（重绑）"
        )
        assert reb_after["dest_root"] == str(fresh_dest), (
            f"rebound_target_dir_info.dest_root 错误: 期望 {fresh_dest}, "
            f"实际 {reb_after['dest_root']}"
        )

        # 进一步：从 fresh 的状态文件里读取 handover，验证持久化后 original 仍不被覆盖
        state_raw = json.loads(fresh_state.read_text(encoding="utf-8"))
        handovers_dict = state_raw["handovers"]
        assert len(handovers_dict) >= 1, f"状态文件中 handovers 数量: {len(handovers_dict)}"
        bundle_in_state = list(handovers_dict.values())[0]
        orig_from_state = bundle_in_state.get("original_dest_dir_record")
        assert orig_from_state is not None, "持久化后 original_dest_dir_record 丢失"
        assert orig_from_state["dest_root"] == str(orig_dest), (
            f"持久化后 original dest_root 仍被覆盖: {orig_from_state['dest_root']}"
        )
        reb_from_state = bundle_in_state.get("rebound_dest_dir_record")
        assert reb_from_state is not None, "持久化后 rebound_dest_dir_record 丢失"
        assert reb_from_state["dest_root"] == str(fresh_dest), (
            f"持久化后 rebound dest_root 错误: {reb_from_state['dest_root']}"
        )

        print(f"[场景2 通过] original={orig_after['dest_root'][-30:]}  "
              f"rebound={reb_after['dest_root'][-30:]}  持久化后一致")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ============================================================
# 回归场景 3：重启后复查还能看清真实落点
# ============================================================
def test_regression_3_restart_review_actual_files():
    """场景 3：导入后"重启"（重新运行 review-handover），
    仍然能独立读磁盘检查真实文件，而不是靠缓存的状态值。"""
    tmp = Path(tempfile.mkdtemp(prefix="handover_reg3_"))
    try:
        origin = _create_origin_workspace_and_handover(tmp)
        handover_json = origin["handover_json"]
        orig_dest = origin["orig_dest"]
        target_dirs = origin["target_dirs"]

        # 导入：先导入为"空目录"场景
        fresh_ws_info = _make_workspace(tmp, "env_B", [], [
            {"name": "noop", "pattern": "noop", "target": "dummy"},
        ])
        fresh_cfg = fresh_ws_info["config_path"]
        fresh_dest = fresh_ws_info["dest_dir"]
        _run_cli(
            f'import-handover -f "{handover_json}" -c "{fresh_cfg}" '
            f'--dest-dir "{fresh_dest}" --json-output',
            cwd=str(fresh_ws_info["ws_dir"]),
            check=False,
        )

        # 第一遍 review：确认 total_found=0
        r1 = _run_cli(
            f'review-handover -c "{fresh_cfg}" --json-output',
            cwd=str(fresh_ws_info["ws_dir"]),
            check=False,
        )
        rev1 = json.loads(r1.stdout)
        afc1 = rev1["actual_file_check"]
        assert afc1["total_found"] == 0, f"首次 review total_found 应为 0, 实际 {afc1['total_found']}"
        assert rev1["unified_validation"]["is_valid"] is False
        assert r1.returncode != 0, "首次 review 空目录应返回非零"

        # 现在手动把所有真实文件复制过来（模拟"恢复"了数据）
        for src_key, src_dir in target_dirs.items():
            dst_dir = fresh_dest / src_key
            dst_dir.mkdir(parents=True, exist_ok=True)
            for f in src_dir.iterdir():
                if f.is_file():
                    shutil.copy2(f, dst_dir / f.name)

        # 第二遍 review（模拟重启后再次跑命令）：total_found 必须全到位
        r2 = _run_cli(
            f'review-handover -c "{fresh_cfg}" --json-output',
            cwd=str(fresh_ws_info["ws_dir"]),
            check=False,
        )
        rev2 = json.loads(r2.stdout)
        afc2 = rev2["actual_file_check"]
        assert afc2["total_expected"] > 0, "total_expected 应为正值"
        assert afc2["total_found"] == afc2["total_expected"], (
            f"文件复制后 total_found 应等于 total_expected={afc2['total_expected']}, "
            f"实际 {afc2['total_found']}"
        )
        assert afc2["total_missing"] == 0, "复制后 total_missing 应为 0"
        assert afc2["all_files_present"] is True, (
            f"文件复制到位后 all_files_present 必须 True, 实际 {afc2['all_files_present']}"
        )

        # 逐目录验证到位
        for pdr in afc2["per_dir_results"]:
            assert pdr["found_count"] == pdr["expected_count"], (
                f"目录 {pdr['actual_dir']} found != expected"
            )
            assert pdr["dir_exists"] is True, f"目录 {pdr['actual_dir']} 应存在"

        # 统一校验链：文件到位 → is_valid=True（前提是没有其它错误）
        assert rev2["unified_validation"]["is_valid"] is True, (
            f"真实文件到位后统一校验必须 True, "
            f"实际 is_valid={rev2['unified_validation']['is_valid']} "
            f"block_reasons={rev2['unified_validation']['block_reasons']}"
        )
        # 此时 verify_status 应是 valid
        assert rev2["verify_status"] == "valid", (
            f"真实文件到位后 verify_status 应为 valid, 实际 {rev2['verify_status']}"
        )
        # CLI 退出码应为 0
        assert r2.returncode == 0, (
            f"真实文件到位后 review 应返回 0, 实际 returncode={r2.returncode}"
        )

        # 再次确认 original 信息仍未被覆盖（即使文件已恢复）
        assert rev2["original_target_dir_info"]["dest_root"] == str(orig_dest), (
            f"original 信息被覆盖了: 期望 {orig_dest}, "
            f"实际 {rev2['original_target_dir_info']['dest_root']}"
        )

        print(f"[场景3 通过] 复制前 found=0, 复制后 found={afc2['total_found']}/{afc2['total_expected']} "
              f"→ is_valid 从 False 变为 True, CLI 返回 0")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ============================================================
# 回归场景 4：非零退出码会让测试直接失败
# ============================================================
def test_regression_4_non_zero_exit_fails_test():
    """场景 4：任何一步命令返回非零退出码时，
    pytest 必须直接断言失败（检查测试脚本对退出码的敏感度）。

    这是对"测试收敛"本身的回归：避免脚本里只看 JSON 值不看 returncode。
    """
    tmp = Path(tempfile.mkdtemp(prefix="handover_reg4_"))
    try:
        origin = _create_origin_workspace_and_handover(tmp)
        handover_json = origin["handover_json"]

        fresh_ws_info = _make_workspace(tmp, "env_B", [], [
            {"name": "noop", "pattern": "noop", "target": "dummy"},
        ])
        fresh_cfg = fresh_ws_info["config_path"]
        fresh_dest = fresh_ws_info["dest_dir"]

        # ===== A) import-handover：空目录 → CLI 必须非零 =====
        r_import = _run_cli(
            f'import-handover -f "{handover_json}" -c "{fresh_cfg}" '
            f'--dest-dir "{fresh_dest}" --json-output',
            cwd=str(fresh_ws_info["ws_dir"]),
            check=False,
        )
        # 这个断言是本场景的核心：只要 returncode == 0，就直接 pytest fail
        assert r_import.returncode != 0, (
            "[回归失败] import-handover 空目录场景，CLI 竟然返回 0！\n"
            "这意味着测试脚本会误判通过（只看 JSON、不看退出码的老问题）。\n"
            "请检查 cli.py 的 sys.exit() 分支是否覆盖了统一校验链。"
        )

        # ===== B) preview-handover：故意传不存在的 rebind_json =====
        bad_rebind = fresh_ws_info["ws_dir"] / "not_exist.json"
        r_preview = _run_cli(
            f'preview-handover -f "{handover_json}" '
            f'--dest-dir "{fresh_dest}" --rebind-json "{bad_rebind}" '
            f'--json-output',
            cwd=str(fresh_ws_info["ws_dir"]),
            check=False,
        )
        assert r_preview.returncode != 0, (
            "[回归失败] preview-handover 传入不存在的 rebind-json, 竟返回 0！"
        )

        # ===== C) review-handover：空目录 → CLI 必须非零 =====
        r_review = _run_cli(
            f'review-handover -c "{fresh_cfg}" --json-output',
            cwd=str(fresh_ws_info["ws_dir"]),
            check=False,
        )
        assert r_review.returncode != 0, (
            "[回归失败] review-handover 空目录场景，CLI 竟然返回 0！\n"
            "请检查 cli.py review 退出码是否走了统一校验链。"
        )

        # ===== D) 额外：如果用 check=True，必须抛 CalledProcessError =====
        #    （这证明测试框架能正确拦截非零退出码）
        import subprocess as _sp
        raised = False
        try:
            _run_cli(
                f'review-handover -c "{fresh_cfg}" --json-output',
                cwd=str(fresh_ws_info["ws_dir"]),
                check=True,
            )
        except _sp.CalledProcessError:
            raised = True
        assert raised is True, (
            "[回归失败] subprocess.run(check=True) 对非零退出码竟然没抛异常！\n"
            "说明整个 pytest 对退出码不敏感，所有验收脚本都会漏判。"
        )

        print(f"[场景4 通过] import rc={r_import.returncode}, "
              f"preview rc={r_preview.returncode}, "
              f"review rc={r_review.returncode} — 全部非零，check=True 会抛异常")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    test_regression_1_empty_fresh_workspace_invalid()
    test_regression_2_original_dir_not_overwritten()
    test_regression_3_restart_review_actual_files()
    test_regression_4_non_zero_exit_fails_test()
    print("\n[OK] 全部 4 个回归场景通过")
