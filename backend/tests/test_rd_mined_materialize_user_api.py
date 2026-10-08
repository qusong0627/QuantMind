"""用户自助物化端点（alpha_agent）契约回归。

背景（2026-10-08）：物化此前只有 admin 面板（固定 argv 全量）与 CLI；用户在
因子挖掘/因子库界面勾选因子手动物化没有入口。新增
``POST /factors/materialize`` 与 ``GET /factors/materialize/status``，
本文件钉死五条契约：

1. **归属先行**：他人 / 历史无主（user_id IS NULL，只读）的 id 一律按
   「不存在」拒绝（全拒 → 404），伪造列表不能借端点写公共训练库；
2. **白名单 argv**：进入子进程 argv 的 id 必须过 ``normalize_factor_ids``，
   恶意串（flag / 空格 / 路径）400 且不 spawn（argv 注入 = 任意命令执行）；
3. **跳过词表与物化器同源**：no_code / market_unsupported / already_materialized
   / rejected_duplicate / rejected_gate 直接复用 ``_eligible_row`` /
   ``_should_materialize``，不是另写的近似；
4. **锁纪律与 admin 面共用**：锁忙 409、启动闩忙 409、回包前确认持锁
   （拿不到/退出如实映射 409/500）；
5. **状态面不回日志尾**（跨租户泄露面），且只回当前用户名下条目。
"""

from __future__ import annotations

import inspect
import subprocess
import sys
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

try:  # pragma: no cover - 环境相关（宿主无 pandas 时跳过）
    from backend.scripts import rd_mined_materialize as mat
    from backend.services.engine.routers import alpha_agent as aa
    from backend.shared.factor_identity import code_fingerprint
    from backend.shared.rd_mined_materialize_launch import (
        START_GUARD,
        normalize_factor_ids,
    )
except Exception as _exc:  # noqa: BLE001
    mat = None
    aa = None
    _IMPORT_ERR = _exc

pytestmark = pytest.mark.skipif(
    mat is None or aa is None, reason="依赖不可用（需容器环境）"
)


def _row(
    factor_id: str,
    *,
    name: str = "因子",
    code: str = "close",
    market: str = "a_share",
    user_id: str | None = "u1",
) -> dict:
    return {
        "factor_id": factor_id,
        "factor_name": name,
        "factor_code": code,
        "market": market,
        "user_id": user_id,
    }


def _manifest_entry(code: str, status: str = "materialized") -> dict:
    return {"status": status, "code_fp": code_fingerprint(code), "column": "col_x"}


def _fake_request(user_id: str = "u1", tenant_id: str = "default"):
    return SimpleNamespace(
        state=SimpleNamespace(user={"user_id": user_id, "tenant_id": tenant_id})
    )


@pytest.fixture()
def isolate_env(monkeypatch, tmp_path):
    """锁文件与面板日志指向 tmp：与任何在跑的生产回填互不干扰。"""
    monkeypatch.setenv("RD_MINED_MATERIALIZE_LOCK", str(tmp_path / "run.lock"))
    monkeypatch.setenv("RD_MINED_MATERIALIZE_WEB_LOG", str(tmp_path / "ui.log"))
    return tmp_path


@pytest.fixture()
def fake_candidates(monkeypatch):
    """候选查询替身：记录参数、回放给定行（不碰真 PG）。"""
    calls: list[dict] = []

    def _apply(rows):
        async def _query(**kwargs):
            calls.append(kwargs)
            return list(rows)

        monkeypatch.setattr(mat, "_query_candidates", _query)

    return SimpleNamespace(apply=_apply, calls=calls)


@pytest.fixture()
def fake_manifest(monkeypatch):
    state = {"current": {}}
    monkeypatch.setattr(mat, "_load_manifest", lambda _root: state["current"])
    return state


def _no_spawn(monkeypatch):
    def _must_not_spawn(*_a, **_kw):  # pragma: no cover - 触发即失败
        raise AssertionError("此路径不得启动物化子进程")

    monkeypatch.setattr(subprocess, "Popen", _must_not_spawn)


