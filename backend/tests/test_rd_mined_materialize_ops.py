"""RD 挖掘因子物化的后台运维面（状态/启动/日志/锁探测）回归。

链路背景（2026-09-29）：物化此前只有 CLI，全量回填只能在机器上敲命令。
本文件锁住新面板面的四条契约：

1. **面板口径 == 真实工作量**：状态里的「待物化」必须走物化器同一套
   ``_eligible_row`` / ``_should_materialize`` 判定（不是另写的近似），
   否则管理员看到 0 待办、点下按钮却跑两小时（或反过来）；
2. **忙时快速拒绝**：锁被占 → start 返回 409，绝不并发起第二个写库进程；
3. **argv 固定**：启动命令是全常量，任何用户/请求输入都不得进入 argv
   （这是子进程入口，注入等于任意命令执行）；
4. **日志尾**：不存在不报错、超限截断不吐半行。
"""

from __future__ import annotations

import contextlib
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from backend.scripts import rd_mined_materialize as mat
from backend.services.api.routers.admin import rd_mined_materialize as ops
from backend.shared.factor_identity import code_fingerprint


def _row(
    factor_id: str,
    *,
    name: str = "因子",
    code: str = "close",
    market: str = "a_share",
) -> dict:
    return {
        "factor_id": factor_id,
        "factor_name": name,
        "factor_code": code,
        "market": market,
    }


def _manifest_entry(code: str, status: str = "materialized") -> dict:
    return {"status": status, "code_fp": code_fingerprint(code), "column": "col_x"}


@pytest.fixture()
def isolate_env(monkeypatch, tmp_path):
    """锁文件与面板日志都指向 tmp：与任何在跑的生产回填互不干扰。"""
    monkeypatch.setenv("RD_MINED_MATERIALIZE_LOCK", str(tmp_path / "run.lock"))
    monkeypatch.setenv("RD_MINED_MATERIALIZE_WEB_LOG", str(tmp_path / "ui.log"))
    return tmp_path


@pytest.fixture()
def fake_panel_deps(monkeypatch):
    """把面板的库/目录读侧换成内存替身（不碰真 parquet 与真 PG）。"""

    def _apply(
        *,
        columns=("symbol", "date", "close", "fac_a"),
        ready: bool = True,
        version_id: str | None = "v-1",
        enabled: set[str] | None = None,
    ):
        class _Reader:
            def __init__(self, market=None):
                self.market = market

            def describe(self, source):
                return SimpleNamespace(
                    columns=list(columns),
                    ready=ready,
                    files=3,
                    min_date="2018-01-02",
                    max_date="2026-09-24",
                )

        class _Session:
            pass

        @contextlib.asynccontextmanager
        async def _fake_session():
            yield _Session()

        async def _fake_published(_session):
            return version_id, set(enabled if enabled is not None else {"fac_a"})

        import backend.services.engine.data_platform.quantdb_factor_reader as qfr
        import backend.shared.database_manager_v2 as dbm

        monkeypatch.setattr(qfr, "QuantDBFactorReader", _Reader)
        monkeypatch.setattr(dbm, "get_session", _fake_session)
        monkeypatch.setattr(mat, "_published_enabled_columns", _fake_published)
        # _lib_root 只做路径拼接，但清单读取换成内存字典，避免读到真实残留
        monkeypatch.setattr(mat, "_load_manifest", lambda _root: _MANIFEST["current"])

    _MANIFEST["current"] = {}
    return _apply


_MANIFEST: dict[str, dict] = {"current": {}}


@pytest.fixture()
def fake_candidates(monkeypatch):
    def _apply(rows):
        async def _query(**_kwargs):
            return list(rows)

        monkeypatch.setattr(mat, "_query_candidates", _query)

    return _apply


# ── 状态汇总：分桶必须与真实物化同口径 ─────────────────────────────────


