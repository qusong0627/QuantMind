"""告警文案测试：用户收到的那条**逐字复现**，外加前后端标签表漂移守卫。

背景（2026-09-24 实测）：通知正文原是告警信封的 `json.dumps`，标的是裸代码。
下面的 `_REAL_ROW` 就是库里那条 `notifications.content`（symbol 600503.SH）。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

#: 库里 2026-09-24 15:09 那条（sentinel_alerts.detail + notifications.title/content 原文）。
_REAL_ROW = {
    "alert_type": "anomaly:price_surge",
    "symbol": "600503.SH",
    "market": "CN",
    "severity": "warn",
    "direction": "down",
    "title": "600503.SH 大幅下行",
    "ts": 1790233748.213,  # 2026-09-24 15:09:08 CST（= msg_id 1790233748213-0 的毫秒纪元）
    "targets": ["600503.SH", "600503.SS"],
    "detail": {
        "payload": {
            "kind": "price_surge",
            "title": "600503.SH 大幅下行",
            "description": "涨跌幅 -9.86%（阈值 5.0%）",
            "metrics": {"price": 2.65, "pct_chg": -0.09863945578231292},
        },
        "actions_hint": ["risk_review"],
        "msg_id": "1790233748213-0",
    },
}


def _fmt(row=None, name="华丽家族"):
    from backend.shared.alert_text import format_sentinel_alert

    return format_sentinel_alert(row or _REAL_ROW, name_resolver=lambda _s: name)


# ── 正文：给人读 ─────────────────────────────────────────────────────


def test_body_is_chinese_prose_not_json_envelope():
    _title, body = _fmt()
    assert "{" not in body and "}" not in body
    assert "alert_type" not in body and "targets" not in body
    # 浮点原值不得出现——数字要按中文口径格式化
    assert "-0.09863945578231292" not in body
    assert "涨跌幅 -9.86%（阈值 5.0%）" in body
    assert body.rstrip().endswith("详情见交易台 · 实时情报。")


def test_body_carries_identity_context_and_extra_metrics():
    _title, body = _fmt()
    lines = body.splitlines()
    # 现价不在描述里 → 单独一行补上；涨跌幅已在描述里 → 不重复
    assert "现价 2.65" in lines
    assert body.count("涨跌幅") == 1
    assert "A股 · 大幅波动 · 利空 · 09-24 15:09" in lines


def test_title_uses_stock_name_and_chinese_severity():
    title, _body = _fmt()
    assert title == "[关注] 华丽家族(600503.SH) 大幅下行"


def test_title_falls_back_to_bare_symbol_when_name_missing():
    title, _body = _fmt(name="")
    assert title == "[关注] 600503.SH 大幅下行"


def test_title_rejects_name_equal_to_symbol():
    """名字服务把代码原样返回时，不得拼出 ``600503.SH(600503.SH)``。"""
    title, _body = _fmt(name="600503.SH")
    assert title == "[关注] 600503.SH 大幅下行"


def test_title_prepends_identity_when_title_has_no_symbol():
    row = {**_REAL_ROW, "title": "大幅下行"}
    title, _body = _fmt(row)
    assert title == "[关注] 华丽家族(600503.SH) 大幅下行"


# ── 缺字段 / 脏字段：降级但绝不编造 ───────────────────────────────────


def test_description_missing_is_rebuilt_from_known_metrics():
    row = {
        **_REAL_ROW,
        "detail": {
            "payload": {
                "kind": "price_surge",
                "metrics": {"price": 2.65, "pct_chg": -0.098},
            }
        },
    }
    _title, body = _fmt(row)
    assert "现价 2.65 · 涨跌幅 -9.80%" in body


def test_unknown_metric_keys_never_reach_the_body():
    """没登记标签的键不进正文（否则正文又变回代码汤）；原始字段留在 detail 里。"""
    row = {
        **_REAL_ROW,
        "detail": {
            "payload": {
                "kind": "price_surge",
                "description": "涨跌幅 -9.86%（阈值 5.0%）",
                "metrics": {"price": 2.65, "this_key_is_unknown": 12345},
            }
        },
    }
    _title, body = _fmt(row)
    assert "this_key_is_unknown" not in body and "12345" not in body


def test_unparseable_metric_value_is_skipped_not_printed_as_nan():
    row = {
        **_REAL_ROW,
        "detail": {"payload": {"kind": "price_surge", "metrics": {"price": "n/a"}}},
    }
    _title, body = _fmt(row)
    assert "nan" not in body.lower() and "n/a" not in body


def test_wildcard_symbol_has_no_identity_and_no_time_gap():
    row = {
        **_REAL_ROW,
        "symbol": "*",
        "targets": [],
        "title": "市场状态转为弱势",
        "alert_type": "regime",
        "direction": "down",
        "detail": {"payload": {"state": "bear"}},
    }
    title, body = _fmt(row)
    assert title == "[关注] 市场状态转为弱势"
    assert "A股 · 市场状态 · 利空 · 09-24 15:09" in body


def test_name_resolver_failure_degrades_to_symbol(monkeypatch):
    """名字索引坏掉只该少一个名字，不该少一条告警（走默认解析器的真实降级路径）。"""
    import backend.shared.stock_name_mapper as mapper

    def boom(_symbol: str) -> str:
        raise RuntimeError("index unavailable")

    monkeypatch.setattr(mapper, "resolve_name", boom)
    from backend.shared.alert_text import format_sentinel_alert

    title, body = format_sentinel_alert(
        {**_REAL_ROW, "detail": {"payload": {"description": "涨跌幅 -9.86%"}}}
    )
    assert title == "[关注] 600503.SH 大幅下行"
    assert "涨跌幅 -9.86%" in body


# ── 前后端标签表漂移守卫（金样对拍）────────────────────────────────────

_GOLDEN_PATH = Path(__file__).resolve().parent / "fixtures" / "alertLabelsGolden.json"


def _golden() -> dict:
    """读金样；**空金样 = 假通过**，所以这里先把两条都非空钉死。

    前端那侧（``electron/src/features/desk/components/__tests__/``）读的是同一份
    文件——金样放在后端包内，是因为后端测试跑在容器里只挂了 ``./backend``。
    """
    doc = json.loads(_GOLDEN_PATH.read_text(encoding="utf-8"))
    assert doc.get("severity") and doc.get("type"), "金样为空（对拍会变成假通过）"
    return doc


def test_severity_labels_match_golden():
    from backend.shared.alert_text import SEVERITY_LABELS

    assert SEVERITY_LABELS == _golden()["severity"]


def test_type_labels_match_golden():
    from backend.shared.alert_text import TYPE_LABELS

    assert TYPE_LABELS == _golden()["type"]


# ── 市场名（唯一实现）────────────────────────────────────────────────


def test_market_label_covers_all_platform_markets_and_keeps_unknown_verbatim():
    from backend.shared.market_labels import market_label

    assert [market_label(m) for m in ("CN", "hk", "us", "CRYPTO", "FUTURES")] == [
        "A股",
        "港股",
        "美股",
        "加密",
        "期货",
    ]
    assert market_label("SG") == "SG"  # 未收录回原样，不编名字
    assert market_label(None) == ""
