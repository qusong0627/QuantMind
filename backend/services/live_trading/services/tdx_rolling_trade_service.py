"""
TDX Rolling Trade Service - 按分数滚动买卖并推送（三档执行模式）

规则（每日推理完成后执行一次；阈值默认走截面分位 rank_pct）：
  1. 个股 rank_pct ≥ 1 - RANK_PCT_THRESHOLD（默认前 10%）→ 进入买入候选
  2. 已持仓且 rank_pct ≤ RANK_PCT_THRESHOLD（默认后 10%）→ 卖出；
     最新推理无该股分位 → 跳过（数据缺失不是卖出信号）
  3. 其余持仓持有不动
  4. 上证指数（000001.SH）收盘 < MA20 → 只卖不买（强制过滤）

  旧 score 模式（显式配置才启用）：fusion_score > 2.2 买 / ≤ 2.2 卖。
  ⚠️ 绝对分依赖模型量纲，当前模型分数约 [-0.33, +0.17]，2.2 只在 0-10 分时代成立。

执行模式（设置页可改，存 Redis，QuantDB 付费会员专属）：
  off   -> 仅推通达信预警（默认，半自动）
  tdx   -> 买卖信号生成真实委托推给通达信（TQ 收费账号 Value=2 直接提交免确认；
            普通账号返回 Value=1 由客户端确认后成交）
  paper -> 买卖信号在本地模拟盘账户直接成交（本地撮合，免确认、零风险）
"""
import asyncio
import logging
import os
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import text

from backend.services.trade_shared.redis_client import redis_client as trade_redis
from backend.shared.database_manager_v2 import get_session
from backend.shared.stock_utils import StockCodeUtil
from backend.services.live_trading.services.tdx_push_service import (
    TdxPushError,
    tdx_pusher,
)
from backend.services.live_trading.services.trading_session import (
    is_trading_time,
    now_shanghai,
    session_refusal_of,
    session_refusal_row,
    split_session_refusals,
)

logger = logging.getLogger(__name__)

# 默认分数阈值（**仅 score 模式**）：fusion_score 高于该阈值即买入。
# ⚠️ 绝对分阈值依赖模型量纲：2026-11-20 实测当前模型 fusion_score 在
# [-0.33, +0.17]，2.2 恒大于所有分数 → 买入永不触发、全部持仓触发卖出。
# 新配置默认走 rank_pct（截面分位），跨模型量纲一致；score 模式仅为旧配置兼容。
DEFAULT_SCORE_THRESHOLD = 2.2
# 阈值口径：rank_pct=截面分位（推荐）/ score=绝对分（旧）。
THRESHOLD_MODES = ("rank_pct", "score")
DEFAULT_THRESHOLD_MODE = "rank_pct"
# 分位阈值：持仓分位 ≤ 该值 → 卖；分位 ≥ 1-该值 → 买（默认后/前 10%）。
DEFAULT_RANK_PCT_THRESHOLD = 0.10
# 默认每只股票固定买入金额（元）
DEFAULT_FIXED_BUY_AMOUNT = 10000.0
# 买入手数向下取整到 100 股
LOT_SIZE = 100
# 上证指数代码（QuantDB index_daily）
INDEX_SYMBOL = "000001.SH"
MA_WINDOW = 20
_MAX_SELL_WARNINGS = 20
_MAX_BUY_WARNINGS = 20

_ROLLING_CONFIG_KEY = "tdx:rolling_config:{tenant_id}:{user_id}"

# 执行模式: off=仅预警 / tdx=通达信下单(收费TQ账号直接提交) / paper=模拟盘直接下单(免确认)
EXECUTE_MODES = ("off", "tdx", "paper")
DEFAULT_EXECUTE_MODE = "off"


def _normalize_execute_mode(value: Any) -> str:
    """校验执行模式，非法值回退 off。"""
    mode = str(value or "").strip().lower()
    return mode if mode in EXECUTE_MODES else DEFAULT_EXECUTE_MODE


@dataclass(frozen=True)
class RollingSettings:
    """滚动买卖配置（阈值口径 + 金额 + 执行模式 + 信号模型）。"""

    threshold_mode: str = DEFAULT_THRESHOLD_MODE
    rank_pct_threshold: float = DEFAULT_RANK_PCT_THRESHOLD
    score_threshold: float = DEFAULT_SCORE_THRESHOLD
    fixed_buy_amount: float = DEFAULT_FIXED_BUY_AMOUNT
    execute_mode: str = DEFAULT_EXECUTE_MODE
    signal_model_id: str = ""


@dataclass(frozen=True)
class SignalModel:
    """策略提醒/滚动买卖的信号模型（显式 > 策略绑定 > 默认模型）。"""

    model_id: str = ""
    display_name: str = ""
    source: str = "unresolved"


def _normalize_threshold_mode(value: Any, default: str = DEFAULT_THRESHOLD_MODE) -> str:
    mode = str(value or "").strip().lower()
    return mode if mode in THRESHOLD_MODES else default


def load_rolling_settings(
    tenant_id: str,
    user_id: str,
) -> RollingSettings:
    """读取滚动买卖完整配置（Redis 优先，env/默认值兜底）。

    兼容迁移：旧配置只有 ``score_threshold``、没有 ``threshold_mode`` 时，按
    **rank_pct** 读取（2026-11-20：旧绝对阈值 2.2 对当前模型恒真，必须迁移）。
    显式保存 ``threshold_mode=score`` 的配置仍按旧口径执行。
    """
    score_threshold = _env_float("TDX_ROLLING_SCORE_THRESHOLD", DEFAULT_SCORE_THRESHOLD)
    rank_pct_threshold = _env_float(
        "TDX_ROLLING_RANK_PCT_THRESHOLD", DEFAULT_RANK_PCT_THRESHOLD
    )
    threshold_mode = _normalize_threshold_mode(
        os.getenv("TDX_ROLLING_THRESHOLD_MODE", "")
    )
    amount = _env_float("TDX_ROLLING_FIXED_BUY_AMOUNT", DEFAULT_FIXED_BUY_AMOUNT)
    raw_mode = os.getenv("TDX_ROLLING_EXECUTE_MODE", "").strip().lower()
    execute_mode = _normalize_execute_mode(raw_mode)
    if not raw_mode and _env_bool("TDX_ROLLING_AUTO_PLACE", False):
        # 旧环境变量兼容: TDX_ROLLING_AUTO_PLACE=true -> tdx
        execute_mode = "tdx"
    signal_model_id = ""
    try:
        from backend.services.trade_shared.redis_client import get_redis

        saved = get_redis().get(
            _ROLLING_CONFIG_KEY.format(tenant_id=tenant_id, user_id=user_id)
        )
        if isinstance(saved, dict):
            if "threshold_mode" in saved:
                threshold_mode = _normalize_threshold_mode(
                    saved.get("threshold_mode"), threshold_mode
                )
            else:
                # 旧配置迁移：绝对分阈值对当前量纲失效
                threshold_mode = DEFAULT_THRESHOLD_MODE
            r = saved.get("rank_pct_threshold")
            if isinstance(r, (int, float)) and 0 < float(r) < 0.5:
                rank_pct_threshold = float(r)
            t = saved.get("score_threshold")
            a = saved.get("fixed_buy_amount")
            m = _normalize_execute_mode(saved.get("execute_mode"))
            p = saved.get("auto_place")
            if isinstance(t, (int, float)) and 0 < float(t) <= 10:
                score_threshold = float(t)
            if isinstance(a, (int, float)) and float(a) > 0:
                amount = float(a)
            if m != DEFAULT_EXECUTE_MODE or saved.get("execute_mode") is not None:
                execute_mode = m
            elif isinstance(p, bool) and p:
                # 旧配置兼容: auto_place=true -> tdx
                execute_mode = "tdx"
            signal_model_id = str(saved.get("signal_model_id") or "").strip()
    except Exception as exc:
        logger.warning("[TdxRolling] 读取滚动配置失败，使用默认值: %s", exc)
    return RollingSettings(
        threshold_mode=threshold_mode,
        rank_pct_threshold=rank_pct_threshold,
        score_threshold=score_threshold,
        fixed_buy_amount=amount,
        execute_mode=execute_mode,
        signal_model_id=signal_model_id,
    )


