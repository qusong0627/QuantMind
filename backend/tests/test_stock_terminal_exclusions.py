"""候选列表风险排除/标注（通道 A 名单 + 通道 B 新闻）的纯函数测试。

盯四件「错了不报错、但会骗人」的事：

1. **排除判据是「risk 桶非空」，不是「出现在标签表里」**。标签表里绝大多数行是
   涨停/大涨这类行情标签（近 20 天命中全市场约 39%），拿「有标签」当判据会静默
   废掉三分之一的市场。
2. **关掉的开关不算数，但标注照样给**：`excluded_counts` 只统计真正执行的通道
   （否则界面上「已排除 1606 只」而实际一只没排），而行级 `excluded` 与开关无关
   （关掉开关时前端仍要把这些行标出来）。
3. **名单缺失 ≠ 空名单**：`list_channel` 必须把 `imported=False` 交出去，
   否则「没名单」会被渲染成「已按名单过滤」——假证据。
4. **同名标的两个通道各记一笔**：用户要看的是每个通道各排掉多少，不是并集大小。
"""

from __future__ import annotations

import pandas as pd
import pytest

from backend.services.api.stock_terminal_exclusions import (
    _slim_tag,
    apply_exclusions,
    exclusion_meta,
    list_channel,
    risk_symbols,
    row_risk,
)
from backend.shared.exclusion_list import ExclusionList


def _df(*symbols: str) -> pd.DataFrame:
    return pd.DataFrame(
        {"Symbol": list(symbols), "Name": [f"股票{s}" for s in symbols]}
    )


def _lst(items: dict, *, asof: str = "2026-09-18") -> ExclusionList:
    return ExclusionList.from_payload(
        {
            "market": "CN",
            "asof": asof,
            "generated_at": f"{asof}T08:00:00Z",
            "counts": {"total": len(items), "blocking": len(items)},
            "items": items,
        }
    )


def _hit(**over) -> dict:
    base = {
        "sources": ["block_buy"],
        "flags": [],
        "reason": "操作员禁用",
        "expire": None,
        "blocking": True,
    }
    base.update(over)
    return base


# ---------------------------------------------------------------- 通道 B 口径


def test_risk_symbols_only_takes_symbols_with_risk_bucket() -> None:
    """只有 risk 桶非空的才进排除集合——这是本模块最容易写错的一处。

    表里 757 只标的绝大多数只有行情类标签；若判据退化成「出现在表里」，
    排除面会从 99 只暴增到 757 只，且表面上一切正常。
    """
    # Arrange
    by_symbol = {
        "600036.SH": {"risk": [{"tag": "立案调查"}], "pos_move": [{"tag": "涨停"}]},
        "000001.SZ": {"risk": [], "pos_move": [{"tag": "涨停"}, {"tag": "大涨"}]},
        "300750.SZ": {"risk": [], "pos_strong": [{"tag": "业绩预增"}]},
        "600519.SH": {"risk": [], "weak": [{"tag": "净利润下滑"}]},
    }

    # Act
    got = risk_symbols(by_symbol)

    # Assert
    assert got == {"600036.SH"}


def test_risk_symbols_ignores_weak_and_warn() -> None:
    """业绩类（weak）与减持解禁（warn）**不得**进默认排除集合。"""
    # Arrange
    by_symbol = {
        "600519.SH": {"weak": [{"tag": "业绩亏损"}]},
        "000001.SZ": {"warn": [{"tag": "解禁"}]},
        "600036.SH": {"risk": [{"tag": "财务造假"}]},
    }

    # Act
    got = risk_symbols(by_symbol)

    # Assert
    assert got == {"600036.SH"}


def test_risk_symbols_handles_empty_input() -> None:
    """空输入返回空集合（不是「全市场都排除」）。"""
    # Act / Assert
    assert risk_symbols({}) == frozenset()


# ---------------------------------------------------------------- 过滤


def test_apply_exclusions_drops_both_channels() -> None:
    """两个通道各自生效：名单 1 只 + 新闻 2 只（无交集）。"""
    # Arrange
    df = _df("600036.SH", "000001.SZ", "300750.SZ", "600519.SH")

    # Act
    out, counts = apply_exclusions(
        df,
        blocked={"600036.SH"},
        news_risk={"000001.SZ", "300750.SZ"},
        exclude_risk_list=True,
        exclude_news_risk=True,
    )

    # Assert
    assert list(out["Symbol"]) == ["600519.SH"]
    assert counts == {"risk_list": 1, "news_risk": 2}


def test_apply_exclusions_counts_only_enabled_channels() -> None:
    """关掉的通道计数必须是 0——否则界面显示「已排除 N 只」而实际没排。"""
    # Arrange
    df = _df("600036.SH", "600519.SH")

    # Act
    out, counts = apply_exclusions(
        df,
        blocked={"600036.SH"},
        news_risk={"600519.SH"},
        exclude_risk_list=False,
        exclude_news_risk=True,
    )

    # Assert
    assert list(out["Symbol"]) == ["600036.SH"]
    assert counts == {"risk_list": 0, "news_risk": 1}


