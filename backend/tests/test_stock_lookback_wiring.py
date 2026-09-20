"""信号准确率回看的**接线**测试：源码级断言。

这里有两条硬理由必须存在，而不是「为了覆盖率」：

1. **不能用 `return_Nd`** 是数据契约级的禁令，违反后端点照样跑通、照样返回数字，
   只是数字是未来收益（标签泄漏）。运行期没有任何东西会拦住它——只有源码断言。
2. **一天只取一个 run** 同理：写错了会静默跨 run 拼分数，得到一份看起来很正常的
   「准确率」。这是本功能最大的坑（实测同日 run 相关低至 −0.08）。
3. **`asof` 必须转 `date`** 已实测炸过 500，且 `fromisoformat` 的紧凑式接受度
   随 Python 版本变（本机过、容器 400）——单测盯值，这里盯调用点。

纯函数的行为测试在 test_stock_lookback.py。
"""

import re
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_MODULE = _ROOT / "backend/services/api/stock_lookback.py"
_ROUTER = _ROOT / "backend/services/api/routers/stock_terminal.py"


@pytest.fixture(scope="module")
def module_src() -> str:
    return _MODULE.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def router_src() -> str:
    return _ROUTER.read_text(encoding="utf-8")


def _squash(s: str) -> str:
    """压掉空白，避免断言被缩进/换行改动误伤。"""
    return re.sub(r"\s+", " ", s)


def _tight(s: str) -> str:
    """连空格一起去掉——跨行调用的实参之间会留一个空格。"""
    return re.sub(r"\s+", "", s)


def _code_only(src: str) -> str:
    """去掉 docstring 与 `#` 注释，只留可执行代码。

    禁令类断言必须在**代码**上做：模块 docstring 里就写着 `return_1d[T]` 这个反例，
    不然测试会被自己写的警告文字绊倒。
    """
    src = re.sub(r'"""(?:.|\n)*?"""', "", src)
    src = re.sub(r"'''(?:.|\n)*?'''", "", src)
    return "\n".join(ln.split("#", 1)[0] for ln in src.splitlines())


def _endpoint_body(router_src: str) -> str:
    """截出 `/signal-lookback` 这一个处理函数。

    本文件是 2200+ 行的公共路由，既有的裸 parquet 路径等历史问题不属于本次改动，
    断言只应盯住新加的这一段。
    """
    start = router_src.index('@router.get("/signal-lookback")')
    rest = router_src[start + 10 :]
    nxt = rest.find("\n@router.")
    return rest[:nxt] if nxt >= 0 else rest


# ---------------------------------------------------------------------------
# 禁令：不得使用未来收益列（标签泄漏）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("src_name", ["module_src", "router_src"])
def test_no_future_return_columns(src_name, request):
    """`features_daily.return_Nd` / `technical_indicators.return_Nd` 是**未来收益**。

    实测 corr(return_1d[T], pct_change[T+1]) = 0.9991。用了它，回看表会「准得离谱」，
    而且没有任何运行期错误——这是本文件存在的第一理由。
    """
    hits = [
        ln
        for ln in _code_only(request.getfixturevalue(src_name)).splitlines()
        if re.search(r"return_\d+d", ln)
    ]
    assert not hits, f"出现未来收益列（标签泄漏）：{hits}"


def test_module_documents_the_forbidden_columns(module_src):
    """禁令要写在模块头里，不是只写在测试里——读代码的人得看得到为什么。"""
    head = module_src[:2000]
    assert "return_Nd" in head
    assert "0.9991" in head or "标签" in head


# ---------------------------------------------------------------------------
# 一天只取一个 run
# ---------------------------------------------------------------------------


def test_scores_sql_picks_exactly_one_run_per_day(module_src):
    """`DISTINCT ON (trade_date)` 是「一整天锚定同一个 run」的唯一实现。

    去掉它 → 同一天多 run 的行会一起进来，名次按混合后的分数现算，结果全错且无报错。
    """
    sql = _squash(module_src)
    assert "DISTINCT ON (trade_date) trade_date, run_id" in sql
    assert (
        "JOIN run_pick p ON p.trade_date = e.trade_date AND p.run_id = e.run_id" in sql
    )


def test_scores_sql_excludes_realtime_batch(module_src):
    """排除 `source='realtime'`（529 只热集批）——它是盘中增量，不该占当日口径。"""
    assert "COALESCE(source, 'batch') <> 'realtime'" in _squash(module_src)


def test_scores_sql_ranks_within_the_picked_run(module_src):
    """名次/分位必须现算：`score_rank` 批量路径恒 NULL、`rank_pct` 同日会多个 1.0。"""
    sql = _squash(module_src)
    assert "RANK() OVER (" in sql
    assert "PERCENT_RANK() OVER (" in sql
    assert "PARTITION BY e.trade_date" in sql


def test_fusion_score_filtered_on_both_sides(module_src):
    """`fusion_score IS NOT NULL` 在 run_pick（选 run）与外层（算名次）都要有。

    只在外层有 → 可能选到一个分数全空的日子；只在里层有 → 名次被 NULL 撑大分母。
    """
    assert _squash(module_src).count("fusion_score IS NOT NULL") >= 2


