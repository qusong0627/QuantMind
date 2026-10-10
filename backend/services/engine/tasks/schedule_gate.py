"""到点判据唯一实现（2026-10-10 审计 H4）。

celery beat 每分钟派发 tick，但派发任务在 worker 里执行——worker 忙过一次
60 秒（重任务/长事务/GC），配置的那一分钟就没有 tick 跑到判据：

* 旧判据 ``cfg.time == now``（精确分钟相等）：错过即**当天整班静默跳发**，
  而调度器 docstring 承诺的「下一分钟重试」只在「分钟恰好相等」时成立——
  承诺是假的（H4「假补跑承诺」）。
* 本模块语义：**早于配置时刻才跳过**（``now >= time`` 即到点）。迟到分钟/
  小时都仍是「当日首次到点」，由各调度器现有的 ``last_run:{...}:{day}``
  日键保证至多一次（键在派发成功之后写，见
  ``market_sync_scheduler.dispatch_due_syncs`` 的标记纪律）。

HH:MM 解析成 ``(时, 分)`` 元组比较，不接受字符串比较：``_normalize`` 用
``strptime("%H:%M")`` 校验但**原样保留**用户输入，``"4:30"`` 这种非补零写法
是合法配置，而 ``"04:35" < "4:30"`` 按字符串为真——会把它误判为「未到点」。
"""

from __future__ import annotations

from datetime import date, datetime, timedelta


def not_due_yet(now: datetime, cfg_time: str) -> bool:
    """``now`` 是否**早于**当日配置时刻（HH:MM）。

    解析失败返回 ``True``（宁可不跑）：配置写入侧 ``_normalize`` 已把非法值
    回退到默认时刻，真正带病到这里只能是绕过了校验的脏 Redis 值——对一个
    看不懂的时刻，「不跑」不会错发，「跑」可能把凌晨任务按白天数据执行。
    """
    try:
        hh_s, mm_s = str(cfg_time).strip().split(":")
        hh, mm = int(hh_s), int(mm_s)
    except (TypeError, ValueError):
        return True
    return (now.hour, now.minute) < (hh, mm)


# ── 交易日历门（P2-3 / 审计 M7）────────────────────────────────────────
#: 同步/填充市场 → 交易日历口径（``trading_calendar.is_trading_day_xcal`` 的
#: 入参）。``None`` = 无日历门（全天候市场）。FUTURES 用 CFFEX（XSHG 历）：
#: 国内期货与 A 股同一套法定节假日（夜盘只改变「一夜算哪个交易日」，
#: 不改变「哪些自然日有交易」）；CUSTOM 重建的是 A 股派生数据集，同 A 股口径。
_SYNC_MARKET_CALENDAR: dict[str, str | None] = {
    "A": "CN",
    "CUSTOM": "CN",
    "FUTURES": "CFFEX",
    "HK": "HK",
    "US": "US",
    "BC": None,
}


def _probe_trading_day(calendar_market: str, day: date) -> bool | None:
    """单个自然日的交易日探针；``None`` = 日历答不了（库缺失/越界）。

    实现唯一（``trading_calendar.is_trading_day_xcal``，同步语境的正规出口）；
    单独成函数只为测试可换替身。延迟 import：调度模块的 import 面不带 DB。
    """
    from backend.shared.trading_calendar import is_trading_day_xcal

    return is_trading_day_xcal(calendar_market, day)


def no_data_window(market: str, now: datetime) -> str | None:
    """「今日与昨日都不是交易日」→ 跳过原因；否则 ``None``（照常派发）。

    语义（M7 的「过夜同步取上一交易日数据」微妙点）：这些调度时刻按平台约定
    都排在次日凌晨（建议值 01:00–07:30）——D 日凌晨跑的同步取的是 **D-1 那个
    自然日收盘**的数据。所以闸门问的不是「今天是不是交易日」：

    * D-1 是交易日（含**周六凌晨取周五**）→ 必须跑；
    * D 是交易日（当天傍晚/夜里的配置）→ 必须跑；
    * 只有 {今日, 昨日} **两个自然日都非交易日**（周日、长假内部日）才是
      「不可能有新数据」的空转，跳过。

    若按「今天不是交易日就跳」，周六凌晨取周五收盘的那班会被拦掉——周五的
    数据要等到周一凌晨才落，这正是要避免的假省。

    日历答不了 / 市场无日历门（BC 全天候）→ ``None``（放行）：与取数闸
    「未知即放行」同哲学——这个闸只许省掉注定为空的空跑，绝不许拦掉可能
    有新数据的班次。DB 手工覆盖（``qm_market_calendar_day``）不参与本闸：
    临时交易日请走界面手动触发同步（已知边界，见 P2-3 执行记录）。
    """
    calendar_market = _SYNC_MARKET_CALENDAR.get(str(market or "").upper())
    if not calendar_market:
        return None
    today = now.date()
    yesterday = today - timedelta(days=1)
    probes = [_probe_trading_day(calendar_market, d) for d in (yesterday, today)]
    if any(v is True for v in probes):
        return None
    if all(v is False for v in probes):
        return (
            f"连续非交易日（{yesterday.isoformat()}、{today.isoformat()} 均非交易日），"
            "无新数据可同步"
        )
    return None