def _fake_spawn(monkeypatch, *, probes=(False, True), pid: int = 4242):
    """Popen 替身：记录调用；probes 迭代器驱动锁探测时序。"""
    calls: list[dict] = []

    class _FakeProc:
        def __init__(self):
            self.pid = pid

        def poll(self):
            return None  # 存活

        def wait(self):
            return 0

    def _fake_popen(argv, **kwargs):
        calls.append({"argv": argv, **kwargs})
        return _FakeProc()

    probe_iter = iter(probes)
    monkeypatch.setattr(subprocess, "Popen", _fake_popen)
    monkeypatch.setattr(mat, "probe_run_lock", lambda: next(probe_iter))
    return calls


# ── 归属与白名单 ───────────────────────────────────────────────────────


@pytest.mark.unit
@pytest.mark.asyncio
async def test_all_foreign_ids_404_without_spawn(
    isolate_env, monkeypatch, fake_candidates
):
    """全是他人的 id → 404（与单因子端点同口径），不 spawn、不泄露谁存在。"""
    fake_candidates.apply([_row("f1", user_id="u2"), _row("f2", user_id="u3")])
    _no_spawn(monkeypatch)

    with pytest.raises(HTTPException) as excinfo:
        await aa.post_factor_materialize(
            _fake_request("u1"),
            body=aa.FactorMaterializeRequest(factor_ids=["f1", "f2"]),
        )
    assert excinfo.value.status_code == 404


@pytest.mark.unit
@pytest.mark.asyncio
async def test_legacy_null_owner_rejected(isolate_env, monkeypatch, fake_candidates):
    """历史无主因子（user_id IS NULL）只读：写路径一律拒绝（同 for_write 语义）。"""
    fake_candidates.apply([_row("f-old", user_id=None)])
    _no_spawn(monkeypatch)

    with pytest.raises(HTTPException) as excinfo:
        await aa.post_factor_materialize(
            _fake_request("u1"),
            body=aa.FactorMaterializeRequest(factor_ids=["f-old"]),
        )
    assert excinfo.value.status_code == 404


@pytest.mark.unit
@pytest.mark.asyncio
async def test_unknown_id_rejected_like_foreign(
    isolate_env, monkeypatch, fake_candidates
):
    """DB 里根本不存在的 id：与其他人的 id 同一拒绝面（不区分，防探测）。"""
    fake_candidates.apply([_row("f1")])
    _no_spawn(monkeypatch)

    with pytest.raises(HTTPException) as excinfo:
        await aa.post_factor_materialize(
            _fake_request("u1"),
            body=aa.FactorMaterializeRequest(factor_ids=["f-ghost"]),
        )
    assert excinfo.value.status_code == 404


@pytest.mark.unit
@pytest.mark.asyncio
async def test_mixed_foreign_reported_in_details(
    isolate_env, monkeypatch, fake_candidates, fake_manifest
):
    """混合列表：自己的照常物化，他人的进 rejected 明细（不打断整单）。"""
    fake_candidates.apply([_row("f1"), _row("f2", user_id="u2")])
    fake_manifest["current"] = {}
    calls = _fake_spawn(monkeypatch)

    resp = await aa.post_factor_materialize(
        _fake_request("u1"),
        body=aa.FactorMaterializeRequest(factor_ids=["f1", "f2"]),
    )
    data = resp["data"]
    assert data["started"] is True
    assert data["materializable"] == ["f1"]
    assert data["rejected"] == [{"factor_id": "f2", "reason": "not_found"}]
    assert calls[0]["argv"] == mat.build_run_command(["f1"])


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bad",
    [
        ["--force"],
        ["-x"],
        ["a b"],
        ["../../etc/passwd"],
        ["a;b"],
        ["a\nb"],
        [""],
        [],
        ["ok", "x" * 200],
    ],
)
async def test_malformed_ids_400_without_spawn(
    isolate_env, monkeypatch, fake_candidates, bad
):
    """恶意/畸形 id：400 且不 spawn（argv 注入回归护栏）。"""
    fake_candidates.apply([])
    _no_spawn(monkeypatch)

    with pytest.raises(HTTPException) as excinfo:
        await aa.post_factor_materialize(
            _fake_request("u1"), body=aa.FactorMaterializeRequest(factor_ids=bad)
        )
    assert excinfo.value.status_code == 400


