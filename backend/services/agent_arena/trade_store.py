# ── 移植注（2026-09-29，arena 原生迁移 A2）─────────────────────────────
# 逐字节副本：quant-Trader `backend/services/trade_store.py`（上游已退役）。
# 改写点两处：① 导入换成本包 arena_config；② sqlite 缓存位置——上游写
# `get_data_root().parent / "trade_cache.sqlite"`，在容器里 parent 就是 `/`，
# 移植时改到 `<数据根>/arena/`（纯缓存，换位置无行为差）。
# ─────────────────────────────────────────────────────────────────────
"""交易记录 SQLite 缓存：position.jsonl 的懒构建索引，加速查询。

不侵入交易路径：首次查询时扫描 JSONL 构建，文件 mtime 变化自动重建。
"""

import json
import sqlite3
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from backend.services.agent_arena.arena_config import get_data_root, trade_cache_file

_DB_PATH: Optional[Path] = None
_mtime_cache: Dict[str, float] = {}


def _db(config: dict) -> Path:
    global _DB_PATH
    if _DB_PATH is None:
        _DB_PATH = trade_cache_file()
        _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    return _DB_PATH


def _ensure_schema(config: dict) -> sqlite3.Connection:
    db = sqlite3.connect(_db(config))
    db.execute(
        """CREATE TABLE IF NOT EXISTS trades (
            market TEXT, agent TEXT, date TEXT, action TEXT,
            symbol TEXT, amount REAL, cash_after REAL,
            PRIMARY KEY (market, agent, date, action, symbol, amount)
        )"""
    )
    db.execute("CREATE INDEX IF NOT EXISTS idx_trades_ma ON trades(market, agent, date)")
    return db


def _scan_and_rebuild(config: dict, market: str) -> None:
    """扫描 position.jsonl 重建该市场索引。"""
    data_dir = get_data_root(config) / {
        "us": "agent_data", "cn": "agent_data_astock", "hk": "agent_data_hk",
    }[market]
    db = _ensure_schema(config)
    db.execute("DELETE FROM trades WHERE market = ?", (market,))
    if data_dir.exists():
        for agent_dir in data_dir.iterdir():
            if not agent_dir.is_dir():
                continue
            pf = agent_dir / "position" / "position.jsonl"
            if not pf.exists():
                continue
            try:
                with pf.open("r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            doc = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        act = doc.get("this_action")
                        if not act:
                            continue
                        db.execute(
                            "INSERT OR REPLACE INTO trades VALUES (?,?,?,?,?,?,?)",
                            (market, agent_dir.name, doc.get("date", ""),
                             act.get("action", ""), act.get("symbol", ""),
                             act.get("amount", 0), doc.get("positions", {}).get("CASH")),
                        )
            except OSError:
                continue
    db.commit()
    db.close()


def _needs_rebuild(config: dict, market: str) -> bool:
    data_dir = get_data_root(config) / {
        "us": "agent_data", "cn": "agent_data_astock", "hk": "agent_data_hk",
    }[market]
    latest = 0.0
    if data_dir.exists():
        for pf in data_dir.glob("*/position/position.jsonl"):
            try:
                latest = max(latest, pf.stat().st_mtime)
            except OSError:
                pass
    key = f"{market}:{data_dir}"
    if _mtime_cache.get(key) != latest:
        _mtime_cache[key] = latest
        return True
    return False


def query_trades(config: dict, market: str, agent: str,
                 limit: int = 200) -> List[Dict[str, Any]]:
    if _needs_rebuild(config, market):
        _scan_and_rebuild(config, market)
    db = _ensure_schema(config)
    rows = db.execute(
        "SELECT date, action, symbol, amount, cash_after FROM trades"
        " WHERE market = ? AND agent = ? ORDER BY date DESC LIMIT ?",
        (market, agent, limit),
    ).fetchall()
    db.close()
    return [
        {"date": r[0], "action": r[1], "symbol": r[2], "amount": r[3], "cash_after": r[4]}
        for r in rows
    ]
