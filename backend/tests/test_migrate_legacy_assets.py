"""P3-② 隔壁资产落盘的判据：白名单 / 两道凭据闸 / 清单 / 复验 / 只读源。

全程在 ``tmp_path`` 里造一棵**形状相同的假源树**跑，不碰真隔壁目录：真源会变（今天还在写），
测试要能天天跑。真源上的实跑由 CLI 在切换日做，产物留档。
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from backend.scripts import migrate_legacy_assets as cli
from backend.shared.legacy_assets import (
    ARCHIVE_DIR,
    MANIFEST_NAME,
    MANIFEST_SEAL,
    REPORT_NAME,
    apply_plan,
    hard_secret_hits,
    plan_legacy_assets,
    redact_json,
    verify,
)

#: 硬形状样例（**故意不是真密钥**，只是形状对，用来验闸门会响）
FAKE_OPENAI_KEY = "sk-" + "A" * 32
#: 软形状样例：Pine/webhook 模板里天然有的字段名（与隔壁 .pine 实测形态一致）
FAKE_PINE = (
    "//@version=5\n"
    'strategy("webhook")\n'
    '// {"token" : "abcd12345678", "action": "buy"}\n'
    "plot(close)\n"
)


# --- 夹具 -------------------------------------------------------------------


def _write(root: Path, rel: str, data: bytes | str) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data.encode("utf-8") if isinstance(data, str) else data)
    return path


def _make_source(root: Path) -> Path:
    """造一棵覆盖全部类别的假源树（含该被闸掉的 4 只、该告警的 1 只）。"""
    _write(root, "logs/live_ledger.json", json.dumps({"agents": {}}) + "\n")
    _write(root, "logs/live_trade_2026-09-23.jsonl", '{"fill": 1}\n{"fill": 2}\n')
    _write(root, "logs/live_watch_2026-09-23.jsonl", '{"watch": 1}\n')
    _write(root, "logs/budget/state.json", json.dumps({"tier": "defensive"}) + "\n")
    _write(root, "logs/review/2026-09-23.md", "# 复盘\n")
    _write(root, "logs/decision_pool.jsonl", '{"pool": 1}\n')
    _write(root, "data/live_watch.json", json.dumps({"watch": []}) + "\n")
    _write(
        root, "data/agent_data_astock/2026-09-23/0001.json", '{"decision": "hold"}\n'
    )
    _write(root, "data/agent_data_astock/2026-09-22/0002.json", '{"decision": "buy"}\n')
    _write(root, "data/pine_library/source/0001.pine", FAKE_PINE)
    _write(root, "data/events/2026-09-23.parquet", b"PAR1\x00\x01\x02PAR1")
    _write(root, "data/risk_block.json", json.dumps({"block": []}) + "\n")
    _write(root, "data/长期排除清单_2026.md", "# 排除\n")
    _write(root, "data/lab_batch/run1.json", '{"batch": 1}\n')
    _write(
        root,
        "configs/default_config.json",
        json.dumps(
            {
                "models": {"name": "deepseek", "openai_api_key": FAKE_OPENAI_KEY},
                "risk": {"max_position": 0.2},
            }
        ),
    )
    # 配置目录里的非 JSON：说明文档与补丁脚本照搬（不走脱敏转换）
    _write(root, "configs/README.md", "# 配置说明\n")
    _write(root, "configs/patches/live_fill_patch.py", "PATCH = True\n")
    # 以下四只：白名单会选中，但分别该被路径闸 / 内容闸拦下
    _write(root, "configs/api_token.json", json.dumps({"token": FAKE_OPENAI_KEY}))
    _write(root, "configs/futu.key", "-----BEGIN PRIVATE KEY-----\nzzz\n")
    _write(root, "data/live_watch_halt.json", json.dumps({"k": FAKE_OPENAI_KEY}))
    _write(root, "configs/broker_secret.json", json.dumps({"s": 1}))
    # 以下：不在任何白名单里（**不搬**，也不该出现在跳过表里——它们根本没被选）
    _write(root, ".env", f"OPENAI_API_KEY={FAKE_OPENAI_KEY}\n")
    _write(root, "config/brokers.json", '{"private_key": "x"}\n')
    _write(root, "logs/random.log", "noise\n")
    _write(root, "data/pine_transpile/0203/prompt.md", "不搬的研究中间产物\n")
    return root


def _plan(root: Path):
    return plan_legacy_assets(root)


def _paths(plan) -> set[str]:
    return {f.path for f in plan.files}


def _apply(root: Path, dest: Path):
    plan = _plan(root)
    report = apply_plan(plan, dest)
    assert report.ok, report.problems
    return plan, report


def _scan_tree(root: Path, needle: str) -> list[str]:
    hits = []
    for p in sorted(Path(root).rglob("*")):
        if p.is_file() and needle in p.read_bytes().decode("latin-1"):
            hits.append(str(p))
    return hits


# --- 选择：白名单 + 路径闸 ---------------------------------------------------


def test_plan_selects_only_whitelisted_files(tmp_path):
    """白名单之外的一律不搬：``.env`` / ``config/`` / 研究中间产物 / 无关日志。"""
    src = _make_source(tmp_path / "src")
    plan = _plan(src)

    got = _paths(plan)
    assert "data/agent_data_astock/2026-09-23/0001.json" in got
    assert "data/agent_data_astock/2026-09-22/0002.json" in got
    assert "data/pine_library/source/0001.pine" in got
    assert "configs/default_config.json" in got
    for absent in (
        ".env",
        "config/brokers.json",
        "logs/random.log",
        "data/pine_transpile/0203/prompt.md",
    ):
        assert absent not in got, f"{absent} 不该被白名单选中"
        assert absent not in {s.path for s in plan.skipped}


def test_directory_categories_are_not_silently_empty(tmp_path):
    """py3.10 的 ``Path.glob('**')`` 只吐目录 ⇒ 整目录类别会搬空；**专测盯这一条**。"""
    src = _make_source(tmp_path / "src")
    plan = _plan(src)
    decisions = [f for f in plan.files if f.category == "decisions"]
    assert len(decisions) == 2, "整目录类别必须真的展开出文件"
    assert plan.categories["knowledge"] == 1


def test_path_gate_blocks_credential_shaped_names(tmp_path):
    """路径闸：被 glob 误收的 ``*token*.json`` / ``*.key`` / ``*secret*`` 一律拒绝。"""
    src = _make_source(tmp_path / "src")
    plan = _plan(src)
    skipped = {s.path: s.reason for s in plan.skipped}

    assert "路径闸" in skipped["configs/api_token.json"]
    assert "路径闸" in skipped["configs/futu.key"]
    assert "路径闸" in skipped["configs/broker_secret.json"]
    assert not plan.ok and not plan.problems, "跳过 = 要人看一眼，不是阻断"
    # 非凭据类名字的文件不受路径闸影响（闸不能连坐）
    assert "configs/default_config.json" in _paths(plan)


def test_content_gate_refuses_hard_secrets(tmp_path):
    """内容闸（硬）：落地字节里出现 ``sk-`` ⇒ 该文件不落盘、留痕。"""
    src = _make_source(tmp_path / "src")
    plan = _plan(src)
    skipped = {s.path: s.reason for s in plan.skipped}

    assert "data/live_watch_halt.json" in skipped
    assert "openai-key" in skipped["data/live_watch_halt.json"]
    assert "data/live_watch_halt.json" not in _paths(plan)


def test_non_json_configs_are_copied_not_choked_on(tmp_path):
    """``configs/**`` 里不只有 JSON：README / 补丁脚本按文本照搬，不许判成「读不懂」。"""
    src = _make_source(tmp_path / "src")
    plan = _plan(src)
    assert not plan.problems, plan.problems
    assert "configs/README.md" in _paths(plan)
    assert "configs/patches/live_fill_patch.py" in _paths(plan)
    patch = next(
        f for f in plan.files if f.path == "configs/patches/live_fill_patch.py"
    )
    assert patch.transforms == (), "非 JSON 不该带脱敏转换"
    assert patch.source_sha256 == patch.landed_sha256, "照搬就该逐字节相同"


def test_redaction_masks_config_secrets_and_keeps_structure(tmp_path):
    """``configs/`` 走脱敏：密钥字段打码、其余结构原样、转换留痕。"""
    src = _make_source(tmp_path / "src")
    dest = tmp_path / "dest"
    plan, _ = _apply(src, dest)

    cfg = plan.files and next(
        f for f in plan.files if f.path == "configs/default_config.json"
    )
    assert cfg.transforms == ("redact-json",)
    assert cfg.source_sha256 != cfg.landed_sha256
    landed = json.loads((dest / ARCHIVE_DIR / cfg.path).read_text(encoding="utf-8"))
    assert landed["models"]["openai_api_key"] == "<redacted>"
    assert landed["models"]["name"] == "deepseek", "非凭据字段不许动"
    assert landed["risk"] == {"max_position": 0.2}


def test_redaction_refuses_unparseable_json():
    """读不懂的配置**不许当成已脱敏搬走**（无法保证里面没有密钥）。"""
    with pytest.raises(ValueError):
        redact_json(b"{not json")


def test_soft_secret_shapes_copy_with_a_warning(tmp_path):
    """软形状（Pine 模板里的 ``"token": "…"``）照搬但告警——一刀切会把知识资产挡在门外。"""
    src = _make_source(tmp_path / "src")
    plan = _plan(src)
    assert "data/pine_library/source/0001.pine" in _paths(plan)
    assert any("0001.pine" in w for w in plan.warnings)
    assert not plan.ok, "有告警 = 要人看一眼"


def test_hard_secret_detector_reads_binary_safely():
    """硬形状扫描对二进制不炸（parquet 也要过闸）。"""
    assert hard_secret_hits(b"PAR1\xff\xfe\x00PAR1") == ()
    assert "openai-key" in hard_secret_hits(FAKE_OPENAI_KEY.encode())


# --- 落盘 + 清单 -------------------------------------------------------------


def test_apply_writes_files_manifest_seal_and_report(tmp_path):
    src = _make_source(tmp_path / "src")
    dest = tmp_path / "dest"
    plan, report = _apply(src, dest)

    assert report.written == len(plan.files)
    for f in plan.files:
        assert (dest / ARCHIVE_DIR / f.path).is_file()
    lines = (dest / MANIFEST_NAME).read_text(encoding="utf-8").splitlines()
    assert len(lines) == len(plan.files)
    rec = json.loads(lines[0])
    assert set(rec) == {
        "path",
        "category",
        "size",
        "source_sha256",
        "landed_sha256",
        "source_mtime",
        "transforms",
        "warnings",
    }
    seal = (dest / MANIFEST_SEAL).read_text(encoding="ascii").split()[0]
    want = hashlib.sha256((dest / MANIFEST_NAME).read_bytes()).hexdigest()
    assert seal == want
    assert "quant-Trader" in (dest / REPORT_NAME).read_text(encoding="utf-8")


def test_no_secret_shaped_bytes_survive_in_the_landed_tree(tmp_path):
    """**最要紧的一条**：整棵落地区里不许出现硬形状凭据（含 configs 脱敏后）。"""
    src = _make_source(tmp_path / "src")
    dest = tmp_path / "dest"
    _apply(src, dest)

    assert _scan_tree(dest, "sk-") == [], "落地区里出现了 sk- 形状的字节"
    assert _scan_tree(dest, "PRIVATE KEY") == []


def test_apply_is_idempotent_and_the_manifest_is_deterministic(tmp_path):
    """同一份源搬两遍：清单与报告**逐字节相同**（无时间戳），可用来证明幂等。"""
    src = _make_source(tmp_path / "src")
    dest = tmp_path / "dest"
    _apply(src, dest)
    first_manifest = (dest / MANIFEST_NAME).read_bytes()
    first_report = (dest / REPORT_NAME).read_bytes()

    _apply(src, dest)
    assert (dest / MANIFEST_NAME).read_bytes() == first_manifest
    assert (dest / REPORT_NAME).read_bytes() == first_report
    assert verify(dest, source_root=src, check_source=True).ok


def test_apply_does_not_touch_the_source(tmp_path):
    """只读源：搬完源树的字节与 mtime 一只都不许变。"""
    src = _make_source(tmp_path / "src")
    before = {
        p.relative_to(src).as_posix(): (p.stat().st_mtime_ns, p.read_bytes())
        for p in sorted(src.rglob("*"))
        if p.is_file()
    }
    _apply(src, tmp_path / "dest")
    after = {
        p.relative_to(src).as_posix(): (p.stat().st_mtime_ns, p.read_bytes())
        for p in sorted(src.rglob("*"))
        if p.is_file()
    }
    assert before == after


def test_apply_refuses_a_plan_with_problems(tmp_path):
    """源不存在 = 阻断：一只都不写（半份档案比没有档案更难查）。"""
    dest = tmp_path / "dest"
    plan = plan_legacy_assets(tmp_path / "nonexistent")
    assert plan.problems
    report = apply_plan(plan, dest)
    assert not report.ok and report.written == 0
    assert not (dest / ARCHIVE_DIR).exists()


def test_apply_aborts_when_the_source_changed_after_the_plan(tmp_path):
    """计划之后源被改过 ⇒ 整批中止，不留半份（哈希不符就停）。"""
    src = _make_source(tmp_path / "src")
    dest = tmp_path / "dest"
    plan = _plan(src)
    _write(src, "logs/live_ledger.json", json.dumps({"agents": {"x": 1}}) + "\n")

    report = apply_plan(plan, dest)
    assert not report.ok
    assert any("源在计划之后变了" in p for p in report.problems)
    assert report.written == 0


# --- 复验：缺失 / 被改 / 多出 / 清单被改 / 源侧漂移 ----------------------------


def test_verify_passes_on_a_fresh_landing(tmp_path):
    src = _make_source(tmp_path / "src")
    dest = tmp_path / "dest"
    _apply(src, dest)
    v = verify(dest, source_root=src, check_source=True)
    assert v.ok, v.detail()
    assert v.checked == len(plan_legacy_assets(src).files)


def test_verify_catches_truncation(tmp_path):
    src = _make_source(tmp_path / "src")
    dest = tmp_path / "dest"
    _apply(src, dest)
    target = dest / ARCHIVE_DIR / "logs/live_trade_2026-09-23.jsonl"
    target.write_bytes(target.read_bytes()[:-1])

    v = verify(dest)
    assert "logs/live_trade_2026-09-23.jsonl" in v.mismatched
    assert not v.ok


def test_verify_catches_a_deleted_file(tmp_path):
    src = _make_source(tmp_path / "src")
    dest = tmp_path / "dest"
    _apply(src, dest)
    (dest / ARCHIVE_DIR / "logs/budget/state.json").unlink()

    v = verify(dest)
    assert v.missing == ["logs/budget/state.json"]
    assert not v.ok


def test_verify_catches_extra_files(tmp_path):
    """多出和缺失一样可疑：落地区里冒出没人认领的文件必须报出来。"""
    src = _make_source(tmp_path / "src")
    dest = tmp_path / "dest"
    _apply(src, dest)
    _write(dest, f"{ARCHIVE_DIR}/logs/qqbot_token.json", '{"token": "x"}\n')

    v = verify(dest)
    assert v.extra == ["logs/qqbot_token.json"]
    assert not v.ok


def test_verify_catches_a_tampered_manifest(tmp_path):
    src = _make_source(tmp_path / "src")
    dest = tmp_path / "dest"
    _apply(src, dest)
    mpath = dest / MANIFEST_NAME
    mpath.write_text(
        mpath.read_text(encoding="utf-8").replace("live_ledger", "live_ledger_x"),
        encoding="utf-8",
    )

    v = verify(dest)
    assert any("清单被改过" in p for p in v.problems)
    assert not v.ok


def test_verify_reports_missing_manifest(tmp_path):
    v = verify(tmp_path / "empty")
    assert not v.ok and any("清单不存在" in p for p in v.problems)


def test_verify_source_check_flags_drift(tmp_path):
    """源侧重比：搬完之后源又变了（今天还在写）⇒ 报漂移，不当成不一致。"""
    src = _make_source(tmp_path / "src")
    dest = tmp_path / "dest"
    _apply(src, dest)
    assert verify(dest, source_root=src, check_source=True).ok

    _write(src, "logs/live_ledger.json", json.dumps({"agents": {"later": 1}}) + "\n")
    v = verify(dest, source_root=src, check_source=True)
    assert "logs/live_ledger.json" in v.source_drift
    assert not v.mismatched, "漂移不是落地区的问题"
    assert not v.ok


# --- CLI：模式、退出码、落地点守卫 -------------------------------------------


def test_cli_plan_apply_verify_round_trip(tmp_path, capsys):
    src = _make_source(tmp_path / "src")
    dest = tmp_path / "dest"
    common = ["--from", str(src), "--dest", str(dest)]

    assert cli.main([*common]) == cli.EXIT_ATTENTION  # 有跳过/告警 ⇒ 要人看一眼
    assert not dest.exists(), "plan 不许写任何东西"
    assert "[跳过]" in capsys.readouterr().out

    assert cli.main([*common, "--apply"]) == cli.EXIT_ATTENTION
    assert (dest / MANIFEST_NAME).is_file()

    assert cli.main([*common, "--verify"]) == cli.EXIT_OK


def test_cli_verify_refuses_a_missing_manifest(tmp_path, capsys):
    dest = tmp_path / "dest"
    assert cli.main(["--verify", "--dest", str(dest)]) == cli.EXIT_USAGE
    assert not dest.exists()
    assert "清单不存在" in capsys.readouterr().out


def remove_repo_residue(path: Path) -> None:
    """删掉仓库树里那个「永不创建」的名字（不存在就什么都不做）。

    **不用 ``ignore_errors=True``**：这些用例要防的恰恰是「产物进了仓」，静默吞掉删
    不掉的事实就等于把要防的东西留在原地。真删不掉就让它红（容器里跑是 root，删得掉）。
    """
    if path.is_dir():
        shutil.rmtree(path)
    elif path.exists():
        path.unlink()


def test_cli_refuses_a_destination_inside_a_git_worktree(tmp_path, capsys):
    """本仓 ``data/`` 是符号链接、``data/*.sql`` 被跟踪：产物落进工作树会被 git 波及。"""
    src = _make_source(tmp_path / "src")
    inside = Path(__file__).resolve().parents[2] / "backend/tests/_never_created"
    remove_repo_residue(inside)  # 上一次跑（尤其变红的负控跑法）的残留先清掉
    try:
        rc = cli.main(["--from", str(src), "--dest", str(inside), "--apply"])

        assert rc == cli.EXIT_USAGE
        assert not inside.exists(), "守卫必须在写任何东西之前就拦住"
        out = capsys.readouterr().out
        assert "拒绝执行" in out and ("git 工作树" in out or "QuantMind 仓库树" in out)
    finally:
        # 变红时这里躺着一份**真的写进仓**的落地区（实测 17 只，含 configs/ 与 logs/），
        # 由容器里的 root 生成；不清掉，下一次基线会先红在 ``exists()`` 上（看起来像
        # 新 bug），而残留本身还会被 git 收走。所以清理写进 finally。
        remove_repo_residue(inside)


def test_cli_accepts_a_destination_outside_the_worktree(tmp_path):
    assert cli._inside_git_worktree(tmp_path / "dest") is None, (
        "在临时目录里不该判成工作树"
    )


def test_the_refusal_has_a_git_free_criterion_for_the_container(tmp_path):
    """**判据两条并列，且 P5 存档守卫与这里共用同一份**（容器里没有 ``.git``）。

    只问 git 的那一版在容器里恒放行（``/app`` 挂载进来时不带 ``.git``），产物就会躺在
    仓库树里等下一次 ``git add``。所以「在仓库树内」这一条**不依赖 git**，而它必须与
    P5 的 ``--record`` 守卫是**同一个实现**——收紧一处漏掉另一处的代价，正好落在恢复
    现场时唯一还留着证据的那个文件上。
    """
    from backend.shared import migration_paths
    from backend.scripts import migrate_legacy_watch as watch_cli

    root = tmp_path / "repo"
    root.mkdir()
    inside = root / "backend" / "archive.json"  # 目录还不存在（第一次创建）

    # 1) 判据本身：**刻意不 git init**（等价于容器里那棵树），仓库树那条仍然拦
    assert not (root / ".git").exists()
    why = migration_paths.artifact_refusal(inside, project_root=root)
    assert why is not None and "仓库树" in why

    # 2) 两处调用点共用同一份实现：真仓树里的名字，两边都拦（容器里也无 .git）
    in_repo = cli.PROJECT_ROOT / "backend/tests/_never_created_archive"
    assert cli._dest_refusal(in_repo) is not None
    assert watch_cli._record_refusal(in_repo) is not None

    # 3) 树外放行（判据不许把合规落点误判成违规——那会让人去改命令而不是查判据）
    outside = tmp_path / "elsewhere" / "archive.json"
    assert cli._dest_refusal(outside) is None
    assert watch_cli._record_refusal(outside) is None


def test_the_refusal_resolves_symlinks_first(tmp_path):
    """本仓 ``data/`` 就是符号链接：判据必须看 ``resolve()`` 之后的落点。

    只看字面路径会两头都错——合规落点（``data/legacy/...`` 解析后在另一块盘上）被误判
    成违规，或者反过来把一条指向仓里的符号链接放行。
    """
    from backend.shared import migration_paths

    real = tmp_path / "repo"
    (real / "sub").mkdir(parents=True)
    link = tmp_path / "link"
    link.symlink_to(real, target_is_directory=True)

    # 经由符号链接进到根里 ⇒ 拦（字面路径 /tmp/.../link/sub 不在根下）
    why = migration_paths.artifact_refusal(link / "sub" / "a.json", project_root=real)
    assert why is not None and "仓库树" in why

    # 反过来：根自己是符号链接，落点经由**解析后的真身**进到根里 ⇒ 也拦
    why = migration_paths.artifact_refusal(real / "sub" / "a.json", project_root=link)
    assert why is not None and "仓库树" in why


def test_cli_rejects_no_source_check_outside_verify(tmp_path, capsys):
    rc = cli.main(["--from", str(tmp_path), "--no-source-check"])
    assert rc == cli.EXIT_USAGE
    assert "只对 --verify 有意义" in capsys.readouterr().out


def test_cli_reports_a_missing_source(tmp_path, capsys):
    rc = cli.main(
        [
            "--from",
            str(tmp_path / "nope"),
            "--dest",
            str(tmp_path / "dest"),
        ]
    )
    assert rc == cli.EXIT_USAGE
    assert "源目录不存在" in capsys.readouterr().out


def test_cli_source_guards():
    """源码级守卫：脚本与共享模块里**不许出现删除/移动**动作（只读源、只增落地区）。"""
    shared = (
        Path(__file__).resolve().parents[1] / "shared" / "legacy_assets.py"
    ).read_text(encoding="utf-8")
    for banned in (
        "shutil.rmtree",
        "os.remove",
        "os.unlink",
        "Path.unlink",
        "shutil.move",
    ):
        assert banned not in shared, f"共享模块出现了删除动作：{banned}"
    import shutil as _shutil

    assert not hasattr(cli, "shutil") or "shutil" not in dir(cli)
    src = Path(cli.__file__).read_text(encoding="utf-8")
    assert "shutil" not in src


def test_shared_module_never_imports_subprocess(tmp_path):
    """共享模块保持纯净（守卫 git 探测只在 CLI 侧）。"""
    shared = (
        Path(__file__).resolve().parents[1] / "shared" / "legacy_assets.py"
    ).read_text(encoding="utf-8")
    assert "subprocess" not in shared


def test_verify_does_not_write(tmp_path):
    """复验是纯读：跑完落地区的字节与 mtime 一只都不许变。"""
    src = _make_source(tmp_path / "src")
    dest = tmp_path / "dest"
    _apply(src, dest)
    before = {
        p.relative_to(dest).as_posix(): (p.stat().st_mtime_ns, p.read_bytes())
        for p in sorted(dest.rglob("*"))
        if p.is_file()
    }
    assert verify(dest, source_root=src, check_source=True).ok
    after = {
        p.relative_to(dest).as_posix(): (p.stat().st_mtime_ns, p.read_bytes())
        for p in sorted(dest.rglob("*"))
        if p.is_file()
    }
    assert before == after


@pytest.mark.skipif(shutil.which("git") is None, reason="需要 git 才能验证工作树探测")
def test_worktree_probe_asks_git_not_string_prefixes(tmp_path):
    """工作树探测要真的问 git：**自建**一个 git 仓库（容器里 /app 没有 .git）。"""
    repo = tmp_path / "repo"
    (repo / "sub").mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(repo)], check=True, timeout=60)

    assert cli._inside_git_worktree(repo / "sub") == str(repo.resolve())
    assert cli._inside_git_worktree(repo / "sub" / "not-yet-created") is not None, (
        "不存在的目标要往上看最近的已存在祖先"
    )
    assert cli._inside_git_worktree(tmp_path / "elsewhere") is None


def test_script_runs_as_a_module(tmp_path):
    """``python -m backend.scripts.migrate_legacy_assets`` 可直接跑（宿主侧入口）。"""
    cp = subprocess.run(
        [
            sys.executable,
            "-m",
            "backend.scripts.migrate_legacy_assets",
            "--from",
            str(tmp_path / "nope"),
            "--dest",
            str(tmp_path / "dest"),
        ],
        cwd=Path(__file__).resolve().parents[2],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert cp.returncode == cli.EXIT_USAGE
    assert "源目录不存在" in cp.stdout