@pytest.mark.unit
@pytest.mark.asyncio
async def test_too_many_ids_400_early(isolate_env, monkeypatch, fake_candidates):
    fake_candidates.apply([])
    _no_spawn(monkeypatch)

    with pytest.raises(HTTPException) as excinfo:
        await aa.post_factor_materialize(
            _fake_request("u1"),
            body=aa.FactorMaterializeRequest(factor_ids=[f"f{i}" for i in range(101)]),
        )
    assert excinfo.value.status_code == 400
    assert "100" in excinfo.value.detail


@pytest.mark.unit
def test_normalize_dedupes_preserving_order():
    assert normalize_factor_ids(["b", "a", "b"]) == ["b", "a"]


# ── 跳过词表与物化器同源 ───────────────────────────────────────────────


@pytest.mark.unit
@pytest.mark.asyncio
async def test_skips_no_code_and_non_a_share(
    isolate_env, monkeypatch, fake_candidates, fake_manifest
):
    """无代码 / 非 a_share → 跳过原因与物化器同词表，不 spawn。"""
    fake_candidates.apply(
        [_row("f-nocode", code="  "), _row("f-us", market="us_stock")]
    )
    fake_manifest["current"] = {}
    _no_spawn(monkeypatch)
    monkeypatch.setattr(mat, "probe_run_lock", lambda: False)

    resp = await aa.post_factor_materialize(
        _fake_request("u1"),
        body=aa.FactorMaterializeRequest(factor_ids=["f-nocode", "f-us"]),
    )
    data = resp["data"]
    assert data["started"] is False
    assert data["materializable"] == []
    assert data["skipped"] == {
        "f-nocode": "no_code",
        "f-us": "market_unsupported",
    }


@pytest.mark.unit
@pytest.mark.asyncio
async def test_terminal_manifest_states_skipped(
    isolate_env, monkeypatch, fake_candidates, fake_manifest
):
    """已物化（同代码）/ 重复拒 / 门禁拒：终态一律跳过，防重复物化。"""
    same = "close * 2"
    fake_candidates.apply(
        [
            _row("f-done", code=same),
            _row("f-rej", code="open"),
            _row("f-gate", code="vwap"),  # 指纹与清单一致，才属「已定终态」
        ]
    )
    fake_manifest["current"] = {
        "f-done": _manifest_entry(same, "materialized"),
        "f-rej": _manifest_entry("open", "rejected_duplicate"),
        "f-gate": _manifest_entry("vwap", "rejected_gate"),
    }
    _no_spawn(monkeypatch)
    monkeypatch.setattr(mat, "probe_run_lock", lambda: False)

    resp = await aa.post_factor_materialize(
        _fake_request("u1"),
        body=aa.FactorMaterializeRequest(factor_ids=["f-done", "f-rej", "f-gate"]),
    )
    assert resp["data"]["skipped"] == {
        "f-done": "already_materialized",
        "f-rej": "rejected_duplicate",
        "f-gate": "rejected_gate",
    }


@pytest.mark.unit
@pytest.mark.asyncio
async def test_force_requeues_terminal_states(
    isolate_env, monkeypatch, fake_candidates, fake_manifest
):
    """force=True：终态也重做（API 能力保留；v1 界面不暴露）。"""
    same = "close * 2"
    fake_candidates.apply([_row("f-done", code=same)])
    fake_manifest["current"] = {"f-done": _manifest_entry(same, "materialized")}
    calls = _fake_spawn(monkeypatch)

    resp = await aa.post_factor_materialize(
        _fake_request("u1"),
        body=aa.FactorMaterializeRequest(factor_ids=["f-done"], force=True),
    )
    assert resp["data"]["materializable"] == ["f-done"]
    assert calls[0]["argv"] == mat.build_run_command(["f-done"])


@pytest.mark.unit
@pytest.mark.asyncio
async def test_code_changed_requeued(
    isolate_env, monkeypatch, fake_candidates, fake_manifest
):
    """同 id 代码改写（指纹漂移）→ 必须重算，不算已物化。"""
    fake_candidates.apply([_row("f-changed", code="close * 9")])
    fake_manifest["current"] = {"f-changed": _manifest_entry("close * 2")}
    _fake_spawn(monkeypatch)

    resp = await aa.post_factor_materialize(
        _fake_request("u1"),
        body=aa.FactorMaterializeRequest(factor_ids=["f-changed"]),
    )
    assert resp["data"]["materializable"] == ["f-changed"]


