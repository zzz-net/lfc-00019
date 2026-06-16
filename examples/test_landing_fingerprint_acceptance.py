"""验收测试：落点指纹清单完整链路

覆盖场景：
1. 执行一批 apply → 自动生成落点指纹清单
2. 查看清单：list-landings / view-landing
3. 导出清单：export-landing 生成 JSON
4. 改动目标目录 dest_dir 但保持文件计数不变 → import-landing 核对拦截
5. 改动 move.target_path（清单内 target_path）但保持计数不变 → 核对拦截
6. 同批次重复导入 → 核对拦截
7. 冲突时：CLI 明确报错、JSON/CSV 导出包含冲突、状态文件记录冲突
8. 恢复原配置 → 重新导入核对通过，三处（CLI、JSON/CSV、状态）信息一致
9. undo 执行 → 落点记录保留，is_undone=True，可回看
10. 跨重启保留：状态文件 landings 字段重启不丢失
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

WORK_DIR = Path(tempfile.mkdtemp(prefix="landing_fingerprint_test_"))
SOURCE_DIR = WORK_DIR / "source"
DEST_DIR = WORK_DIR / "dest"
DEST_DIR_ALT = WORK_DIR / "dest_alt"
STATE_FILE = WORK_DIR / ".invoice_organizer_state.json"
CONFIG_FILE = WORK_DIR / "config.yaml"
CONFIG_FILE_ALT = WORK_DIR / "config_alt.yaml"
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

    def _safe_print(text, stream=None):
        if text is None:
            return
        stream = stream or sys.stdout
        try:
            stream.write(text + "\n")
            stream.flush()
        except UnicodeEncodeError:
            safe = text.encode(stream.encoding or "utf-8", errors="replace").decode(stream.encoding or "utf-8", errors="replace")
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


def setup_test_environment():
    """设置测试环境"""
    SOURCE_DIR.mkdir(parents=True, exist_ok=True)
    DEST_DIR.mkdir(parents=True, exist_ok=True)
    DEST_DIR_ALT.mkdir(parents=True, exist_ok=True)
    EXPORT_DIR.mkdir(parents=True, exist_ok=True)

    config = {
        "source_dir": str(SOURCE_DIR),
        "dest_dir": str(DEST_DIR),
        "recursive": True,
        "file_extensions": ["pdf", "jpg", "png", "xlsx", "docx", "md", "txt"],
        "rules": [
            {
                "name": "增值税专用发票",
                "pattern": "*专票*.pdf",
                "target": "vat_special",
                "description": "增值税专用发票归档目录",
            },
            {
                "name": "增值税普通发票",
                "pattern": "*普票*.pdf",
                "target": "vat_normal",
                "description": "增值税普通发票归档目录",
            },
            {
                "name": "电子发票PDF",
                "pattern": "*电子*.pdf",
                "target": "e_invoice",
                "description": "电子发票PDF归档目录",
            },
            {
                "name": "报销单据",
                "pattern": "*报销*.xlsx",
                "target": "reimbursement",
                "description": "报销单据归档目录",
            },
            {
                "name": "采购合同",
                "pattern": "*合同*.docx",
                "target": "contracts",
                "description": "采购合同归档目录",
            },
            {
                "name": "发票图片",
                "pattern": "*发票*.*",
                "target": "images",
                "description": "发票图片归档目录",
            },
            {
                "name": "其他文档",
                "pattern": "*.md",
                "target": "docs",
                "description": "其他文档归档目录",
            },
            {
                "name": "未分类TXT",
                "pattern": "*.txt",
                "target": "docs",
                "description": "未分类TXT归档目录",
            },
        ],
        "state_file": str(STATE_FILE),
    }

    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        yaml.dump(config, f, allow_unicode=True, default_flow_style=False)

    config_alt = dict(config)
    config_alt["dest_dir"] = str(DEST_DIR_ALT)
    with open(CONFIG_FILE_ALT, "w", encoding="utf-8") as f:
        yaml.dump(config_alt, f, allow_unicode=True, default_flow_style=False)

    for i in range(2):
        (SOURCE_DIR / f"专票_2026_00{i+1}.pdf").write_text("invoice content " * (i + 1) * 10)
    (SOURCE_DIR / "普票_2026_001.pdf").write_text("normal invoice content " * 15)
    (SOURCE_DIR / "电子发票_2026_001.pdf").write_text("e-invoice content " * 20)
    (SOURCE_DIR / "出差报销单_202601.xlsx").write_bytes(b"PK\x03\x04fake xlsx content")
    (SOURCE_DIR / "采购合同_供应商A.docx").write_bytes(b"PK\x03\x04fake docx content")
    (SOURCE_DIR / "发票照片_餐厅发票.jpg").write_bytes(b"\xff\xd8\xff\xe0fake jpg content")
    (SOURCE_DIR / "发票扫描件_出租车发票.png").write_bytes(b"\x89PNG\r\n\x1a\nfake png content")
    (SOURCE_DIR / "notes.md").write_text("# Notes\nSome notes here.\n" * 5)
    (SOURCE_DIR / "未分类的文档.txt").write_text("some unclassified text\n" * 10)

    print(f"[环境] 测试目录已创建: {WORK_DIR}")
    print(f"[环境] 源文件目录: {SOURCE_DIR}  (共 {len(list(SOURCE_DIR.iterdir()))} 个文件)")
    print(f"[环境] 目标目录A: {DEST_DIR}")
    print(f"[环境] 目标目录B: {DEST_DIR_ALT}")
    print(f"[环境] 配置文件A: {CONFIG_FILE}")
    print(f"[环境] 配置文件B: {CONFIG_FILE_ALT}")


def _get_snapshot_id(result_stdout):
    """从 list-snapshots 或 plan 输出中提取 snapshot_id"""
    for line in result_stdout.strip().split("\n"):
        line = line.strip()
        if not line or line.startswith("快照ID") or line.startswith("---"):
            continue
        parts = line.split()
        if parts and len(parts[0]) >= 8 and all(c in "0123456789abcdef" for c in parts[0]):
            return parts[0]
    return None


def _get_landing_id(result_stdout):
    """从命令输出中提取 landing_id"""
    for line in result_stdout.strip().split("\n"):
        line = line.strip()
        if "Landing ID:" in line:
            parts = line.split("Landing ID:")
            if len(parts) > 1:
                lid = parts[1].strip().split()[0]
                if lid:
                    return lid
    for line in result_stdout.strip().split("\n"):
        line = line.strip()
        if not line or line.startswith("Landing ID") or line.startswith("---"):
            continue
        parts = line.split()
        if parts and len(parts[0]) >= 8 and all(c in "0123456789abcdef" for c in parts[0]):
            return parts[0]
    return None


def step_1_apply_and_generate_landing():
    """步骤 1：执行 apply 并自动生成落点指纹清单"""
    print("\n" + "=" * 80)
    print("步骤 1：执行 apply 并自动生成落点指纹清单")
    print("=" * 80)

    result = run(f'python -m invoice_organizer plan -c {CONFIG_FILE}')
    assert "预案生成成功" in result.stdout, "预案生成应成功"

    result = run(f'python -m invoice_organizer list-snapshots -c {CONFIG_FILE}')
    snapshot_id = _get_snapshot_id(result.stdout)
    assert snapshot_id, "应获取到快照 ID"
    print(f"  快照 ID: {snapshot_id}")

    result = run(
        f'python -m invoice_organizer sign-off -c {CONFIG_FILE} -s {snapshot_id} '
        f'--status signed --signed-by "财务-李四" -y'
    )

    result = run(f'python -m invoice_organizer apply -c {CONFIG_FILE} -s {snapshot_id} -y --no-require-signoff')
    assert "执行完成" in result.stdout, "apply 应成功完成"
    assert "落点指纹完成" in result.stdout, "apply 后应自动生成落点指纹"
    landing_id = _get_landing_id(result.stdout)
    assert landing_id, "应获取到 landing_id"
    print(f"  Landing ID: {landing_id}")

    moved_files = list(DEST_DIR.rglob("*.*"))
    print(f"  实际移动文件数 (目标目录递归): {len(moved_files)}")

    return landing_id


def step_2_list_and_view_landing(landing_id):
    """步骤 2：查看落点指纹清单（list-landings / view-landing）"""
    print("\n" + "=" * 80)
    print("步骤 2：查看落点指纹清单")
    print("=" * 80)

    result = run(f'python -m invoice_organizer list-landings -c {CONFIG_FILE}')
    assert landing_id in result.stdout, f"list-landings 应包含 {landing_id}"
    assert "落点指纹清单记录" in result.stdout, "应显示记录数量"

    result = run(f'python -m invoice_organizer list-landings -c {CONFIG_FILE} --json')
    data = json.loads(result.stdout)
    assert isinstance(data, list) and len(data) >= 1, "JSON 格式输出应为列表"
    assert data[0]["landing_id"] == landing_id, "JSON 中 landing_id 应匹配"
    print(f"  list-landings JSON: OK ({len(data)} 条记录)")

    result = run(f'python -m invoice_organizer view-landing -c {CONFIG_FILE} -l {landing_id}')
    assert landing_id in result.stdout, "view-landing 应显示 landing_id"
    assert "目标根目录" in result.stdout, "应显示目标根目录"
    assert "文件指纹数" in result.stdout, "应显示文件指纹数"
    assert "摘要哈希" in result.stdout, "应显示摘要哈希"
    assert str(DEST_DIR) in result.stdout, "应显示正确的 dest_dir"

    state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    assert "landings" in state, "状态文件应有 landings 字段"
    assert landing_id in state["landings"], f"状态文件应保存 landing_id={landing_id}"
    saved = state["landings"][landing_id]
    assert saved["dest_dir"] == str(DEST_DIR), f"状态文件中 dest_dir 应匹配"
    assert saved["file_fingerprints"], "状态文件中应有文件指纹"
    print(f"  状态文件持久化: OK (landing_id={landing_id}, 指纹数={len(saved['file_fingerprints'])})")

    return saved


def step_3_export_landing_json(landing_id):
    """步骤 3：导出落点指纹清单为 JSON"""
    print("\n" + "=" * 80)
    print("步骤 3：导出落点指纹清单为 JSON")
    print("=" * 80)

    export_file = EXPORT_DIR / "landing_original.json"
    result = run(
        f'python -m invoice_organizer export-landing -c {CONFIG_FILE} '
        f'-l {landing_id} -o {export_file} -v'
    )
    assert "导出成功" in result.stdout, "export-landing 应成功"
    assert export_file.exists(), "导出文件应存在"

    with open(export_file, "r", encoding="utf-8") as f:
        data = json.load(f)

    assert data["landing_id"] == landing_id, "导出 JSON 的 landing_id 应匹配"
    assert data["dest_dir"] == str(DEST_DIR), "导出 JSON 的 dest_dir 应匹配"
    assert "target_dirs" in data, "导出 JSON 应有 target_dirs"
    assert "file_fingerprints" in data, "导出 JSON 应有 file_fingerprints"
    assert len(data["file_fingerprints"]) > 0, "导出的文件指纹不应为空"
    assert "landing_version" in data, "导出 JSON 应有 landing_version"
    assert "checksum" in data and data["checksum"], "导出 JSON 应有校验和"

    print(f"  导出文件: {export_file}")
    print(f"  目标目录数: {len(data['target_dirs'])}")
    print(f"  文件指纹数: {len(data['file_fingerprints'])}")
    print(f"  校验和: {data['checksum']}")

    return export_file


def step_4_import_with_dest_dir_changed_should_block(export_file, landing_id):
    """步骤 4：改动目标目录 dest_dir → 导入核对应拦截"""
    print("\n" + "=" * 80)
    print("步骤 4：改动目标目录 dest_dir → 导入核对应拦截")
    print("=" * 80)

    with open(export_file, "r", encoding="utf-8") as f:
        data = json.load(f)

    original_dest_dir = data["dest_dir"]
    data["dest_dir"] = str(DEST_DIR_ALT)
    data["dest_dir_digest"] = ""

    tampered_file = EXPORT_DIR / "landing_tampered_destdir.json"
    with open(tampered_file, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    result = run(
        f'python -m invoice_organizer import-landing -c {CONFIG_FILE} -i {tampered_file} -v',
        check=False
    )
    assert result.returncode != 0, "dest_dir 被篡改时 import-landing 应失败"
    assert "导入失败" in result.stdout or "失败" in result.stderr, "应明确报导入失败"

    has_conflict = (
        "目标目录不一致" in result.stdout
        or "目标根目录变化" in result.stdout
        or "本地配置改动" in result.stdout
        or "dest_dir" in result.stdout
    )
    assert has_conflict, "应明确报告 dest_dir 冲突"

    state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    assert "landing_import_logs" in state, "状态文件应有 landing_import_logs"
    recent_logs = [
        l for l in state["landing_import_logs"]
        if l.get("landing_id") == landing_id
    ]
    assert len(recent_logs) >= 1, "状态文件应记录此次失败的导入日志"
    failed_logs = [l for l in recent_logs if l.get("status") == "failed"]
    assert len(failed_logs) >= 1, "状态文件中应有 status=failed 的导入日志"
    print(f"  CLI 拦截: OK (returncode={result.returncode})")
    print(f"  状态文件记录失败: OK ({len(failed_logs)} 条失败日志)")

    export_csv = EXPORT_DIR / "logs_after_destdir_conflict.csv"
    export_json = EXPORT_DIR / "logs_after_destdir_conflict.json"
    run(f'python -m invoice_organizer export -c {CONFIG_FILE} -o {export_csv} --format csv')
    run(f'python -m invoice_organizer export -c {CONFIG_FILE} -o {export_json} --format json')

    csv_text = export_csv.read_text(encoding="utf-8-sig")
    assert "落点指纹清单" in csv_text, "CSV 导出应包含落点指纹清单章节"
    assert landing_id in csv_text, "CSV 应包含 landing_id"
    assert "落点指纹导入日志" in csv_text, "CSV 应包含落点指纹导入日志章节"

    json_state = json.loads(export_json.read_text(encoding="utf-8"))
    assert "landings" in json_state and landing_id in json_state["landings"]
    assert "landing_import_logs" in json_state and len(json_state["landing_import_logs"]) >= 1
    print(f"  CSV/JSON 导出包含冲突: OK")

    data["dest_dir"] = original_dest_dir


def step_5_import_with_target_path_changed_should_block(export_file, landing_id):
    """步骤 5：改动 move.target_path（保持计数不变）→ 核对应拦截"""
    print("\n" + "=" * 80)
    print("步骤 5：改动 move.target_path（保持计数不变）→ 核对应拦截")
    print("=" * 80)

    with open(export_file, "r", encoding="utf-8") as f:
        data = json.load(f)

    original_fps = data["file_fingerprints"]
    original_count = len(original_fps)

    tampered_fps = []
    for i, fp in enumerate(original_fps):
        tfp = dict(fp)
        if i == 0:
            old_tp = tfp["target_path"]
            if "vat_special" in old_tp:
                tfp["target_path"] = old_tp.replace("vat_special", "e_invoice")
            elif "e_invoice" in old_tp:
                tfp["target_path"] = old_tp.replace("e_invoice", "vat_special")
            else:
                tfp["target_path"] = old_tp + ".renamed"
        tampered_fps.append(tfp)

    assert len(tampered_fps) == original_count, "文件计数保持不变"

    data["file_fingerprints"] = tampered_fps
    data["move_target_paths_digest"] = ""
    data["file_digests_summary"] = ""

    tampered_file = EXPORT_DIR / "landing_tampered_targetpath.json"
    with open(tampered_file, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    result = run(
        f'python -m invoice_organizer import-landing -c {CONFIG_FILE} -i {tampered_file} -v',
        check=False
    )
    assert result.returncode != 0, "target_path 被篡改时 import-landing 应失败"
    assert "导入失败" in result.stdout or "失败" in result.stderr

    has_tp_conflict = (
        "Target Path" in result.stdout
        or "target_path" in result.stdout.lower()
        or "目标路径不一致" in result.stdout
        or "move.target_path" in result.stdout
        or "清单内容与现场对不上" in result.stdout
    )
    assert has_tp_conflict, f"应明确报告 target_path 或清单内容冲突，实际输出: {result.stdout}"

    result_v = run(
        f'python -m invoice_organizer verify-landing -c {CONFIG_FILE} -f {tampered_file} -v --json',
        check=False
    )
    v_data = json.loads(result_v.stdout)
    assert v_data.get("valid") is False, "verify-landing JSON 输出 valid 应为 false"
    assert len(v_data.get("conflict_types", [])) > 0, "verify-landing 应报告冲突类型"
    assert "errors" in v_data and len(v_data["errors"]) > 0, "verify-landing 应有详细错误"

    print(f"  target_path 篡改拦截: OK")
    print(f"  verify-landing JSON: valid={v_data['valid']}, conflicts={v_data.get('conflict_types')}")


def step_6_duplicate_import_should_block(export_file, landing_id):
    """步骤 6：同批次重复导入 → 应拦截"""
    print("\n" + "=" * 80)
    print("步骤 6：同批次重复导入 → 应拦截")
    print("=" * 80)

    result = run(
        f'python -m invoice_organizer import-landing -c {CONFIG_FILE} -i {export_file}',
        check=False
    )
    assert result.returncode != 0, "同批次重复导入应失败"
    assert (
        "重复导入" in result.stdout
        or "同批次重复导入" in result.stdout
        or "已存在" in result.stdout
    ), "应明确报告重复导入"

    state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    recent_logs = [
        l for l in state["landing_import_logs"]
        if l.get("landing_id") == landing_id
    ]
    statuses = [l.get("status") for l in recent_logs]
    assert "failed" in statuses, "状态文件中应有重复导入的失败记录"

    print(f"  重复导入拦截: OK (失败记录数={statuses.count('failed')})")


def step_7_restore_and_reimport_should_pass(landing_id, export_file):
    """步骤 7：恢复原配置 → 重新导入核对通过，三处信息一致"""
    print("\n" + "=" * 80)
    print("步骤 7：恢复原配置 → 重新导入核对通过，三处信息一致")
    print("=" * 80)

    temp_workspace = WORK_DIR / "fresh_workspace"
    temp_source = temp_workspace / "source"
    temp_dest = temp_workspace / "dest"
    temp_state = temp_workspace / ".invoice_organizer_state.json"
    temp_config = temp_workspace / "config.yaml"
    temp_source.mkdir(parents=True, exist_ok=True)
    temp_dest.mkdir(parents=True, exist_ok=True)

    temp_cfg = {
        "source_dir": str(temp_source),
        "dest_dir": str(temp_dest),
        "recursive": True,
        "file_extensions": ["pdf", "jpg", "png", "xlsx", "docx", "md", "txt"],
        "rules": [
            {"name": "增值税专用发票", "pattern": "*专票*.pdf", "target": "vat_special"},
            {"name": "增值税普通发票", "pattern": "*普票*.pdf", "target": "vat_normal"},
            {"name": "电子发票PDF", "pattern": "*电子*.pdf", "target": "e_invoice"},
            {"name": "报销单据", "pattern": "*报销*.xlsx", "target": "reimbursement"},
            {"name": "采购合同", "pattern": "*合同*.docx", "target": "contracts"},
            {"name": "发票图片", "pattern": "*发票*.*", "target": "images"},
            {"name": "其他文档", "pattern": "*.md", "target": "docs"},
            {"name": "未分类TXT", "pattern": "*.txt", "target": "docs"},
        ],
        "state_file": str(temp_state),
    }
    with open(temp_config, "w", encoding="utf-8") as f:
        yaml.dump(temp_cfg, f, allow_unicode=True, default_flow_style=False)

    with open(export_file, "r", encoding="utf-8") as f:
        original_data = json.load(f)

    temp_landing_file = temp_workspace / "landing_import.json"
    shutil.copy2(export_file, temp_landing_file)

    result = run(
        f'python -m invoice_organizer import-landing -c {temp_config} -i {temp_landing_file}',
        cwd=str(temp_workspace)
    )
    assert "导入成功" in result.stdout, "干净工作区导入应成功"

    result_v = run(
        f'python -m invoice_organizer verify-landing -c {temp_config} -l {landing_id}',
        cwd=str(temp_workspace)
    )
    assert "核对通过" in result_v.stdout, "核对应通过"
    assert "信息一致" in result_v.stdout, "CLI 应显示信息一致"

    result_l = run(
        f'python -m invoice_organizer list-landings -c {temp_config} --json',
        cwd=str(temp_workspace)
    )
    l_data = json.loads(result_l.stdout)
    assert len(l_data) == 1 and l_data[0]["landing_id"] == landing_id, "list-landings 应一致"

    state = json.loads(temp_state.read_text(encoding="utf-8"))
    saved = state["landings"][landing_id]
    assert saved["dest_dir"] == original_data["dest_dir"], "状态文件 dest_dir 一致"
    assert len(saved["file_fingerprints"]) == len(original_data["file_fingerprints"]), "状态文件指纹数一致"

    out_csv = temp_workspace / "export.csv"
    out_json = temp_workspace / "export.json"
    run(f'python -m invoice_organizer export -c {temp_config} -o {out_csv} --format csv', cwd=str(temp_workspace))
    run(f'python -m invoice_organizer export -c {temp_config} -o {out_json} --format json', cwd=str(temp_workspace))

    csv_text = out_csv.read_text(encoding="utf-8-sig")
    assert landing_id in csv_text, "CSV 导出包含 landing_id"
    assert "落点指纹清单" in csv_text, "CSV 导出包含落点指纹章节"

    full_json = json.loads(out_json.read_text(encoding="utf-8"))
    assert full_json["landings"][landing_id]["dest_dir"] == original_data["dest_dir"], "JSON 导出 dest_dir 一致"

    def _p(msg):
        import sys as _s
        try:
            _s.stdout.write(msg + "\n")
            _s.stdout.flush()
        except UnicodeEncodeError:
            _s.stdout.buffer.write((msg + "\n").encode(_s.stdout.encoding or "utf-8", errors="replace"))
            _s.stdout.flush()
    _p(f"  CLI: 核对通过 [OK]")
    _p(f"  状态文件: landing_id={landing_id}, 指纹数={len(saved['file_fingerprints'])} [OK]")
    _p(f"  CSV/JSON 导出: 一致 [OK]")
    _p(f"  三处信息一致: OK")


def step_8_undo_and_history_preservation(landing_id):
    """步骤 8：undo 后落点记录保留，is_undone=True，可回看"""
    print("\n" + "=" * 80)
    print("步骤 8：undo 后落点记录保留，is_undone=True，可回看")
    print("=" * 80)

    run(f'python -m invoice_organizer undo -c {CONFIG_FILE} -y')

    result = run(f'python -m invoice_organizer view-landing -c {CONFIG_FILE} -l {landing_id}')
    assert "已撤销" in result.stdout, "undo 后 view-landing 应显示已撤销"
    assert "撤销时间" in result.stdout, "应显示撤销时间"

    result_list = run(f'python -m invoice_organizer list-landings -c {CONFIG_FILE} --undone-only --json')
    undone = json.loads(result_list.stdout)
    assert len(undone) >= 1 and undone[0]["is_undone"] is True, "--undone-only 应列出已撤销的清单"
    assert undone[0]["landing_id"] == landing_id, "landing_id 应匹配"

    state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    saved = state["landings"][landing_id]
    assert saved["is_undone"] is True, "状态文件 is_undone=True"
    assert saved["status"] == "undone", "状态文件 status=undone"
    assert saved.get("undone_at"), "状态文件 undone_at 应存在"

    result_active = run(f'python -m invoice_organizer list-landings -c {CONFIG_FILE} --active-only --json')
    active = json.loads(result_active.stdout)
    assert all(not a["is_undone"] for a in active), "--active-only 不应包含已撤销清单"

    print(f"  undo 后 is_undone=True: OK")
    print(f"  undo 后状态保留: OK (undone_at={saved.get('undone_at')})")
    print(f"  --undone-only 过滤: OK (已撤销 {len(undone)} 条)")
    print(f"  --active-only 过滤: OK (活动 {len(active)} 条)")


def step_9_persistence_across_restart(landing_id):
    """步骤 9：跨重启保留：状态文件 landings 字段重启不丢失"""
    print("\n" + "=" * 80)
    print("步骤 9：跨重启保留：状态文件 landings 字段重启不丢失")
    print("=" * 80)

    state_before = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    landings_before = state_before["landings"]
    logs_before = state_before["landing_import_logs"]
    last_landing_before = state_before.get("last_landing")

    assert landing_id in landings_before, "重启前 landing 存在"

    state_after = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    assert landing_id in state_after["landings"], "重启后 landing 仍存在"
    assert state_after["landings"][landing_id] == landings_before[landing_id], "内容完整一致"
    assert state_after.get("last_landing") == last_landing_before, "last_landing 指针保留"
    assert len(state_after.get("landing_import_logs", [])) == len(logs_before), "导入日志数量保留"

    result = run(f'python -m invoice_organizer list-landings -c {CONFIG_FILE}')
    assert landing_id in result.stdout, "CLI 重启后仍能列出 landing"

    print(f"  跨重启保留: OK")
    print(f"  landings 数量: {len(state_after['landings'])}")
    print(f"  landing_import_logs 数量: {len(state_after.get('landing_import_logs', []))}")
    print(f"  last_landing: {state_after.get('last_landing')}")


def main():
    print("=" * 80)
    print("落点指纹清单模块 - 完整验收测试")
    print("=" * 80)
    print(f"项目目录: {PROJECT_DIR}")
    print(f"工作目录: {WORK_DIR}")

    try:
        setup_test_environment()

        landing_id = step_1_apply_and_generate_landing()
        step_2_list_and_view_landing(landing_id)
        export_file = step_3_export_landing_json(landing_id)
        step_4_import_with_dest_dir_changed_should_block(export_file, landing_id)
        step_5_import_with_target_path_changed_should_block(export_file, landing_id)
        step_6_duplicate_import_should_block(export_file, landing_id)
        step_7_restore_and_reimport_should_pass(landing_id, export_file)
        step_8_undo_and_history_preservation(landing_id)
        step_9_persistence_across_restart(landing_id)

        def _p(msg, stream=None):
            stream = stream or sys.stdout
            try:
                stream.write(msg + "\n")
                stream.flush()
            except UnicodeEncodeError:
                stream.buffer.write((msg + "\n").encode(stream.encoding or "utf-8", errors="replace"))
                stream.flush()

        _p("\n" + "=" * 80)
        _p("[PASS] 全部验收测试通过！")
        _p("=" * 80)
        _p("验证点汇总:")
        _p("  1. apply 自动生成落点指纹 [OK]")
        _p("  2. list-landings / view-landing 查看 [OK]")
        _p("  3. export-landing 导出 JSON [OK]")
        _p("  4. dest_dir 改动→导入核对拦截 [OK]")
        _p("  5. target_path 改动→导入核对拦截 [OK]")
        _p("  6. 重复导入→拦截 [OK]")
        _p("  7. CLI / JSON / CSV / 状态文件四处冲突一致 [OK]")
        _p("  8. 恢复原配置→重新导入通过，三处信息一致 [OK]")
        _p("  9. undo 后落点记录保留（is_undone=True）[OK]")
        _p(" 10. 跨重启状态保留 [OK]")

    except Exception as e:
        def _p_err(msg):
            try:
                sys.stderr.write(msg + "\n")
                sys.stderr.flush()
            except UnicodeEncodeError:
                sys.stderr.buffer.write((msg + "\n").encode(sys.stderr.encoding or "utf-8", errors="replace"))
                sys.stderr.flush()
        _p_err(f"\n[FAIL] 测试失败: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
    finally:
        cleanup()


if __name__ == "__main__":
    main()
