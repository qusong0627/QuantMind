"""mining_pool_rebuild 运维面 + 因子池端点测试。

契约（端点面）：
- 刷新端点的 owner 恒为鉴权身份（不接受客户端 user_id）；busy→409、
  argv 校验失败→400、启动失败→500；
- 状态端点做属主掩码：最近一次刷新不是调用者时只回 running 位与
  ``other_user``，不回显日志/scope；
- ``universe`` 空串归一为 None（全量），非法 market → 400。

契约（运维面）：
- ``build_run_command`` 是唯一的 argv 组装口：白名单拒注入、market 枚举、
  universe 空串省略 flag；
- flock 独占互斥、env 路径可隔离（测试不许碰真实 /data）；
- 状态文件读写在损坏/缺失时降级 unknown，绝不抛。
"""

from __future__ import annotations

import os
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:  # pragma: no cover - 环境相关
    from backend.scripts import mining_pool_rebuild as mpr
    from backend.services.engine.routers import alpha_agent as aa
except Exception as _exc:  # noqa: BLE001
    mpr = None
    aa = None
    _IMPORT_ERR = _exc

from fastapi import HTTPException

pytestmark = pytest.mark.skipif(
    mpr is None or aa is None, reason="依赖不可用（需容器环境）"
)


def _fake_request(user_id: str = "u1", tenant_id: str = "default"):
    return types.SimpleNamespace(
        state=types.SimpleNamespace(user={"user_id": user_id, "tenant_id": tenant_id})
    )


@pytest.fixture()
def isolated_paths(tmp_path, monkeypatch):
    """把锁/状态/日志三个路径全指到 tmp，防测试碰真实 /data 与默认锁。"""
    lock = tmp_path / "rebuild.lock"
    status = tmp_path / "status.json"
    web_log = tmp_path / "ui.log"
    monkeypatch.setenv("QM_POOL_REBUILD_LOCK", str(lock))
    monkeypatch.setenv("QM_POOL_REBUILD_STATUS", str(status))
    monkeypatch.setenv("QM_POOL_REBUILD_WEB_LOG", str(web_log))
    return types.SimpleNamespace(lock=lock, status=status, web_log=web_log)


class TestBuildRunCommand:
    def test_valid_argv_is_fixed_module_invocation(self) -> None:
        cmd = mpr.build_run_command(
            user_id="user-1", market="a_share", universe="csi300", dry_run=False
        )
        assert cmd[:3] == [sys.executable, "-m", "backend.scripts.mining_pool_rebuild"]
        assert cmd[3:7] == ["--user", "user-1", "--market", "a_share"]
        assert "--universe" in cmd
        assert cmd[-1] == "--apply"

    def test_empty_universe_omits_flag(self) -> None:
        cmd = mpr.build_run_command(user_id="u", market="a_share", universe="")
        assert "--universe" not in cmd
        assert cmd[-1] == "--dry-run"
        cmd_none = mpr.build_run_command(user_id="u", market="a_share", universe=None)
        assert cmd_none == cmd

    def test_rejects_unknown_market(self) -> None:
        with pytest.raises(ValueError, match="market"):
            mpr.build_run_command(user_id="u", market="nasdaq")

    @pytest.mark.parametrize(
        "bad_user",
        ["", "a b", "--force", "u;rm", "u$(x)", "u\n", "x" * 200],
    )
    def test_rejects_unsafe_user_id(self, bad_user: str) -> None:
        with pytest.raises(ValueError, match="user_id"):
            mpr.build_run_command(user_id=bad_user)

    @pytest.mark.parametrize("bad_universe", ["a b", "--apply", "csi300/../x", "。"])
    def test_rejects_unsafe_universe(self, bad_universe: str) -> None:
        with pytest.raises(ValueError, match="universe"):
            mpr.build_run_command(user_id="u", market="a_share", universe=bad_universe)

    def test_returns_list_not_shell_string(self) -> None:
        cmd = mpr.build_run_command(user_id="u")
        assert isinstance(cmd, list)
        assert all(isinstance(part, str) for part in cmd)


