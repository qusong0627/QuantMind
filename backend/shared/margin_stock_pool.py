"""固定融资融券股票池服务。"""

from __future__ import annotations

import json
import logging
import threading
from dataclasses import dataclass
from pathlib import Path
from collections.abc import Iterable

import pandas as pd

logger = logging.getLogger(__name__)


def normalize_symbol(symbol: str) -> str:
    text = str(symbol or "").strip().upper()
    if not text:
        return ""
    if text.endswith(".0") and text[:-2].isdigit():
        text = text[:-2]
    if len(text) == 8 and text[:2] in {"SH", "SZ", "BJ"}:
        return text
    if len(text) == 9 and "." in text:
        left, right = text.split(".", 1)
        if len(left) == 6 and right in {"SH", "SZ", "BJ"}:
            return right + left
    if text.isdigit() and len(text) < 6:
        text = text.zfill(6)
    if len(text) == 6 and text.isdigit():
        if text.startswith(("6", "9")):
            return "SH" + text
        if text.startswith(("0", "2", "3")):
            return "SZ" + text
        if text.startswith(("4", "8")):
            return "BJ" + text
    return text


@dataclass(frozen=True)
class MarginPoolSnapshot:
    symbols: frozenset[str]
    source_path: str
    imported_at: pd.Timestamp
    record_count: int


class MarginStockPoolService:
    """从固定 Excel 文件加载两融股票池，并提供只读判定能力。"""

    def __init__(self, source_path: str | Path):
        self.source_path = Path(source_path)
        self._lock = threading.Lock()
        self._snapshot: MarginPoolSnapshot | None = None

    def refresh(self) -> MarginPoolSnapshot:
        with self._lock:
            symbols = self._load_symbols()
            snapshot = MarginPoolSnapshot(
                symbols=symbols,
                source_path=str(self.source_path.resolve()),
                imported_at=pd.Timestamp.utcnow(),
                record_count=int(len(symbols)),
            )
            self._snapshot = snapshot
            logger.info(
                "融资融券股票池加载完成: path=%s count=%d",
                snapshot.source_path,
                snapshot.record_count,
            )
            return snapshot

    def _load_symbols(self) -> frozenset[str]:
        suffix = self.source_path.suffix.lower()
        if suffix in {".xlsx", ".xls"}:
            return self._load_from_excel()
        if suffix == ".json":
            return self._load_from_json()
        if suffix == ".txt":
            return self._load_from_instrument_text()
        raise ValueError(f"不支持的融资融券股票池格式: {self.source_path}")

    def _load_from_json(self) -> frozenset[str]:
        """从 JSON 文件加载股票池"""
        with open(self.source_path, encoding="utf-8") as f:
            data = json.load(f)

        # 支持两种 JSON 结构：
        # 1. {"stocks": [{"股票代码": "300981", ...}, ...]}
        # 2. {"symbols": ["SH600000", "SZ000001", ...]}
        symbols: set[str] = set()

        if "stocks" in data and isinstance(data["stocks"], list):
            # 从 stocks 数组中提取股票代码
            for stock in data["stocks"]:
                # 尝试多种可能的字段名
                for key in ["股票代码", "code", "symbol", "股票代码"]:
                    if key in stock:
                        normalized = normalize_symbol(str(stock[key]))
                        if normalized:
                            symbols.add(normalized)
                        break
        elif "symbols" in data and isinstance(data["symbols"], list):
            # 直接使用 symbols 数组
            for sym in data["symbols"]:
                normalized = normalize_symbol(str(sym))
                if normalized:
                    symbols.add(normalized)

        return frozenset(symbols)

    def _load_from_excel(self) -> frozenset[str]:
        df = pd.read_excel(self.source_path, dtype=str)
        code_col = next(
            (col for col in df.columns if "股票代码" in str(col)),
            None,
        )
        if code_col is None:
            raise ValueError(f"融资融券清单缺少股票代码列: {self.source_path}")
        return frozenset(normalize_symbol(value) for value in df[code_col].tolist() if normalize_symbol(value))

    def _load_from_instrument_text(self) -> frozenset[str]:
        symbols: set[str] = set()
        for line in self.source_path.read_text(encoding="utf-8").splitlines():
            text = line.strip()
            if not text:
                continue
            symbol = normalize_symbol(text.split("\t", 1)[0])
            if symbol:
                symbols.add(symbol)
        return frozenset(symbols)

    def snapshot(self) -> MarginPoolSnapshot:
        current = self._snapshot
        if current is not None:
            return current
        return self.refresh()

    def is_margin_eligible(self, symbol: str) -> bool:
        normalized = normalize_symbol(symbol)
        if not normalized:
            return False
        return normalized in self.snapshot().symbols

    def filter_symbols(self, symbols: Iterable[str]) -> list[str]:
        snapshot = self.snapshot()
        return [
            normalized
            for normalized in (normalize_symbol(sym) for sym in symbols)
            if normalized and normalized in snapshot.symbols
        ]


_SERVICES: dict[str, MarginStockPoolService] = {}


def get_margin_stock_pool_service(source_path: str | Path | None = None) -> MarginStockPoolService:
    root = Path(__file__).resolve().parents[2]
    resolved = Path(source_path) if source_path else root / "data" / "融资融券.xlsx"
    service_key = str(resolved.resolve())
    service = _SERVICES.get(service_key)
    if service is None:
        service = MarginStockPoolService(resolved)
        _SERVICES[service_key] = service
    return service
