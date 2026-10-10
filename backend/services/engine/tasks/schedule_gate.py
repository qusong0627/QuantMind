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

from datetime import datetime


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