class TestLockOps:
    def test_lock_is_exclusive_within_process(self, isolated_paths) -> None:
        handle = mpr._acquire_run_lock()
        assert handle is not None
        try:
            assert mpr.probe_run_lock() is True
            assert mpr._acquire_run_lock() is None  # 第二把拿不到
        finally:
            handle.close()
        assert mpr.probe_run_lock() is False  # 释放后可再拿

    def test_lock_path_env_isolation(self, isolated_paths, monkeypatch) -> None:
        handle = mpr._acquire_run_lock()
        assert handle is not None
        try:
            monkeypatch.setenv("QM_POOL_REBUILD_LOCK", str(isolated_paths.lock) + ".b")
            assert mpr.probe_run_lock() is False  # 换路径 = 换锁
        finally:
            handle.close()


class TestStatusIO:
    def test_roundtrip(self, isolated_paths) -> None:
        mpr._write_status({"status": "done", "summary": {"factors": 3}})
        data = mpr.read_status()
        assert data["status"] == "done"
        assert data["summary"] == {"factors": 3}
        assert data["path"] == str(isolated_paths.status)

    def test_missing_is_unknown(self, isolated_paths) -> None:
        assert mpr.read_status()["status"] == "unknown"

    def test_corrupt_is_unknown_with_note(self, isolated_paths) -> None:
        isolated_paths.status.write_text("{not json", encoding="utf-8")
        data = mpr.read_status()
        assert data["status"] == "unknown"
        assert "note" in data

    def test_non_dict_is_unknown(self, isolated_paths) -> None:
        isolated_paths.status.write_text("[1,2]", encoding="utf-8")
        assert mpr.read_status()["status"] == "unknown"


class TestRefreshStatusAndLog:
    def test_composition(self, isolated_paths) -> None:
        mpr._write_status({"status": "failed", "error": "boom"})
        data = mpr.refresh_status()
        assert data["status"] == "failed"
        assert data["running"] is False
        assert data["log"]["exists"] is False  # 日志还没写过

    def test_tail_web_log_reads_file(self, isolated_paths) -> None:
        isolated_paths.web_log.write_text("line1\nline2\n", encoding="utf-8")
        data = mpr.tail_web_log()
        assert data["exists"] is True
        assert data["lines"] == ["line1", "line2"]

    def test_tail_web_log_refuses_symlink(self, isolated_paths) -> None:
        target = isolated_paths.web_log.parent / "secret.txt"
        target.write_text("secret", encoding="utf-8")
        os.symlink(target, isolated_paths.web_log)
        data = mpr.tail_web_log()
        assert data["exists"] is False
        assert "符号链接" in data["note"]


class TestSpawnRefreshGuards:
    @pytest.mark.asyncio
    async def test_busy_lock_raises_busy(self, isolated_paths) -> None:
        handle = mpr._acquire_run_lock()
        assert handle is not None
        try:
            with pytest.raises(mpr.RefreshBusyError):
                await mpr.spawn_refresh(user_id="u1")
        finally:
            handle.close()

    @pytest.mark.asyncio
    async def test_argv_validation_raises_value_error(self, isolated_paths) -> None:
        with pytest.raises(ValueError, match="user_id"):
            await mpr.spawn_refresh(user_id="bad user")


class TestMetricsMissing:
    def test_all_present_is_false(self) -> None:
        row = {"rre": "0.6", "ann_turnover": "12.5", "ann_return_net": "0.08"}
        assert mpr._metrics_missing(row) is False

    @pytest.mark.parametrize("key", ["rre", "ann_turnover", "ann_return_net"])
    def test_any_blank_is_true(self, key: str) -> None:
        row = {"rre": "0.6", "ann_turnover": "12.5", "ann_return_net": "0.08"}
        row[key] = ""
        assert mpr._metrics_missing(row) is True

    def test_none_is_true(self) -> None:
        assert mpr._metrics_missing({}) is True


