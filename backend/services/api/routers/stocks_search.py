"""
股票搜索接口（API 网关本地索引版）

目标：
1. 避免前端直连第三方行情服务产生 CORS 问题。
2. 通过服务器本地 JSON 索引提供稳定、低延迟的搜索能力。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from threading import RLock
from typing import Any, Dict, List

from fastapi import APIRouter, HTTPException, Query

from backend.shared.logging_config import get_logger

logger = get_logger(__name__)

router = APIRouter(prefix="/api/v1/stocks", tags=["Stocks"])


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


@dataclass
class StockIndexItem:
    symbol: str
    code: str
    exchange: str
    name: str
    abbr: str = ""
    pinyin: str = ""

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> StockIndexItem:
        symbol = str(raw.get("symbol") or "").strip().upper()
        code = str(raw.get("code") or "").strip()
        exchange = str(raw.get("exchange") or "").strip().upper()
        name = str(raw.get("name") or "").strip()
        abbr = str(raw.get("abbr") or "").strip().lower()
        pinyin = str(raw.get("pinyin") or "").strip().lower()

        if not symbol and code and exchange:
            symbol = f"{code}.{exchange}"
        if not code and symbol and "." in symbol:
            code = symbol.split(".", 1)[0]
        if not exchange and symbol and "." in symbol:
            exchange = symbol.split(".", 1)[1]

        return cls(
            symbol=symbol,
            code=code,
            exchange=exchange,
            name=name,
            abbr=abbr,
            pinyin=pinyin,
        )

    def searchable_text(self) -> str:
        return " ".join(
            [
                self.symbol.lower(),
                self.code.lower(),
                self.name.lower(),
                self.abbr,
                self.pinyin,
            ]
        ).strip()

    def to_result(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "code": self.symbol,  # 前端历史字段沿用 code，统一返回标准代码
            "name": self.name,
            "market": self.exchange,
        }


class StockIndexStore:
    def __init__(self) -> None:
        # 支持多个备选路径，优先使用环境变量，然后尝试容器内挂载路径
        candidate_paths = [
            os.getenv("STOCK_INDEX_JSON_PATH"),
            "/data/stocks/stocks_index.json",  # Docker 挂载路径
            "data/stocks/stocks_index.json",   # 相对路径
            "/app/data/stocks/stocks_index.json",  # 容器内绝对路径
        ]
        self.path = None
        for p in candidate_paths:
            if p and os.path.exists(p):
                self.path = os.path.abspath(p)
                break
        if not self.path:
            # 回退到默认路径（会在 _load_if_needed 中报错）
            self.path = os.path.abspath(os.getenv("STOCK_INDEX_JSON_PATH", "data/stocks/stocks_index.json"))
        self._lock = RLock()
        self._mtime: float = -1.0
        self._items: list[StockIndexItem] = []

    def _load_if_needed(self) -> None:
        with self._lock:
            if not os.path.exists(self.path):
                raise FileNotFoundError(self.path)

            mtime = os.path.getmtime(self.path)
            if mtime == self._mtime:
                return

            with open(self.path, encoding="utf-8") as f:
                payload = json.load(f)

            raw_items = payload.get("items")
            if not isinstance(raw_items, list):
                raise ValueError("stocks_index.json 缺少 items 数组")

            loaded: list[StockIndexItem] = []
            for raw in raw_items:
                if not isinstance(raw, dict):
                    continue
                item = StockIndexItem.from_dict(raw)
                if item.symbol and item.name:
                    loaded.append(item)

            self._items = loaded
            self._mtime = mtime
            logger.info("已加载股票索引: path=%s count=%s", self.path, len(loaded))

    def search(self, keyword: str, limit: int) -> list[dict[str, Any]]:
        self._load_if_needed()
        k = keyword.strip().lower()
        if not k:
            return []

        starts: list[StockIndexItem] = []
        contains: list[StockIndexItem] = []
        for item in self._items:
            text = item.searchable_text()
            if text.startswith(k) or item.symbol.lower().startswith(k) or item.code.lower().startswith(k):
                starts.append(item)
            elif k in text:
                contains.append(item)

            if len(starts) >= limit:
                break

        merged = (starts + contains)[:limit]
        return [x.to_result() for x in merged]

    def status(self) -> dict[str, Any]:
        exists = os.path.exists(self.path)
        stat = os.stat(self.path) if exists else None
        return {
            "path": self.path,
            "exists": exists,
            "size": stat.st_size if stat else 0,
            "mtime": datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat() if stat else None,
            "loaded_count": len(self._items),
        }


stock_index_store = StockIndexStore()


@router.get("/search")
async def search_stocks(
    q: str = Query(..., description="搜索关键词"),
    limit: int = Query(20, ge=1, le=200, description="最大返回数量"),
):
    try:
        results = stock_index_store.search(keyword=q, limit=limit)
    except FileNotFoundError as exc:
        logger.error("股票索引文件不存在: %s", exc)
        raise HTTPException(
            status_code=503,
            detail=(
                "股票索引未就绪，请先在服务器执行构建脚本：" "python backend/services/api/scripts/build_stock_index.py"
            ),
        )
    except Exception as exc:
        logger.error("股票索引搜索失败: %s", exc, exc_info=True)
        raise HTTPException(status_code=500, detail=f"股票搜索失败: {exc}")

    return {
        "query": q,
        "results": results,
        "total": len(results),
        "timestamp": _now_iso(),
        "source": "stocks-index-json",
    }


@router.get("/search/status")
async def search_status():
    return {
        "status": "ok",
        "timestamp": _now_iso(),
        "index": stock_index_store.status(),
    }
