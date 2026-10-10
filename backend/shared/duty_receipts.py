"""P2-5 值班回执键空间——生产者（trade 侧）与死手检查（celery 侧）的唯一契约。

死手检查住在 celery worker（**另一个进程树**，trade 整体死亡时它还能响），它核对
的每一项都是 Redis 里一把「这件事已经发生过」的回执键。键字符串一旦两侧各写
一份必然漂移——漂移的表现是「回执明明写了、死手却报缺失」，是最难查的一类假
故障。所以全部键构造收敛到本模块；其中收盘报表两把键是 P2-5 之前就存在的
（``daily_pnl_report_task`` 的 ``_DONE_KEY`` / ``_REGISTERED_KEY``），本模块按
**逐字一致**复刻，`test_duty_receipts.py` 有交叉断言钉死两侧。

回执清单（死手检查核对）：
- ``pnl_report``：15:10 收盘报表。done 键 = QQ 已送达；``registered:…:nodata``
  = 当日无数据（合法终态）；``registered:…:unsent`` = 送了没成（死手要报）。
- ``duty_summary``：15:40 值班摘要。done 键 = 已推送（P2-5 新增）。
- ``stall_check``：决策轮「整天没跑」检查已执行（值 = 检查结论 JSON；
  ``decision_round_runner._stall_watch`` 写）。
- 死手自己的 done / alerted 键：全部核对通过落 done（当日封账）；alerted 存
  「已告警过的缺失集」（集合收缩不重复响、补交后发恢复）。
"""

from __future__ import annotations

from datetime import date

#: 决策轮停滞检查回执（值 = JSON 结论，见 ``decision_round_io.write_stall_check``）。
STALL_CHECK_KEY_PREFIX = "trade:decision-round:stall-check"
STALL_CHECK_TTL_SECONDS = 7 * 24 * 3600


def stall_check_key(day: date) -> str:
    """停滞检查回执键（ISO 日期——与决策轮 log 条目的 ``day`` 字段同格式）。"""
    return f"{STALL_CHECK_KEY_PREFIX}:{day.isoformat()}"


# ── 收盘报表（P2-5 之前已存在；与 daily_pnl_report_task 逐字一致，改则双改）──


def pnl_report_done_key(day: date) -> str:
    return f"trade:daily-pnl-report:done:{day.strftime('%Y%m%d')}"


def pnl_report_registered_key(day: date, outcome: str) -> str:
    return f"trade:daily-pnl-report:registered:{day.strftime('%Y%m%d')}:{outcome}"


# ── 值班摘要（P2-5 新增；写入方 = duty_summary 任务）──

#: 摘要全文 JSON 的留存键（TTL 30 天，人查）。
DUTY_SUMMARY_TTL_SECONDS = 30 * 24 * 3600
#: done 键 TTL：3 天（死手只核对当日；隔日回看走全文键）。
DUTY_SUMMARY_DONE_TTL_SECONDS = 3 * 24 * 3600


def duty_summary_key(day: date) -> str:
    return f"trade:duty-summary:{day.strftime('%Y%m%d')}"


def duty_summary_done_key(day: date) -> str:
    return f"trade:duty-summary:done:{day.strftime('%Y%m%d')}"


def duty_summary_registered_key(day: date, outcome: str) -> str:
    return f"trade:duty-summary:registered:{day.strftime('%Y%m%d')}:{outcome}"


# ── 死手检查自身（写入方 = duty_deadman 任务）──

#: 全部核对通过 → 当日封账（幂等短路；次日换键）。
DEADMAN_DONE_TTL_SECONDS = 7 * 24 * 3600


def deadman_done_key(day: date) -> str:
    return f"trade:duty-deadman:done:{day.strftime('%Y%m%d')}"


def deadman_alerted_key(day: date) -> str:
    """已告警缺失集（JSON list）。缺失集只收缩：集不变不重复响；空集+曾告警 → 恢复。"""
    return f"trade:duty-deadman:alerted:{day.strftime('%Y%m%d')}"
