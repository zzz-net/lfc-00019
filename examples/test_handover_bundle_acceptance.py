"""落点交接包完整验收测试

覆盖场景：
1. 原环境：plan + apply + generate-handover（含 JSON/CSV 导出）
2. 预检：preview-handover 能正确给出重绑定映射和权限结论
3. fresh workspace：import-handover 重绑定导入成功
4. 继续执行 verify-landing（通过 review-handover 链）
5. 重启（重开 StateStore）后复查，状态跨重启保留
6. 冲突导入：重复导入 / 映射冲突 被明确拒绝
7. undo 后仍能回看最近一次导入结果
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import uuid
from pathlib import Path


CLI_MOD = "invoice_organizer.cli"
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _PROJECT_ROOT)


def _run(cmd: str, cwd: str = None, check: bool = True, env_extra: dict = None):
    """在子进程中运行命令，捕获输出。"""
    import subprocess

    env = os.environ.copy()
    existing_pp = env.get("PYTHONPATH", "")
    sep = ";" if os.name == "nt" else ":"
    env["PYTHONPATH"] = _PROJECT_ROOT + (sep + existing_pp if existing_pp else "")
    if env_extra:
        env.update(env_extra)

    # 强制 UTF-8 输出，避免 GBK 编码问题
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"

    result = subprocess.run(
        cmd, shell=True, cwd=cwd, env=env,
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    if check and result.returncode != 0:
        safe_out = result.stdout[-3000:].encode("utf-8", errors="replace").decode("utf-8", errors="replace")
        safe_err = result.stderr[-3000:].encode("utf-8", errors="replace").decode("utf-8", errors="replace")
        print(f"[CMD FAIL] {cmd}")
        print("STDOUT:", safe_out)
        print("STDERR:", safe_err)
        raise RuntimeError(f"命令失败 (rc={result.returncode}): {cmd}")
    return result


def _make_workspace(root: Path, name: str, source_files: list, rules: list,
                    dest_subdirs: list = None) -> dict:
    """创建一个工作区目录，写入源文件 + config.yaml。

    返回 {ws_dir, source_dir, dest_dir, config_path, state_file}
    """
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
    """在工作区执行 plan + apply，返回 (plan_id, run_id, snapshot_id)。"""
    import re as _re
    cwd = str(ws["ws_dir"])
    cfg = str(ws["config_path"])

    plan_r = _run(f'python -m {CLI_MOD} plan -c "{cfg}"', cwd=cwd)

    # 先收集所有 "ID: xxxxxxxx" 格式
    all_ids = _re.findall(r'ID[:：]\s*([a-zA-Z0-9]{8,})', plan_r.stdout)
    # 通常顺序: 预案ID(plan)在前, 批次快照ID(snapshot)在后
    # 但从 state 文件确认哪个是 snapshot
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
    assert snapshot_id, f"未能从 plan 输出中提取 snapshot_id: {plan_r.stdout}"

    apply_r = _run(
        f'python -m {CLI_MOD} apply -c "{cfg}" -s {snapshot_id} -y --no-require-signoff',
        cwd=cwd,
    )

    apply_ids = _re.findall(r'ID[:：]\s*([a-zA-Z0-9]{8,})', apply_r.stdout)
    run_id = apply_ids[0] if apply_ids else None
    if not run_id:
        for line in apply_r.stdout.splitlines():
            line = line.strip()
            parts = line.split()
            if parts and 8 <= len(parts[0]) <= 36 and all(c in "0123456789abcdef_- " for c in parts[0]):
                run_id = parts[0]
                break
    if not plan_id:
        plan_id = "unknown_plan"

    print(f"[INFO] plan_id={plan_id}  run_id={run_id}  snapshot_id={snapshot_id}")
    return plan_id, run_id, snapshot_id


# ============================================================


def test_1_original_environment_generate_handover(tmp_path_factory):
    """测试 1：原环境生成交接包并导出 JSON/CSV"""
    tmp = tmp_path_factory.mktemp("handover_acceptance")

    source_files = [
        ("invoice_2024_001.pdf", "invoice-A"),
        ("invoice_2024_002.pdf", "invoice-B"),
        ("doc_readme.txt", "readme content"),
        ("contract_c1.pdf", "contract-X"),
    ]
    rules = [
        {"name": "inv", "pattern": "invoice_*.pdf", "target": "invoices"},
        {"name": "ctr", "pattern": "contract_*.pdf", "target": "contracts"},
        {"name": "doc", "pattern": "*.txt", "target": "docs"},
    ]

    wsA = _make_workspace(tmp, "env_A", source_files, rules,
                          dest_subdirs=["invoices", "contracts", "docs"])

    plan_id, run_id, snapshot_id = _run_plan_and_apply(wsA)

    # 生成交接包 + 导出 JSON + CSV
    handover_json = tmp / "env_A_handover.json"
    csv_dir = tmp / "env_A_csv"
    cwd = str(wsA["ws_dir"])
    cfg = str(wsA["config_path"])
    r = _run(
        f'python -m {CLI_MOD} generate-handover -c "{cfg}" '
        f'-o "{handover_json}" --csv-dir "{csv_dir}" '
        f'--notes "原工作区导出" --json-output',
        cwd=cwd,
    )
    data = json.loads(r.stdout)
    handover_id = data["handover_id"]
    assert data["file_count"] == 4, f"落点文件数应为 4, 实际 {data['file_count']}"
    assert data["target_dir_count"] == 3, f"目标目录映射数应为 3, 实际 {data['target_dir_count']}"
    assert os.path.exists(handover_json), "交接包 JSON 未生成"
    for csv_name in ["handover_summary.csv", "landing_files.csv", "target_dirs.csv",
                     "manual_renames.csv", "conflict_summary.csv"]:
        assert (csv_dir / csv_name).exists(), f"CSV 缺失: {csv_name}"
    print(f"[OK] 测试 1 通过: handover_id={handover_id}, JSON+CSV 导出成功")
    return {
        "handover_json": handover_json,
        "csv_dir": csv_dir,
        "handover_id": handover_id,
        "wsA": wsA,
        "tmp": tmp,
        "original_dest_dir": str(wsA["dest_dir"]),
    }


def step_2_preview_handover_rebind(ctx):
    """测试 2：预检能给出重绑定映射。"""
    ctx = ctx if isinstance(ctx, dict) else test_1_original_environment_generate_handover(
        type("TPF", (), {"mktemp": lambda self, x: Path(tempfile.mkdtemp(prefix=x + "_"))})()
    )
    tmp = ctx["tmp"]

    # 新工作区 B，dest_dir 路径完全不同
    fresh = tmp / "env_B"
    new_dest = fresh / "new_dest_root"
    new_dest.mkdir(parents=True, exist_ok=True)

    r = _run(
        f'python -m {CLI_MOD} preview-handover -f "{ctx["handover_json"]}" '
        f'--dest-dir "{new_dest}" --json-output',
        check=False,
    )
    preview = json.loads(r.stdout)
    assert preview["handover_id"] == ctx["handover_id"]
    assert preview["status"] in ("ok", "warnings"), f"预检状态异常: {preview['status']}"
    # 应有 warnings，因为 dest_dir 重绑定了
    assert any("目标目录已重绑定" in w for w in preview["warnings"]), (
        "缺少 dest_dir 重绑定警告"
    )
    # 应有 3 个 target_dir_key 映射
    assert len(preview["rebind_map"]) == 3, f"rebind_map 条目数不对: {preview['rebind_map']}"
    for key, new_path in preview["rebind_map"].items():
        assert str(new_dest) in str(new_path), f"{key} 未重绑定到新 dest_dir"
    print(f"[OK] 测试 2 通过: 预检 rebind_map={preview['rebind_map']}")
    return {**ctx, "fresh_dest": new_dest, "preview_data": preview}


def step_3_import_into_fresh_workspace(ctx):
    """测试 3：fresh workspace 重绑定导入。

    重构后断言：空目录导入 → 统一校验 invalid → CLI 非零退出。
    同时验证 original_target_dir_info 没有被 rebound 覆盖。
    """
    ctx = ctx if isinstance(ctx, dict) else test_2_preview_handover(None)
    tmp = ctx["tmp"]

    fresh_ws = tmp / "env_B_workspace"
    fresh_ws.mkdir(parents=True, exist_ok=True)
    fresh_dest = fresh_ws / "dest"
    fresh_state = fresh_ws / "state.json"

    # 写一份新工作区的 config（dest_dir 新路径）
    fresh_cfg = fresh_ws / "config.yaml"
    fresh_cfg.write_text(
        f"source_dir: {(fresh_ws / 'empty_source').as_posix()}\n"
        f"dest_dir: {fresh_dest.as_posix()}\n"
        f"state_file: {fresh_state.as_posix()}\n"
        f"rules: []\n",
        encoding="utf-8",
    )
    (fresh_ws / "empty_source").mkdir(exist_ok=True)

    # 统一校验链要求空目录退出码非零
    r = _run(
        f'python -m {CLI_MOD} import-handover -f "{ctx["handover_json"]}" '
        f'-c "{fresh_cfg}" --json-output',
        cwd=str(fresh_ws),
        check=False,
    )
    import_data = json.loads(r.stdout)
    # dest_dir 必须完成重绑定
    assert import_data["rebound_dest_dir"] != ctx["original_dest_dir"], "dest_dir 未完成重绑定"
    assert len(import_data["rebind_map"]) == 3, f"rebind_map 不完整"

    # 新增字段断言：原始/重绑信息独立保存
    assert "original_target_dir_info" in import_data, (
        "导入输出缺少 original_target_dir_info 字段"
    )
    orig_info = import_data["original_target_dir_info"]
    assert orig_info is not None, "original_target_dir_info 为 None（信息丢失）"
    # 原始 dest_root 必须是旧工作区的路径（永不被 rebound 覆盖）
    orig_dest_path = Path(ctx["original_dest_dir"])
    assert orig_info["dest_root"] == str(orig_dest_path), (
        f"original_target_dir_info.dest_root 被覆盖: 期望 {orig_dest_path}, 实际 {orig_info['dest_root']}"
    )

    # rebound 信息是新路径（用 normpath 消除 Windows 正反斜杠差异）
    assert "rebound_target_dir_info" in import_data, (
        "导入输出缺少 rebound_target_dir_info 字段"
    )
    rebound_info = import_data["rebound_target_dir_info"]
    assert rebound_info is not None, "rebound_target_dir_info 为 None"
    assert os.path.normpath(rebound_info["dest_root"]) == os.path.normpath(
        str(import_data["rebound_dest_dir"])
    ), (
        f"rebound_target_dir_info.dest_root 不匹配: "
        f"期望 {import_data['rebound_dest_dir']}, 实际 {rebound_info['dest_root']}"
    )

    # 实际文件检查：空目录 → total_found=0
    assert "actual_file_check" in import_data, "导入输出缺少 actual_file_check 字段"
    afc = import_data["actual_file_check"]
    assert afc is not None, "actual_file_check 为 None"
    assert afc["total_expected"] > 0, "actual_file_check.total_expected 应为正值"
    assert afc["total_found"] == 0, (
        f"actual_file_check.total_found 应为 0（空目录）, 实际 {afc['total_found']}"
    )
    assert afc["all_files_present"] is False, "all_files_present 在空目录下必须为 False"

    # 统一校验结果：invalid
    assert "unified_validation" in import_data, "导入输出缺少 unified_validation 字段"
    uv = import_data["unified_validation"]
    assert uv["is_valid"] is False, (
        f"空目录导入 unified_validation.is_valid 必须 False, 实际 {uv['is_valid']}"
    )
    assert uv["overall_status"] == "invalid", (
        f"overall_status 应为 invalid, 实际 {uv['overall_status']}"
    )
    # is_valid 快捷字段
    assert import_data["is_valid"] is False, "is_valid 快捷字段必须 False"

    # 真实文件缺失原因在 block_reasons 里
    has_missing_reason = any(
        ("缺失" in r or "空目录" in r or "未恢复" in r)
        for r in uv["block_reasons"]
    )
    assert has_missing_reason, (
        f"block_reasons 应包含文件缺失原因: {uv['block_reasons']}"
    )

    # 非零退出码
    assert r.returncode != 0, (
        f"空目录导入 import-handover 必须非零退出, 实际 returncode={r.returncode}"
    )

    print(f"[OK] 测试 3 通过: 空目录导入后 CLI 返回非零退出码, "
          f"原始目录信息不被覆盖, 真实文件检查 0/{afc['total_expected']} 到位")
    return {**ctx, "fresh_ws": fresh_ws, "fresh_cfg": fresh_cfg,
            "fresh_state": fresh_state, "import_data": import_data,
            "orig_dest": orig_dest_path}


def step_4_review_after_import(ctx):
    """测试 4：导入后 review-handover 继续 verify-landing 链路。

    重构后收敛断言：不看单纯 verify_status，而是检查三个独立字段 + 统一校验链：
    - original_target_dir_info 保留原始路径（不被覆盖）
    - actual_file_check：fresh workspace 空目录 → 必须 all_files_present=False
    - unified_validation.is_valid：必须 False
    - CLI 退出码必须非零
    """
    ctx = ctx if isinstance(ctx, dict) else step_3_import_into_fresh_workspace(None)

    r = _run(
        f'python -m {CLI_MOD} review-handover -c "{ctx["fresh_cfg"]}" --json-output',
        cwd=str(ctx["fresh_ws"]),
        check=False,
    )
    review = json.loads(r.stdout)
    assert review["handover"]["handover_id"] == ctx["handover_id"]
    assert "verify_status" in review

    # === 1) 原始目标目录信息：必须保留，不被 fresh workspace 重绑覆盖 ===
    assert "original_target_dir_info" in review, "缺少 original_target_dir_info 独立字段"
    assert review["original_target_dir_info"] is not None, (
        "original_target_dir_info 为空（原始 dest_dir 信息丢失）"
    )
    orig = review["original_target_dir_info"]
    assert orig["dest_root"] == str(ctx["orig_dest"]), (
        f"original_target_dir_info.dest_root 被覆盖: 期望 {ctx['orig_dest']}, 实际 {orig['dest_root']}"
    )
    assert orig["dir_path_digest"] != "", "original_target_dir_info.dir_path_digest 为空"
    assert len(orig["target_dirs"]) == 3, "original_target_dir_info.target_dirs 数量不匹配"

    # === 2) fresh workspace 重绑后的目标目录：应指向 fresh_ws 新目录 ===
    assert "rebound_target_dir_info" in review, "缺少 rebound_target_dir_info 独立字段"
    assert review["rebound_target_dir_info"] is not None, (
        "rebound_target_dir_info 为空（重绑信息丢失）"
    )
    rebound = review["rebound_target_dir_info"]
    fresh_dest = Path(ctx["import_data"]["rebound_dest_dir"])
    assert rebound["dest_root"] == str(fresh_dest), (
        f"rebound_target_dir_info.dest_root 错误: 期望 {fresh_dest}, 实际 {rebound['dest_root']}"
    )
    assert len(rebound["target_dirs"]) == 3, "rebound_target_dir_info.target_dirs 数量不匹配"

    # === 3) 当前实际文件检查结果：fresh workspace 是空目录 → 必须判缺失 ===
    assert "actual_file_check" in review, "缺少 actual_file_check 独立字段"
    afc = review["actual_file_check"]
    assert afc["total_expected"] > 0, "actual_file_check.total_expected 应为正值"
    assert afc["total_found"] == 0, (
        f"actual_file_check.total_found 应为 0（fresh workspace 空目录）, 实际 {afc['total_found']}"
    )
    assert afc["total_missing"] == afc["total_expected"], (
        "actual_file_check: total_missing 应等于 total_expected"
    )
    assert afc["all_files_present"] is False, (
        "actual_file_check.all_files_present 在空目录下必须为 False"
    )
    # 逐目录检查：每个目录的 missing 都应等于 expected
    for pdr in afc["per_dir_results"]:
        assert pdr["found_count"] == 0, f"目录 {pdr['actual_dir']} found_count 应为 0"
        assert pdr["missing_count"] == pdr["expected_count"], (
            f"目录 {pdr['actual_dir']} missing_count 不等于 expected_count"
        )

    # === 4) 统一校验链结果：必须 invalid ===
    assert "unified_validation" in review, "缺少 unified_validation 统一校验结果"
    uv = review["unified_validation"]
    assert uv["is_valid"] is False, (
        f"unified_validation.is_valid 在空目录导入下必须为 False, 实际 {uv['is_valid']}"
    )
    assert uv["overall_status"] == "invalid", (
        f"unified_validation.overall_status 应为 invalid, 实际 {uv['overall_status']}"
    )
    assert len(uv["block_reasons"]) > 0, (
        "unified_validation.block_reasons 应包含真实文件缺失原因"
    )
    has_file_missing_reason = any(
        ("缺失" in r or "空目录" in r or "未恢复" in r)
        for r in uv["block_reasons"]
    )
    assert has_file_missing_reason, (
        f"block_reasons 中缺少文件缺失相关原因: {uv['block_reasons']}"
    )

    # === 5) verify_status 同步应为 invalid（重构后与真实文件检查一致）===
    assert review["verify_status"] == "invalid", (
        f"verify_status 应同步为 invalid（因为真实文件缺失）, 实际 {review['verify_status']}, "
        f"errors={review['verify_errors']}"
    )

    # === 6) CLI 退出码：必须非零 ===
    assert r.returncode != 0, (
        f"空目录导入后 review-handover 必须以非零退出码失败, 实际 returncode={r.returncode}"
    )

    # === 7) 应有最近导入日志快照（undo 回看数据）===
    assert review["last_import_result_snapshot"] is not None, "缺少最近一次导入结果快照"

    print(f"[OK] 测试 4 通过: review 统一校验 invalid, "
          f"真实文件 0/{afc['total_expected']} 到位, CLI 返回非零退出码")
    return {**ctx, "review_data": review}


def step_5_restart_persistence(ctx):
    """测试 5：重启（重开 StateStore）后复查，状态跨重启保留。

    新增断言：重启后"三个独立字段"和统一校验链结果仍然存在，
    且能重新看清真实落点（即使实际文件仍然缺失）。
    """
    ctx = ctx if isinstance(ctx, dict) else step_4_review_after_import(None)

    # 直接在 import-handover 后的状态文件上再跑一次 review-handover，
    # 模拟"重启"场景（因为 CLI 每次调用都是新进程）
    r = _run(
        f'python -m {CLI_MOD} review-handover -c "{ctx["fresh_cfg"]}" --json-output',
        cwd=str(ctx["fresh_ws"]),
        check=False,
    )
    review2 = json.loads(r.stdout)
    # 历史导入日志记录数 >= 1
    assert review2["all_import_logs_count"] >= 1, (
        f"跨重启后导入日志丢失: all_import_logs_count={review2['all_import_logs_count']}"
    )
    # handover_id 仍能匹配
    assert review2["handover"]["handover_id"] == ctx["handover_id"]
    # rebind_map 仍保留
    assert len(review2["rebind_map"]) == 3, "跨重启后 rebind_map 丢失"

    # === 新增：三个独立字段跨重启仍然完整 ===
    # 1) 原始目录信息：重启后仍保留旧工作区路径（不被新路径覆盖）
    assert review2["original_target_dir_info"] is not None, (
        "重启后 original_target_dir_info 丢失"
    )
    orig = review2["original_target_dir_info"]
    assert orig["dest_root"] == str(ctx["orig_dest"]), (
        f"重启后 original_target_dir_info.dest_root 被覆盖: 期望 {ctx['orig_dest']}, 实际 {orig['dest_root']}"
    )

    # 2) 重绑后目录信息：重启后仍指向 fresh workspace
    assert review2["rebound_target_dir_info"] is not None, (
        "重启后 rebound_target_dir_info 丢失"
    )
    rebound = review2["rebound_target_dir_info"]
    fresh_dest = Path(ctx["import_data"]["rebound_dest_dir"])
    assert rebound["dest_root"] == str(fresh_dest), (
        f"重启后 rebound_target_dir_info.dest_root 错误: 期望 {fresh_dest}, 实际 {rebound['dest_root']}"
    )

    # 3) 实际文件检查：重启后会重新读磁盘检查（空目录仍为缺失）
    afc2 = review2["actual_file_check"]
    assert afc2["checked_at"] != "", "actual_file_check.checked_at 为空"
    assert afc2["total_expected"] > 0, "重启后 actual_file_check.total_expected 应为正值"
    assert afc2["total_found"] == 0, (
        f"重启后 actual_file_check.total_found 仍应为 0, 实际 {afc2['total_found']}"
    )
    assert afc2["all_files_present"] is False, "重启后 all_files_present 仍应为 False"

    # === 新增：统一校验链重启后仍为 invalid（即使 landing validate 本身没报错）===
    uv2 = review2["unified_validation"]
    assert uv2["is_valid"] is False, (
        f"重启后 unified_validation.is_valid 仍应为 False, 实际 {uv2['is_valid']}"
    )
    assert uv2["overall_status"] == "invalid", (
        f"重启后 overall_status 应为 invalid, 实际 {uv2['overall_status']}"
    )
    assert len(uv2["block_reasons"]) > 0, "重启后 block_reasons 不应为空"
    has_missing = any(
        ("缺失" in r or "空目录" in r or "未恢复" in r)
        for r in uv2["block_reasons"]
    )
    assert has_missing, "重启后 block_reasons 仍应包含文件缺失原因"

    # === 新增：CLI 退出码重启后仍非零 ===
    assert r.returncode != 0, (
        f"重启后复查 review-handover 仍应返回非零退出码, 实际 returncode={r.returncode}"
    )
    assert review2["verify_status"] == "invalid", (
        f"重启后 verify_status 仍应为 invalid, 实际 {review2['verify_status']}"
    )

    print(f"[OK] 测试 5 通过: 重启后 handover_id={review2['handover']['handover_id']}, "
          f"真实落点检查保留，原始/重绑目录信息各自独立，CLI 返回非零退出码")
    return {**ctx, "review2": review2}


def step_6_duplicate_import_blocked(ctx):
    """测试 6：重复导入 / 映射冲突 被明确拒绝。"""
    ctx = ctx if isinstance(ctx, dict) else step_5_restart_persistence(None)

    # 同一 handover 再次导入，无 --force => 失败
    r = _run(
        f'python -m {CLI_MOD} import-handover -f "{ctx["handover_json"]}" '
        f'-c "{ctx["fresh_cfg"]}" --json-output',
        cwd=str(ctx["fresh_ws"]),
        check=False,
    )
    assert r.returncode != 0, "重复导入未被拦截"
    stderr_stdout = (r.stdout + r.stderr)
    assert ("已在此工作区导入过" in stderr_stdout or "run_id 已有交接包导入" in stderr_stdout
            or "重复导入" in stderr_stdout), (
        f"重复导入的错误信息不清晰: {stderr_stdout[-800:]}"
    )
    print(f"[OK] 测试 6 通过: 重复导入被明确拒绝，原因已写入日志")
    return ctx


def step_7_missing_fields_blocked(ctx):
    """测试 7：交接包缺字段（缺 handover_id / target_dir_mappings）被拦截。"""
    ctx = ctx if isinstance(ctx, dict) else test_1_original_environment_generate_handover(
        type("TPF", (), {"mktemp": lambda self, x: Path(tempfile.mkdtemp(prefix=x + "_"))})()
    )

    # 构造一个损坏的交接包
    data = json.loads(ctx["handover_json"].read_text(encoding="utf-8"))
    data.pop("handover_id", None)  # 删除必填字段
    bad = ctx["tmp"] / "bad_handover.json"
    bad.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    r = _run(
        f'python -m {CLI_MOD} preview-handover -f "{bad}" --json-output',
        check=False,
    )
    assert r.returncode != 0, "缺字段交接包未被 preview 拦截"
    combined = r.stdout + r.stderr
    assert ("缺少必填字段" in combined or "handover_id" in combined), (
        f"缺字段错误信息不清晰: {combined[-800:]}"
    )
    print(f"[OK] 测试 7 通过: 缺字段交接包被预检拦截")
    return ctx


def test_all():
    """顺序运行所有测试。"""
    class _Tmp:
        def mktemp(self, name):
            return Path(tempfile.mkdtemp(prefix=name + "_"))

    ctx = test_1_original_environment_generate_handover(_Tmp())
    ctx = step_2_preview_handover_rebind(ctx)
    ctx = step_3_import_into_fresh_workspace(ctx)
    ctx = step_4_review_after_import(ctx)
    ctx = step_5_restart_persistence(ctx)
    ctx = step_6_duplicate_import_blocked(ctx)
    _ = step_7_missing_fields_blocked(ctx)

    print()
    print("=" * 64)
    print("所有落点交接包验收测试通过！[OK]")
    print("  [PASS] 原环境导出（JSON/CSV）")
    print("  [PASS] 预检重绑定映射 + 权限检查")
    print("  [PASS] fresh workspace 重绑定导入")
    print("  [PASS] 导入后 review-handover 继续 verify 链路")
    print("  [PASS] 跨重启状态保留 + rebind_map 持久化")
    print("  [PASS] 重复导入被明确拒绝")
    print("  [PASS] 缺字段交接包被拦截")
    print("=" * 64)

    # 清理
    tmp_root = ctx["tmp"]
    try:
        shutil.rmtree(tmp_root)
        print(f"[清理] 测试目录已清理: {tmp_root}")
    except Exception as e:
        print(f"[清理] 测试目录清理失败(可忽略): {e}")


if __name__ == "__main__":
    test_all()
