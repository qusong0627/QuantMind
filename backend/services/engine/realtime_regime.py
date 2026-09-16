"""日内市场状态服务（T-P6-13）：指数（默认沪深300）+ 热集广度 → regime 快照 + 总线事件。

**口径单源**：滚动/分级全部走 ``shared/market_regime``（与日频 MarketStateService 共用，
15:00 终值收敛后与日频公式逐值同——见 ``backend/tests/test_realtime_regime.py`` 的口径金样）。

**数据源与诚实边界**：
- 指数日线历史 = QuantDB ``qdb_index_daily``（close+volume）；
- 指数 live = 行情源 ``market:snapshot:sh000300``（2026-09-17 探针：TDX 订阅通道接受指数码，
  热集构建器将指数列为常驻源）——**无 live 值不发快照、不臆造**（计数 `skipped_no_live`）；
- 广度（涨跌家数/涨跌停近似）来自**热集快照**，覆盖=527 只样本而非全市场——如实标注
  ``breadth.coverage=hot_set``，**不参与 state 判定**（state 只吃指数三输入，与日频一致）。

**输出**：Redis 快照 ``qm:regime:intraday``（Hash）+ 情报总线 ``regime`` 事件（bear→warn）。
门控 ``qm:realtime:regime:config``（enabled/cadence_s，热读，默认关）；engine lifespan 接线。
"""

from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime, timedelta, timezone
from collections.abc import Callable
from typing import Any

from backend.shared.intel_events import build_event, publish_event
from backend.shared.market_regime import (
    DEFAULT_REGIME_INDEX,
    DEFAULT_WINDOW,
    POSITION_BY_STATE,
    classify_regime,
    forming_inputs,
)

logger = logging.getLogger(__name__)

CST = timezone(timedelta(hours=8))
CONFIG_KEY = "qm:realtime:regime:config"
SNAPSHOT_KEY = "qm:regime:intraday"
DEFAULT_CADENCE_S = 60.0
HISTORY_DAYS = 120  # 日历日：覆盖 window+ 缓冲


def _now() -> datetime:
    return datetime.now(tz=CST)


def _main_redis(decode: bool = True):
    import os

    import redis as redis_lib

    return redis_lib.Redis(
        host=os.getenv("REDIS_HOST") or "redis",
        port=int(os.getenv("REDIS_PORT", "6379")),
        db=int(os.getenv("REDIS_DB", "0")),
        password=os.getenv("REDIS_PASSWORD") or None,
        decode_responses=decode, socket_connect_timeout=3, socket_timeout=5,
    )


def _remote_redis():
    from backend.shared.remote_quote_config import make_sync_client

    return make_sync_client()


class RegimeConfig:
    __slots__ = ("enabled", "cadence_s", "index_symbol", "window")

    def __init__(self, *, enabled: bool = False, cadence_s: float = DEFAULT_CADENCE_S,
                 index_symbol: str = DEFAULT_REGIME_INDEX, window: int = DEFAULT_WINDOW) -> None:
        self.enabled = enabled
        self.cadence_s = max(10.0, float(cadence_s))
        self.index_symbol = index_symbol
        self.window = max(5, int(window))

    @classmethod
    def from_mapping(cls, raw: dict[str, Any] | None) -> RegimeConfig:
        raw = raw or {}
        try:
            cadence = float(raw.get("cadence_s") or DEFAULT_CADENCE_S)
            window = int(raw.get("window") or DEFAULT_WINDOW)
        except (TypeError, ValueError):
            cadence, window = DEFAULT_CADENCE_S, DEFAULT_WINDOW
        return cls(
            enabled=str(raw.get("enabled") or "").strip().lower() in {"1", "true", "yes", "on"},
            cadence_s=cadence,
            index_symbol=str(raw.get("index_symbol") or DEFAULT_REGIME_INDEX).strip() or DEFAULT_REGIME_INDEX,
            window=window,
        )


def _load_config_sync() -> RegimeConfig:
    try:
        client = _main_redis()
        raw = client.hgetall(CONFIG_KEY) or {}
        client.close()
        return RegimeConfig.from_mapping(raw)
    except Exception:  # noqa: BLE001
        return RegimeConfig()