@pytest.mark.unit
@pytest.mark.asyncio
async def test_overview_buckets_match_materialize_predicates(
    isolate_env, fake_panel_deps, fake_candidates
):
    """六类候选各自落入正确桶：new/retry/code_changed 待办，其余跳过。"""
    same_code = "close * 2"
    manifest = {
        "f-done": _manifest_entry(same_code, "materialized"),  # 代码未变 → 跳过
        "f-rej": _manifest_entry("open", "rejected_duplicate"),  # 值级重复 → 跳过
        # 生产只写 materialized / rejected_duplicate / error 三种状态，
        # 「失败待重试」的清单形状必须是 "error"（此前夹具写成 "failed"，
        # 与前端的失败磁贴一起错到一块去了）
        "f-failed": {"status": "error", "code_fp": ""},  # 失败 → 重试
        "f-changed": _manifest_entry("close * 3", "materialized"),  # 代码改写 → 重算
    }
    fake_panel_deps(enabled={"fac_a"})
    _MANIFEST["current"] = manifest
    fake_candidates(
        [
            _row("f-new"),  # 清单无 → new
            _row("f-done", code=same_code),
            _row("f-rej", code="open"),
            _row("f-failed", code="vwap"),
            _row("f-changed", code="close * 4"),  # 与清单指纹不同 → code_changed
            _row("f-nocode", code="  "),
            _row("f-us", market="us_stock"),
        ]
    )

    overview = await mat.materialize_overview()

    cand = overview["candidates"]
    assert cand["total"] == 7
    assert cand["pending"] == 3
    assert cand["pending_reasons"] == {"new": 1, "retry": 1, "code_changed": 1}
    assert cand["skipped"] == {
        "already_materialized": 1,
        "rejected_duplicate": 1,
        "no_code": 1,
        "market_unsupported": 1,
    }
    assert overview["manifest"]["total"] == 4
    assert overview["manifest"]["by_status"] == {
        "materialized": 2,
        "rejected_duplicate": 1,
        "error": 1,
    }
    # 盘上因子列 = 去键列/必需列后的 fac_a
    assert overview["library"]["factor_columns"] == 1
    assert overview["library"]["partitions"] == 3
    # 已发布 enabled == 盘上列 → 目录最新
    assert overview["catalog"] == {
        "published_version": "v-1",
        "published_columns": 1,
        "up_to_date": True,
    }


@pytest.mark.unit
@pytest.mark.asyncio
async def test_overview_catalog_stale_when_columns_differ(
    isolate_env, fake_panel_deps, fake_candidates
):
    """对照：盘上多一列 / 无发布版本时 ``up_to_date`` 必须为假。

    防止上一条的 ``True`` 断言在恒真实现下也通过。
    """
    fake_panel_deps(
        columns=("symbol", "date", "close", "fac_a", "fac_b"), enabled={"fac_a"}
    )
    fake_candidates([])

    overview = await mat.materialize_overview()
    assert overview["library"]["factor_columns"] == 2
    assert overview["catalog"]["up_to_date"] is False

    fake_panel_deps(version_id=None, enabled=set())
    overview = await mat.materialize_overview()
    assert overview["catalog"]["published_version"] is None
    assert overview["catalog"]["up_to_date"] is False