def test_apply_exclusions_counts_intersection_twice_but_drops_once() -> None:
    """同一只票同时在两个名单上：两个通道各记一笔（用户要看每通道排掉多少），
    但表只被削减一次（并集），不会把它算成两只。"""
    # Arrange
    df = _df("600036.SH", "600519.SH")

    # Act
    out, counts = apply_exclusions(
        df,
        blocked={"600036.SH"},
        news_risk={"600036.SH"},
        exclude_risk_list=True,
        exclude_news_risk=True,
    )

    # Assert
    assert list(out["Symbol"]) == ["600519.SH"]
    assert counts == {"risk_list": 1, "news_risk": 1}


def test_apply_exclusions_is_noop_when_both_off() -> None:
    """两个开关都关：表原样返回（且计数为 0）。"""
    # Arrange
    df = _df("600036.SH")

    # Act
    out, counts = apply_exclusions(
        df,
        blocked={"600036.SH"},
        news_risk={"600036.SH"},
        exclude_risk_list=False,
        exclude_news_risk=False,
    )

    # Assert
    assert list(out["Symbol"]) == ["600036.SH"]
    assert counts == {"risk_list": 0, "news_risk": 0}


def test_apply_exclusions_handles_empty_frame() -> None:
    """空表：直接返回，不炸。"""
    # Act
    out, counts = apply_exclusions(
        _df(),
        blocked={"600036.SH"},
        news_risk=set(),
        exclude_risk_list=True,
        exclude_news_risk=True,
    )

    # Assert
    assert out.empty
    assert counts == {"risk_list": 0, "news_risk": 0}


def test_apply_exclusions_matches_suffix_codes_exactly() -> None:
    """口径是后缀式精确匹配：前缀式 ``SH600036`` 不是 ``600036.SH``，排不掉。"""
    # Arrange
    df = pd.DataFrame({"Symbol": ["SH600036", "600036.SH"]})

    # Act
    out, counts = apply_exclusions(
        df,
        blocked={"600036.SH"},
        news_risk=set(),
        exclude_risk_list=True,
        exclude_news_risk=False,
    )

    # Assert
    assert list(out["Symbol"]) == ["SH600036"]
    assert counts["risk_list"] == 1


# ---------------------------------------------------------------- 行级载荷


def test_row_risk_returns_none_without_any_hit() -> None:
    """无命中无标注 → None（前端据此不渲染徽章区）。"""
    # Act / Assert
    assert row_risk("600519.SH", lst=None) is None
    assert row_risk("600519.SH", lst=_lst({}), blocked=frozenset()) is None


def test_row_risk_flags_excluded_independent_of_switch() -> None:
    """行级 ``excluded`` 与开关无关——关掉开关时前端仍要标出这些行。"""
    # Arrange
    lst = _lst({"600036.SH": _hit()})

    # Act
    got = row_risk("600036.SH", lst=lst, blocked=lst.symbols())

    # Assert
    assert got is not None
    assert got["excluded"] is True
    assert got["hits"][0]["reason"] == "操作员禁用"


def test_row_risk_marks_news_risk_as_excluded_without_list_hit() -> None:
    """新闻 risk 档单独也能置 ``excluded``（名单没命中时）。"""
    # Arrange
    news = {"risk": [{"tag": "立案调查", "n": 2, "last": "2026-09-18Z"}], "weak": []}

    # Act
    got = row_risk("600036.SH", lst=None, news=news)

    # Assert
    assert got is not None
    assert got["excluded"] is True
    assert "hits" not in got
    assert got["news"]["risk"][0]["tag"] == "立案调查"


def test_row_risk_keeps_positive_and_negative_side_by_side() -> None:
    """利空与利好同时命中就都带上，不做优先级吞并。"""
    # Arrange
    news = {
        "risk": [{"tag": "警示函", "n": 1, "last": "2026-09-18Z"}],
        "pos_strong": [{"tag": "业绩预增", "n": 1, "last": "2026-09-19Z"}],
    }

    # Act
    got = row_risk("600036.SH", lst=None, news=news)

    # Assert
    assert got is not None
    assert got["news"]["risk"] and got["news"]["pos_strong"]
    assert got["excluded"] is True


def test_row_risk_ignores_expired_list_entry() -> None:
    """窗口已过的名单项不再进 hits（名单自己带 expire，到期即失效）。"""
    # Arrange
    lst = _lst(
        {"600036.SH": _hit(expire="2026-01-01", reason="限售解禁窗口")},
        asof="2026-09-18",
    )

    # Act
    got = row_risk("600036.SH", lst=lst, blocked=lst.symbols(), news=None)

    # Assert
    assert got is None


def test_row_risk_with_missing_list_still_annotates_news() -> None:
    """名单没导入（``lst=None``）时新闻标注照常给——两通道互不牵连。"""
    # Arrange
    news = {"weak": [{"tag": "净利润下滑", "n": 1, "last": "2026-09-18Z"}]}

    # Act
    got = row_risk("600036.SH", lst=None, news=news)

    # Assert
    assert got is not None
    assert got["excluded"] is False  # weak 不参与默认排除
    assert got["news"]["weak"][0]["tag"] == "净利润下滑"