# ── 锁纪律与 spawn ─────────────────────────────────────────────────────


@pytest.mark.unit
@pytest.mark.asyncio
async def test_lock_busy_409_no_spawn(
    isolate_env, monkeypatch, fake_candidates, fake_manifest
):
    fake_candidates.apply([_row("f1")])
    fake_manifest["current"] = {}
    _no_spawn(monkeypatch)
    monkeypatch.setattr(mat, "probe_run_lock", lambda: True)

    with pytest.raises(HTTPException) as excinfo:
        await aa.post_factor_materialize(
            _fake_request("u1"), body=aa.FactorMaterializeRequest(factor_ids=["f1"])
        )
    assert excinfo.value.status_code == 409
    assert "已有物化进程在运行" in excinfo.value.detail


@pytest.mark.unit
@pytest.mark.asyncio
async def test_start_guard_busy_409(
    isolate_env, monkeypatch, fake_candidates, fake_manifest
):
    """启动闩被占（上一个 POST 握手中）→ 409，不起子进程。"""
    fake_candidates.apply([_row("f1")])
    fake_manifest["current"] = {}
    _no_spawn(monkeypatch)
    monkeypatch.setattr(mat, "probe_run_lock", lambda: False)

    assert START_GUARD.acquire(blocking=False)
    try:
        with pytest.raises(HTTPException) as excinfo:
            await aa.post_factor_materialize(
                _fake_request("u1"),
                body=aa.FactorMaterializeRequest(factor_ids=["f1"]),
            )
    finally:
        START_GUARD.release()
    assert excinfo.value.status_code == 409


@pytest.mark.unit
@pytest.mark.asyncio
async def test_spawn_fixed_argv_and_confirmed(
    isolate_env, monkeypatch, fake_candidates, fake_manifest
):
    """快乐路径：白名单 argv 单 token、cwd=仓库根、脱离进程组、回包确认持锁。"""
    fake_candidates.apply([_row("f1"), _row("f2")])
    fake_manifest["current"] = {}
    calls = _fake_spawn(monkeypatch)  # probes: 预检 False → 握手 True

    resp = await aa.post_factor_materialize(
        _fake_request("u1"),
        body=aa.FactorMaterializeRequest(factor_ids=["f1", "f2"]),
    )
    data = resp["data"]
    assert data["started"] is True and data["running"] is True
    assert data["confirmed"] is True and data["pid"] == 4242
    assert data["materializable"] == ["f1", "f2"]
    assert data["log_path"] == str(isolate_env / "ui.log")

    assert len(calls) == 1
    call = calls[0]
    assert call["argv"] == [
        sys.executable,
        "-m",
        "backend.scripts.rd_mined_materialize",
        "--factor-ids=f1,f2",
        "--register",
    ]
    assert call["start_new_session"] is True
    assert call["stdin"] == subprocess.DEVNULL
    assert call["stderr"] == subprocess.STDOUT
    assert (isolate_env / "ui.log").is_file()
    assert not (isolate_env / "ui.log.tmp").exists()
    # 候选查询走只读面：不带 DDL 副作用
    assert fake_candidates.calls[-1]["ensure"] is False