class RealtimeRegimeService:
    """日内 regime 常驻服务（计算在循环线程；发布 best-effort）。"""

    def __init__(
        self,
        *,
        config_loader: Callable[[], RegimeConfig] | None = None,
        history_loader: Callable[[str], dict[str, Any]] | None = None,
        index_snapshot_fetcher: Callable[[str], dict[str, Any] | None] | None = None,
        breadth_fetcher: Callable[[], dict[str, Any] | None] | None = None,
        publisher: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self._config_loader = config_loader or _load_config_sync
        self._history_loader = history_loader
        self._index_snapshot_fetcher = index_snapshot_fetcher
        self._breadth_fetcher = breadth_fetcher
        self._publisher = publisher
        self._task: Any = None
        self._stop: Any = None  # asyncio.Event（start 时创建，避免导入期绑 loop）
        self._lock = threading.Lock()
        self.counters: dict[str, Any] = {
            "cycles": 0, "published": 0, "skipped_no_live": 0, "errors": 0,
            "last_state": None, "last_ts": None, "last_error": None,
        }

    # ── 默认数据源 ──────────────────────────────────────────────────

    def _default_history(self, symbol: str) -> dict[str, Any]:
        from backend.services.engine.data_platform.quantdb_hub import QuantDBDataHub
        from backend.shared.stock_utils import StockCodeUtil

        hub = QuantDBDataHub.get_instance()
        end = _now().date()
        start = end - timedelta(days=HISTORY_DAYS)
        df = hub.fetch_series(
            "qdb_index_daily", StockCodeUtil.to_suffix(symbol),
            start.strftime("%Y%m%d"),  # hub 口径：YYYYMMDD 字符串（datetime 会炸在切片解析）
            end.strftime("%Y%m%d"),
            columns=["close", "volume"],
        )
        if df is None or len(df) == 0:
            return {"dates": [], "closes": [], "volumes": []}
        # hub 口径：RangeIndex + dt 列（int YYYYMMDD）——按 dt 排序并转 YYYY-MM-DD 标签
        if "dt" in df.columns:
            df = df.sort_values("dt")

            def _label(v: Any) -> str:
                text = str(int(v))
                return f"{text[:4]}-{text[4:6]}-{text[6:8]}"

            dates = [_label(v) for v in df["dt"].tolist()]
        else:
            dates = [d.strftime("%Y-%m-%d") if hasattr(d, "strftime") else str(d)[:10] for d in df.index]
        return {
            "dates": dates,
            "closes": [float(v) for v in df["close"].tolist()],
            "volumes": [float(v) for v in df.get("volume", []).tolist()] if "volume" in df else [],
        }

    def _default_index_snapshot(self, symbol: str) -> dict[str, Any] | None:
        client = _remote_redis()
        if client is None:
            return None
        try:
            code = symbol.split(".")[0]
            market = symbol.split(".")[-1].lower()
            data = client.hgetall(f"market:snapshot:{market}{code}")
            if not data:
                return None
            close = float(data.get("Now") or 0)
            if close <= 0:
                return None
            volume = data.get("Volume")
            return {
                "close": close,
                "volume": float(volume) if volume not in (None, "") else None,
                "ts": float(data.get("timestamp") or 0),
            }
        except Exception:  # noqa: BLE001
            return None
        finally:
            try:
                client.close()
            except Exception:  # noqa: BLE001
                pass

    def _default_breadth(self) -> dict[str, Any] | None:
        """热集广度（样本覆盖，如实标注；不参与 state）。"""
        client = _remote_redis()
        if client is None:
            return None
        try:
            hot = sorted(client.smembers("qm:hot_set:symbols") or [])
            if not hot:
                return None
            up = down = flat = 0
            limit_up = limit_down = 0
            for start in range(0, len(hot), 200):
                chunk = hot[start: start + 200]
                pipe = client.pipeline(transaction=False)
                for sym in chunk:
                    code, mk = sym.split(".")
                    pipe.hgetall(f"market:snapshot:{mk.lower()}{code}")
                for data in pipe.execute():
                    if not data:
                        continue
                    try:
                        now_p = float(data.get("Now") or 0)
                        pre = float(data.get("PreClose") or 0)
                        lup = float(data.get("LimitUp") or 0)
                        ldn = float(data.get("LimitDown") or 0)
                    except (TypeError, ValueError):
                        continue
                    if now_p <= 0 or pre <= 0:
                        continue
                    if lup > 0 and now_p >= lup - 0.004:
                        limit_up += 1
                    if ldn > 0 and now_p <= ldn + 0.004:
                        limit_down += 1
                    if now_p > pre:
                        up += 1
                    elif now_p < pre:
                        down += 1
                    else:
                        flat += 1
            return {
                "coverage": "hot_set", "sample": len(hot),
                "up": up, "down": down, "flat": flat,
                "limit_up": limit_up, "limit_down": limit_down,
            }
        except Exception:  # noqa: BLE001
            return None
        finally:
            try:
                client.close()
            except Exception:  # noqa: BLE001
                pass

    # ── 单轮 ────────────────────────────────────────────────────────

    def build_once(self) -> dict[str, Any] | None:
        cfg = self._config_loader()
        if not cfg.enabled:
            return None
        history = (self._history_loader or self._default_history)(cfg.index_symbol)
        live = (self._index_snapshot_fetcher or self._default_index_snapshot)(cfg.index_symbol)
        if live is None:
            with self._lock:
                self.counters["skipped_no_live"] += 1
            return None
        forming = forming_inputs(
            history.get("closes") or [], history.get("volumes") or [],
            live.get("close"), live.get("volume"), cfg.window,
        )
        state = classify_regime(forming["ret"], forming["vol"], forming["vratio"])
        breadth = (self._breadth_fetcher or self._default_breadth)()
        payload = {
            "state": state,
            "index": cfg.index_symbol,
            "window": cfg.window,
            "ret": forming["ret"],
            "vol": forming["vol"],
            "vratio": forming["vratio"],
            "inputs_ok": forming["ok"],
            "notes": forming["notes"],
            "breadth": breadth,
            "position_hint": POSITION_BY_STATE.get(state),
            "ts": time.time(),
            "generated_at": _now().isoformat(),
        }
        (self._publisher or self._default_publish)(payload)
        with self._lock:
            self.counters["published"] += 1
            self.counters["last_state"] = state
            self.counters["last_ts"] = payload["ts"]
        return payload

    def _default_publish(self, payload: dict[str, Any]) -> None:
        """快照（Hash）+ 总线事件（best-effort；失败计数不抛出）。"""
        try:
            client = _main_redis()
            client.hset(SNAPSHOT_KEY, mapping={k: json.dumps(v, ensure_ascii=False, default=str)
                                               if isinstance(v, (dict, list)) else str(v)
                                               for k, v in payload.items()})
            event = build_event(
                type="regime",
                market="CN",
                level="warn" if payload["state"] == "bear" else "info",
                targets=[payload["index"]],
                payload={
                    "state": payload["state"],
                    "ret": payload["ret"],
                    "vol": payload["vol"],
                    "vratio": payload["vratio"],
                    "notes": payload["notes"],
                    "breadth": payload["breadth"],
                },
                source="realtime_regime",
            )
            publish_event(client, event)
            client.expire(SNAPSHOT_KEY, 86400 * 3)
            client.close()
        except Exception as exc:  # noqa: BLE001
            with self._lock:
                self.counters["errors"] += 1
                self.counters["last_error"] = f"{type(exc).__name__}: {exc}"
            logger.warning("[regime] 发布失败: %s", exc)

    # ── 循环 ────────────────────────────────────────────────────────

    async def run_forever(self) -> None:
        import asyncio

        logger.info("[regime] service loop started")
        while not self._stop.is_set():
            cfg = self._config_loader()
            try:
                if cfg.enabled:
                    await asyncio.to_thread(self.build_once)
                    with self._lock:
                        self.counters["cycles"] += 1
            except Exception as exc:  # noqa: BLE001
                with self._lock:
                    self.counters["errors"] += 1
                    self.counters["last_error"] = f"{type(exc).__name__}: {exc}"
                logger.warning("[regime] 周期失败: %s", exc)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=cfg.cadence_s)
            except asyncio.TimeoutError:
                pass

    def start(self) -> None:
        import asyncio

        if self._task is None or self._task.done():
            self._stop = asyncio.Event()
            self._task = asyncio.get_running_loop().create_task(
                self.run_forever(), name="realtime-regime"
            )

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            try:
                import asyncio

                await asyncio.wait_for(self._task, timeout=3)
            except Exception:  # noqa: BLE001
                self._task.cancel()

    def status(self) -> dict[str, Any]:
        cfg = self._config_loader()
        with self._lock:
            return {
                "enabled": cfg.enabled,
                "cadence_s": cfg.cadence_s,
                "index_symbol": cfg.index_symbol,
                "window": cfg.window,
                "loop_alive": bool(self._task and not self._task.done()),
                **dict(self.counters),
            }


_default: RealtimeRegimeService | None = None


def default_service() -> RealtimeRegimeService:
    global _default
    if _default is None:
        _default = RealtimeRegimeService()
    return _default