class TestPanelNeedsRebuild:
    """--panels 重建判据：缺文件/坏读/缺 fret（旧格式）→ True；新鲜+非强制 → False。

    缺 fret 的旧面板自动升级是组合实验室的先决条件（rank-IC 目标取 fret），
    判据错了的表现是静默不补 → 组合页永远报「缺收益列」。
    """

    def _write_panel(self, tmp_path, monkeypatch, fid: str, *, with_ret: bool):
        import numpy as np
        import pandas as pd

        from backend.services.engine.mining_plugins import pool_panels

        monkeypatch.setenv("QM_FACTOR_POOL_PANEL_DIR", str(tmp_path / "panels"))
        idx = pd.MultiIndex.from_product(
            [["2026-01-05", "2026-01-06"], ["SH600000", "SZ000001"]],
            names=["datetime", "instrument"],
        )
        values = pd.Series(np.arange(len(idx), dtype=float), index=idx)
        ret = values * 0.001 if with_ret else None
        return pool_panels.write_panel("a_share", fid, values, forward_return=ret)

    def test_missing_file_needs_rebuild(self, tmp_path, monkeypatch):
        monkeypatch.setenv("QM_FACTOR_POOL_PANEL_DIR", str(tmp_path / "panels"))
        assert mpr._panel_needs_rebuild("a_share", "nope", force=False) is True

    def test_panel_without_fret_needs_rebuild(self, tmp_path, monkeypatch):
        self._write_panel(tmp_path, monkeypatch, "legacy", with_ret=False)
        assert mpr._panel_needs_rebuild("a_share", "legacy", force=False) is True

    def test_fresh_panel_with_fret_skipped(self, tmp_path, monkeypatch):
        self._write_panel(tmp_path, monkeypatch, "fresh", with_ret=True)
        assert mpr._panel_needs_rebuild("a_share", "fresh", force=False) is False

    def test_force_overrides_fresh_panel(self, tmp_path, monkeypatch):
        self._write_panel(tmp_path, monkeypatch, "fresh", with_ret=True)
        assert mpr._panel_needs_rebuild("a_share", "fresh", force=True) is True

    def test_corrupt_file_needs_rebuild(self, tmp_path, monkeypatch):
        from backend.services.engine.mining_plugins import pool_panels

        monkeypatch.setenv("QM_FACTOR_POOL_PANEL_DIR", str(tmp_path / "panels"))
        self._write_panel(tmp_path, monkeypatch, "bad", with_ret=True)
        pool_panels.panel_path("a_share", "bad").write_bytes(b"junk")
        # read_panel 坏读返回 None → 视为需重建
        assert mpr._panel_needs_rebuild("a_share", "bad", force=False) is True


class TestNormalizeValueIndex:
    """值索引 0 层规整为 datetime64（--panels 对齐前置步骤）。

    RED 起源（2026-10-08 实测）：旧实现 ``set_levels(get_level_values(0))``
    传的是逐行数组，而 set_levels 要的是「该层唯一值」——跨标的重复日期
    直接撞 MultiIndex 唯一性校验（真实面板堆栈 64MB「Level values must be
    unique」）；单标的测试数据碰不到，跨标的一跑就炸。
    """

    def test_multi_instrument_repeated_dates_no_crash(self) -> None:
        import pandas as pd

        idx = pd.MultiIndex.from_arrays(
            [
                ["2026-01-05", "2026-01-05", "2026-01-06", "2026-01-06"],
                ["SH600000", "SH600036", "SH600000", "SH600036"],
            ],
            names=["datetime", "instrument"],
        )
        out = mpr._normalize_value_index(idx)
        assert isinstance(out, pd.MultiIndex)
        assert out.names == ["datetime", "instrument"]
        assert str(out.get_level_values(0).dtype) == "datetime64[ns]"
        assert list(out.get_level_values(1)) == [
            "SH600000",
            "SH600036",
            "SH600000",
            "SH600036",
        ]

    def test_mixed_date_forms_fall_back_to_from_arrays(self) -> None:
        import pandas as pd

        # '2026-01-05' 与 '2026-01-05 00:00:00' 在 levels 里是两个唯一值，
        # to_datetime 折叠成同一个 Timestamp → levels 路径不成立，须退重建
        idx = pd.MultiIndex.from_arrays(
            [["2026-01-05", "2026-01-05 00:00:00"], ["SH600000", "SH600036"]],
            names=["datetime", "instrument"],
        )
        out = mpr._normalize_value_index(idx)
        assert len(out) == 2
        assert list(out.get_level_values(1)) == ["SH600000", "SH600036"]
        assert out.get_level_values(0)[0] == pd.Timestamp("2026-01-05")
        assert out.get_level_values(0)[1] == pd.Timestamp("2026-01-05")

    def test_plain_index_converts_and_keeps_name(self) -> None:
        import pandas as pd

        out = mpr._normalize_value_index(
            pd.Index(["2026-01-05", "2026-01-06"], name="datetime")
        )
        assert str(out.dtype) == "datetime64[ns]"
        assert out.name == "datetime"


