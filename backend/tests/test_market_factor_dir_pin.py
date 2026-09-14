"""推理数据目录解析：模型 pin 优先，市场默认目录兜底。

跨市场迁移场景回归：自定义数据集训练、注册到常规市场的模型，metadata.json
里 quantdb_dir 指向训练数据根（如 /data/quantcustom），而 context.market 已被
改为 CN。若就绪检查按市场目录（/data/quantdb）解析，会因缺列而门禁失败、
静默兜底到系统模型 —— 必须与推理模板 _quantdb_reader 一样 pin 优先。
"""

from pathlib import Path

from backend.services.engine.inference.script_runner import (
    _resolve_market_factor_data_dir,
)


def test_pinned_quantdb_dir_wins_over_market_dir(tmp_path, monkeypatch):
    pinned = tmp_path / "quantcustom"
    market_dir = tmp_path / "quantdb"
    pinned.mkdir()
    market_dir.mkdir()
    monkeypatch.setattr(
        "backend.services.engine.data_platform.quantdb_factor_reader.market_data_dir",
        lambda _market=None: market_dir,
    )

    meta = {
        "data_source": "quantdb_factors",
        "quantdb_dir": str(pinned),
        "context": {"market": "CN"},
    }

    assert _resolve_market_factor_data_dir(meta) == str(pinned)


def test_falls_back_to_market_dir_when_pin_missing_or_invalid(tmp_path, monkeypatch):
    market_dir = tmp_path / "quantdb"
    market_dir.mkdir()
    monkeypatch.setattr(
        "backend.services.engine.data_platform.quantdb_factor_reader.market_data_dir",
        lambda _market=None: market_dir,
    )

    for meta in (
        {"context": {"market": "CN"}},
        {"quantdb_dir": "", "context": {"market": "CN"}},
        {"quantdb_dir": str(tmp_path / "not-there"), "context": {"market": "CN"}},
    ):
        assert _resolve_market_factor_data_dir(meta) == str(market_dir)


def test_missing_market_normalizes_to_cn_custom_stays_custom(tmp_path, monkeypatch):
    """市场缺省按 CN 解析；CUSTOM 是注册在册市场，原样传递（迁移前口径）。"""
    seen = {}

    def _fake_market_data_dir(market=None):
        seen["market"] = market
        return tmp_path

    monkeypatch.setattr(
        "backend.services.engine.data_platform.quantdb_factor_reader.market_data_dir",
        _fake_market_data_dir,
    )

    assert _resolve_market_factor_data_dir({"context": {}}) == str(tmp_path)
    assert seen["market"] == "CN"

    assert _resolve_market_factor_data_dir({"context": {"market": "CUSTOM"}}) == str(
        tmp_path
    )
    assert seen["market"] == "CUSTOM"