# ---------------------------------------------------------------- 载荷裁剪


def test_slim_tag_keeps_evidence_only_for_risk() -> None:
    """只有 risk 档带证据标题（唯一会拦买的档，要能回答「凭什么」）。

    其余四档只给标签与条数：100 行 × 5 档 × 3 条标题会把列表响应撑大一个数量级，
    而列表是滚动翻页的高频接口。
    """
    # Arrange
    row = {
        "tag": "立案调查",
        "n": 3,
        "first": "2026-09-01Z",
        "last": "2026-09-18Z",
        "samples": ["标题一", "标题二", "标题三"],
    }

    # Act
    risk = _slim_tag(row, "risk")
    weak = _slim_tag(row, "weak")

    # Assert
    assert risk["samples"] == ["标题一", "标题二", "标题三"]
    assert "samples" not in weak
    assert weak["tag"] == "立案调查" and weak["n"] == 3


def test_slim_tag_caps_evidence_at_three() -> None:
    """证据标题最多 3 条（下钻够用，再多是灌水）。"""
    # Arrange
    row = {"tag": "立案调查", "n": 9, "samples": [f"标题{i}" for i in range(9)]}

    # Act
    got = _slim_tag(row, "risk")

    # Assert
    assert len(got["samples"]) == 3


# ---------------------------------------------------------------- 元信息


def test_exclusion_meta_keeps_both_channels_separate() -> None:
    """两条通道各带自己的基准/新鲜度，不合成一个含糊的「已过滤」。"""
    # Act
    meta = exclusion_meta(
        list_meta={"imported": True, "asof": "2026-09-18", "stale_days": 2},
        news_meta={"available": True, "window_end": "2026-09-20", "stale_days": 0},
        counts={"st": 204, "risk_list": 1606, "news_risk": 99},
        extra={"risk_list_size": 1606},
    )

    # Assert
    assert meta["list"]["imported"] is True
    assert meta["news"]["window_end"] == "2026-09-20"
    assert meta["excluded"]["risk_list"] == 1606
    assert meta["risk_list_size"] == 1606


def test_list_channel_reports_not_imported_when_file_missing(
    monkeypatch, tmp_path
) -> None:
    """名单文件不在盘 → ``imported=False`` + 空集合。

    这是本模块最重要的一条纪律：调用方**必须**能把「没有名单」显示出来。
    返回空集合但不带标记的话，界面会显示成「已按名单过滤（0 只）」。
    """
    # Arrange: 指到空目录
    from backend.shared import exclusion_list as el

    monkeypatch.setenv(el.EXCLUSION_DIR_ENV, str(tmp_path))
    el.clear_cache()

    # Act
    lst, blocked, meta = list_channel(today="2026-09-20")

    # Assert
    assert lst is None
    assert blocked == frozenset()
    assert meta["imported"] is False
    assert "reason" in meta


def test_list_channel_reads_imported_file(monkeypatch, tmp_path) -> None:
    """文件在盘 → 集合与元信息都可用（正对照，防上一条只是恒真）。"""
    # Arrange
    import json

    from backend.shared import exclusion_list as el

    monkeypatch.setenv(el.EXCLUSION_DIR_ENV, str(tmp_path))
    el.clear_cache()
    (tmp_path / "cn.json").write_text(
        json.dumps(
            {
                "market": "CN",
                "asof": "2026-09-18",
                "generated_at": "2026-09-18T08:00:00Z",
                "counts": {"total": 1, "blocking": 1},
                "items": {"600036.SH": _hit()},
            }
        ),
        encoding="utf-8",
    )

    # Act
    lst, blocked, meta = list_channel(today="2026-09-20")

    # Assert
    assert lst is not None
    assert blocked == {"600036.SH"}
    assert meta["imported"] is True
    assert meta["stale_days"] == 2


@pytest.mark.parametrize(
    ("exclude_risk", "exclude_news", "expect_left"),
    [
        (True, True, ["600519.SH"]),
        (True, False, ["000001.SZ", "600519.SH"]),
        (False, True, ["600036.SH", "600519.SH"]),
        (False, False, ["600036.SH", "000001.SZ", "600519.SH"]),
    ],
)
def test_apply_exclusions_toggle_matrix(
    exclude_risk: bool, exclude_news: bool, expect_left: list[str]
) -> None:
    """四个开关组合各查一遍（关一个，命中数必须上升）。"""
    # Arrange
    df = _df("600036.SH", "000001.SZ", "600519.SH")

    # Act
    out, _ = apply_exclusions(
        df,
        blocked={"600036.SH"},
        news_risk={"000001.SZ"},
        exclude_risk_list=exclude_risk,
        exclude_news_risk=exclude_news,
    )

    # Assert
    assert list(out["Symbol"]) == expect_left
