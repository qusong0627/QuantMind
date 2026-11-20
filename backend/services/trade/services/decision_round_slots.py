"""决策轮槽位契约（从 ``decision_round_core`` 拆出，2026-11-20）。

纯逻辑：槽位表、到点判定、状态键；无 IO。拆出原因：core 顶到 800 行上限。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime
from zoneinfo import ZoneInfo

from backend.shared.decision.contract import SCHEMA_INTRADAY, SCHEMA_REBALANCE

CST = ZoneInfo("Asia/Shanghai")
DEFAULT_GRACE_MIN = 45
SLOT_KEY_PREFIX = "trade:decision-round:slot"
DONE_KEY_PREFIX = "trade:decision-round:done"

# ── 槽位表 ───────────────────────────────────────────────────────────
@dataclass(frozen=True, slots=True)
class RoundSlot:
    """一个到点要跑的轮次（北京墙钟）。"""

    hhmm: str
    schema: str
    catch_up: bool = False

    def __post_init__(self) -> None:
        if len(self.hhmm) != 4 or not self.hhmm.isdigit():
            raise ValueError(f"槽位时刻必须是 4 位 HHMM，收到 {self.hhmm!r}")
        if self.schema not in (SCHEMA_REBALANCE, SCHEMA_INTRADAY):
            raise ValueError(f"未知 schema={self.schema!r}")

    @property
    def hour(self) -> int:
        return int(self.hhmm[:2])

    @property
    def minute(self) -> int:
        return int(self.hhmm[2:])

    @property
    def label(self) -> str:
        return f"{self.hhmm[:2]}:{self.hhmm[2:]}"

    def due_at(self, day: date) -> datetime:
        """该槽位当日的到点时刻（aware，北京时间）。"""
        return datetime(
            day.year, day.month, day.day, self.hour, self.minute, tzinfo=CST
        )


#: 槽位表（顺序即执行顺序；同 tick 多槽按时间跑）。
SLOTS: tuple[RoundSlot, ...] = (
    RoundSlot("0830", SCHEMA_INTRADAY),
    RoundSlot("0900", SCHEMA_INTRADAY),
    RoundSlot("0935", SCHEMA_REBALANCE),
    RoundSlot("1000", SCHEMA_INTRADAY),
    RoundSlot("1005", SCHEMA_REBALANCE, catch_up=True),
    # 10:15：10:10 全市场快照刚出；原槽位不删。
    RoundSlot("1015", SCHEMA_INTRADAY),
    RoundSlot("1100", SCHEMA_INTRADAY),
    RoundSlot("1105", SCHEMA_REBALANCE, catch_up=True),
    RoundSlot("1200", SCHEMA_INTRADAY),
    RoundSlot("1300", SCHEMA_INTRADAY),
    RoundSlot("1400", SCHEMA_INTRADAY),
    RoundSlot("1445", SCHEMA_INTRADAY),
)
SLOTS_BY_HHMM: dict[str, RoundSlot] = {s.hhmm: s for s in SLOTS}


def round_id_for(day: date, slot: RoundSlot) -> str:
    """``rnd-{YYYYMMDD}-{HHMM}``（进腿幂等键的 round 段）。"""
    return f"rnd-{day:%Y%m%d}-{slot.hhmm}"


# ── 纯函数：到点判定与位置戳 ─────────────────────────────────────────
def due_slots(
    now: datetime,
    *,
    grace_min: int = DEFAULT_GRACE_MIN,
    slots: Sequence[RoundSlot] = SLOTS,
) -> tuple[RoundSlot, ...]:
    """当前到点且在宽限窗内的槽位（按时刻升序；空 = 本轮无事）。

    晚于 ``due_at + grace_min`` 的槽位**不再补**（见 ``DEFAULT_GRACE_MIN``）。
    """
    out: list[RoundSlot] = []
    for slot in slots:
        due = slot.due_at(now.date())
        if due <= now and (now - due).total_seconds() <= grace_min * 60:
            out.append(slot)
    return tuple(sorted(out, key=lambda s: s.hhmm))


def slot_keys(day: date, slot: RoundSlot, agent: str = "") -> tuple[str, str]:
    """``(槽位键, 当日该 schema 的 done 键)``；``agent`` 非空时两把键都带家段。

    家段（P2.9）不是装饰：这两个键回答的是「这一槽谁在跑 / 今天跑过了没」，而多模型
    名册下**每家各自跑一轮**。不带家段时第一家跑完就把第二家挡在认领键外、
    或让补跑槽读到「今天出过了」——状态键上一切正常，另外几家整天空转（迁移计划里
    「迁完只剩一个模型」那条警告的机制）。

    空 ``agent`` **逐字保留历史键形**：升级那一刻名册还没开，若键形变了，`claim` 与
    `done` 会集体查无此人 ⇒ 当天补跑槽把已经出过决策的槽**再跑一遍真钱**。
    """
    seg = f":{agent}" if agent else ""
    return (
        f"{SLOT_KEY_PREFIX}:{day:%Y%m%d}:{slot.hhmm}:{slot.schema}{seg}",
        f"{DONE_KEY_PREFIX}:{day:%Y%m%d}:{slot.schema}{seg}",
    )


