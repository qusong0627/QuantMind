"""Qlib 数据读取适配（研究模块；qlib 为延迟导入，纯函数层可离线测试）。"""

from __future__ import annotations

import logging
from pathlib import Path

import pandas as pd

log = logging.getLogger("kronos_ashare.data")

FIELDS = ("$open", "$high", "$low", "$close", "$volume", "$amount", "$change")


def init_qlib(provider_uri: str) -> None:
    """初始化 Qlib（容器内/远端节点执行）。"""
    import qlib
    from qlib.constant import REG_CN

    provider = Path(provider_uri).expanduser()
    if not provider.is_dir():
        raise FileNotFoundError(f"qlib provider not found: {provider}")
    qlib.init(provider_uri=str(provider), region=REG_CN)
    log.info("qlib initialized: %s", provider)


def read_instruments(provider_uri: str, name: str = "csi300") -> list[str]:
    """读取 Qlib instruments 文件（symbol\\tstart\\tend）。"""
    path = Path(provider_uri).expanduser() / "instruments" / f"{name}.txt"
    if not path.is_file():
        raise FileNotFoundError(f"instruments file not found: {path}")
    symbols: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        parts = line.split()
        if parts:
            symbols.append(parts[0])
    return symbols


def trading_calendar(start: str, end: str) -> pd.DatetimeIndex:
    """日频交易日历。"""
    from qlib.data import D

    cal = D.calendar(start_time=start, end_time=end, freq="day")
    return pd.DatetimeIndex(cal)


def load_panel(symbols: list[str], start: str, end: str) -> dict[str, pd.DataFrame]:
    """批量读取 OHLCVA + change，按标的分组成 DataFrame。"""
    from qlib.data import D

    raw = D.features(symbols, list(FIELDS), start_time=start, end_time=end, freq="day")
    if raw is None or raw.empty:
        return {}
    raw = raw.rename(columns=lambda c: str(c).lstrip("$"))
    panels: dict[str, pd.DataFrame] = {}
    for symbol in symbols:
        try:
            sub = raw.xs(symbol, level="instrument")
        except (KeyError, ValueError):
            try:
                sub = raw.xs(symbol, level=0)
            except (KeyError, ValueError):
                continue
        sub = sub.sort_index()
        sub = sub[~sub.index.duplicated(keep="last")]
        panels[symbol] = sub
    return panels
