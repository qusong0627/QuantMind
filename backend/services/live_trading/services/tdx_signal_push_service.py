"""
TDX Signal Push Service - 把模型推理选股推送到通达信

读取 engine_signal_scores 最新推理的 Top N 股票（按 fusion_score），
推送到通达信：自定义板块 + 预警信号（带买入价/排名/理由）+ 界面消息。

复用入口：
  - script_runner.py（推理完成后自动推送）
  - trade /tdx/push-signals API（前端手动重推）
"""
import asyncio
import logging
import os
from datetime import datetime
from typing import Any

from backend.shared.database_manager_v2 import get_session
from backend.shared.stock_utils import StockCodeUtil
from backend.services.live_trading.services.tdx_push_service import (
    TdxPushError,
    tdx_pusher,
)
from backend.services.trade_shared.utils.stock_lookup import lookup_symbol_name

logger = logging.getLogger(__name__)

DEFAULT_TOP_N = 20
# 融合分数有效数字截断，避免推送超长小数
_SCORE_DIGITS = 4
_QUANTDB_NAME_DIR = "/data/quantdb/2_base_sector/instrument_detail"


def _quantdb_name_table() -> str | None:
    """SDK 新版落盘 instrument_list.parquet，旧版 instrument_detail.parquet。"""
    for name in ("instrument_list.parquet", "instrument_detail.parquet"):
        p = os.path.join(_QUANTDB_NAME_DIR, name)
        if os.path.exists(p):
            return p
    return None


def _batch_lookup_names(suffixes: list[str]) -> dict[str, str]:
    """股票名: stocks_index.json 优先, QuantDB instrument_detail 兜底 (批量)。

    stocks_index.json 的 key 是前缀格式 (SZ301599)，suffix 需转成 SH600519 再查。
    """
    result: dict[str, str] = {}
    for suffix in suffixes:
        suffix = str(suffix or "").strip()
        if not suffix:
            continue
        name = ""
        # suffix 格式 600519.SH → 前缀格式 SH600519
        if "." in suffix:
            code, market = suffix.split(".", 1)
            prefix = f"{market}{code}"
            name = lookup_symbol_name(prefix) or ""
        name = name or lookup_symbol_name(suffix) or ""
        if name:
            result[suffix] = name
    missing = [s for s in suffixes if s not in result]
    if not missing:
        return result
    table_path = _quantdb_name_table()
    if table_path is None:
        return result
    try:
        import duckdb

        con = duckdb.connect(":memory:")
        rows = con.execute(
            "SELECT Symbol, Name FROM read_parquet(?) WHERE Symbol IN ("
            + ",".join("?" for _ in missing)
            + ")",
            [table_path, *missing],
        ).fetchall()
        con.close()
        for symbol, name in rows:
            if name:
                result[str(symbol)] = str(name).strip()
    except Exception:
        pass
    return result