def load_rolling_config(
    tenant_id: str,
    user_id: str,
) -> tuple[float, float, str]:
    """兼容包装：返回 (score_threshold, fixed_buy_amount, execute_mode)。

    新代码（策略提醒/滚动买卖）用 ``load_rolling_settings``；L2 实时等旧调用方
    只关心金额/执行模式，保留三元组避免破坏。
    """
    settings = load_rolling_settings(tenant_id, user_id)
    return settings.score_threshold, settings.fixed_buy_amount, settings.execute_mode


def save_rolling_config(
    tenant_id: str,
    user_id: str,
    *,
    score_threshold: float,
    fixed_buy_amount: float,
    execute_mode: str = DEFAULT_EXECUTE_MODE,
    threshold_mode: str | None = None,
    rank_pct_threshold: float | None = None,
    signal_model_id: str | None = None,
) -> None:
    """保存滚动买卖配置到 Redis（设置页）。

    同时写入旧字段 auto_place（execute_mode != off），兼容旧读取方。
    threshold_mode 缺省不写（读取端按旧配置迁移到 rank_pct）。
    """
    from backend.services.trade_shared.redis_client import get_redis

    mode = _normalize_execute_mode(execute_mode)
    payload: dict[str, Any] = {
        "score_threshold": float(score_threshold),
        "fixed_buy_amount": float(fixed_buy_amount),
        "execute_mode": mode,
        "auto_place": mode != DEFAULT_EXECUTE_MODE,
    }
    if threshold_mode is not None:
        payload["threshold_mode"] = _normalize_threshold_mode(threshold_mode)
    if rank_pct_threshold is not None:
        payload["rank_pct_threshold"] = float(rank_pct_threshold)
    if signal_model_id is not None:
        payload["signal_model_id"] = str(signal_model_id).strip()
    get_redis().set(
        _ROLLING_CONFIG_KEY.format(tenant_id=tenant_id, user_id=user_id),
        payload,
    )


async def resolve_signal_model(
    tenant_id: str,
    user_id: str,
    explicit_model_id: str = "",
) -> SignalModel:
    """解析策略提醒/滚动买卖使用的信号模型。

    优先级：显式配置 → 策略绑定（最近更新）→ 用户默认模型（CN）→ 未解析。
    「最近一次完成的推理 run」不再作为信号来源：多模型并行时它会随机漂移
    （2026-11-20：同一持仓在三个模型下 rank_pct 从 0.05 跳到 0.87）。
    """
    explicit = str(explicit_model_id or "").strip()
    async with get_session(read_only=True) as db:
        if explicit:
            row = (
                (
                    await db.execute(
                        text(
                            """
                            SELECT model_id,
                                   COALESCE(metadata_json->>'display_name', model_id) AS display_name
                            FROM qm_user_models
                            WHERE tenant_id = :tenant_id AND user_id = :user_id
                              AND model_id = :model_id AND status = 'ready'
                            LIMIT 1
                            """
                        ),
                        {
                            "tenant_id": tenant_id,
                            "user_id": user_id,
                            "model_id": explicit,
                        },
                    )
                )
                .mappings()
                .first()
            )
            if row:
                return SignalModel(
                    str(row["model_id"]), str(row["display_name"]), "explicit"
                )
            logger.warning(
                "[TdxRolling] 指定信号模型不可用，回退绑定/默认模型: %s", explicit
            )
        row = (
            (
                await db.execute(
                    text(
                        """
                        SELECT b.model_id,
                               COALESCE(m.metadata_json->>'display_name', b.model_id) AS display_name
                        FROM qm_strategy_model_bindings b
                        LEFT JOIN qm_user_models m
                          ON m.tenant_id = b.tenant_id
                         AND m.user_id = b.user_id
                         AND m.model_id = b.model_id
                        WHERE b.tenant_id = :tenant_id AND b.user_id = :user_id
                        ORDER BY b.updated_at DESC
                        LIMIT 1
                        """
                    ),
                    {"tenant_id": tenant_id, "user_id": user_id},
                )
            )
            .mappings()
            .first()
        )
        if row:
            return SignalModel(
                str(row["model_id"]), str(row["display_name"]), "strategy_binding"
            )
        row = (
            (
                await db.execute(
                    text(
                        """
                        SELECT model_id,
                               COALESCE(metadata_json->>'display_name', model_id) AS display_name
                        FROM qm_user_models
                        WHERE tenant_id = :tenant_id AND user_id = :user_id
                          AND is_default = TRUE
                        LIMIT 1
                        """
                    ),
                    {"tenant_id": tenant_id, "user_id": user_id},
                )
            )
            .mappings()
            .first()
        )
        if row:
            return SignalModel(
                str(row["model_id"]), str(row["display_name"]), "default"
            )
    return SignalModel()


def _env_float(name: str, default: float) -> float:
    import os

    raw = os.getenv(name, "").strip()
    try:
        return float(raw) if raw else default
    except ValueError:
        return default