@pytest.mark.unit
@pytest.mark.asyncio
async def test_overview_manifest_last_at_and_db_degrade(
    isolate_env, fake_panel_deps, fake_candidates, monkeypatch
):
    """清单 ``last_at`` 取最大时间戳；DB 抖动时目录段降级带 error 而非 500。"""
    fake_panel_deps()
    _MANIFEST["current"] = {
        "f-1": {"status": "materialized", "at": "2026-09-20T01:00:00Z"},
        "f-2": {"status": "error", "at": "2026-09-28T09:30:00Z"},
        "f-3": {"status": "materialized"},  # 无 at：不得污染最大值
    }
    fake_candidates([])

    overview = await mat.materialize_overview()
    assert overview["manifest"]["last_at"] == "2026-09-28T09:30:00Z"

    class _BoomSession:
        async def __aenter__(self):
            raise RuntimeError("PG 连接池耗尽")

        async def __aexit__(self, *exc):
            return False

    import backend.shared.database_manager_v2 as dbm

    monkeypatch.setattr(dbm, "get_session", lambda: _BoomSession())
    overview = await mat.materialize_overview()
    assert overview["catalog"]["up_to_date"] is False
    assert "PG 连接池耗尽" in overview["catalog"]["error"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_overview_degrades_not_fails_when_library_unreadable(
    isolate_env, fake_panel_deps, fake_candidates, monkeypatch
):
    """库读侧炸了：面板降级带 error 字段返回，而不是把整个状态接口打成 500。"""

    class _Boom:
        def __init__(self, market=None):
            pass

        def describe(self, source):
            raise RuntimeError("parquet 目录不存在")

    import backend.services.engine.data_platform.quantdb_factor_reader as qfr

    fake_panel_deps()
    fake_candidates([])
    monkeypatch.setattr(qfr, "QuantDBFactorReader", _Boom)

    overview = await mat.materialize_overview()

    assert overview["library"]["ready"] is False
    assert "parquet 目录不存在" in overview["library"]["error"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_query_candidates_ensure_flag_controls_ddl(isolate_env, monkeypatch):
    """``ensure`` 是 DDL 的唯一开关：状态面 False 一次不许跑，真实运行 True 照跑。

    建表迁移是一串无条件 ``CREATE/ALTER TABLE``（连索引），面板 10s 轮询
    反复触发会反复取 ACCESS EXCLUSIVE 锁。此测试同时给出正反两向，
    防止「干脆把 ensure 拆了」这种把门槛拆没了的修法。
    """
    import backend.services.engine.qlib_app.services.rd_agent_persistence as persist
    import backend.shared.database_manager_v2 as dbm

    calls: list[str] = []

    async def _record_ensure(self):
        calls.append("ensure")

    class _Result:
        def mappings(self):
            return self

        def all(self):
            return []

    class _Session:
        async def execute(self, *_a, **_kw):
            return _Result()

    @contextlib.asynccontextmanager
    async def _fake_session(**_kw):
        yield _Session()

    monkeypatch.setattr(
        persist.RDAgentFactorPersistence, "ensure_tables", _record_ensure
    )
    monkeypatch.setattr(dbm, "get_session", _fake_session)

    assert await mat._query_candidates(ensure=False) == []
    assert calls == []  # 状态面：一次 DDL 都不许有

    assert await mat._query_candidates(ensure=True) == []
    assert calls == ["ensure"]  # 对照：真实运行仍然建表


@pytest.mark.unit
@pytest.mark.asyncio
async def test_overview_counts_real_rows_without_ddl(
    isolate_env, fake_panel_deps, monkeypatch
):
    """概述走真实查询路径：不跑 DDL，行数照常进分桶（不是空对空的假通过）。"""
    fake_panel_deps()

    import backend.services.engine.qlib_app.services.rd_agent_persistence as persist
    import backend.shared.database_manager_v2 as dbm

    rows_out: list[dict] = [_row("f-real")]
    calls: list[str] = []

    async def _record_ensure(self):
        calls.append("ensure")

    class _Result:
        def mappings(self):
            return self

        def all(self):
            return list(rows_out)

    class _Session:
        async def execute(self, *_a, **_kw):
            return _Result()

    @contextlib.asynccontextmanager
    async def _fake_session(**_kw):
        yield _Session()

    monkeypatch.setattr(
        persist.RDAgentFactorPersistence, "ensure_tables", _record_ensure
    )
    monkeypatch.setattr(dbm, "get_session", _fake_session)

    overview = await mat.materialize_overview()

    assert calls == []  # 轮询路径不带 DDL 副作用
    assert overview["candidates"]["total"] == 1
    assert overview["candidates"]["pending"] == 1


@pytest.mark.unit
@pytest.mark.asyncio
async def test_overview_candidates_db_error_degrades_not_500(
    isolate_env, fake_panel_deps, monkeypatch
):
    """候选查询炸了（PG 抖动）：candidates 带 error 降级，其余段照常返回。"""
    fake_panel_deps()

    async def _boom(**_kwargs):
        raise RuntimeError("PG 连接池耗尽")

    monkeypatch.setattr(mat, "_query_candidates", _boom)

    overview = await mat.materialize_overview()

    assert overview["candidates"]["total"] == 0
    assert overview["candidates"]["pending"] == 0
    assert "PG 连接池耗尽" in overview["candidates"]["error"]
    assert overview["library"]["ready"] is True  # 库面不陪葬


# ── 锁探测与启动 ───────────────────────────────────────────────────────


@pytest.mark.unit
def test_probe_run_lock_reflects_holder(isolate_env):
    """拿得到锁 = 没人在跑；锁被持有 = 运行中；释放后回到未运行。"""
    assert mat.probe_run_lock() is False

    handle = mat._acquire_run_lock()
    assert handle is not None
    try:
        assert mat.probe_run_lock() is True
    finally:
        handle.close()

    assert mat.probe_run_lock() is False


@pytest.mark.unit
def test_probe_run_lock_unwritable_path_treated_as_idle(
    isolate_env, tmp_path, monkeypatch
):
    """锁文件不可写 → 按未运行处理（误报运行中会让面板永久卡死）。"""
    monkeypatch.setenv(
        "RD_MINED_MATERIALIZE_LOCK", str(tmp_path / "no" / "such" / "dir" / "x.lock")
    )
    assert mat.probe_run_lock() is False


@pytest.mark.unit
def test_build_run_command_is_fixed_argv():
    """命令是全常量：入口模块、--register 固定，argv[0] 为当前解释器。"""
    assert mat.build_run_command() == [
        sys.executable,
        "-m",
        "backend.scripts.rd_mined_materialize",
        "--register",
    ]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_start_rejects_when_busy(isolate_env):
    """有物化在跑 → 409；且不得启动任何子进程。"""
    handle = mat._acquire_run_lock()
    assert handle is not None
    try:
        with pytest.raises(HTTPException) as excinfo:
            await ops.start_rd_mined_materialize()
        assert excinfo.value.status_code == 409
    finally:
        handle.close()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_start_spawns_fixed_command_detached(
    isolate_env, monkeypatch, fake_panel_deps, fake_candidates
):
    """空闲时启动：固定 argv、cwd=仓库根、日志重定向、脱离进程组。

    回包前必须确认子进程拿住锁：预检（False）→ 子进程存活 → 握手里探测到
    持锁（True）。这正是生产时序（子进程冷启 ~4s 后才拿锁）。
    """
    calls: list[dict] = []

    class _FakeProc:
        pid = 4242

        def poll(self):
            return None  # 存活

        def wait(self):
            return 0

    def _fake_popen(argv, **kwargs):
        calls.append({"argv": argv, **kwargs})
        return _FakeProc()

    probes = iter([False, True])  # 预检未运行 → 握手确认持锁

    fake_panel_deps()
    fake_candidates([])
    monkeypatch.setattr(subprocess, "Popen", _fake_popen)
    monkeypatch.setattr(mat, "probe_run_lock", lambda: next(probes))

    result = await ops.start_rd_mined_materialize()

    assert result["started"] is True
    assert result["pid"] == 4242
    assert result["log_path"] == str(isolate_env / "ui.log")
    assert "确认" in result["message"]
    assert len(calls) == 1
    call = calls[0]
    assert call["argv"] == mat.build_run_command()
    assert Path(call["cwd"]) == mat.project_root()
    assert call["start_new_session"] is True
    assert call["stderr"] is subprocess.STDOUT
    assert call["stdin"] is subprocess.DEVNULL
    assert isinstance(call["env"], dict) and "PATH" in call["env"]
    # 日志经 .tmp 原子换名落到正式路径（父侧句柄关闭不泄漏 fd；
    # 子进程持有的是 fork 出的副本，跟随 inode 继续写正式路径）
    assert (isolate_env / "ui.log").is_file()
    assert not (isolate_env / "ui.log.tmp").exists()
    assert call["stdout"].closed is True


@pytest.mark.unit
@pytest.mark.asyncio
async def test_start_returns_500_when_log_unwritable(
    isolate_env, tmp_path, monkeypatch
):
    """日志不可写 → 500，且不启动子进程（宁可不跑，也不能跑了没日志）。"""
    blocker = tmp_path / "blocker"
    blocker.write_text("x", encoding="utf-8")
    monkeypatch.setenv("RD_MINED_MATERIALIZE_WEB_LOG", str(blocker / "ui.log"))

    def _must_not_spawn(*_args, **_kwargs):
        raise AssertionError("日志不可写时不得启动子进程")

    monkeypatch.setattr(subprocess, "Popen", _must_not_spawn)

    with pytest.raises(HTTPException) as excinfo:
        await ops.start_rd_mined_materialize()
    assert excinfo.value.status_code == 500


@pytest.mark.unit
@pytest.mark.asyncio
async def test_start_child_exit_reports_500_not_started(isolate_env, monkeypatch):
    """子进程起完即退（导入失败等）：如实 500 + 退出码，绝不假装 started。"""
    exit_code = 3

    class _FakeProc:
        pid = 5151

        def poll(self):
            return exit_code

        def wait(self):
            return exit_code

    monkeypatch.setattr(subprocess, "Popen", lambda argv, **kw: _FakeProc())
    monkeypatch.setattr(mat, "probe_run_lock", lambda: False)

    with pytest.raises(HTTPException) as excinfo:
        await ops.start_rd_mined_materialize()

    assert excinfo.value.status_code == 500
    assert f"退出码 {exit_code}" in excinfo.value.detail


@pytest.mark.unit
@pytest.mark.asyncio
async def test_start_child_lock_loss_is_409_not_started(isolate_env, monkeypatch):
    """探测→启动之间别家抢先：本次子进程取锁失败退出 → 409（未写任何数据）。"""

    class _FakeProc:
        pid = 6161

        def poll(self):
            return 0

        def wait(self):
            return 0

    probes = iter([False, False, True])  # 预检 → 我方启动中 → 别家已持锁
    monkeypatch.setattr(subprocess, "Popen", lambda argv, **kw: _FakeProc())
    monkeypatch.setattr(mat, "probe_run_lock", lambda: next(probes))

    with pytest.raises(HTTPException) as excinfo:
        await ops.start_rd_mined_materialize()

    assert excinfo.value.status_code == 409
    assert "未取得独占锁" in excinfo.value.detail


@pytest.mark.unit
@pytest.mark.asyncio
async def test_start_second_post_409_while_confirm_pending(isolate_env, monkeypatch):
    """启动闩被上一个 POST 占着（握手中）→ 第二个 POST 直接 409，不起子进程。"""
    monkeypatch.setattr(mat, "probe_run_lock", lambda: False)

    def _must_not_spawn(*_a, **_kw):
        raise AssertionError("握手中不得再起子进程")

    monkeypatch.setattr(subprocess, "Popen", _must_not_spawn)

    assert ops._START_GUARD.acquire(blocking=False)
    try:
        with pytest.raises(HTTPException) as excinfo:
            await ops.start_rd_mined_materialize()
    finally:
        ops._START_GUARD.release()

    assert excinfo.value.status_code == 409
    assert "确认尚未完成" in excinfo.value.detail


@pytest.mark.unit
@pytest.mark.asyncio
async def test_start_confirm_timeout_alive_reports_unconfirmed(
    isolate_env, monkeypatch
):
    """慢机器上超时仍未确认锁但进程活着：started=True，措辞如实标「未确认」。"""

    class _FakeProc:
        pid = 7171

        def poll(self):
            return None  # 存活

        def wait(self):
            return 0

    monkeypatch.setattr(subprocess, "Popen", lambda argv, **kw: _FakeProc())
    monkeypatch.setattr(mat, "probe_run_lock", lambda: False)
    monkeypatch.setattr(ops, "_START_CONFIRM_TIMEOUT_S", 0.2)
    monkeypatch.setattr(ops, "_START_CONFIRM_POLL_S", 0.02)

    result = await ops.start_rd_mined_materialize()

    assert result["started"] is True
    assert "未确认" in result["message"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_start_refuses_symlink_log_path(isolate_env, monkeypatch):
    """日志路径被摆成符号链接 → 500 拒绝，绝不顺着链接写（防覆盖任意文件）。"""
    victim = isolate_env / "victim.txt"
    victim.write_text("重要内容", encoding="utf-8")
    (isolate_env / "ui.log").symlink_to(victim)

    def _must_not_spawn(*_a, **_kw):
        raise AssertionError("符号链接路径不得启动子进程")

    monkeypatch.setattr(subprocess, "Popen", _must_not_spawn)
    monkeypatch.setattr(mat, "probe_run_lock", lambda: False)

    with pytest.raises(HTTPException) as excinfo:
        await ops.start_rd_mined_materialize()

    assert excinfo.value.status_code == 500
    assert "符号链接" in excinfo.value.detail
    assert victim.read_text(encoding="utf-8") == "重要内容"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_start_spawn_failure_keeps_previous_log(isolate_env, monkeypatch):
    """Popen 抛 OSError：上一轮日志必须原样保留（先写 .tmp、成功才换名）。"""
    log = isolate_env / "ui.log"
    log.write_text("上一轮的日志", encoding="utf-8")

    def _boom(*_a, **_kw):
        raise OSError("ENOENT: 解释器不见了")

    monkeypatch.setattr(subprocess, "Popen", _boom)
    monkeypatch.setattr(mat, "probe_run_lock", lambda: False)

    with pytest.raises(HTTPException) as excinfo:
        await ops.start_rd_mined_materialize()

    assert excinfo.value.status_code == 500
    assert log.read_text(encoding="utf-8") == "上一轮的日志"
    assert not (isolate_env / "ui.log.tmp").exists()


# ── 状态端点与日志尾 ───────────────────────────────────────────────────


@pytest.mark.unit
@pytest.mark.asyncio
async def test_status_composes_running_overview_log(
    isolate_env, fake_panel_deps, fake_candidates
):
    """status = running + overview + log 三段；运行态来自锁探测。"""
    fake_panel_deps()
    fake_candidates([_row("f-1")])

    idle = await ops.rd_mined_materialize_status()
    assert idle["running"] is False
    assert idle["overview"]["candidates"]["pending"] == 1
    assert idle["log"]["exists"] is False

    handle = mat._acquire_run_lock()
    assert handle is not None
    try:
        running = await ops.rd_mined_materialize_status()
        assert running["running"] is True
    finally:
        handle.close()


@pytest.mark.unit
def test_web_log_path_defaults_under_data_dir(monkeypatch):
    """不设覆盖时落回容器内 /data（宿主 ./data 卷可见，运维能直接翻文件）。"""
    monkeypatch.delenv("RD_MINED_MATERIALIZE_WEB_LOG", raising=False)
    assert str(mat._web_log_path()) == "/data/rd_mined_materialize_ui.log"


@pytest.mark.unit
def test_tail_web_log_absent_and_tail(isolate_env):
    log_path = isolate_env / "ui.log"
    assert mat.tail_web_log() == {"path": str(log_path), "exists": False, "lines": []}

    log_path.write_text(
        "\n".join(f"line-{i}" for i in range(5)) + "\n", encoding="utf-8"
    )
    tail = mat.tail_web_log(max_lines=2)
    assert tail["exists"] is True
    assert tail["lines"] == ["line-3", "line-4"]
    assert tail["size"] == log_path.stat().st_size


@pytest.mark.unit
def test_tail_web_log_drops_partial_first_line_on_byte_truncation(isolate_env):
    """字节截断落在行中间：首行是半个行，必须丢弃而不是展示残片。"""
    log_path = isolate_env / "ui.log"
    log_path.write_text("aaa\nbbb\n", encoding="utf-8")

    tail = mat.tail_web_log(max_bytes=6)

    assert tail["truncated"] is True
    assert tail["lines"] == ["bbb"]


@pytest.mark.unit
def test_tail_web_log_keeps_complete_first_line_when_boundary_aligned(isolate_env):
    """截断点恰好落在行首：首行完整，必须保留（旧启发式会白丢一行）。"""
    log_path = isolate_env / "ui.log"
    log_path.write_text("aaa\nbbb\n", encoding="utf-8")

    tail = mat.tail_web_log(max_bytes=4)  # 8 字节文件，边界正好在 "aaa\\n" 之后

    assert tail["truncated"] is True
    assert tail["lines"] == ["bbb"]


@pytest.mark.unit
def test_tail_web_log_refuses_symlink(isolate_env):
    """日志路径被摆成符号链接：拒读（面板不是别人的文件浏览器）。"""
    victim = isolate_env / "secret.txt"
    victim.write_text("机密", encoding="utf-8")
    (isolate_env / "ui.log").symlink_to(victim)

    tail = mat.tail_web_log()

    assert tail["exists"] is False
    assert tail["lines"] == []
    assert "符号链接" in tail["note"]


# ── 收尾子进程回收线程（防僵尸） ───────────────────────────────────────


@pytest.mark.unit
def test_reaper_waits_and_logs(caplog):
    class _Proc:
        pid = 7

        def wait(self):
            return 3

    with caplog.at_level("WARNING"):
        ops._reap_materialize_process(_Proc(), ops.logger)
    assert any("退出码 3" in r.message for r in caplog.records)