class TestPoolScope:
    def test_empty_universe_normalizes_to_none(self) -> None:
        assert aa._pool_scope("a_share", "") == ("a_share", None)
        assert aa._pool_scope("a_share", "  ") == ("a_share", None)

    def test_value_universe_is_stripped(self) -> None:
        assert aa._pool_scope("a_share", " csi300 ") == ("a_share", "csi300")

    def test_unknown_market_raises_400(self) -> None:
        with pytest.raises(HTTPException) as err:
            aa._pool_scope("nasdaq", "")
        assert err.value.status_code == 400


class TestRefreshEndpoint:
    @pytest.mark.asyncio
    async def test_uses_authenticated_identity_and_normalizes(
        self, monkeypatch
    ) -> None:
        calls: list[dict] = []

        async def fake_spawn(**kwargs):
            calls.append(kwargs)
            return {"started": True, "pid": 1}

        monkeypatch.setattr(mpr, "spawn_refresh", fake_spawn)
        resp = await aa.post_pool_refresh(
            _fake_request("u1"), market="a_share", universe="", dry_run=True
        )
        assert resp["code"] == 200
        assert calls == [
            {"user_id": "u1", "market": "a_share", "universe": None, "dry_run": True}
        ]

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("exc", "expected_status"),
        [
            (mpr.RefreshBusyError("busy"), 409),
            (ValueError("bad"), 400),
            (mpr.RefreshStartError("dead"), 500),
        ],
    )
    async def test_exception_mapping(self, monkeypatch, exc, expected_status) -> None:
        async def fake_spawn(**kwargs):
            raise exc

        monkeypatch.setattr(mpr, "spawn_refresh", fake_spawn)
        with pytest.raises(HTTPException) as err:
            await aa.post_pool_refresh(
                _fake_request(), market="a_share", universe="", dry_run=True
            )
        assert err.value.status_code == expected_status

    @pytest.mark.asyncio
    async def test_bad_market_400_before_spawn(self, monkeypatch) -> None:
        async def fake_spawn(**kwargs):  # pragma: no cover - 不应被调用
            raise AssertionError("spawn 不应被调用")

        monkeypatch.setattr(mpr, "spawn_refresh", fake_spawn)
        with pytest.raises(HTTPException) as err:
            await aa.post_pool_refresh(
                _fake_request(), market="nasdaq", universe="", dry_run=True
            )
        assert err.value.status_code == 400


class TestRefreshStatusEndpoint:
    @pytest.mark.asyncio
    async def test_owner_passthrough(self, monkeypatch) -> None:
        payload = {
            "status": "done",
            "running": False,
            "args": {"user": "u1", "market": "a_share"},
            "log": {"exists": True, "lines": ["x"]},
        }
        monkeypatch.setattr(mpr, "refresh_status", lambda: dict(payload))
        resp = await aa.get_pool_refresh_status(_fake_request("u1"))
        assert resp["data"]["args"]["user"] == "u1"
        assert resp["data"]["log"]["lines"] == ["x"]

    @pytest.mark.asyncio
    async def test_other_user_masked(self, monkeypatch) -> None:
        payload = {
            "status": "done",
            "running": True,
            "args": {"user": "u9", "market": "a_share"},
            "summary": {"factors": 99},
            "log": {"exists": True, "lines": ["他人日志"]},
        }
        monkeypatch.setattr(mpr, "refresh_status", lambda: dict(payload))
        resp = await aa.get_pool_refresh_status(_fake_request("u1"))
        data = resp["data"]
        assert data["status"] == "other_user"
        assert data["running"] is True
        assert data["log"]["lines"] == []
        assert "args" not in data and "summary" not in data

    @pytest.mark.asyncio
    async def test_no_owner_passthrough(self, monkeypatch) -> None:
        monkeypatch.setattr(mpr, "refresh_status", lambda: {"status": "unknown"})
        resp = await aa.get_pool_refresh_status(_fake_request("u1"))
        assert resp["data"]["status"] == "unknown"