def _env_bool(name: str, default: bool) -> bool:
    import os

    raw = os.getenv(name, "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


def _to_suffix(symbol: str) -> str:
    return StockCodeUtil.to_suffix(str(symbol or "").strip()) or str(symbol or "").strip()


class TdxRollingTradeService:
    """滚动买卖: 分数>2.2 买 / 掉下 2.2 卖 / 大盘低于 MA20 只卖不买。"""

    # ---------- 数据侧 ----------

    async def load_latest_scores_with_rank(
        self,
        *,
        tenant_id: str,
        user_id: str,
        run_id: str | None = None,
        trade_date: str | None = None,
        model_id: str | None = None,
    ) -> tuple[str | None, dict[str, float], dict[str, float], str | None]:
        """读取推理 run 的 symbol->fusion_score 与 rank_pct。

        优先 run_id；否则 trade_date 指定的预测日期（推历史分数）；否则取
        ``model_id`` 的最新完成 run（为空时取全局最新，仅旧调用方使用）。
        返回 (run_id, score_map, rank_map, prediction_trade_date)，key 为 suffix。
        """
        async with get_session(read_only=True) as db:
            prediction_trade_date: str | None = None
            if run_id:
                selected_run = run_id
                row = (
                    await db.execute(
                        text(
                            """
                            SELECT prediction_trade_date
                            FROM qm_model_inference_runs
                            WHERE run_id = :run_id LIMIT 1
                            """
                        ),
                        {"run_id": run_id},
                    )
                ).mappings().first()
                if row:
                    prediction_trade_date = str(row.get("prediction_trade_date") or "")
            else:
                query = """
                    SELECT run_id, prediction_trade_date::text AS prediction_trade_date
                    FROM qm_model_inference_runs
                    WHERE tenant_id = :tenant_id
                      AND user_id = :user_id
                      AND status = 'completed'
                """
                params: dict[str, Any] = {"tenant_id": tenant_id, "user_id": user_id}
                if model_id:
                    query += " AND model_id = :model_id"
                    params["model_id"] = model_id
                if trade_date:
                    query += " AND prediction_trade_date = :trade_date"
                    try:
                        params["trade_date"] = datetime.strptime(trade_date, "%Y-%m-%d").date()
                    except ValueError:
                        return None, {}, None
                query += " ORDER BY created_at DESC LIMIT 1"
                row = (
                    await db.execute(text(query), params)
                ).mappings().first()
                if not row:
                    return None, {}, None
                selected_run = str(row.get("run_id") or "").strip() or None
                prediction_trade_date = str(row.get("prediction_trade_date") or "")

            if not selected_run:
                return None, {}, None

            rows = (
                await db.execute(
                    text(
                        """
                        SELECT symbol, fusion_score, rank_pct
                        FROM engine_signal_scores
                        WHERE run_id = :run_id
                          AND tenant_id = :tenant_id
                          AND user_id = :user_id
                          AND (universe_tag IS NULL OR universe_tag = 'CN')
                        """
                    ),
                    {"run_id": selected_run, "tenant_id": tenant_id, "user_id": user_id},
                )
            ).mappings().all()

            score_map: dict[str, float] = {}
            rank_map: dict[str, float] = {}
            for r in rows:
                symbol = _to_suffix(str(r.get("symbol") or ""))
                score = r.get("fusion_score")
                rank = r.get("rank_pct")
                if not symbol:
                    continue
                if isinstance(score, (int, float)):
                    score_map[symbol] = float(score)
                if isinstance(rank, (int, float)):
                    rank_map[symbol] = float(rank)

            return selected_run, score_map, rank_map, prediction_trade_date

    async def load_latest_scores(
        self,
        *,
        tenant_id: str,
        user_id: str,
        run_id: str | None = None,
        trade_date: str | None = None,
        model_id: str | None = None,
    ) -> tuple[str | None, dict[str, float], str | None]:
        """兼容包装：返回 (run_id, score_map, prediction_trade_date)。

        L2 实时/采集等旧调用方只关心分数；新代码用
        ``load_latest_scores_with_rank`` 取分位。
        """
        selected_run, score_map, _rank_map, prediction_trade_date = (
            await self.load_latest_scores_with_rank(
                tenant_id=tenant_id,
                user_id=user_id,
                run_id=run_id,
                trade_date=trade_date,
                model_id=model_id,
            )
        )
        return selected_run, score_map, prediction_trade_date

    async def load_positions_from_tdx(self) -> tuple[list[dict[str, Any]], str]:
        """从通达信桥拉取当前持仓。

        返回 (positions, error)。positions 元素包含 symbol(600519.SH)/
        name/volume/available_volume/cost_price/market_value。
        """
        try:
            positions = await tdx_pusher.pull_positions()
        except TdxPushError as exc:
            return [], f"通达信桥持仓拉取失败: {exc}"
        except Exception as exc:
            return [], f"通达信桥持仓拉取失败: {exc}"

        normalized: list[dict[str, Any]] = []
        for p in positions or []:
            raw = str(p.get("stock_code") or "").strip()
            if not raw:
                continue
            symbol = _to_suffix(raw)
            volume = int(float(p.get("total_volume") or p.get("volume") or 0))
            if volume <= 0:
                continue
            normalized.append(
                {
                    "symbol": symbol,
                    "name": str(p.get("stock_name") or "").strip(),
                    "volume": volume,
                    "available_volume": int(float(p.get("available_volume") or volume)),
                    "cost_price": float(p.get("cost_price") or 0),
                    "market_value": float(p.get("market_value") or 0),
                }
            )
        return normalized, ""

    async def pull_today_orders(self, stock_code: str = "") -> list:
        """拉取通达信当日委托（含已成交/在途/废单）。

        返回桥规范化后的委托列表: order_id/stock_code/side/status/
        order_price/filled_price/filled_volume/total_volume。
        """
        return await tdx_pusher.pull_orders(stock_code)

    async def cancel_order(self, stock_code: str, order_id: str) -> dict:
        """撤单。返回 {"success": bool, "message": str}。

        撤单可能撤销失败或"撤销前已成交", 调用方需随后 pull_today_orders
        复核最终状态, 不能仅凭 success 判断结果。
        """
        return await tdx_pusher.cancel_order(stock_code, order_id)

    async def load_positions_from_paper(
        self, tenant_id: str, user_id: str
    ) -> tuple[list[dict[str, Any]], str]:
        """从本地模拟盘账户拉取当前持仓（模拟直接下单模式）。

        返回 (positions, error)，positions 元素与 load_positions_from_tdx 同构，
        额外带 raw_symbol（模拟盘持仓原 key，卖出时沿用保证对账一致）。
        """
        try:
            from backend.services.trade_shared.redis_client import redis_client as trade_redis
            from backend.services.trade_shared.simulation_manager import (
                SimulationAccountManager,
                canonical_sim_uid,
            )

            user_id_int = canonical_sim_uid(user_id)
            if user_id_int <= 0:
                return [], f"无效的用户 ID: {user_id}"

            account = await SimulationAccountManager(trade_redis).get_account(
                user_id_int, tenant_id=tenant_id
            )
        except Exception as exc:
            return [], f"模拟盘持仓拉取失败: {exc}"
        if not account:
            return [], "模拟盘账户未初始化，请先在模拟交易页创建账户"

        normalized: list[dict[str, Any]] = []
        for raw_symbol, pos in (account.get("positions") or {}).items():
            if not isinstance(pos, dict):
                continue
            # 多空持仓同 key（SYMBOL::side），只取多头
            key = str(raw_symbol or "").strip()
            if "::" in key and not key.lower().endswith("::long"):
                continue
            base = key.split("::")[0]
            symbol = _to_suffix(base)
            volume = int(float(pos.get("volume") or 0))
            if not symbol or volume <= 0:
                continue
            normalized.append(
                {
                    "symbol": symbol,
                    "name": str(pos.get("name") or "").strip(),
                    "volume": volume,
                    "available_volume": int(float(pos.get("available_volume") or volume)),
                    "cost_price": float(pos.get("cost") or 0),
                    "market_value": float(pos.get("market_value") or 0),
                    "raw_symbol": key,
                }
            )
        return normalized, ""

    async def is_index_above_ma20(self) -> tuple[bool, str]:
        """上证指数是否站上 MA20。

        返回 (above, detail)。数据缺失时返回 (False, 原因)，安全侧只卖不买。
        """
        try:
            from datetime import date as _date
            from datetime import timedelta

            from backend.services.engine.data_platform.quantdb_hub import QuantDBDataHub

            hub = QuantDBDataHub()
            # 取近 60 个自然日数据，足够 MA20 且覆盖长假
            df = hub.fetch_index_kline(
                INDEX_SYMBOL,
                _date.today() - timedelta(days=90),
                _date.today(),
            )
            if df is None or df.empty:
                return False, "QuantDB 无上证指数数据"
            df = df.drop_duplicates(subset=["trade_date"], keep="last")
            close = df["close"].astype(float)
            if len(close) < MA_WINDOW:
                return False, f"指数数据不足 {MA_WINDOW} 日"
            latest = float(close.iloc[-1])
            ma20 = float(close.tail(MA_WINDOW).mean())
            return latest >= ma20, (
                f"上证 {latest:.2f} vs MA20 {ma20:.2f} ({'之上' if latest >= ma20 else '之下'})"
            )
        except Exception as exc:
            return False, f"指数检查失败: {exc}"

    # ---------- 信号计算 ----------

    def compute_rolling_signals(
        self,
        *,
        score_map: dict[str, float],
        positions: list[dict[str, Any]],
        index_above_ma20: bool,
        fixed_buy_amount: float = DEFAULT_FIXED_BUY_AMOUNT,
        score_threshold: float = DEFAULT_SCORE_THRESHOLD,
        rank_map: dict[str, float] | None = None,
        threshold_mode: str = "score",
        rank_pct_threshold: float = DEFAULT_RANK_PCT_THRESHOLD,
    ) -> dict[str, Any]:
        """计算滚动买卖信号。

        ``threshold_mode``:
        - ``rank_pct``（默认新配置）：持仓分位 ≤ rank_pct_threshold → 卖；
          分位 ≥ 1-rank_pct_threshold → 买；**缺分位 → 跳过（不卖）**。
        - ``score``（旧配置）：分数 ≤ score_threshold → 卖；> 阈值 → 买；
          缺分数按旧语义卖（仅为兼容，不再作为默认）。

        返回 {buys, sells, holds, unscored, market_detail, signal_basis}:
          buys: [{symbol,name,score,rank_pct,volume,close,reason}]
          sells: [{symbol,name,score,rank_pct,volume,reason}]
        """
        mode = _normalize_threshold_mode(threshold_mode, "score")
        rank_map = rank_map or {}
        buy_cut = max(0.0, 1.0 - float(rank_pct_threshold))
        held = {p["symbol"]: p for p in positions}

        sells: list[dict[str, Any]] = []
        holds: list[dict[str, Any]] = []
        unscored: list[dict[str, Any]] = []
        for symbol, pos in held.items():
            score = score_map.get(symbol)
            rank = rank_map.get(symbol)
            if mode == "rank_pct":
                if rank is None:
                    # 机构口径：缺分位是数据缺失，不是卖出信号
                    unscored.append(
                        {
                            "symbol": symbol,
                            "name": pos.get("name") or symbol,
                            "volume": pos["volume"],
                            "reason": "最新推理无该股分位（跳过，不卖）",
                        }
                    )
                    continue
                if float(rank) <= float(rank_pct_threshold):
                    sells.append(
                        {
                            "symbol": symbol,
                            "name": pos.get("name") or symbol,
                            "score": score,
                            "rank_pct": float(rank),
                            "volume": pos["volume"],
                            "available_volume": pos["available_volume"],
                            "reason": (
                                f"截面位置靠后（分位 {float(rank):.3f} ≤ "
                                f"{float(rank_pct_threshold):.3f}）"
                            ),
                        }
                    )
                else:
                    holds.append(
                        {
                            "symbol": symbol,
                            "name": pos.get("name") or symbol,
                            "score": score,
                            "rank_pct": float(rank),
                            "volume": pos["volume"],
                        }
                    )
                continue
            if score is None or score <= score_threshold:
                sells.append(
                    {
                        "symbol": symbol,
                        "name": pos.get("name") or symbol,
                        "score": score,
                        "rank_pct": rank,
                        "volume": pos["volume"],
                        "available_volume": pos["available_volume"],
                        "reason": (
                            f"最新分数低于{score_threshold}阈值"
                            if score is not None
                            else "最新推理已无该股"
                        ),
                    }
                )
            else:
                holds.append(
                    {
                        "symbol": symbol,
                        "name": pos.get("name") or symbol,
                        "score": score,
                        "rank_pct": rank,
                        "volume": pos["volume"],
                    }
                )

        buys: list[dict[str, Any]] = []
        if index_above_ma20:
            if mode == "rank_pct":
                candidates = sorted(
                    (
                        (sym, float(rank), score_map.get(sym))
                        for sym, rank in rank_map.items()
                        if float(rank) >= buy_cut and sym not in held
                    ),
                    key=lambda item: item[1],
                    reverse=True,
                )
            else:
                candidates = sorted(
                    (
                        (sym, None, float(score))
                        for sym, score in score_map.items()
                        if score > score_threshold and sym not in held
                    ),
                    key=lambda item: item[2] or 0.0,
                    reverse=True,
                )
            close_map = _batch_last_close([sym for sym, _rank, _score in candidates])
            for sym, rank, score in candidates:
                close = close_map.get(sym, 0.0)
                if close <= 0:
                    continue
                volume = int((fixed_buy_amount / close) // LOT_SIZE) * LOT_SIZE
                if volume < LOT_SIZE:
                    continue
                if mode == "rank_pct":
                    reason = f"分位 {rank:.3f} ≥ {buy_cut:.3f}"
                else:
                    reason = f"分数 {score:.2f} > {score_threshold}"
                buys.append(
                    {
                        "symbol": sym,
                        "score": round(float(score), 4) if score is not None else None,
                        "rank_pct": round(float(rank), 4) if rank is not None else None,
                        "volume": volume,
                        "close": round(close, 2),
                        "amount": round(volume * close, 2),
                        "reason": reason,
                    }
                )

        return {
            "buys": buys,
            "sells": sells,
            "holds": holds,
            "unscored": unscored,
            "index_above_ma20": index_above_ma20,
            "threshold_mode": mode,
            "score_threshold": score_threshold,
            "rank_pct_threshold": float(rank_pct_threshold),
            "fixed_buy_amount": fixed_buy_amount,
        }

    # ---------- 推送 ----------

    # ---------- 真实下单 ----------

    async def place_rolling_orders(
        self,
        *,
        run_id: str,
        buys: list[dict[str, Any]],
        sells: list[dict[str, Any]],
        tenant_id: str = "default",
        user_id: str | None = None,
        source: str = "tdx_rolling",
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """把滚动买卖信号生成为真实委托推给通达信（客户端弹确认框）。

        卖单: 市价 + 可卖量（先卖）
        买单: 收盘价限价 + 整手数量
        返回 (placed_orders, failed_orders)。
        """
        placed: list[dict[str, Any]] = []
        failed: list[dict[str, Any]] = []

        async def _submit(item: dict[str, Any], side: str) -> None:
            if side == "sell":
                volume = int(item.get("available_volume") or 0)
                price: float | None = None
                price_type = 1  # 市价
            else:
                volume = int(item.get("volume") or 0)
                price = float(item.get("close") or 0)
                price_type = 0  # 限价
            if volume <= 0 or (side == "buy" and (price or 0) <= 0):
                failed.append({**item, "side": side, "error": "数量或价格无效"})
                return
            plan_id = f"rolling_{run_id}_{item['symbol']}_{side}"
            # 风控闸（T-RC-02b 覆盖扩展：滚动 + L2 主单共用本函数）：影子期判定留痕不拦单；
            # 闸不可用/判定异常 = fail-closed 拒单（与 OrderRouter 内嵌同纪律）
            try:
                from backend.services.trade.services.risk_gate_service import (
                    check_direct_order as _risk_check,
                )

                if user_id:
                    _risk_uid: Any = user_id
                else:
                    from backend.shared.simulation_account_keys import (
                        resolve_db_account_user,
                    )

                    _risk_uid = resolve_db_account_user("TDX_ACCOUNT_USER_ID")
                risk = await _risk_check(
                    tenant_id=tenant_id,
                    user_id=_risk_uid,
                    symbol=item["symbol"],
                    side=side,
                    quantity=volume,
                    price=price,
                    order_type="market" if side == "sell" else "limit",
                    source=source,
                    remarks=plan_id,
                )
            except Exception as exc:  # noqa: BLE001 - 闸不可用=fail-closed
                failed.append(
                    {**item, "side": side, "error": f"风控闸不可用（fail-closed）: {exc}"}
                )
                return
            if not risk.passed:
                failed.append(
                    {
                        **item,
                        "side": side,
                        "error": f"风控拒单[{risk.rule_id or 'risk'}]: {risk.reason}",
                    }
                )
                return
            try:
                resp = await tdx_pusher.place_order(
                    stock_code=item["symbol"],
                    side=side,
                    volume=volume,
                    price=price,
                    price_type=price_type,
                    plan_id=plan_id,
                )
                # 时段闸拒单（跨 11:35/15:05 的运行：本轮开始时在盘内、提交时已在盘外）：
                # 这单**没有提交过**，不是下单失败。混进 failed 会推「N 条腿下单失败，
                # 请核对 orders 补单」——而去核对的话 orders 里根本没有这些单。
                refusal = session_refusal_of(resp)
                if refusal:
                    failed.append(session_refusal_row(item, side, refusal))
                    return
                # 桥返回 {plan_id, status, orders:[{stock_code,side,volume,status,order_id,message}]}
                first = (resp.get("orders") or [{}])[0] if isinstance(resp, dict) else {}
                order_status = str(first.get("status") or resp.get("status") or "unknown")
                if order_status in ("rejected", "error"):
                    failed.append(
                        {
                            **item,
                            "side": side,
                            "error": str(first.get("message") or resp.get("message") or "下单被拒"),
                        }
                    )
                else:
                    placed.append(
                        {
                            **item,
                            "side": side,
                            "plan_id": plan_id,
                            "order_id": str(first.get("order_id") or ""),
                            "status": order_status,
                            "message": str(first.get("message") or ""),
                        }
                    )
            except TdxPushError as exc:
                failed.append({**item, "side": side, "error": str(exc)})
            except Exception as exc:
                failed.append({**item, "side": side, "error": str(exc)})

        # 先卖后买
        for item in sells:
            await _submit(item, "sell")
        for item in buys:
            await _submit(item, "buy")
        return placed, failed

    async def place_paper_orders(
        self,
        *,
        tenant_id: str,
        user_id: str,
        run_id: str,
        buys: list[dict[str, Any]],
        sells: list[dict[str, Any]],
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """把滚动买卖信号在本地模拟盘账户直接成交（免确认、零风险）。

        卖单: 市价 + 可卖量（先卖，沿用模拟盘持仓原 key 保证与既有持仓对账）
        买单: 市价 + 整手数量（买入统一用前缀格式，符合全站内部存储规范）
        返回 (placed_orders, failed_orders)。
        """
        from backend.services.trade_shared.redis_client import redis_client as trade_redis
        from backend.services.trade_shared.simulation_manager import (
            SimulationAccountManager,
            canonical_sim_uid,
        )
        from backend.shared.database_manager_v2 import get_db_manager

        user_id_int = canonical_sim_uid(user_id)
        if user_id_int <= 0:
            return [], [{"error": f"无效的用户 ID: {user_id}"}]

        db_manager = get_db_manager()
        placed: list[dict[str, Any]] = []
        failed: list[dict[str, Any]] = []

        async def _submit(item: dict[str, Any], side: str) -> None:
            if side == "sell":
                volume = int(item.get("available_volume") or 0)
                symbol = str(item.get("raw_symbol") or item.get("symbol") or "").strip()
            else:
                volume = int(item.get("volume") or 0)
                symbol = StockCodeUtil.to_prefix(
                    str(item.get("symbol") or "")
                ) or str(item.get("symbol") or "").strip()
            if volume <= 0 or not symbol:
                failed.append({**item, "side": side, "error": "数量或股票代码无效"})
                return
            try:
                # T-P2-01：改经 OrderRouter 唯一入口（获得账户锁/幂等/涨跌停/费用/落账）；
                # paper 模式刻意不动真单（mirror=False，免确认零风险）；自动化路径允许如实降级。
                from backend.services.simulation.services.order_router import (
                    OrderRequest,
                    submit_order,
                )
                from backend.shared.order_contract import SOURCE_TDX_ROLLING

                async with db_manager.get_session() as db:
                    routed = await submit_order(
                        db,
                        trade_redis,
                        OrderRequest(
                            tenant_id=tenant_id,
                            user_id=user_id_int,
                            symbol=symbol,
                            side=side,
                            quantity=volume,
                            order_type="market",
                            price=float(item.get("close") or 0) or None,
                            source=SOURCE_TDX_ROLLING,
                            remarks=f"TdxRolling paper: {run_id}",
                            run_id=str(run_id or ""),
                            mirror=False,
                            strict_market=False,
                        ),
                    )
                    if routed.success:
                        placed.append(
                            {
                                **item,
                                "side": side,
                                "order_id": str(routed.order_id or ""),
                                "status": "filled",
                                "message": (
                                    f"模拟成交 {routed.filled_quantity}股@{routed.fill_price}"
                                    f"（{routed.price_source}）"
                                ),
                            }
                        )
                    else:
                        failed.append({**item, "side": side, "error": routed.message})
            except Exception as exc:
                failed.append({**item, "side": side, "error": str(exc)})

        # 先卖后买
        for item in sells:
            await _submit(item, "sell")
        for item in buys:
            await _submit(item, "buy")
        return placed, failed

    async def run_rolling_push(
        self,
        *,
        tenant_id: str,
        user_id: str,
        run_id: str | None = None,
        trade_date: str | None = None,
        fixed_buy_amount: float | None = None,
        push_message: bool = True,
        check_index: bool = True,
    ) -> dict[str, Any]:
        """执行一次滚动买卖检查并推送（三档执行模式）——**带崩溃可见性的外壳**。

        异常照旧上抛（调用方的 try/except 与重试逻辑一个字不改），但先推一条
        告警：这条腿是定时/事件驱动的，崩在算信号之前时面板与通知中心都没有痕迹，
        而它承载的是一整批买卖动作（本轮推理的信号全都没落地）。
        """
        try:
            return await self._run_rolling_push(
                tenant_id=tenant_id,
                user_id=user_id,
                run_id=run_id,
                trade_date=trade_date,
                fixed_buy_amount=fixed_buy_amount,
                push_message=push_message,
                check_index=check_index,
            )
        except Exception as exc:
            await _alert_rolling_crash(exc, user_id=user_id)
            raise

    async def _run_rolling_push(
        self,
        *,
        tenant_id: str,
        user_id: str,
        run_id: str | None = None,
        trade_date: str | None = None,
        fixed_buy_amount: float | None = None,
        push_message: bool = True,
        check_index: bool = True,
    ) -> dict[str, Any]:
        """执行一次滚动买卖检查并推送（三档执行模式）。

        返回 {success, run_id, market, buys, sells, holds, warnings, error}
        """
        settings = load_rolling_settings(tenant_id, user_id)
        threshold_mode = settings.threshold_mode
        rank_pct_threshold = settings.rank_pct_threshold
        score_threshold = settings.score_threshold
        execute_mode = settings.execute_mode
        if fixed_buy_amount is None:
            fixed_buy_amount = settings.fixed_buy_amount
        signal_model = await resolve_signal_model(
            tenant_id, user_id, settings.signal_model_id
        )
        if not signal_model.model_id:
            logger.warning(
                "[TdxRolling] 未解析到信号模型（无策略绑定/默认模型），回退最新完成 run"
            )
        # 时段事实（一次取值，下面三处共用：真单闸门、预警面、结果字典）。
        in_session = is_trading_time()

        # 直接下单（通达信实盘或模拟盘）已对全部登录用户开放（会员门控移除）
        # 通达信实盘下单需要桥；模拟盘直接下单不依赖桥（预警推送尽力而为）
        bridge_ok = tdx_pusher.enabled
        if execute_mode == "tdx" and not bridge_ok:
            outcome = {
                "success": False,
                "error": "TDX_BRIDGE_URL/TOKEN 未配置",
                "buys": [],
                "sells": [],
            }
            await _alert_rolling_run(outcome, user_id=user_id)
            return outcome

        selected_run, score_map, rank_map, prediction_trade_date = (
            await self.load_latest_scores_with_rank(
                tenant_id=tenant_id,
                user_id=user_id,
                run_id=run_id,
                trade_date=trade_date,
                model_id=signal_model.model_id or None,
            )
        )
        if not selected_run or (not score_map and not rank_map):
            outcome = {
                "success": False,
                "error": (
                    f"没有可用的推理信号（日期 {trade_date}）"
                    if trade_date
                    else "没有可用的推理信号（检查 run_id 或最新推理是否完成）"
                ),
                "buys": [],
                "sells": [],
            }
            await _alert_rolling_run(outcome, user_id=user_id)
            return outcome

        # 持仓来源: 模拟直接下单读本地模拟盘，其余读通达信桥
        if execute_mode == "paper":
            positions, pos_error = await self.load_positions_from_paper(tenant_id, user_id)
        else:
            positions, pos_error = await self.load_positions_from_tdx()
        # 持仓读不到 ⇒ **本轮不产生任何对外产物**（预警与真单都不发）。
        # 两个 loader 都是「出错返回 ([], 原因)」而不是抛，此前那行 warning 之后流程
        # 照走：held={} 让卖出腿全空（想卖的卖不掉）、买入腿把每只达标标的都当成
        # 新仓买满一篮子。持仓未知时算出来的信号不是"少一条"，是**算错**。
        positions_unreadable = bool(pos_error)
        if positions_unreadable:
            logger.warning("[TdxRolling] %s（本轮信号整体跳过）", pos_error)

        # 推历史分数时不再用当日大盘 MA20 过滤（历史日期应只看当天的信号）
        if check_index and not trade_date:
            index_above, market_detail = await self.is_index_above_ma20()
        else:
            index_above, market_detail = True, "历史日期推送: 跳过当日大盘过滤"

        signals = self.compute_rolling_signals(
            score_map=score_map,
            positions=positions,
            index_above_ma20=index_above,
            fixed_buy_amount=fixed_buy_amount,
            score_threshold=score_threshold,
            rank_map=rank_map,
            threshold_mode=threshold_mode,
            rank_pct_threshold=rank_pct_threshold,
        )

        buys = signals["buys"]
        sells = signals["sells"]

        # 补股票名
        need_names = [b["symbol"] for b in buys] + [s["symbol"] for s in sells]
        from backend.services.live_trading.services.tdx_signal_push_service import (
            _batch_lookup_names,
        )

        name_map = _batch_lookup_names(need_names)
        for item in buys:
            item["name"] = name_map.get(item["symbol"], "") or item["symbol"]
        for item in sells:
            if not item.get("name") or item["name"] == item["symbol"]:
                item["name"] = name_map.get(item["symbol"], "") or item["symbol"]

        warnings_total = 0
        results: dict[str, Any] = {}

        # 预警/消息推送需要桥（模拟直接下单模式桥不可用时跳过，不影响成交）。
        # 持仓读不到时整体跳过：预警是按 held 算出来的，held={} 时买预警会是一篮子
        # 新仓、卖预警一条没有——推出去等于给操作者一份错的建议。
        if bridge_ok and not positions_unreadable:
            # 卖出预警
            if sells:
                sell_list = sells[:_MAX_SELL_WARNINGS]
                try:
                    warn_signals = [
                        {
                            "symbol": s["symbol"],
                            "side": "sell",
                            "price": 0,
                            "close": 0,
                            "volume": s["available_volume"],
                            "reason": f"卖 {s['name']} {s['reason']}",
                        }
                        for s in sell_list
                    ]
                    resp = await tdx_pusher.push_warnings(warn_signals)
                    warnings_total += len(sell_list)
                    results["sell_warnings"] = {"success": True, "result": resp}
                except TdxPushError as exc:
                    logger.warning("[TdxRolling] 卖出预警推送失败: %s", exc)
                    results["sell_warnings"] = {"success": False, "error": str(exc)}

            # 买入预警
            if buys:
                buy_list = buys[:_MAX_BUY_WARNINGS]
                try:
                    warn_signals = [
                        {
                            "symbol": b["symbol"],
                            "side": "buy",
                            "price": b["close"],
                            "close": b["close"],
                            "volume": b["volume"],
                            "reason": f"买 {b['name']} 分{b['score']} {b['volume']}股",
                        }
                        for b in buy_list
                    ]
                    resp = await tdx_pusher.push_warnings(warn_signals)
                    warnings_total += len(buy_list)
                    results["buy_warnings"] = {"success": True, "result": resp}
                except TdxPushError as exc:
                    logger.warning("[TdxRolling] 买入预警推送失败: %s", exc)
                    results["buy_warnings"] = {"success": False, "error": str(exc)}

            # 汇总消息
            if push_message:
                try:
                    date_label = f"（{prediction_trade_date or selected_run}）"
                    lines = [f"滚动检查{date_label}: 大盘 {market_detail}"]
                    if not index_above and not trade_date:
                        lines.append("大盘低于MA20, 只卖不买")
                    if sells:
                        lines.append(
                            "卖出: " + "|".join(f"{s['name']}({s['reason']})" for s in sells[:10])
                        )
                    if buys:
                        lines.append(
                            "买入: " + "|".join(f"{b['name']}:{b['score']}" for b in buys[:10])
                        )
                    if not sells and not buys:
                        lines.append("无买卖动作")
                    await tdx_pusher.push_message("MSG,QuantMind 滚动买卖|" + "\n".join(lines))
                    results["message"] = {"success": True}
                except TdxPushError as exc:
                    logger.warning("[TdxRolling] 消息推送失败: %s", exc)
                    results["message"] = {"success": False, "error": str(exc)}
        elif positions_unreadable:
            results["warnings_skipped"] = {
                "success": False,
                "error": f"持仓读不到，预警整体跳过: {pos_error}",
            }
        elif execute_mode == "paper":
            results["warnings_skipped"] = {
                "success": False,
                "error": "通达信桥未配置，仅执行模拟盘成交",
            }

        # 直接下单（可选）—— 先卖后买
        #   tdx 模式: 通达信客户端弹确认框
        #   paper 模式: 模拟盘本地撮合直接成交（免确认）
        # 持仓读不到 ⇒ 一律不下单（见上方 positions_unreadable 注释）。
        # 非交易时段 ⇒ **真单**不下（paper 是本地撮合，盘后演练正需要它，不受限）。
        # 这条与推送触发时机无关：rolling 是「每次推理完跑一次」拉动的，推理
        # 完全可能在盘后完成（收盘后重算/补跑），那一刻的真单只会被柜台拒或被
        # 客户端挂成次日单。物理出口还有一道闸（tdx_push_service.place_order），
        # 这里这道负责**如实登记**"本轮有几条被时段压下了"。
        placed_orders: list[dict[str, Any]] = []
        failed_orders: list[dict[str, Any]] = []
        session_suppressed = 0
        if (
            not positions_unreadable
            and execute_mode == "tdx"
            and not in_session
            and (buys or sells)
        ):
            session_suppressed = len(buys) + len(sells)
            results["execute_skipped"] = {
                "success": False,
                "error": (
                    f"非交易时段（{now_shanghai().strftime('%H:%M')}），"
                    f"本轮 {session_suppressed} 条真单未提交"
                ),
            }
        elif not positions_unreadable and execute_mode == "tdx" and (buys or sells):
            placed_orders, failed_orders = await self.place_rolling_orders(
                run_id=selected_run,
                buys=buys[:_MAX_BUY_WARNINGS],
                sells=sells[:_MAX_SELL_WARNINGS],
            )
        elif not positions_unreadable and execute_mode == "paper" and (buys or sells):
            placed_orders, failed_orders = await self.place_paper_orders(
                tenant_id=tenant_id,
                user_id=user_id,
                run_id=selected_run,
                buys=buys[:_MAX_BUY_WARNINGS],
                sells=sells[:_MAX_SELL_WARNINGS],
            )
        # 跨 11:35/15:05 的那一轮：本轮开头在盘内、提交时已在盘外。这些单被时段闸
        # 挡在门内（``session_refused`` 带内标记），**不算下单失败**——与上面那条
        # 「本轮开始时就不在时段内」是同一件事的两种到达方式，故并进同一个计数。
        failed_orders, session_refusals = split_session_refusals(failed_orders)
        if session_refusals:
            session_suppressed += len(session_refusals)
            results.setdefault(
                "execute_skipped",
                {
                    "success": False,
                    "error": (
                        f"非交易时段（{now_shanghai().strftime('%H:%M')}），"
                        f"本轮 {session_suppressed} 条真单未提交"
                    ),
                },
            )
        if placed_orders:
            results["orders"] = {"success": True, "placed": placed_orders}
        if failed_orders:
            results["orders_failed"] = {"success": False, "failed": failed_orders}

        # 本轮有几条对外产物被「持仓读不到」压下了（预警/真单都算）。
        # 只统计**确实被压下的**：没有信号可压时这条失败没有后果，也不该报警。
        leg_armed = bridge_ok or execute_mode != DEFAULT_EXECUTE_MODE
        signals_suppressed = (
            len(buys) + len(sells) if (positions_unreadable and leg_armed) else 0
        )

        logger.info(
            "[TdxRolling] run=%s buys=%d sells=%d holds=%d market=%s orders=%d/%d%s%s",
            selected_run,
            len(buys),
            len(sells),
            len(signals["holds"]),
            market_detail,
            len(placed_orders),
            len(placed_orders) + len(failed_orders),
            f" suppressed={signals_suppressed}" if signals_suppressed else "",
            f" session_skipped={session_suppressed}" if session_suppressed else "",
        )
        outcome = {
            "success": True,
            "run_id": selected_run,
            "prediction_trade_date": prediction_trade_date,
            "market": {
                "above_ma20": index_above,
                "detail": market_detail,
                "index_symbol": INDEX_SYMBOL,
                "ma_window": MA_WINDOW,
            },
            "score_threshold": score_threshold,
            "threshold_mode": threshold_mode,
            "rank_pct_threshold": rank_pct_threshold,
            "signal_model": {
                "model_id": signal_model.model_id,
                "display_name": signal_model.display_name,
                "source": signal_model.source,
            },
            "unscored": signals.get("unscored") or [],
            "execute_mode": execute_mode,
            "bridge_ok": bridge_ok,
            "auto_place": execute_mode != DEFAULT_EXECUTE_MODE,
            "positions_source": "paper" if execute_mode == "paper" else "tdx",
            "positions_count": len(positions),
            "positions_error": pos_error,
            "signals_suppressed": signals_suppressed,
            "session_suppressed": session_suppressed,
            "in_trading_hours": in_session,
            "buys": buys,
            "sells": sells,
            "holds": signals["holds"],
            "warnings": warnings_total,
            "placed_orders": placed_orders,
            "failed_orders": failed_orders,
            "results": results,
            "pushed_at": datetime.now().isoformat(timespec="seconds"),
        }
        await _alert_rolling_run(outcome, user_id=user_id)
        return outcome


def alert_user_id(user_id: object) -> str:
    """告警收件人坐标：**能收通知的账户**，不是「谁跑的这一轮」。

    定时全局任务以 ``user_id="system"`` 跑推理并原样传到告警面——而
    ``notifications.user_id`` 有外键指向 ``users(user_id)``（存量只有账户名一行），
    这个值插不进去 ⇒ 投递被判失败 ⇒ 按纪律「未送达不记去重键」⇒ 每次运行重试、
    每次都失败 = 一条通知都发不出去（失败可见性整体失效，且日志里只有一行 warning）。

    非数字（``system``/``admin`` 之类执行者身份）与空值一律回退到**账户坐标**
    ——与这两条腿下单用的是同一座账户（``TDX_ACCOUNT_USER_ID``）。数字身份原样归一
    （管理员族收口 ``10000001``，其余补零 8 位），``users`` 里存的就是这个形状。
    """
    from backend.shared.simulation_account_keys import (
        normalize_runtime_user,
        resolve_db_account_user,
    )

    raw = str(user_id or "").strip()
    if raw.isdigit():
        return normalize_runtime_user(raw)
    return resolve_db_account_user("TDX_ACCOUNT_USER_ID")


async def _alert_rolling_run(outcome: dict[str, Any], *, user_id: str | None) -> None:
    """一次运行之后的失败可见性：有失败推一条（一天一次），干净的一次负责发「已恢复」。

    **绝不抛**：调用点在「信号已算完、单可能已经出去」之后，通知层的毛病不许把
    这次运行变成异常（同 ``decision_round_alerts.alert_round`` 的纪律）。
    """
    from backend.services.live_trading.services.tdx_exec_alerts import (
        FAMILY_ROLLING,
        alert_exec_leg,
        resolve_exec_leg,
        rolling_alerts,
        rolling_round_is_clean,
    )

    try:
        uid = alert_user_id(user_id)
        alerts = rolling_alerts(outcome)
        if alerts:
            await alert_exec_leg(FAMILY_ROLLING, alerts, user_id=uid, redis=trade_redis)
        elif rolling_round_is_clean(outcome):
            # 「alerts 为空」不等于「这一轮是好的」：被自家时段闸压住的补跑、
            # 盘外的 tdx 轮次都既没有告警也没有动作。见 ``rolling_round_is_clean``。
            await resolve_exec_leg(FAMILY_ROLLING, user_id=uid, redis=trade_redis)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[TdxRolling] 失败可见性推送异常: %s", exc, exc_info=True)


async def _alert_rolling_crash(exc: BaseException, *, user_id: str | None) -> None:
    """运行中途抛异常 ⇒ 推一条（一天一次）。**绝不抛**。

    此前这条路只有 ``script_runner`` 的一行 ``logger.warning``：定时任务崩了，
    面板上没有痕迹、通知中心没有一条——而失败可见性本来就是为「人不在场」建的。
    """
    from backend.services.live_trading.services.tdx_exec_alerts import (
        FAMILY_ROLLING,
        Alert,
        KIND_CYCLE_ERROR,
        alert_exec_leg,
    )

    try:
        await alert_exec_leg(
            FAMILY_ROLLING,
            [
                Alert(
                    kind=KIND_CYCLE_ERROR,
                    level="error",
                    title=f"滚动买卖运行异常：{' '.join(str(exc).split())[:60]}",
                    content=(
                        f"{type(exc).__name__}: {exc}\n"
                        "本轮**在算出信号之前就中断了**（没有委托、也没有预警），"
                        "所以这次推理对应的买卖动作整批缺失。\n"
                        "下一轮推理完成后会自动重来；连续出现查后端日志 [TdxRolling] 段"
                        "（异常带 exc_info，有文件行号）。"
                    ),
                )
            ],
            user_id=alert_user_id(user_id),
            redis=trade_redis,
        )
    except Exception as alert_exc:  # noqa: BLE001
        logger.warning("[TdxRolling] 崩溃告警推送异常: %s", alert_exc, exc_info=True)


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
            bar = market_data.get_bar(symbol, latest_date)
            if bar is not None and bar.close > 0:
                result[symbol] = float(bar.close)
    except Exception as exc:
        logger.warning("[TdxRolling] QuantDB 收盘价读取失败: %s", exc)
    return result


tdx_rolling_trader = TdxRollingTradeService()