# ---------------------------------------------------------------------------
# 单一事实源
# ---------------------------------------------------------------------------


def test_signal_coverage_threshold_is_imported_not_duplicated(module_src):
    """覆盖阈值必须**引用** stock_terminal 的 `_MIN_SIGNAL_COVERAGE`。

    抄一个字面量 1000 出来 → 列表页改了阈值，回看表的锚点日期跟列表对不上。
    """
    assert (
        "from backend.services.api.routers.stock_terminal import _MIN_SIGNAL_COVERAGE"
        in _squash(module_src)
    )
    # 不得出现裸阈值常量
    assert not re.search(r"^\s*_?MIN_(SIGNAL_)?COVERAGE\s*=\s*1000", module_src, re.M)


def test_daily_forward_path_declared_once(module_src):
    """前复权视图路径只在本模块声明一处，路由不得另写字面量。"""
    assert module_src.count('"1_kline_data/daily_forward"') == 1


def test_new_endpoint_does_not_redeclare_the_view_path(router_src, module_src):
    """新端点自己不得再写一份路径字面量，更不得裸拼 parquet 分区路径。

    （`/list` 里另有一处历史裸 glob，属存量问题，不在本次改动范围——故只截本端点。）
    """
    body = _endpoint_body(router_src)
    assert "1_kline_data/daily_forward" not in body
    assert "read_parquet(" not in body
    assert "daily_forward" in module_src  # 路径确实存在于唯一事实源里


def test_symbol_normalization_delegates_to_stock_utils(module_src):
    """归一必须走 `StockCodeUtil.to_suffix`，不得手写交易所推断。

    层次口径是仓库的硬约定（QuantDB=后缀式 / PG=前缀式），手写一份必然漂。
    """
    assert "StockCodeUtil.to_suffix" in module_src
    assert "StockCodeUtil.to_prefix" in module_src


def test_no_hand_rolled_exchange_guessing(module_src):
    """不得按代码首位猜交易所（6→SH / 0,3→SZ …）——那是 StockCodeUtil 的职责。"""
    body = module_src.split('"""', 2)[-1]  # 跳过模块 docstring
    assert not re.search(r'startswith\(\("?6', body)


# ---------------------------------------------------------------------------
# asof：date 对象契约
# ---------------------------------------------------------------------------


def test_asof_bind_is_converted_to_date(module_src):
    """绑定值必须过 `parse_asof` 变成 `date`。

    实测传字符串：`DataError: 'str' object has no attribute 'toordinal'` → **500**。
    """
    assert 'params["asof"] = asof_d' in module_src
    assert "asof_d = parse_asof(asof)" in module_src


def test_router_validates_asof_before_calling(module_src, router_src):
    """路由层把非法 asof 转成 400，而不是让 asyncpg 抛 500。"""
    body = _squash(router_src)
    assert "sl.parse_asof(asof)" in body
    assert 'detail="asof 须为 YYYY-MM-DD"' in body


# ---------------------------------------------------------------------------
# 阻塞调用必须出事件循环
# ---------------------------------------------------------------------------


def test_blocking_calls_dispatched_off_event_loop(router_src):
    """parquet 读、交易日历、Redis 同步客户端都是阻塞的，必须走 `asyncio.to_thread`。

    直接 await 之外同步调用 → 卡住整个 api 进程（不只是这个请求）。
    """
    body = _tight(_endpoint_body(router_src))
    for fn in (
        "sl.latest_partition_on_or_before",
        "sl.fetch_close_panel",
        "sl.load_live_quotes",
    ):
        assert f"asyncio.to_thread({fn}" in body, f"{fn} 没走 asyncio.to_thread"


def test_sql_runner_is_awaited_not_threaded(router_src):
    """取数 SQL 是 async 的，只能 await——包进 to_thread 会拿到 coroutine 当结果。"""
    body = _tight(_endpoint_body(router_src))
    assert "awaitsl.fetch_signal_ladder(" in body
    assert "awaitsl.fetch_lookback_scores(" in body
    assert "asyncio.to_thread(sl.fetch_signal_ladder" not in body
    assert "asyncio.to_thread(sl.fetch_lookback_scores" not in body


def test_endpoint_registered_under_expected_path(router_src):
    """路由路径与前端/文档对齐（改路径必须同时改前端，别静默 404）。"""
    assert '@router.get("/signal-lookback")' in router_src


def test_endpoint_returns_standard_envelope(router_src):
    """统一信封 `{success, data}`——前端 service 层按 `resp.data?.data` 取值。"""
    assert '"success": True' in _endpoint_body(router_src)


def test_endpoint_declares_price_source_and_comparability(router_src):
    """前端表头要靠这三个字段标注口径与黄条，缺一个就只能瞎显示。"""
    body = _endpoint_body(router_src)
    for key in ('"price_source"', '"live_count"', '"comparable"', '"live"', '"mixed"'):
        assert key in body, f"响应缺少 {key}"
