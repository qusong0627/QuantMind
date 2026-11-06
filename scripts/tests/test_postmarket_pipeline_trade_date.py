"""盘后链「交易日」解析口径的回归闸（2026-11-06）。

旧实现只认 ``daily_unadjusted`` 的最大分区：它缺分区时**静默退回前一天**，
流水线于是把昨天的候选池重算一遍（文件名/日志都像成功），而当天的池一整天
都不出现——2026-11-05 崩机腰斩同步、11-06 全天空池就是这么来的。

新口径：``daily_forward`` 与 ``daily_unadjusted`` 取较新者，并在两者不一致时
显式告警（不一致 = 同步没跑完）。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # scripts/
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # 仓库根（backend.*）

import postmarket_pipeline as pp  # noqa: E402


@pytest.fixture()
def logs(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    captured: list[str] = []
    monkeypatch.setattr(pp, "log", captured.append)
    return captured


def _stub(monkeypatch: pytest.MonkeyPatch, forward: str | None, unadjusted: str | None) -> None:
    table = {
        "1_kline_data/daily_forward": forward,
        "1_kline_data/daily_unadjusted": unadjusted,
    }
    monkeypatch.setattr(pp, "_max_partition_dt", lambda rel: table.get(rel))


def test_prefers_newer_forward_and_warns_on_mismatch(monkeypatch, logs):
    """崩机场景：forward 已到 11-05、unadjusted 停在 11-04 → 取 11-05 且必须告警。"""
    _stub(monkeypatch, "20261105", "20261104")

    assert pp.resolve_trade_date() == "20261105"
    assert any("数据不齐" in line for line in logs), "口径不一致必须喊出来，不许静默降级"


def test_quiet_when_both_calibers_agree(monkeypatch, logs):
    _stub(monkeypatch, "20261105", "20261105")

    assert pp.resolve_trade_date() == "20261105"
    assert not any("数据不齐" in line for line in logs)


def test_falls_back_to_unadjusted_when_forward_missing(monkeypatch, logs):
    """forward 分区读不到（历史库只有不复权）时，仍能用 unadjusted 定交易日。"""
    _stub(monkeypatch, None, "20261105")

    assert pp.resolve_trade_date() == "20261105"


def test_exits_when_both_calibers_unreadable(monkeypatch, logs):
    _stub(monkeypatch, None, None)

    with pytest.raises(SystemExit):
        pp.resolve_trade_date()
