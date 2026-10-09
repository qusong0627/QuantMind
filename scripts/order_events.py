#!/usr/bin/env python3
"""委托事件的唯一读侧口径：还要不要告警 / 人工签收 / 账本自动消解。

写侧是 live_fills.record_event → data/live_order_events.json，
id = "<kind>:<code>:<北京日期>"，写入时按 EVENT_KEEP_H 惰性清理旧事件。
本模块提供读侧的全部语义，**只用标准库**（alert.sh 调的是系统 /usr/bin/python3，
见 alert_checks.py 同一约束；网络/venv 依赖一个都不许有）。

背景（2026-09-20）：周五两个止损卖单失败事件在周六/周日北京 23:00 被重播两晚。
根因一在 alert.sh（按日去重键是裸 `date +%F`，主机 JST 提前 1 小时翻日，已修）；
根因二的形态是：事件一旦写上就没有「已解决」出口——写侧 24h 惰性清理只在
下一次写入时生效，周末没有写入，事件就永远停在告警面上。

一条事件「不再告警」有四种理由（判定优先级从高到低）：

  1. resolved —— 人工 ack（resolved_ts / resolved_by / resolved_reason 落盘，
     并追加到 live_order_events_ack.jsonl 只增不删的审计尾巴）
  2. quiet    —— alert=False 或空文案（只留痕的审计尾巴，如 watch_discard）
  3. auto     —— 卖域事件且账本已无该标的持仓（"这笔卖出没做成"的意图已消失）
  4. expired  —— 超过 EVENT_KEEP_H（与写侧清理同一常量，单一口径）

自动消解只覆盖「这笔卖出没做成」的卖域事件（unfilled(side=sell) / sell_failed /
dup_unresolved / refire）。**只要还有任一 agent 持有该 code 就继续告警**——账本
写着还有持仓，卖失败事件就必须继续有人看（2026-09-11 教训：止损没卖出去而持仓
裸奔一整天，零告警）。**no_order_id / outcome_unknown 不自动消解**：这两条的
前提就是「账本不可信（成交可能未记账）」，不能反过来拿账本推断已解决——它们的
出口是人工核对后的 ack。账本缺档/结构不认识时一律禁用自动消解（宁多扰勿漏报）。

红线：告警路径（active_events / order_event_lines）对数据文件**只读**；
唯一的写入口是 ack()，且与 live_fills.record_event 共用同一把 flock
（data/live_reconcile.lock），读-改-写不许互相覆盖。

CLI（运维用）：
  /usr/bin/python3 scripts/order_events.py list data/live_order_events.json [logs/live_ledger.json]
  /usr/bin/python3 scripts/order_events.py ack  data/live_order_events.json <id>... [--by 谁] [--reason 说明]
"""
from __future__ import annotations

import fcntl
import json
import os
import sys
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

# 中国无夏令时，固定 +08:00 即北京；不依赖 tzdata（系统 python3 上最稳）。
CN_TZ = timezone(timedelta(hours=8))

# 事件保留窗口（小时）——写侧惰性清理与读侧告警共用这一个值。
# 改它同时改变两侧行为：事件在告警面上最多活这么久。
EVENT_KEEP_H = 24

# ack 与 live_fills._file_lock 共用的锁文件名（同目录同约定，一致性由测试钉住）。
LOCK_NAME = "live_reconcile.lock"

# 签收审计尾巴：事件文件里的 resolved_* 会随保留窗口一起被清掉，
# 这里追加一份只增不删的记录（谁、何时、为什么签收了哪条事件）。
ACK_LOG_NAME = "live_order_events_ack.jsonl"

# 状态枚举（evaluate 的返回值）
ALERT = "alert"          # 仍需告警
RESOLVED = "resolved"    # 人工已签收
AUTO = "auto"            # 账本已无持仓，自动消解
EXPIRED = "expired"      # 超过保留窗口
QUIET = "quiet"          # alert=False / 空文案，只留痕

# 构造上只出现在卖出路径的 kind（side 可能是 ""，如 dup_unresolved 实录）。
# 注意 watch_halt（条件位卖出连续废单当日熔断）**故意不在此列**：它要的是人工确认
# 柜台为什么拒单（2026-09-21 审查 LOW-1），账本无持仓不能证明问题已处理——留在
# 告警面等人 ack，不做自动消解。
_SELL_ONLY_KINDS = {"sell_failed", "dup_unresolved", "refire"}

USAGE = (
    "usage: order_events.py list <events.json> [ledger.json] [--now ISO]\n"
    "       order_events.py ack  <events.json> <id>... [--by 谁] [--reason 说明] [--now ISO]"
)


def now_cn() -> datetime:
    return datetime.now(CN_TZ)


def parse_ts(s) -> datetime | None:
    """ISO 时间戳 → aware datetime；读不出/非字符串 → None（判不了年龄）。"""
    try:
        t = datetime.fromisoformat(str(s))
    except (TypeError, ValueError):
        return None
    return t if t.tzinfo else t.replace(tzinfo=CN_TZ)


