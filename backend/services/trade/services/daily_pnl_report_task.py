"""收盘收益报表 → QQ 推送（每日日终）。

每日（默认 ``15:10``，上海时区——实盘台账结算 finalize 在 15:05，收盘值已定）
把当日**实盘两通道**（通达信桥 / 迅投 QMT）的日终台账汇总成一条消息推到 QQ：

    收盘收益 · 2026-10-08

    **通达信桥**
    总资产 ¥918,397.51 ｜ 当日 +¥446.00（+0.05%）
    持仓 4 只 ｜ 现金 ¥889,591.51 ｜ 市值 ¥28,806.00

数据源 = ``real_account_ledger_daily_snapshots`` 的**当日行**，经
``list_real_account_daily_ledgers_by_family`` 家族读（跨 09-18 账户改名与
user 别名，与实盘账户页/风控档位同一口径，禁另起一套）。当日盈亏金额 =
总资产 − 日初权益；百分比走 ``resolve_daily_pnl_pct`` 与卡片逐位同源。

当日有真单镜像事件时（P2-2），附一段**镜像台账**（``mirror:skipped`` /
``mirror:failed`` 两本账的当日计数，按原因聚合 Top-N）——镜像跳过/失败此前只在
双轨对账里可见，而那份报告只在**有未解释缺口**时才推 QQ；一切都被解释的「安静日」
里运营看不到「今天其实跳过了 N 笔真单」。两本账都空则不出该段（空段会被读成
「镜像在跑且一切正常」，而「镜像根本没跑」由决策审计行的 mirror 段回答）。

纪律：
* 当日无台账行（节假日 / 桥停更）→ **不发送、不造假报表**，不落 done 标记，
  稍后周期继续等（数据迟到仍以当天口径补发，过 0 点作废）；
* QQ 未送达（未配置/被拒）→ ``sent=False``，不落 done 标记，下周期重试；
* 当日行总资产为 0（空快照/读错）→ 跳过该通道，绝不发 ¥0 报表；
* 发送走 ``qq_notify.notify``（常态事件，不走告警等级过滤）后台线程，
  不阻塞交易事件循环；
* 发送结果登记进站内通知面（``notification_publisher`` 管理员 fanout，与
  sentinel/桥告警同口径）：已送达/未送达/无数据都落一条痕，每（日, 结果）
  至多一条——审计 H11「报表送达与否不在通知登记处」的修复。

环境变量：
  DAILY_PNL_REPORT_ENABLED      默认 "1"
  DAILY_PNL_REPORT_TIME         默认 "15:10"（上海时区）
  DAILY_PNL_REPORT_INTERVAL_SEC 默认 60
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import date, datetime

from backend.services.live_trading.services.trading_session import TZ
from backend.shared import qq_notify

logger = logging.getLogger(__name__)

_REPORT_KEY = "trade:daily-pnl-report:{date}"
_DONE_KEY = "trade:daily-pnl-report:done:{date}"
_REPORT_TTL_SECONDS = 30 * 24 * 3600

#: 通知面登记去重键：每（日, 结果）至多一条痕（结果 ∈ sent/unsent/nodata）。
_REGISTERED_KEY = "trade:daily-pnl-report:registered:{date}:{outcome}"

#: 决策账户 user 的 env 名（与 decision_round_core / live_family 同源口径）。
ENV_ACCOUNT_USER = "QM_DECISION_ACCOUNT_USER_ID"

#: 实盘两通道（label 与实盘账户页一致）。
REAL_ACCOUNT_CHANNELS: tuple[tuple[str, str], ...] = (
    ("tdx", "通达信桥"),
    ("qmt", "迅投 QMT"),
)

#: 台账读取窗口：只需覆盖「今天」；家族读窗口内每日至多合并一行。
_LOOKBACK_DAYS = 5


def _env_bool(name: str, default: bool) -> bool:
    raw = str(os.getenv(name, "")).strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


def _config() -> dict:
    return {
        "enabled": _env_bool("DAILY_PNL_REPORT_ENABLED", True),
        "time": str(os.getenv("DAILY_PNL_REPORT_TIME", "15:10")),
        "interval": max(10, int(os.getenv("DAILY_PNL_REPORT_INTERVAL_SEC", "60"))),
    }


def parse_report_time(raw: str) -> tuple[int, int]:
    """解析 ``HH:MM``，非法值回落 15:10（结算 finalize 15:05 之后）。"""
    try:
        hour, minute = str(raw).strip().split(":", 1)
        h, m = int(hour), int(minute)
        if 0 <= h < 24 and 0 <= m < 60:
            return h, m
    except (TypeError, ValueError):
        pass
    return 15, 10


def _redis_client(redis) -> object | None:
    return getattr(redis, "client", None) if redis is not None else None


# ---------- 报表组装（纯函数） ----------


def _fmt_money(value: float) -> str:
    return f"¥{float(value):,.2f}"


def _fmt_signed_money(value: float) -> str:
    rounded = round(float(value), 2)
    sign = "-" if rounded < 0 else "+"
    return f"{sign}¥{abs(rounded):,.2f}"


def _fmt_signed_pct(value: float) -> str:
    rounded = round(float(value), 2)
    sign = "-" if rounded < 0 else "+"
    return f"{sign}{abs(rounded):.2f}%"


def _format_reason_counts(counts: dict, limit: int = 5) -> str:
    """``{"symbol:reason": n}`` → ``reason×n、reason×n``（按原因聚合、Top-N）。

    ``reason`` 自身可能带冒号（``broker_not_qmt_exec:tdx_bridge``），故只按**第一个**
    冒号切符号段；切不出结构（无冒号）的字段名原样当原因——宁可显示得糙，不可静默丢。
    """
    by_reason: dict[str, int] = {}
    for field, n in (counts or {}).items():
        name = str(field)
        reason = name.split(":", 1)[1] if ":" in name else name
        try:
            by_reason[reason] = by_reason.get(reason, 0) + int(n)
        except (TypeError, ValueError):
            continue
    top = sorted(by_reason.items(), key=lambda kv: (-kv[1], kv[0]))[:limit]
    return "、".join(f"{reason}×{n}" for reason, n in top)


def _mirror_block(mirror: dict) -> str:
    """镜像台账段（P2-2）：跳过/失败计数 + 原因 Top-N（失败带对账提示）。"""
    skipped = int(mirror.get("skipped_total") or 0)
    failed = int(mirror.get("failed_total") or 0)
    lines = ["**真单镜像 · 当日**", f"跳过 {skipped} 笔 ｜ 失败 {failed} 笔"]
    if skipped:
        lines.append(f"跳过原因：{_format_reason_counts(mirror.get('skipped') or {})}")
    if failed:
        lines.append(
            f"失败原因：{_format_reason_counts(mirror.get('failed') or {})}"
            "（模拟已成交而真单未成，缺口以双轨对账为准）"
        )
    return "\n".join(lines)


def build_report(
    channels: list[dict], day: date, mirror: dict | None = None
) -> tuple[str, str] | None:
    """组装 (title, content)；无任何通道数据返回 None（调用方据此不发送）。

    ``mirror`` 为 :func:`collect_mirror_stats` 的当日镜像统计（无事件 = None）。
    """
    if not channels:
        return None
    title = f"收盘收益 · {day.isoformat()}"
    blocks: list[str] = []
    for c in channels:
        pnl, pct = c.get("day_pnl"), c.get("day_pnl_pct")
        if pnl is None or pct is None:
            # 分母不可得显示 —（0 的语义是"打平"，不许冒充缺失）
            pnl_line = f"总资产 {_fmt_money(c['total_asset'])} ｜ 当日 —"
        else:
            pnl_line = (
                f"总资产 {_fmt_money(c['total_asset'])} ｜ 当日 "
                f"{_fmt_signed_money(pnl)}（{_fmt_signed_pct(pct)}）"
            )
        blocks.append(
            "\n".join(
                [
                    f"**{c['label']}**",
                    pnl_line,
                    f"持仓 {int(c.get('position_count') or 0)} 只 ｜ "
                    f"现金 {_fmt_money(c.get('cash') or 0.0)} ｜ "
                    f"市值 {_fmt_money(c.get('market_value') or 0.0)}",
                ]
            )
        )
    if len(channels) >= 2:
        total = sum(float(c["total_asset"]) for c in channels)
        pnls = [c.get("day_pnl") for c in channels]
        total_line = f"两账户合计 {_fmt_money(total)}"
        if all(p is not None for p in pnls):
            sum_pnl = sum(pnls)
            total_line += f" ｜ 当日 {_fmt_signed_money(sum_pnl)}"
            base_sum = sum(
                float(c["total_asset"]) - float(c.get("day_pnl") or 0.0)
                for c in channels
                if c.get("day_pnl_pct") is not None
            )
            if base_sum > 0 and all(c.get("day_pnl_pct") is not None for c in channels):
                total_line += f"（{_fmt_signed_pct(sum_pnl / base_sum * 100.0)}）"
        blocks.append(total_line)
    if mirror and (
        int(mirror.get("skipped_total") or 0) or int(mirror.get("failed_total") or 0)
    ):
        blocks.append(_mirror_block(mirror))
    return title, "\n\n".join(blocks)


# ---------- 数据收集 ----------


async def collect_day_channels(db, day: date) -> list[dict]:
    """读实盘两通道**当日**台账行（家族读，与账户页同口径）。

    当日行不存在（节假日 / 桥停更）或总资产为 0 → 该通道不进报表。
    """
    from backend.shared.simulation_account_keys import resolve_db_account_user

    from .real_account_ledger_service import (
        list_real_account_daily_ledgers_by_family,
        resolve_daily_pnl_pct,
    )

    user = resolve_db_account_user(ENV_ACCOUNT_USER)
    channels: list[dict] = []
    for key, label in REAL_ACCOUNT_CHANNELS:
        rows = await list_real_account_daily_ledgers_by_family(
            db,
            tenant_id="default",
            user_id=user,
            account_id=f"{key}-default-{user}",
            days=_LOOKBACK_DAYS,
        )
        row = next((r for r in rows if r.snapshot_date == day), None)
        if row is None:
            continue
        total = float(row.total_asset or 0.0)
        if total <= 1e-8:
            logger.warning(
                "[DailyPnlReport] %s 当日台账总资产为 0（空快照/读错），跳过", key
            )
            continue
        base = float(row.day_open_equity or 0.0)
        channels.append(
            {
                "key": key,
                "label": label,
                "total_asset": total,
                "cash": float(row.cash or 0.0),
                "market_value": float(row.market_value or 0.0),
                "position_count": int(row.position_count or 0),
                "day_pnl": round(total - base, 2) if base > 0 else None,
                "day_pnl_pct": resolve_daily_pnl_pct(
                    total_asset=total, day_open_equity=base
                ),
                "source": str(getattr(row, "source", "") or ""),
            }
        )
    return channels


def collect_mirror_stats(redis, day: date) -> dict | None:
    """当日真单镜像台账（P2-2）：跳过/失败计数，供报表出镜像段。

    读侧走 ``real_mirror_service.load_skips`` / ``load_failures``（两本台账的
    唯一出处，与双轨对账同一口径）。**两本账都空 → None**：当日无镜像事件，
    报表不出镜像段——空段会被读成「镜像在跑且一切正常」，而「镜像根本没跑」
    这个问题归决策审计行的 mirror 段回答，不归日报。
    """
    from backend.services.live_trading.services.real_mirror_service import (
        load_failures,
        load_skips,
    )

    date_str = day.strftime("%Y%m%d")
    skips = load_skips(redis, date_str)
    failures = load_failures(redis, date_str)
    if not skips and not failures:
        return None
    return {
        "skipped": skips,
        "failed": failures,
        "skipped_total": int(sum(skips.values())),
        "failed_total": int(sum(failures.values())),
    }


# ---------- 发送 ----------


def _save_report(redis, date_str: str, title: str, content: str) -> None:
    client = _redis_client(redis)
    if client is None:
        return
    try:
        client.set(
            _REPORT_KEY.format(date=date_str),
            json.dumps(
                {
                    "date": date_str,
                    "title": title,
                    "content": content,
                    "generated_at": datetime.now(TZ).isoformat(timespec="seconds"),
                },
                ensure_ascii=False,
            ),
            ex=_REPORT_TTL_SECONDS,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("[DailyPnlReport] 报表写入失败: %s", exc)


# ---------- 通知面登记（审计 H11） ----------

#: 结果 → 通知等级：已送达 success / 未送达 error / 无数据 warning。
_REGISTER_LEVELS = {"sent": "success", "unsent": "error", "nodata": "warning"}


def _claim_registration(redis, date_str: str, outcome: str) -> bool:
    """抢占当日该结果的登记权（SET NX）。Redis 不可用不拦截——痕迹优先于去重。"""
    client = _redis_client(redis)
    if client is None:
        return True
    try:
        return bool(
            client.set(
                _REGISTERED_KEY.format(date=date_str, outcome=outcome),
                "1",
                ex=_REPORT_TTL_SECONDS,
                nx=True,
            )
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("[DailyPnlReport] 登记去重键写入失败（放行）: %s", exc)
        return True


def _release_registration(redis, date_str: str, outcome: str) -> None:
    """登记未成功（库不可达/无管理员）时释放占位，让下个周期可补登记。"""
    client = _redis_client(redis)
    if client is None:
        return
    try:
        client.delete(_REGISTERED_KEY.format(date=date_str, outcome=outcome))
    except Exception as exc:  # noqa: BLE001
        logger.warning("[DailyPnlReport] 登记去重键释放失败: %s", exc)


async def _register_delivery(
    redis, date_str: str, *, outcome: str, title: str, content: str
) -> bool:
    """把当日发送结果登记进站内通知面（管理员 fanout，通知中心可查）。

    审计 H11：报表送达与否此前只能看 QQ 侧 / done 键，通知面无痕。已送达
    （``qq_alert=False``——报表本身已走 QQ，不再二次推送）、未送达（error，
    QQ 旁路把失败告警到手机）、无数据（warning，桥停更日通知面不再空白）
    三种结果各落一条；每（日, 结果）至多一条——未送达时任务每周期重试，
    逐次登记会把通知中心刷屏。登记是旁路：任何失败只记日志、释放占位，
    绝不回冲发送主链。
    """
    if not _claim_registration(redis, date_str, outcome):
        return False
    delivered = 0
    try:
        from backend.shared import notification_publisher as np

        delivered, audience = await np.publish_notification_to_admins_async(
            title=title,
            content=content,
            type="trading",
            level=_REGISTER_LEVELS[outcome],
            qq_alert=outcome == "unsent",
        )
        if audience == 0:
            logger.warning("[DailyPnlReport] 无管理员用户可登记: %s", title)
    except Exception as exc:  # noqa: BLE001 - 登记失败不回冲主链
        logger.warning("[DailyPnlReport] 通知面登记失败: %s", exc)
    if delivered <= 0:
        _release_registration(redis, date_str, outcome)
        return False
    logger.info("[DailyPnlReport] 已登记进通知面（%s）: %s", outcome, title)
    return True


async def run_daily_pnl_report(redis, *, today: date | None = None, db=None) -> dict:
    """生成并推送当日收盘收益报表。

    ``sent`` 只有 QQ 明确送达才为 True——调用方据此决定是否落 done 标记；
    无台账行返回 ``skipped="no_ledger_rows"``（不发送、不算失败，稍后重试）。
    """
    day = today or datetime.now(TZ).date()
    date_str = day.strftime("%Y%m%d")
    try:
        if db is None:
            from backend.shared.database_manager_v2 import get_session

            async with get_session(read_only=True) as session:
                channels = await collect_day_channels(session, day)
        else:
            channels = await collect_day_channels(db, day)
    except Exception as exc:  # noqa: BLE001 - 读失败按"没有数据"重试，不算发送
        logger.error("[DailyPnlReport] 台账读取失败: %s", exc, exc_info=True)
        return {"date": date_str, "sent": False, "error": f"ledger_read_failed: {exc}"}

    # 镜像台账（P2-2）：读失败只是缺一段增量信息，**不拖垮报表**（与台账读的
    # fail-closed 相反——那份是报表本体，这份是旁注）。
    mirror = None
    if channels:
        try:
            mirror = collect_mirror_stats(redis, day)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[DailyPnlReport] 镜像台账读取失败（报表照发）: %s", exc)

    built = build_report(channels, day, mirror=mirror)
    if built is None:
        # 无数据不是失败、也不算送达，但桥停更日通知面不该依旧空白（H11）
        await _register_delivery(
            redis,
            date_str,
            outcome="nodata",
            title=f"收盘收益报表无数据 · {day.isoformat()}",
            content=(
                "到点仍无当日实盘台账行，报表未发送；数据迟到仍会补发（过 0 点作废）。"
                "若持续无行，先查桥是否在写 real_account_ledger_daily_snapshots"
                "（结算 finalize 应在 15:05 前完成）。"
            ),
        )
        return {"date": date_str, "sent": False, "skipped": "no_ledger_rows"}

    title, content = built
    _save_report(redis, date_str, title, content)
    sent = False
    try:
        # notify 为同步 HTTP（8s 超时），放线程里跑，不占交易事件循环
        sent = bool(await asyncio.to_thread(qq_notify.notify, title, content))
    except Exception as exc:  # noqa: BLE001 - notify 自带全兜底，这里只防意外
        logger.warning("[DailyPnlReport] QQ 推送异常: %s", exc)
    logger.info(
        "[DailyPnlReport] %s 报表 %s（通道：%s）",
        date_str,
        "已推送" if sent else "未送达，稍后重试",
        ",".join(c["key"] for c in channels),
    )
    # 送达/未送达都登记进通知面（H11），每（日, 结果）至多一条
    await _register_delivery(
        redis,
        date_str,
        outcome="sent" if sent else "unsent",
        title=f"收盘收益报表{'已送达' if sent else '未送达'} · {day.isoformat()}",
        content=(
            content
            if sent
            else f"{content}\n\nQQ 推送未成功（每周期重试，送达后补登「已送达」）。"
        ),
    )
    return {
        "date": date_str,
        "sent": sent,
        "channels": [c["key"] for c in channels],
    }


# ---------- 常驻任务 ----------


async def _is_trading_day(day: date) -> bool:
    """交易日判定（与 ``risk_tier_producer`` 同口径：日历不可用按工作日近似）。

    台账行本身就是"有数据"的最终判据；这里的日历闸门只用来**挡节假日**——
    桥在节假日会写平值 stub 行，不挡的话会推一条 +0.00% 的空报表。
    """
    try:
        from backend.shared.trading_calendar import calendar_service

        return await calendar_service.is_trading_day(
            market="SSE", trade_date=day, tenant_id="default", user_id="0"
        )
    except Exception:  # noqa: BLE001
        return day.weekday() < 5


async def run_daily_pnl_report_task() -> None:
    """常驻循环：每交易日到点后跑一次，QQ 送达才落 Redis done 标记。"""
    cfg = _config()
    if not cfg["enabled"]:
        logger.info(
            "[DailyPnlReport] 收盘收益报表任务关闭（DAILY_PNL_REPORT_ENABLED=0）"
        )
        return

    from backend.services.trade_shared.deps import get_redis

    target_h, target_m = parse_report_time(cfg["time"])
    logger.info(
        "[DailyPnlReport] 收盘收益报表任务启动：每交易日 %02d:%02d 推送",
        target_h,
        target_m,
    )
    last_error = ""
    trading_day_memo: tuple[date, bool] | None = None
    while True:
        try:
            now = datetime.now(TZ)
            today = now.date()
            if (now.hour, now.minute) >= (target_h, target_m):
                if trading_day_memo is None or trading_day_memo[0] != today:
                    trading_day_memo = (today, await _is_trading_day(today))
                if trading_day_memo[1]:
                    redis = get_redis()
                    client = _redis_client(redis)
                    done_key = _DONE_KEY.format(date=today.strftime("%Y%m%d"))
                    already = False
                    if client is not None:
                        try:
                            already = bool(client.exists(done_key))
                        except Exception:  # noqa: BLE001
                            already = False
                    if not already:
                        result = await run_daily_pnl_report(redis, today=today)
                        # 只有 QQ 送达才落 done；未送达/无数据下一周期继续试
                        if result.get("sent") and client is not None:
                            try:
                                client.set(done_key, "1", ex=3 * 24 * 3600)
                            except Exception:  # noqa: BLE001
                                pass
            last_error = ""
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            message = str(exc)
            if message != last_error:
                logger.error("[DailyPnlReport] 任务异常: %s", exc, exc_info=True)
                last_error = message
        await asyncio.sleep(cfg["interval"])