class TdxSignalPushService:
    """把推理 Top N 选股推送到通达信（板块+预警+消息）。"""

    @staticmethod
    def _pick_stocks(signals: list[dict[str, Any]], top_n: int) -> list[dict[str, Any]]:
        scored = [
            s
            for s in signals
            if isinstance(s.get("fusion_score"), (int, float))
        ]
        scored.sort(key=lambda s: float(s["fusion_score"]), reverse=True)
        return scored[: max(1, top_n)]

    async def load_top_stocks(
        self,
        *,
        tenant_id: str,
        user_id: str,
        run_id: str | None = None,
        top_n: int = DEFAULT_TOP_N,
    ) -> tuple[str | None, list[dict[str, Any]]]:
        """读取推理 Top N 信号。返回 (run_id, stocks)。

        run_id 为空时取该用户最新推理。stocks 元素包含
        symbol(600519.SH)/name/score/rank/close/side/limit_up/limit_down/is_st。
        """
        from sqlalchemy import text

        async with get_session(read_only=True) as db:
            if run_id:
                selected_run = run_id
            else:
                row = (
                    (
                        await db.execute(
                            text(
                                """
                                SELECT run_id FROM qm_model_inference_runs
                                WHERE tenant_id = :tenant_id
                                  AND user_id = :user_id
                                  AND status = 'completed'
                                ORDER BY created_at DESC LIMIT 1
                                """
                            ),
                            {"tenant_id": tenant_id, "user_id": user_id},
                        )
                    )
                    .mappings()
                    .first()
                )
                selected_run = str((row or {}).get("run_id") or "").strip() or None

            if not selected_run:
                return None, []

            rows = (
                (
                    await db.execute(
                        text(
                            """
                            SELECT symbol, fusion_score, signal_side
                            FROM engine_signal_scores
                            WHERE run_id = :run_id
                              AND tenant_id = :tenant_id
                              AND user_id = :user_id
                              AND (universe_tag IS NULL OR universe_tag = 'CN')
                            ORDER BY fusion_score DESC NULLS LAST, symbol ASC
                            """
                        ),
                        {
                            "run_id": selected_run,
                            "tenant_id": tenant_id,
                            "user_id": user_id,
                        },
                    )
                )
                .mappings()
                .all()
            )
            top = self._pick_stocks(
                [dict(r) for r in rows],
                top_n,
            )
            return selected_run, top

    async def build_push_payload(
        self,
        *,
        tenant_id: str,
        user_id: str,
        run_id: str | None = None,
        top_n: int = DEFAULT_TOP_N,
        block_code: str = "",
        block_name: str = "QuantMind今日选股",
        push_warnings: bool = True,
        push_message: bool = True,
    ) -> dict[str, Any]:
        """组装并执行完整推送：板块 + 预警 + 消息。

        返回: {success, run_id, prediction_trade_date, pushed, skipped, warnings,
               message, stocks:[{rank,symbol,name,score,close,side}]}
        """
        if not tdx_pusher.enabled:
            return {
                "success": False,
                "error": "TDX_BRIDGE_URL/TOKEN 未配置",
                "stocks": [],
            }

        selected_run, top = await self.load_top_stocks(
            tenant_id=tenant_id,
            user_id=user_id,
            run_id=run_id,
            top_n=top_n,
        )
        if not selected_run or not top:
            return {
                "success": False,
                "error": "没有可推送的推理信号（检查 run_id 或最新推理是否完成）",
                "run_id": selected_run,
                "stocks": [],
            }

        # 批量取最近收盘价（板块展示 + 预警参考价）
        symbols = [str(s["symbol"]) for s in top]
        price_map = await asyncio.to_thread(
            self._batch_last_close,
            symbols,
        )

        # 剔除 ST/停牌/无收盘价
        name_map = _batch_lookup_names([StockCodeUtil.to_suffix(str(s["symbol"])) for s in top])
        picked: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []
        for s in top:
            symbol = str(s["symbol"]).strip()
            suffix = StockCodeUtil.to_suffix(symbol)
            name = name_map.get(suffix, "")
            close = price_map.get(symbol, 0.0)
            if close <= 0:
                skipped.append({"symbol": suffix or symbol, "reason": "无收盘价"})
                continue
            if name and ("ST" in name.upper() or name.startswith("*")):
                skipped.append({"symbol": suffix or symbol, "name": name, "reason": "ST"})
                continue
            picked.append(
                {
                    "symbol": suffix,
                    "name": name,
                    "score": round(float(s["fusion_score"]), _SCORE_DIGITS),
                    "close": round(close, 2),
                    "side": str(s.get("signal_side") or "BUY").upper(),
                }
            )
        if not picked:
            return {
                "success": False,
                "error": "Top N 全部被过滤（ST/停牌/无行情）",
                "run_id": selected_run,
                "skipped": skipped,
                "stocks": [],
            }

        for idx, s in enumerate(picked, start=1):
            s["rank"] = idx

        results: dict[str, Any] = {}
        # 1) 板块
        try:
            block_resp = await tdx_pusher.push_signals_to_block(
                [s["symbol"] for s in picked],
                block_code=block_code,
                block_name=block_name,
                show=True,
            )
            results["block"] = {"success": True, "result": block_resp}
        except TdxPushError as exc:
            logger.warning("[TdxSignalPush] 板块推送失败: %s", exc)
            results["block"] = {"success": False, "error": str(exc)}

        # 2) 预警（带分数排名与理由，通达信内可双击闪电下单）
        warnings = 0
        if push_warnings:
            try:
                warn_signals = [
                    {
                        "symbol": s["symbol"],
                        "side": "buy" if s["side"] != "SELL" else "sell",
                        "price": s["close"],
                        "close": s["close"],
                        "volume": 0,
                        "reason": f"Top{s['rank']} {s['name']} 分{s['score']}",
                    }
                    for s in picked
                ]
                warn_resp = await tdx_pusher.push_warnings(warn_signals)
                warnings = len(picked)
                results["warnings"] = {"success": True, "result": warn_resp}
            except TdxPushError as exc:
                logger.warning("[TdxSignalPush] 预警推送失败: %s", exc)
                results["warnings"] = {"success": False, "error": str(exc)}

        # 3) 消息
        if push_message:
            try:
                summary = "|".join(
                    f"{s['rank']}.{s['name'] or s['symbol']}:{s['score']}"
                    for s in picked[:10]
                )
                await tdx_pusher.push_message(
                    f"MSG,QuantMind 今日选股 {len(picked)} 只|{summary}"
                )
                results["message"] = {"success": True}
            except TdxPushError as exc:
                logger.warning("[TdxSignalPush] 消息推送失败: %s", exc)
                results["message"] = {"success": False, "error": str(exc)}

        logger.info(
            "[TdxSignalPush] 推送完成 run=%s pushed=%d skipped=%d warnings=%d",
            selected_run,
            len(picked),
            len(skipped),
            warnings,
        )
        return {
            "success": True,
            "run_id": selected_run,
            "pushed": len(picked),
            "skipped": skipped,
            "warnings": warnings,
            "stocks": picked,
            "results": results,
            "pushed_at": datetime.now().isoformat(timespec="seconds"),
        }

    @staticmethod
    def _batch_last_close(symbols: list[str]) -> dict[str, float]:
        """批量从 QuantDB 取最近交易日收盘价（与模拟撮合同源）。"""
        result: dict[str, float] = {}
        if not symbols:
            return result
        try:
            from backend.services.simulation.services.local_market_data import (
                get_local_market_data,
            )

            # 进程内共享实例：复用交易日枚举与按日行情缓存
            market_data = get_local_market_data()
            latest_date = market_data.latest_trade_date()
            if latest_date is None:
                return result
            for symbol in symbols:
                suffix = StockCodeUtil.to_suffix(symbol)
                if not suffix:
                    continue
                bar = market_data.get_bar(suffix, latest_date)
                if bar is not None and bar.close > 0:
                    result[symbol] = float(bar.close)
        except Exception as exc:
            logger.warning("[TdxSignalPush] QuantDB 收盘价读取失败: %s", exc)
        return result


tdx_signal_pusher = TdxSignalPushService()