def load_doc(path) -> dict:
    """读事件文件；缺失/损坏/不是字典 → {}（读侧绝不抛异常打断告警）。"""
    try:
        doc = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return doc if isinstance(doc, dict) else {}


def _is_expired(ts, now: datetime) -> bool:
    """超过 EVENT_KEEP_H → True。时间戳不可解析 → False（与写侧「不删」同向）。"""
    t = parse_ts(ts)
    return t is not None and t < now - timedelta(hours=EVENT_KEEP_H)


def prune_doc(doc: dict, now: datetime) -> dict:
    """写侧惰性清理（live_fills.record_event 调用）：按 EVENT_KEEP_H 丢旧条目。

    语义与老内联式 `(_parse_ts(ts) or cutoff) >= cutoff` 完全一致：
    不可解析的 ts 保留（宁可留一条读不懂的记录，也不悄悄丢）。
    """
    return {k: v for k, v in doc.items()
            if not (isinstance(v, dict) and _is_expired(v.get("ts"), now))}


def sell_domain(kind: str, side: str) -> bool:
    """这条事件是不是「某笔卖出没做成」域（自动消解的适用域）。"""
    if kind == "unfilled":
        return side == "sell"
    if kind in _SELL_ONLY_KINDS:
        return side != "buy"
    return False


def holdings_codes(ledger) -> set[str] | None:
    """账本 → 所有 agent 的持仓代码并集（volume>0）。

    返回 None = 账本不可用（缺档/结构不认识）→ 调用方禁用自动消解。
    空 agents 但带 version/applied_fills 的完整账本按「全员空仓」处理（合法形态）；
    两者都没有的空壳更像损坏文件，按不可用处理（宁可继续告警）。
    """
    if not isinstance(ledger, dict):
        return None
    agents = ledger.get("agents")
    if not isinstance(agents, dict):
        return None
    if not agents and "version" not in ledger and "applied_fills" not in ledger:
        return None
    held: set[str] = set()
    for state in agents.values():
        positions = state.get("positions") if isinstance(state, dict) else None
        if not isinstance(positions, dict):
            continue
        for code, pos in positions.items():
            vol = pos.get("volume") if isinstance(pos, dict) else None
            if isinstance(vol, (int, float)) and vol > 0:
                held.add(str(code))
    return held


def evaluate(ev, *, ledger=None, now: datetime | None = None) -> str:
    """单条事件 → ALERT / RESOLVED / QUIET / AUTO / EXPIRED（唯一判定入口）。"""
    now = now or now_cn()
    if not isinstance(ev, dict):
        return QUIET
    if ev.get("resolved_ts"):
        return RESOLVED
    if not ev.get("alert") or not str(ev.get("msg") or "").strip():
        return QUIET
    if sell_domain(str(ev.get("kind") or ""), str(ev.get("side") or "")):
        held = holdings_codes(ledger)
        if held is not None and str(ev.get("code") or "") not in held:
            return AUTO
    if _is_expired(ev.get("ts"), now):
        return EXPIRED
    return ALERT


def status_rows(doc, *, ledger=None, now: datetime | None = None) -> list[tuple[str, str, dict]]:
    """[(事件id, 状态, 事件), ...] 按时间升序（list CLI 与审计共用）。"""
    if not isinstance(doc, dict):
        return []
    rows = [(str(e.get("ts") or ""), str(k), evaluate(e, ledger=ledger, now=now), e)
            for k, e in doc.items() if isinstance(e, dict)]
    return [(k, st, e) for _, k, st, e in sorted(rows)]


def active_events(doc, *, ledger=None, now: datetime | None = None) -> list[tuple[str, dict]]:
    """仍需告警的事件 [(id, ev), ...]，按时间升序。**纯读**。"""
    return [(k, e) for k, st, e in status_rows(doc, ledger=ledger, now=now) if st == ALERT]


def order_event_lines(doc, *, ledger=None, now: datetime | None = None) -> list[str]:
    """仍需告警的事件 → ["<id>|<msg>", ...]（alert.sh 每行取一个事件）。**纯读**。"""
    return [f"{k}|{str(e.get('msg') or '')}"
            for k, e in active_events(doc, ledger=ledger, now=now)]


# ---------- 人工签收（唯一写入口） ----------

def lock_path_for(events_path) -> Path:
    """ack 的锁文件：与 live_fills.RECONCILE_LOCK_FILE 同目录同名（一致性由测试钉住）。

    record_event 是读-改-写，ack 也是读-改-写：不同锁 = 并发时互相覆盖。
    """
    return Path(events_path).with_name(LOCK_NAME)


@contextmanager
def _file_lock(lock_path: Path):
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fh = open(lock_path, "a+", encoding="utf-8")
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        fh.close()


