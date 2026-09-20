"""排除名单（通道 A）纯函数测试。

盯四件「错了也不报错、但会骗人」的事：

1. **后缀归一**：源文件是 6 位裸码（`002622`），而过滤侧 `df["Symbol"]` 是后缀式
   （`002622.SZ`）——不归一就是静默查空、1606 只一只都排除不掉，而界面上
   一切正常。注意 `fundamental_flags` 是裸码、`news_blacklist`/`block_buy`
   已经是后缀式，两种形态都要吃下。
2. **多源合并且不丢原因**：同一只票可能同时在基本面名单与新闻名单里，合并后
   两边的 reason 都要留得住（否则下钻看不到为什么被排除）。
3. **expire 语义**：`risk_block` 的解禁条带 `expire`（时间窗），过期即失效；
   但若同一只票还有一个**永久**来源，则它不该因为前者的 expire 一起失效。
4. **名单缺失 ≠ 空名单**：文件不在盘必须返回 None 并让调用方显式显示「未导入」，
   绝不能静默当成「没有风险股」——那就是假证据。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from backend.shared.exclusion_list import (
    ExclusionList,
    build_payload,
    load_exclusion_list,
)


def _fundamental() -> dict:
    return {
        "asof": "2026-09-18",
        "generated_at": "2026-09-18T08:35:43+08:00",
        "items": {
            "002622": {"flags": ["fin"], "asof": "2026-09-18", "reason": "连续6年亏损"},
            "605199": {
                "flags": ["fin", "trend", "illiquid"],
                "asof": "2026-09-18",
                "reason": "资产负债率86%；长期下跌",
            },
        },
    }


def _risk_block() -> dict:
    return {
        "asof": "2026-09-18",
        "generated_at": "2026-09-18T08:36:00+08:00",
        "items": {
            "002622": {"reason": "09/18解禁2.6%流通盘", "kind": "unlock", "expire": "2026-09-23"},
            "688368": {"reason": "解禁31.8%流通盘", "kind": "unlock", "expire": "2026-09-23"},
            "600370": {"reason": "股价1.2元低于预警线", "kind": "penny"},
        },
        "warns": {"600370": "质押比例79%"},
        "watch": {},
    }


def _news_blacklist() -> dict:
    return {
        "since": "2026-01-01",
        "until": "2026-09-11",
        "generated_at": "2026-09-11T10:00:00+08:00",
        "items": [
            {
                "code": "002622.SZ",
                "name": "某某股份",
                "cats": ["处罚", "立案"],
                "near_cats": ["立案"],
                "n": 114,
                "near_n": 87,
                "first": "2026-06-03",
                "last": "2026-09-09",
            }
        ],
    }


def _live_symbols() -> dict:
    return {"block_buy": ["000559.SZ", "002622.SZ"], "allow_st": False}


# ---------------------------------------------------------------- 后缀归一


def test_build_payload_normalizes_bare_codes_to_suffix() -> None:
    """源里是裸码 002622，产出必须是 002622.SZ（否则过滤侧静默查空）。"""
    # Arrange
    raw = {"fundamental_flags": _fundamental()}

    # Act
    payload = build_payload(raw, generated_at="2026-09-20T10:00:00Z")

    # Assert
    assert "002622.SZ" in payload["items"]  # 深市 00 开头
    assert "605199.SH" in payload["items"]  # 沪市 60 开头（605 是沪主板，不是深市）
    assert "002622" not in payload["items"]


def test_build_payload_keeps_already_suffixed_codes() -> None:
    """news_blacklist / block_buy 本来就是后缀式，不能二次改写。"""
    # Arrange
    raw = {"news_blacklist": _news_blacklist(), "live_symbols": _live_symbols()}

    # Act
    payload = build_payload(raw, generated_at="2026-09-20T10:00:00Z")

    # Assert
    assert "000559.SZ" in payload["items"]
    assert payload["items"]["000559.SZ"]["sources"] == ["block_buy"]


# ---------------------------------------------------------------- 多源合并


def test_multi_source_symbol_merges_sources_and_keeps_every_reason() -> None:
    """同一只票命中三个来源：sources 要全、每个来源的 reason 都要留得住。"""
    # Arrange
    raw = {
        "fundamental_flags": _fundamental(),
        "risk_block": _risk_block(),
        "live_symbols": _live_symbols(),
    }

    # Act
    payload = build_payload(raw, generated_at="2026-09-20T10:00:00Z")
    item = payload["items"]["002622.SZ"]

    # Assert
    assert item["sources"] == ["block_buy", "fundamental_flags", "risk_block"]
    assert "连续6年亏损" in item["reason"]
    assert "解禁2.6%流通盘" in item["reason"]
    assert set(item["by_source"]) == {"block_buy", "fundamental_flags", "risk_block"}


def test_merged_flags_are_deduped_and_sorted() -> None:
    """flags 取并集且稳定排序（前端按它出标签，顺序抖动会造成无谓 diff）。"""
    # Arrange
    raw = {"fundamental_flags": _fundamental(), "risk_block": _risk_block()}

    # Act
    payload = build_payload(raw, generated_at="2026-09-20T10:00:00Z")

    # Assert
    assert payload["items"]["002622.SZ"]["flags"] == ["fin", "unlock"]


# ---------------------------------------------------------------- expire 语义


def test_merged_reason_dedupes_shared_clauses_across_sources() -> None:
    """两个来源各自带上同一句话时，合并理由只留一遍，逐源明细一句不动。

    实测：隔壁 ``fundamental_flags`` 与 ``risk_block`` 都会写「连续3年亏损…」，
    整段比对去不掉（两段文字并不相同），合并后同一句话出现两遍 ——
    在表格里读起来像系统复读，还会把该来源独有的那句挤到看不见的地方。
    """
    # Arrange：两个来源，前半句相同、后半句各自独有
    raw = {
        "fundamental_flags": {
            "asof": "2026-09-18",
            "items": {"600606": {"flags": ["fin"], "reason": "连续3年亏损；资产负债率92%"}},
        },
        "risk_block": {
            "asof": "2026-09-18",
            "items": {
                "600606": {"reason": "股价1.32元低于2.0元预警线；连续3年亏损", "kind": "penny"}
            },
        },
    }

    # Act
    payload = build_payload(raw, generated_at="2026-09-20T10:00:00Z")
    item = payload["items"]["600606.SH"]

    # Assert：合并后「连续3年亏损」只出现一次，两句独有的话都留着
    assert item["reason"].count("连续3年亏损") == 1
    assert "资产负债率92%" in item["reason"]
    assert "股价1.32元低于2.0元预警线" in item["reason"]
    # 逐源明细对得上源文件（`by_source` 不去重，下钻要能看到各源原话）
    assert "连续3年亏损" in item["by_source"]["fundamental_flags"]["reason"]
    assert "连续3年亏损" in item["by_source"]["risk_block"]["reason"]


def test_expire_is_none_when_any_source_is_permanent() -> None:
    """有永久来源（基本面）时，不被另一来源的 expire 带过期。"""
    # Arrange
    raw = {"fundamental_flags": _fundamental(), "risk_block": _risk_block()}

    # Act
    payload = build_payload(raw, generated_at="2026-09-20T10:00:00Z")

    # Assert
    assert payload["items"]["002622.SZ"]["expire"] is None


def test_expire_is_max_when_all_sources_expire() -> None:
    """全来源都带窗口时，取最晚的那个（早过期的那个不该提前解除）。"""
    # Arrange
    raw = {
        "risk_block": {
            "asof": "2026-09-18",
            "items": {
                "688368": {"reason": "解禁", "kind": "unlock", "expire": "2026-09-23"},
                "688369": {"reason": "解禁", "kind": "unlock", "expire": "2026-09-30"},
            },
        }
    }

    # Act
    payload = build_payload(raw, generated_at="2026-09-20T10:00:00Z")

    # Assert
    assert payload["items"]["688368.SH"]["expire"] == "2026-09-23"


def test_expired_item_drops_out_of_symbols_but_stays_explainable() -> None:
    """过期项不再参与排除，但 explain 仍能说出「曾因什么被排除」。"""
    # Arrange
    raw = {"risk_block": _risk_block()}
    lst = ExclusionList.from_payload(build_payload(raw, generated_at="2026-09-20T10:00:00Z"))

    # Act
    valid = lst.symbols(today="2026-09-24")  # 已过 09-23
    hit = lst.explain("688368", today="2026-09-24")

    # Assert
    assert "688368.SH" not in valid
    assert hit is not None
    assert hit.expired is True


def test_item_within_window_is_excluded() -> None:
    """窗口内必须生效（否则解禁前禁买这条形同虚设）。"""
    # Arrange
    raw = {"risk_block": _risk_block()}
    lst = ExclusionList.from_payload(build_payload(raw, generated_at="2026-09-20T10:00:00Z"))

    # Act / Assert
    assert "688368.SH" in lst.symbols(today="2026-09-23")  # 窗口末日仍有效
    assert "688368.SH" not in lst.symbols(today="2026-09-24")


# ---------------------------------------------------------------- meta / 缺失


def test_meta_reports_counts_and_staleness() -> None:
    """基准日与陈旧天数是运维要看的（名单不刷新就没人知道）。"""
    # Arrange
    raw = {"fundamental_flags": _fundamental(), "live_symbols": _live_symbols()}
    lst = ExclusionList.from_payload(build_payload(raw, generated_at="2026-09-20T10:00:00Z"))

    # Act
    meta = lst.meta(today="2026-09-30")

    # Assert
    assert meta["asof"] == "2026-09-18"
    assert meta["stale_days"] == 12
    assert meta["counts"]["total"] == 3
    # 源名取**策略名** `block_buy`（不是文件名 `live_symbols`）：必须与
    # items[*].sources 同一套命名，否则「按源计数」与「按源打标签」对不上。
    assert meta["counts"]["by_source"]["block_buy"] == 2
    assert set(meta["counts"]["by_source"]) == {"block_buy", "fundamental_flags"}


def test_load_returns_none_when_file_missing(tmp_path: Path) -> None:
    """名单没导入 → None（调用方必须显式显示「未导入」），不是空名单。"""
    # Act
    lst = load_exclusion_list("CN", root=tmp_path)

    # Assert
    assert lst is None


def test_load_returns_none_on_corrupt_file(tmp_path: Path) -> None:
    """文件损坏同样按「未导入」处理——半份名单比没有名单更危险。"""
    # Arrange
    (tmp_path / "cn.json").write_text("{ this is not json", encoding="utf-8")

    # Act
    lst = load_exclusion_list("CN", root=tmp_path)

    # Assert
    assert lst is None


def test_load_round_trips_payload(tmp_path: Path) -> None:
    """落盘再读回，命中结果必须一致（导入器与容器侧同一份契约）。"""
    # Arrange
    payload = build_payload(
        {"fundamental_flags": _fundamental(), "live_symbols": _live_symbols()},
        generated_at="2026-09-20T10:00:00Z",
    )
    (tmp_path / "cn.json").write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    # Act
    lst = load_exclusion_list("CN", root=tmp_path)

    # Assert
    assert lst is not None
    hit = lst.explain("002622.SZ")
    assert hit is not None
    # 落盘再读回要连**多源与顺序**一起保住（002622 同时命中基础面与操作员黑名单）
    assert hit.sources == ("block_buy", "fundamental_flags")
    assert "连续6年亏损" in hit.reason


def test_explain_returns_none_for_unlisted_symbol() -> None:
    """没命中的票返回 None（调用方据此不渲染徽章）。"""
    # Arrange
    lst = ExclusionList.from_payload(
        build_payload({"fundamental_flags": _fundamental()}, generated_at="2026-09-20T10:00:00Z")
    )

    # Act / Assert
    assert lst.explain("600519.SH") is None  # 贵州茅台，不在名单里
    assert lst.explain("605199.SH") is not None  # 反证：名单里的确实能查到


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        ("600519", "600519.SH"),
        ("000001", "000001.SZ"),
        ("300750", "300750.SZ"),
        ("688981", "688981.SH"),
        ("830001", "830001.BJ"),
    ],
)
def test_bare_code_exchange_inference(code: str, expected: str) -> None:
    """裸码补后缀要按交易所规则，不能一律 .SZ。"""
    # Arrange
    raw = {"fundamental_flags": {"asof": "2026-09-18", "items": {code: {"flags": ["fin"], "reason": "x"}}}}

    # Act
    payload = build_payload(raw, generated_at="2026-09-20T10:00:00Z")

    # Assert
    assert expected in payload["items"]