class TestPoolReadEndpoints:
    @pytest.mark.asyncio
    async def test_overview_passes_scope(self, monkeypatch) -> None:
        from backend.services.engine.mining_plugins import pool_service

        calls: list[dict] = []

        async def fake_overview(**kwargs):
            calls.append(kwargs)
            return {"total": 0}

        monkeypatch.setattr(pool_service, "pool_overview", fake_overview)
        resp = await aa.get_pool_overview(
            _fake_request("u1"), market="hong_kong", universe=""
        )
        assert resp == {"code": 200, "data": {"total": 0}}
        assert calls == [{"user_id": "u1", "market": "hong_kong", "universe": None}]

    @pytest.mark.asyncio
    async def test_factors_passes_paging_and_sort(self, monkeypatch) -> None:
        from backend.services.engine.mining_plugins import pool_service

        calls: list[dict] = []

        async def fake_list(**kwargs):
            calls.append(kwargs)
            return {"total": 0, "items": []}

        monkeypatch.setattr(pool_service, "list_pool_factors", fake_list)
        # 直调契约：FastAPI 默认值不参与直调，所有 Query 参数必须显式传，
        # 否则拿到的是 Query(...) 对象（.strip() 会直接 AttributeError）
        await aa.get_pool_factors(
            _fake_request("u1"),
            market="a_share",
            universe="csi300",
            limit=20,
            offset=40,
            sort="ic",
            include_archived=False,
            category="",
        )
        assert calls == [
            {
                "user_id": "u1",
                "market": "a_share",
                "universe": "csi300",
                "limit": 20,
                "offset": 40,
                "sort": "ic",
                "include_archived": False,
                "category": None,
            }
        ]

    @pytest.mark.asyncio
    async def test_factors_rejects_unknown_category(self, monkeypatch) -> None:
        """未知大类显式 400（不静默空列表），且不得触达 service。"""
        from backend.services.engine.mining_plugins import pool_service

        async def fake_list(**kwargs):  # pragma: no cover - 不应被调用
            raise AssertionError("list_pool_factors 不应被调用")

        monkeypatch.setattr(pool_service, "list_pool_factors", fake_list)
        with pytest.raises(HTTPException) as err:
            await aa.get_pool_factors(
                _fake_request("u1"),
                market="a_share",
                universe="",
                limit=20,
                offset=0,
                sort="pool_score",
                include_archived=False,
                category="MOMENTUM",
            )
        assert err.value.status_code == 400
        assert "momentum" in err.value.detail

    @pytest.mark.asyncio
    async def test_graph_passes_max_nodes(self, monkeypatch) -> None:
        from backend.services.engine.mining_plugins import pool_service

        calls: list[dict] = []

        async def fake_graph(**kwargs):
            calls.append(kwargs)
            return {"nodes": [], "edges": []}

        monkeypatch.setattr(pool_service, "pool_graph", fake_graph)
        resp = await aa.get_pool_graph(
            _fake_request("u1"),
            market="a_share",
            universe="",
            max_nodes=50,
            include_archived=False,  # 直调契约：Query 默认值不参与直调，必须显式传
        )
        assert resp["data"] == {"nodes": [], "edges": []}
        assert calls == [
            {
                "user_id": "u1",
                "market": "a_share",
                "universe": None,
                "max_nodes": 50,
                "include_archived": False,
            }
        ]


class TestMetricsRegistryGates:
    @pytest.mark.asyncio
    async def test_registry_includes_gate_descriptors(self) -> None:
        resp = await aa.get_metrics_registry(_fake_request())
        data = resp["data"]
        assert isinstance(data["metrics"], list) and data["metrics"]
        assert isinstance(data["gates"], list) and data["gates"]
        gate_keys = {g["key"] for g in data["gates"]}
        # 与 gates/builtin.py 注册的内置五门禁一致
        assert {
            "pfs_floor",
            "rre_floor",
            "ic_pool_pct",
            "turnover_cap",
            "net_return_floor",
        } <= gate_keys
        for gate in data["gates"]:
            assert gate["default_mode"] in ("soft", "hard")