def _atomic_write(path: Path, doc: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(path)


def ack(path, ids, *, by: str, reason: str, now: datetime | None = None) -> dict:
    """人工签收：写 resolved_ts/by/reason 审计字段。返回 {acked, already, missing}。

    幂等且不改写首次结论（审计完整性）：已签收的 id 进 already，不覆盖。
    一个 id 都没有签收时不落盘（文件字节不变）。签收成功的条目同时追加到
    <事件文件同目录>/live_order_events_ack.jsonl（只增不删：事件记录本身会随
    保留窗口被清理，审计要活得更久）。
    """
    now = now or now_cn()
    path = Path(path)
    res: dict[str, list[str]] = {"acked": [], "already": [], "missing": []}
    with _file_lock(lock_path_for(path)):
        doc = load_doc(path)
        for ev_id in ids:
            ev = doc.get(ev_id)
            if not isinstance(ev, dict):
                res["missing"].append(ev_id)
                continue
            if ev.get("resolved_ts"):
                res["already"].append(ev_id)
                continue
            doc[ev_id] = {**ev, "resolved_ts": now.isoformat(),
                          "resolved_by": str(by or ""), "resolved_reason": str(reason or "")}
            res["acked"].append(ev_id)
        if res["acked"]:
            _atomic_write(path, doc)
            try:  # 审计尾巴失败不推翻已落盘的签收：降级为 stderr 提醒
                with open(path.with_name(ACK_LOG_NAME), "a", encoding="utf-8") as fh:
                    for ev_id in res["acked"]:
                        fh.write(json.dumps({"acked_ts": now.isoformat(), "id": ev_id,
                                             "by": str(by or ""), "reason": str(reason or ""),
                                             "event": doc[ev_id]}, ensure_ascii=False) + "\n")
            except OSError as exc:
                print(f"⚠️ 签收已落盘，但审计日志写入失败：{exc}", file=sys.stderr)
    return res


# ---------- CLI ----------

def _split_flags(args: list[str]) -> tuple[list[str], dict]:
    pos, flags, i = [], {}, 0
    while i < len(args):
        a = args[i]
        if a.startswith("--"):
            flags[a[2:]] = args[i + 1] if i + 1 < len(args) else ""
            i += 2
        else:
            pos.append(a)
            i += 1
    return pos, flags


def _flag_now(flags: dict) -> datetime:
    raw = flags.get("now")
    if not raw:
        return now_cn()
    t = parse_ts(raw)
    if t is None:
        print(f"✗ --now 不是 ISO 时间：{raw}", file=sys.stderr)
        raise SystemExit(2)
    return t


def _fmt(ev_id: str, ev: dict) -> str:
    ts = str(ev.get("ts") or "")
    ts = ts[5:16].replace("T", " ") if len(ts) >= 16 else ts
    return f"{ev_id}  {ts}  {str(ev.get('msg') or '')}"


_LABELS = {ALERT: "告警", AUTO: "自动消解", EXPIRED: "已过期", RESOLVED: "已签收"}


def _cmd_list(pos: list[str], flags: dict) -> int:
    if not pos:
        print(USAGE, file=sys.stderr)
        return 2
    ledger = load_doc(pos[1]) if len(pos) > 1 else None
    rows = status_rows(load_doc(pos[0]), ledger=ledger, now=_flag_now(flags))
    counts = {st: sum(1 for _, s, _ in rows if s == st) for st in set(_LABELS) | {QUIET}}
    print(f"告警 {counts[ALERT]} 条｜自动消解 {counts[AUTO]}（账本已无该标的持仓）"
          f"｜已过期 {counts[EXPIRED]}｜已签收 {counts[RESOLVED]}"
          f"｜留痕 {counts[QUIET]} 条（alert=False，不告警）")
    for st in (ALERT, AUTO, EXPIRED, RESOLVED):
        for ev_id, s, ev in rows:
            if s == st:
                print(f"[{_LABELS[st]}] {_fmt(ev_id, ev)}")
    return 0


def _cmd_ack(pos: list[str], flags: dict) -> int:
    if len(pos) < 2:
        print(USAGE, file=sys.stderr)
        return 2
    res = ack(pos[0], pos[1:], by=flags.get("by") or os.environ.get("USER") or "manual",
              reason=flags.get("reason") or "人工核对后签收", now=_flag_now(flags))
    if res["acked"]:
        print(f"已签收 {len(res['acked'])} 条：" + "，".join(res["acked"]))
    if res["already"]:
        print(f"已签收过（跳过，保留首次结论）：{'，'.join(res['already'])}")
    if res["missing"]:
        print(f"✗ 不存在的事件 id：{'，'.join(res['missing'])}", file=sys.stderr)
        return 1
    return 0


def main(argv: list[str]) -> int:
    if not argv or argv[0] not in ("list", "ack"):
        print(USAGE, file=sys.stderr)
        return 2
    pos, flags = _split_flags(argv[1:])
    return _cmd_list(pos, flags) if argv[0] == "list" else _cmd_ack(pos, flags)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
