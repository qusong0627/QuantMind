"""
股票列表API
从Qlib instruments数据提供股票列表和搜索功能
"""

import os
from pathlib import Path
from typing import Dict, List, Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

router = APIRouter(tags=["instruments"])  # 不要添加prefix，让main.py统一管理

# 股票数据缓存
_stock_cache: list[dict[str, str]] | None = None


class StockInfo(BaseModel):
    """股票信息"""

    symbol: str
    start_date: str
    end_date: str


# ... (rest of imports)


def _load_qlib_instruments() -> list[dict[str, str]]:
    """
    加载Qlib instruments数据
    格式: SYMBOL\tSTART_DATE\tEND_DATE
    例如: SH600000    2005-01-04    2025-12-31
    """
    global _stock_cache

    if _stock_cache is not None:
        return _stock_cache

    # 获取项目根目录 (app/api -> app -> qlib_service -> backend -> root)
    project_root = Path(__file__).resolve().parent.parent.parent.parent.parent
    default_path = project_root / "db" / "qlib_data" / "instruments" / "all.txt"

    # 优先使用环境变量
    qlib_data_path = Path(os.getenv("QLIB_INSTRUMENTS_PATH", str(default_path)))

    if not qlib_data_path.exists():
        # 尝试使用相对路径（如果是docker环境，可能挂载在不同位置）
        # 这里假设如果绝对路径不存在，可能是在Docker容器内的 /data 目录
        docker_path = Path("/data/qlib_data/instruments/all.txt")
        if docker_path.exists():
            qlib_data_path = docker_path
        else:
            raise FileNotFoundError(f"Qlib instruments file not found: {qlib_data_path}")

    stocks = []
    with open(qlib_data_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue

            parts = line.split("\t")
            if len(parts) >= 3:
                symbol = parts[0]
                start_date = parts[1]
                end_date = parts[2]

                # 保持原始前缀格式: SH600000
                if len(symbol) >= 8:
                    market = symbol[:2]  # SH, SZ, BJ
                    code = symbol[2:]  # 600000
                    formatted_symbol = symbol
                else:
                    formatted_symbol = symbol
                    code = symbol
                    market = ""

                # 统一剔除北交所
                if market.upper() == "BJ":
                    continue

                stocks.append(
                    {
                        "symbol": formatted_symbol,
                        "code": code,
                        "market": market,
                        "start_date": start_date,
                        "end_date": end_date,
                    }
                )

    _stock_cache = stocks
    return stocks


@router.get("/instruments/all", response_model=list[StockInfo])
async def get_all_instruments() -> list[StockInfo]:
    """
    获取所有股票列表
    """
    try:
        stocks = _load_qlib_instruments()
        return [
            StockInfo(
                symbol=s["symbol"],
                start_date=s["start_date"],
                end_date=s["end_date"],
            )
            for s in stocks
        ]
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to load instruments: {str(e)}")


@router.get("/instruments/search")
async def search_instruments(
    q: str = Query(..., description="搜索关键词（代码或简称）", min_length=1),
    limit: int = Query(10, description="返回结果数量", ge=1, le=100),
) -> list[dict[str, str]]:
    """
    搜索股票
    支持代码搜索（如：600000, 000001）
    注意：不包含股票名称，前端需要通过其他API获取名称
    """
    try:
        stocks = _load_qlib_instruments()

        # 转换查询为大写
        query = q.upper()

        # 搜索匹配
        results = []
        for stock in stocks:
            # 匹配代码
            if query in stock["code"]:
                results.append(
                    {
                        "symbol": stock["symbol"],
                        "code": stock["code"],
                        "market": stock["market"],
                        "start_date": stock["start_date"],
                        "end_date": stock["end_date"],
                    }
                )

                if len(results) >= limit:
                    break

        return results
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Search failed: {str(e)}")


@router.get("/instruments/count")
async def get_instruments_count() -> dict[str, int]:
    """
    获取股票数量统计
    """
    try:
        stocks = _load_qlib_instruments()

        # 按市场统计
        market_count = {}
        for stock in stocks:
            market = stock.get("market", "UNKNOWN")
            market_count[market] = market_count.get(market, 0) + 1

        return {"total": len(stocks), "by_market": market_count}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to get count: {str(e)}")
