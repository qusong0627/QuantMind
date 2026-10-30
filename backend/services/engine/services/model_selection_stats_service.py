"""截面选股 · 单模型 TopK 历史统计服务。

读 ``pred.parquet`` 的 ``split='test'`` 截面排名，按周期换股 / 每日滚动两种方式
做纸面组合模拟。价格一律取 QuantDB ``daily_forward``（前复权）收盘价；
不产生任何订单/委托/成交语义。

口径与淘汰判据见 ``docs/截面选股_Top50历史统计_细案_20261021.md``。
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from collections import OrderedDict
from collections.abc import Mapping
from datetime import date, datetime
from pathlib import Path
from typing import Any

import duckdb
import pandas as pd

from backend.services.engine.data_platform.quantdb_hub import QuantDBDataHub
from backend.services.engine.inference.model_backfill import resolve_pred_candidates
from backend.services.engine.inference.selection_stats import simulate_selection
from backend.shared import model_registry as model_registry_module
from backend.shared.stock_utils import StockCodeUtil

logger = logging.getLogger(__name__)

BENCHMARK_SYMBOL = "000300.SH"
# CUSTOM（/data/quantcustom）是 A 股筛选保留因子集，价格与基准同源 A 股日线，
# 选股统计口径与 CN 完全一致；2026-10-30 起纳入支持范围（默认模型即为 CUSTOM）。
SUPPORTED_MARKETS: frozenset[str] = frozenset({"CN", "CUSTOM"})
DEFAULT_HOLD_DAYS = 10
MAX_TOP_K = 50
MIN_TOTAL_SIGNAL_DAYS = 5
SHORT_WINDOW_SIGNAL_DAYS = 20
MIN_PRICE_COVERAGE_PCT = 90.0
CACHE_TTL_SECONDS = 300
CACHE_MAX_ENTRIES = 8


class SelectionStatsError(Exception):
    """带机读 code 的业务错误（路由层映射为 HTTP detail）。"""

    def __init__(self, code: str, message: str, status_code: int = 422):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


_CACHE: OrderedDict[tuple, tuple[float, dict[str, Any]]] = OrderedDict()
_CACHE_LOCK = threading.Lock()


def _iso(value: date | None) -> str | None:
    return value.isoformat() if value else None


def _as_date(value: Any) -> date:
    # datetime 是 date 的子类：必须先判 datetime，否则 pandas/DuckDB 返回的
    # datetime 会被原样放行，字典键变成 datetime、用 date 查永远 miss
    # （2026-10-21 实测：基准 415 条全 miss，超额恒 null）。
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return pd.Timestamp(value).date()


class ModelSelectionStatsService:
    """单模型 TopK 历史统计（进程内 LRU 缓存单位资金结果）。"""

    def __init__(self, *, hub: Any | None = None, resolver: Any | None = None):
        self._hub = hub or QuantDBDataHub()
        self._resolver = resolver or model_registry_module.model_registry_service

    # ── 对外入口 ──────────────────────────────────────────────────────
    async def compute(
        self,
        *,
        tenant_id: str,
        user_id: str,
        model_id: str,
        start: date | None = None,
        end: date | None = None,
        top_k: int = MAX_TOP_K,
        hold_days: int | None = None,
        capital: float = 10_000_000.0,
    ) -> dict[str, Any]:
        self._validate(
            top_k=top_k, hold_days=hold_days, capital=capital, start=start, end=end
        )

        resolved = await self._resolver.resolve_effective_model(
            tenant_id=tenant_id,
            user_id=user_id,
            model_id=str(model_id).strip(),
        )
        if resolved.fallback_used and resolved.model_source in {
            "user_default",
            "system_fallback",
        }:
            raise SelectionStatsError(
                "model_unavailable", f"模型不可用或未就绪: {model_id}", status_code=404
            )
        if not resolved.storage_path:
            raise SelectionStatsError(
                "model_unavailable", f"模型路径不可用: {model_id}", status_code=404
            )

        storage_path = str(resolved.storage_path)
        metadata = self._read_metadata(storage_path)
        market = str(metadata.get("market") or "CN").upper()
        if market not in SUPPORTED_MARKETS:
            raise SelectionStatsError(
                "unsupported_market",
                "当前仅支持 A 股（含自定义 A 股因子集）模型的选股统计"
                f"（模型市场: {market}）",
            )

        default_horizon = self._metadata_int(
            metadata, ("target_horizon_days", "horizon_days"), DEFAULT_HOLD_DAYS, 1, 60
        )
        hold = hold_days if hold_days is not None else default_horizon
        lag = self._metadata_int(metadata, ("execution_lag_days",), 1, 0, 10)

        pred_path = self._pred_path(storage_path)
        if pred_path is None:
            raise SelectionStatsError(
                "pred_unavailable", "模型未找到 pred.parquet，无法统计历史选股"
            )

        cache_key = (
            str(resolved.effective_model_id or model_id),
            str(pred_path),
            pred_path.stat().st_mtime_ns,
            _iso(start),
            _iso(end),
            int(top_k),
            int(hold),
            int(lag),
        )
        unit = self._cache_get(cache_key)
        if unit is None:
            unit = await asyncio.to_thread(
                self._compute_unit,
                pred_path=pred_path,
                model_id=str(resolved.effective_model_id or model_id),
                model_name=str(
                    metadata.get("display_name")
                    or metadata.get("model_name")
                    or metadata.get("name")
                    or model_id
                ),
                market=market,
                default_horizon=default_horizon,
                hold_days=int(hold),
                execution_lag=lag,
                top_k=int(top_k),
                start=start,
                end=end,
            )
            self._cache_set(cache_key, unit)

        return self._present(unit, capital=float(capital), requested_hold=hold)

    # ── 校验 ──────────────────────────────────────────────────────────
    @staticmethod
    def _validate(
        *,
        top_k: int,
        hold_days: int | None,
        capital: float,
        start: date | None,
        end: date | None,
    ) -> None:
        if (
            not isinstance(top_k, int)
            or isinstance(top_k, bool)
            or not 1 <= top_k <= MAX_TOP_K
        ):
            raise SelectionStatsError("invalid_params", f"top_k 必须在 1..{MAX_TOP_K}")
        if hold_days is not None:
            if (
                not isinstance(hold_days, int)
                or isinstance(hold_days, bool)
                or not 1 <= hold_days <= 60
            ):
                raise SelectionStatsError("invalid_params", "hold_days 必须在 1..60")
        if capital is None or not (0 < float(capital) <= 1e12):
            raise SelectionStatsError(
                "invalid_params", "capital 必须大于 0 且不超过 1e12"
            )
        if start and end and start > end:
            raise SelectionStatsError("invalid_params", "start 不能晚于 end")

    # ── 元数据 ────────────────────────────────────────────────────────
    @staticmethod
    def _read_metadata(storage_path: str) -> dict[str, Any]:
        path = Path(storage_path) / "metadata.json"
        if not path.is_file():
            return {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("读取模型元数据失败 %s: %s", path, exc)
            return {}

    @staticmethod
    def _metadata_int(
        metadata: Mapping[str, Any],
        keys: tuple[str, ...],
        default: int,
        low: int,
        high: int,
    ) -> int:
        for key in keys:
            value = metadata.get(key)
            if value is None:
                continue
            try:
                number = int(value)
            except (TypeError, ValueError):
                continue
            return max(low, min(high, number))
        return default

    @staticmethod
    def _pred_path(storage_path: str) -> Path | None:
        for candidate in resolve_pred_candidates(storage_path):
            if candidate.is_file():
                return candidate
        return None

    # ── 计算（工作线程） ──────────────────────────────────────────────
    def _compute_unit(
        self,
        *,
        pred_path: Path,
        model_id: str,
        model_name: str,
        market: str,
        default_horizon: int,
        hold_days: int,
        execution_lag: int,
        top_k: int,
        start: date | None,
        end: date | None,
    ) -> dict[str, Any]:
        first_day, last_day, total_signal_days = self._read_test_bounds(pred_path)
        if total_signal_days < MIN_TOTAL_SIGNAL_DAYS:
            raise SelectionStatsError(
                "insufficient_signal_days",
                f"测试集有效信号日仅 {total_signal_days} 天，至少需要 {MIN_TOTAL_SIGNAL_DAYS} 天",
            )

        warnings: list[str] = []
        effective_start = first_day
        effective_end = last_day
        if start is not None:
            if start < first_day:
                warnings.append("effective_start_clamped")
            effective_start = max(start, first_day)
        if end is not None:
            if end > last_day:
                warnings.append("effective_end_clamped")
            effective_end = min(end, last_day)
        if effective_start > effective_end:
            raise SelectionStatsError(
                "insufficient_signal_days", "请求区间内没有可用的测试集信号日"
            )

        picks_by_signal = self._load_top_picks(
            pred_path,
            effective_start,
            effective_end,
            top_k,
        )
        if not picks_by_signal:
            raise SelectionStatsError(
                "insufficient_signal_days", "请求区间内没有有效的截面评分"
            )
        if len(picks_by_signal) < SHORT_WINDOW_SIGNAL_DAYS:
            warnings.append("short_window")

        symbol_map = self._suffix_symbols(picks_by_signal)
        if not symbol_map:
            raise SelectionStatsError(
                "quote_source_unavailable", "入选标的无法映射到行情代码"
            )

        price_frame = self._hub.fetch_daily_close_batch(
            list(symbol_map.values()),
            min(picks_by_signal),
            date.today(),
            adjust="qfq",
        )
        calendar, closes = self._pivot_closes(price_frame, symbol_map)
        if not calendar or not closes:
            raise SelectionStatsError(
                "quote_source_unavailable", "QuantDB 前复权收盘价不可用"
            )

        benchmark = self._load_benchmark(min(picks_by_signal), calendar[-1])

        result = simulate_selection(
            calendar=calendar,
            picks_by_signal=picks_by_signal,
            closes=closes,
            hold_days=hold_days,
            execution_lag_days=execution_lag,
            benchmark_closes=benchmark or None,
            top_k=top_k,
        )

        coverage = result["summary"]["price_coverage_pct"]
        if coverage is None or coverage < MIN_PRICE_COVERAGE_PCT:
            raise SelectionStatsError(
                "quote_source_unavailable",
                f"入选标的收盘价覆盖率仅 {coverage if coverage is not None else 0:.2f}%"
                f"（低于 {MIN_PRICE_COVERAGE_PCT:.0f}%），拒绝用不可信价格统计",
            )

        rows = result["cohorts"]
        if not rows:
            raise SelectionStatsError(
                "insufficient_signal_days", "请求区间内无法形成任何持有期"
            )

        engine_warnings = [code for code in result["warnings"] if code not in warnings]
        all_warnings = warnings + engine_warnings
        first_entry = rows[0]["entry_date"]
        last_mark = max(row["mark_date"] for row in rows)
        trading_days = sum(
            1 for day in calendar if first_entry <= day.isoformat() <= last_mark
        )

        return {
            "model_id": model_id,
            "model_name": model_name,
            "market": market,
            "split": "test",
            "requested": {
                "start": _iso(start),
                "end": _iso(end),
                "top_k": top_k,
                "hold_days": hold_days,
            },
            "effective": {
                "start": effective_start.isoformat(),
                "end": effective_end.isoformat(),
                "first_signal_day": min(picks_by_signal).isoformat(),
                "last_signal_day": max(picks_by_signal).isoformat(),
                "first_entry_day": first_entry,
                "last_exit_day": last_mark,
                "trading_days": trading_days,
                "signal_days": len(picks_by_signal),
            },
            "params": {
                "horizon_days": default_horizon,
                "execution_lag_days": execution_lag,
                "entry_convention": "close",
                "exit_convention": "close",
                "price_adjust": "qfq",
                "fee_convention": "none",
                "tradability": "not_modeled",
                "benchmark": BENCHMARK_SYMBOL,
                "cohort_mode": "daily" if hold_days == 1 else "period",
            },
            "summary": result["summary"],
            "months": result["months"],
            "cohorts": rows,
            "points": result["points"],
            "warnings": all_warnings,
            "quality": result["quality"],
        }

    # ── DuckDB 读取 ───────────────────────────────────────────────────
    @staticmethod
    def _read_test_bounds(pred_path: Path) -> tuple[date, date, int]:
        escaped = str(pred_path).replace("'", "''")
        sql = f"""
            SELECT MIN(CAST(trade_date AS DATE)) AS first_day,
                   MAX(CAST(trade_date AS DATE)) AS last_day,
                   COUNT(DISTINCT CAST(trade_date AS DATE)) AS day_count
            FROM read_parquet('{escaped}')
            WHERE split = 'test' AND pred IS NOT NULL
        """
        con = duckdb.connect()
        try:
            row = con.execute(sql).fetchone()
        except Exception as exc:  # noqa: BLE001 - 统一映射为数据不可用
            logger.warning("读取 pred.parquet 测试区间失败 %s: %s", pred_path, exc)
            raise SelectionStatsError(
                "split_unavailable", "pred.parquet 缺少可用的 test 分片"
            ) from exc
        finally:
            con.close()

        if not row or row[0] is None or row[1] is None:
            raise SelectionStatsError(
                "split_unavailable", "pred.parquet 中没有 split='test' 的有效评分"
            )
        return _as_date(row[0]), _as_date(row[1]), int(row[2] or 0)

    @staticmethod
    def _load_top_picks(
        pred_path: Path,
        start: date,
        end: date,
        top_k: int,
    ) -> dict[date, list[str]]:
        escaped = str(pred_path).replace("'", "''")
        sql = f"""
            WITH ranked AS (
                SELECT symbol,
                       CAST(trade_date AS DATE) AS trade_day,
                       pred,
                       ROW_NUMBER() OVER (
                           PARTITION BY CAST(trade_date AS DATE)
                           ORDER BY pred DESC, symbol
                       ) AS rn
                FROM read_parquet('{escaped}')
                WHERE split = 'test'
                  AND pred IS NOT NULL
                  AND CAST(trade_date AS DATE) BETWEEN ? AND ?
            )
            SELECT symbol, trade_day
            FROM ranked
            WHERE rn <= ?
            ORDER BY trade_day, rn
        """
        con = duckdb.connect()
        try:
            rows = con.execute(sql, [start, end, int(top_k)]).fetchall()
        except Exception as exc:  # noqa: BLE001
            logger.warning("抽取 TopK 失败 %s: %s", pred_path, exc)
            raise SelectionStatsError(
                "split_unavailable", "pred.parquet 读取失败"
            ) from exc
        finally:
            con.close()

        picks: dict[date, list[str]] = {}
        for symbol, trade_day in rows:
            day = _as_date(trade_day)
            picks.setdefault(day, []).append(str(symbol))
        return picks

    # ── 行情 ──────────────────────────────────────────────────────────
    @staticmethod
    def _suffix_symbols(
        picks_by_signal: Mapping[date, list[str]],
    ) -> dict[str, str]:
        mapping: dict[str, str] = {}
        for symbols in picks_by_signal.values():
            for symbol in symbols:
                if symbol in mapping:
                    continue
                try:
                    suffix = StockCodeUtil.to_suffix(symbol)
                except Exception:  # noqa: BLE001 - 单票映射失败不拖垮全量
                    suffix = ""
                if suffix:
                    mapping[symbol] = suffix
        return mapping

    @staticmethod
    def _pivot_closes(
        price_frame: pd.DataFrame,
        symbol_map: Mapping[str, str],
    ) -> tuple[list[date], dict[str, dict[date, float]]]:
        if (
            price_frame is None
            or price_frame.empty
            or "close" not in price_frame.columns
        ):
            return [], {}

        frame = price_frame[["symbol", "trade_date", "close"]].copy()
        frame["trade_date"] = pd.to_datetime(frame["trade_date"]).dt.date
        frame["close"] = pd.to_numeric(frame["close"], errors="coerce")
        frame = frame.dropna(subset=["close"])
        frame = frame[frame["close"] > 0]
        if frame.empty:
            return [], {}

        wide = frame.pivot_table(
            index="trade_date", columns="symbol", values="close", aggfunc="last"
        )
        calendar = sorted(_as_date(day) for day in wide.index)
        suffix_to_prefix = {value: key for key, value in symbol_map.items()}

        closes: dict[str, dict[date, float]] = {}
        for suffix in wide.columns:
            prefix = suffix_to_prefix.get(str(suffix))
            if not prefix:
                continue
            series = wide[suffix].dropna()
            closes[prefix] = {
                _as_date(day): float(value) for day, value in series.items()
            }
        return calendar, closes

    def _load_benchmark(self, start: date, end: date) -> dict[date, float]:
        try:
            frame = self._hub.fetch_index_kline(BENCHMARK_SYMBOL, start, end)
        except Exception as exc:  # noqa: BLE001 - 基准缺失只降级，不阻断
            logger.warning("读取基准 %s 失败: %s", BENCHMARK_SYMBOL, exc)
            return {}
        if frame is None or frame.empty or "close" not in frame.columns:
            return {}

        benchmark: dict[date, float] = {}
        for day, close in zip(frame["trade_date"], frame["close"], strict=True):
            try:
                value = float(close)
            except (TypeError, ValueError):
                continue
            if value > 0:
                benchmark[_as_date(day)] = value
        return benchmark

    # ── 展示（金额缩放） ──────────────────────────────────────────────
    @staticmethod
    def _present(
        unit: dict[str, Any],
        *,
        capital: float,
        requested_hold: int,
    ) -> dict[str, Any]:
        summary = dict(unit["summary"])
        points = [dict(point) for point in unit["points"]]
        cohorts = [dict(row) for row in unit["cohorts"]]
        months = [dict(row) for row in unit["months"]]

        total_return = summary.get("total_return_pct")
        final_equity = (
            round(capital * (1.0 + total_return / 100.0), 2)
            if total_return is not None
            else None
        )
        summary["initial_capital"] = round(capital, 2)
        summary["final_equity"] = final_equity
        summary["profit_amount"] = (
            round(final_equity - capital, 2) if final_equity is not None else None
        )

        for point in points:
            if point.get("equity") is not None:
                point["equity"] = round(capital * point["equity"], 2)
        for row in cohorts:
            if row.get("equity") is not None:
                row["equity"] = round(capital * row["equity"], 2)
        for row in months:
            for key in ("start_equity", "end_equity", "profit_amount"):
                if row.get(key) is not None:
                    row[key] = round(capital * row[key], 2)

        requested = dict(unit["requested"])
        requested["capital"] = round(capital, 2)
        requested["hold_days"] = requested_hold

        return {
            "model_id": unit["model_id"],
            "model_name": unit["model_name"],
            "market": unit["market"],
            "split": unit["split"],
            "requested": requested,
            "effective": unit["effective"],
            "params": unit["params"],
            "summary": summary,
            "months": months,
            "cohorts": cohorts,
            "points": points,
            "warnings": unit["warnings"],
            "quality": unit["quality"],
        }

    # ── 缓存 ──────────────────────────────────────────────────────────
    @staticmethod
    def clear_cache() -> None:
        with _CACHE_LOCK:
            _CACHE.clear()

    @staticmethod
    def _cache_get(key: tuple) -> dict[str, Any] | None:
        now = time.monotonic()
        with _CACHE_LOCK:
            entry = _CACHE.get(key)
            if entry is None:
                return None
            stored_at, value = entry
            if now - stored_at > CACHE_TTL_SECONDS:
                _CACHE.pop(key, None)
                return None
            _CACHE.move_to_end(key)
            return value

    @staticmethod
    def _cache_set(key: tuple, value: dict[str, Any]) -> None:
        with _CACHE_LOCK:
            _CACHE[key] = (time.monotonic(), value)
            _CACHE.move_to_end(key)
            while len(_CACHE) > CACHE_MAX_ENTRIES:
                _CACHE.popitem(last=False)
