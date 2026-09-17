"""
股票名称映射服务 - 从 stocks_index.json 加载股票代码与名称映射
"""

import json
import logging
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# 默认路径
DEFAULT_STOCKS_INDEX_PATH = Path("/app/data/stocks/stocks_index.json")
# 代码根（含 backend/ 的那一层）：源码运行为仓库根，便携包为包根。
_REPO_ROOT = Path(__file__).parent.parent.parent
_FALLBACK_PATHS = [
    Path("/app/data/stocks/stocks_index.json"),
    Path("/workspace/data/stocks/stocks_index.json"),
    # docker-compose 把仓库 data/ 挂到 /data（容器内实际命中这一条）
    Path("/data/stocks/stocks_index.json"),
    # 源码 / 便携包：<root>/data/stocks（曾写成上溯三级，便携包会指到包外一层，
    # 查不到索引且只留一条 WARNING → 中文名静默退化成代码）
    _REPO_ROOT / "data" / "stocks" / "stocks_index.json",
]

# 裸码 → 交易所推断（与 CLAUDE.md 分层口径一致）：6/9→SH、0/2/3→SZ、4/8→BJ
_CODE_HEAD_TO_EXCHANGE = {
    "6": "SH",
    "9": "SH",
    "0": "SZ",
    "2": "SZ",
    "3": "SZ",
    "4": "BJ",
    "8": "BJ",
}


def normalize_symbol(symbol: str) -> str | None:
    """任意层口径代码 → 后缀式（stocks_index.json 键口径，如 ``600085.SH``）。

    支持三种形态：后缀式（``002552.sz``）、前缀式（``SH600721``）、裸码（``600085``，
    按首位数字推断交易所）。无法识别返回 None。
    """
    s = str(symbol or "").strip().upper()
    if not s:
        return None
    # 前缀式：SH600721 / SZ000001 / BJ430047
    if len(s) == 8 and s[:2] in ("SH", "SZ", "BJ") and s[2:].isdigit():
        return f"{s[2:]}.{s[:2]}"
    # 后缀式：600085.SH / 002552.sz（大写化后）
    if "." in s:
        code, _, exchange = s.partition(".")
        if code.isdigit() and exchange in ("SH", "SZ", "BJ"):
            return f"{code}.{exchange}"
        return None
    # 裸码：按首位数字推断交易所
    if len(s) == 6 and s.isdigit():
        exchange = _CODE_HEAD_TO_EXCHANGE.get(s[0])
        if exchange:
            return f"{s}.{exchange}"
    return None


class StockNameMapper:
    """股票代码到名称的映射器"""

    _instance: Optional["StockNameMapper"] = None
    _initialized: bool = False

    def __new__(cls) -> "StockNameMapper":
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self):
        if self._initialized:
            return
        self._mapping: dict[str, str] = {}
        self._load_mapping()
        StockNameMapper._initialized = True

    def _load_mapping(self) -> None:
        """加载股票代码名称映射"""
        for path in _FALLBACK_PATHS:
            if path.exists():
                try:
                    with open(path, encoding="utf-8") as f:
                        data = json.load(f)
                    items = data.get("items", [])
                    for item in items:
                        symbol = item.get("symbol", "")
                        name = item.get("name", "")
                        if symbol and name:
                            self._mapping[symbol] = name
                    logger.info(
                        "Loaded %d stock name mappings from %s",
                        len(self._mapping),
                        path,
                    )
                    return
                except Exception as e:
                    logger.warning("Failed to load stock names from %s: %s", path, e)
                    continue

        logger.warning(
            "No stocks_index.json found, stock name mapping will be empty. "
            "Checked paths: %s",
            [str(p) for p in _FALLBACK_PATHS],
        )

    def get_name(self, symbol: str) -> str:
        """获取股票名称，未找到时返回原代码"""
        if not symbol:
            return symbol
        return self._mapping.get(symbol, symbol)

    def resolve(self, symbol: str) -> str:
        """任意层口径代码 → 名称；未收录返回空串（调用方自行回退显示代码）。"""
        key = normalize_symbol(symbol)
        if not key:
            return ""
        return self._mapping.get(key, "")

    def enrich_with_name(self, data: dict | list, symbol_key: str = "symbol") -> dict | list:
        """
        为数据添加 name 字段

        Args:
            data: 单个字典或字典列表
            symbol_key: 股票代码的键名

        Returns:
            添加了 name 字段的数据
        """
        if isinstance(data, list):
            for item in data:
                if isinstance(item, dict) and symbol_key in item:
                    item["name"] = self.get_name(item[symbol_key])
            return data
        elif isinstance(data, dict) and symbol_key in data:
            data["name"] = self.get_name(data[symbol_key])
            return data
        return data

    def refresh(self) -> None:
        """重新加载映射"""
        self._mapping.clear()
        self._load_mapping()


# 全局单例
_mapper: StockNameMapper | None = None


def get_stock_name_mapper() -> StockNameMapper:
    """获取股票名称映射器单例"""
    global _mapper
    if _mapper is None:
        _mapper = StockNameMapper()
    return _mapper


def get_stock_name(symbol: str) -> str:
    """便捷函数：获取股票名称"""
    return get_stock_name_mapper().get_name(symbol)


def resolve_name(symbol: str) -> str:
    """便捷函数：任意层口径代码 → 名称；未收录/无法识别返回空串（调用方回退显示代码）。"""
    return get_stock_name_mapper().resolve(symbol)