@pytest.mark.unit
@pytest.mark.asyncio
async def test_confirm_timeout_alive_reports_unconfirmed(
    isolate_env, monkeypatch, fake_candidates, fake_manifest
):
    """慢机器超时未确认但进程存活：started=True、running=False、措辞如实。"""
    fake_candidates.apply([_row("f1")])
    fake_manifest["current"] = {}
    _fake_spawn(monkeypatch)
    # 全程探测不到锁（子进程还在解释器冷启）：覆盖迭代器探测，语义更直白
    monkeypatch.setattr(mat, "probe_run_lock", lambda: False)
    monkeypatch.setattr(aa, "_MATERIALIZE_CONFIRM_TIMEOUT_S", 0.2)
    monkeypatch.setattr(aa, "_MATERIALIZE_CONFIRM_POLL_S", 0.02)

    resp = await aa.post_factor_materialize(
        _fake_request("u1"), body=aa.FactorMaterializeRequest(factor_ids=["f1"])
    )
    data = resp["data"]
    assert data["started"] is True and data["running"] is False
    assert "未确认" in data["message"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_symlink_log_maps_500(
    isolate_env, monkeypatch, fake_candidates, fake_manifest
):
    """日志路径被摆成符号链接：映射 500「符号链接」，绝不顺链写。"""
    victim = isolate_env / "victim.txt"
    victim.write_text("重要内容", encoding="utf-8")
    (isolate_env / "ui.log").symlink_to(victim)

    fake_candidates.apply([_row("f1")])
    fake_manifest["current"] = {}
    _no_spawn(monkeypatch)
    monkeypatch.setattr(mat, "probe_run_lock", lambda: False)

    with pytest.raises(HTTPException) as excinfo:
        await aa.post_factor_materialize(
            _fake_request("u1"), body=aa.FactorMaterializeRequest(factor_ids=["f1"])
        )
    assert excinfo.value.status_code == 500
    assert "符号链接" in excinfo.value.detail
    assert victim.read_text(encoding="utf-8") == "重要内容"


# ── 状态面 ─────────────────────────────────────────────────────────────


@pytest.mark.unit
@pytest.mark.asyncio
async def test_status_maps_manifest_and_hides_foreign(
    isolate_env, monkeypatch, fake_candidates, fake_manifest
):
    """状态只回自己名下条目；他人 id 直接缺席（不泄露存在性）。"""
    fake_candidates.apply([_row("f1"), _row("f2", user_id="u2")])
    fake_manifest["current"] = {
        "f1": {"status": "materialized", "at": "2026-10-06T09:00:00Z"},
        "f2": {"status": "materialized", "at": "2026-10-07T09:00:00Z"},
    }

    resp = await aa.get_factor_materialize_status(
        _fake_request("u1"), factor_ids="f1,f2"
    )
    data = resp["data"]
    assert data["running"] is False
    assert data["factors"] == [
        {"factor_id": "f1", "status": "materialized", "at": "2026-10-06T09:00:00Z"}
    ]
    assert data["last_at"] == "2026-10-06T09:00:00Z"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_status_none_for_unmaterialized(
    isolate_env, monkeypatch, fake_candidates, fake_manifest
):
    """无清单条目 = none（与「已在跑但还没裁决」可区分）。"""
    fake_candidates.apply([_row("f1")])
    fake_manifest["current"] = {}

    resp = await aa.get_factor_materialize_status(_fake_request("u1"), factor_ids="f1")
    assert resp["data"]["factors"] == [
        {"factor_id": "f1", "status": "none", "at": None}
    ]
    assert resp["data"]["last_at"] is None


@pytest.mark.unit
@pytest.mark.asyncio
async def test_status_running_reflects_lock(
    isolate_env, monkeypatch, fake_candidates, fake_manifest
):
    fake_candidates.apply([_row("f1")])
    fake_manifest["current"] = {}

    handle = mat._acquire_run_lock()
    assert handle is not None
    try:
        resp = await aa.get_factor_materialize_status(
            _fake_request("u1"), factor_ids="f1"
        )
        assert resp["data"]["running"] is True
    finally:
        handle.close()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_status_rejects_malformed(isolate_env, monkeypatch, fake_candidates):
    fake_candidates.apply([])
    with pytest.raises(HTTPException) as excinfo:
        await aa.get_factor_materialize_status(_fake_request("u1"), factor_ids="a b")
    assert excinfo.value.status_code == 400


# ── 命令组装安全轨 ─────────────────────────────────────────────────────


@pytest.mark.unit
def test_build_run_command_no_args_unchanged():
    """admin 面固定 argv 逐字节不变（ops 回归另有一道，双钉）。"""
    assert mat.build_run_command() == [
        sys.executable,
        "-m",
        "backend.scripts.rd_mined_materialize",
        "--register",
    ]


@pytest.mark.unit
def test_build_run_command_empty_list_raises():
    """显式空列表宁可炸：CLI 空列表会退化成「全库物化」。"""
    with pytest.raises(ValueError):
        mat.build_run_command([])


@pytest.mark.unit
def test_endpoints_require_authenticated_identity():
    """authz 契约：两个端点都必须先取认证身份（防未来重构把鉴权删了）。"""
    for fn in (
        aa.post_factor_materialize,
        aa.get_factor_materialize_status,
    ):
        src = inspect.getsource(fn)
        assert "get_authenticated_identity(" in src, fn.__name__
