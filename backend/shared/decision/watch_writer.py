"""``watch`` 决策 → 止盈止损规则表（P2.4：IO 适配层，读-改-写）。

纯核心在 :mod:`backend.shared.decision.watch_map`（``plan_watch``），本层只做三件事：
**按归属整组替换**、**与人工规则共存**、**写不进去就说写不进去**。

为什么归属是规则表里的一个字段
------------------------------
隔壁 ``data/live_watch.json`` 是一张 ``{agent: [规则]}`` 的表——归属由**结构**给出。
本仓执行器的规则表是一张**平表**，而且 ``normalize_rule`` 只认 ``DEFAULT_RULE`` 词表里的
键（词表外的键静默丢弃）。所以归属要落地只有一条路：**进词表**
（``sltp_executor.DEFAULT_RULE["owner"]``），值 ``""`` = 人工、``"llm:<agent>"`` = 决策层某组。
另起一个 Redis 键记「谁挂了哪些标的」是不行的：那份索引和规则表之间没有事务，
人工在控制面删一条规则，索引就变成了不会自己纠正的谎言。

整组替换的边界（与人工规则共存）
--------------------------------
* **只动自己那一组**：本轮把 ``owner == 我`` 的规则全部换掉（本轮没 watch 就整组清空，
  与隔壁「最新分析说了算」同义），其余规则一条不碰；
* **同标的冲突时人工优先**：某个标的已有人工规则（或别家 agent 的规则），本轮的
  watch **不覆盖**它，改判为「未挂上」并写明理由。理由是失败模式不对称——自动化写者
  每轮都来，人工规则是偶发的一次性动作，让自动化静默删掉人工写的唯一一条保护，
  事后没人能回答「那条止损去哪了」。被顶掉的一律进审计表，看得见、可交还；
* **同标的多条只挂第一条**：本执行器的状态位**按标的唯一**（``state["rules"][symbol]``），
  同标的两条规则会共用同一个状态位——第二条挂上去也不会触发（状态已被第一条占成
  ``submitted``），那才是真正的静默丢失：模型以为两个价位都有人看着。故第二条明确拒绝。

写路径的 fail-closed
--------------------
本层是「读当前表 → 改 → 写回」，读失败绝不能当成「表是空的」——那等于把所有规则
（含人工的）一把抹掉。故：

1. 读走 ``executor.read_config_strict``（``RedisClient.get`` 会把读失败吞成 ``None``，
   正是它把「Redis 抖了」伪装成「没有规则」）；
2. 写过 ``save_config`` 之后**回读校验**：自己那组是否真的落库、人工那组是否一条没少；
3. 校验不过 → 本轮结论一律是「未挂上」+ 写明原因（回读说没落库的规则，审计表里
   不能记成 ``armed``）。

摘掉旧规则之后的状态清理
------------------------
规则摘了，``state["rules"][symbol]`` 还在（状态按标的存在，不含 owner）。清理口径对齐
CLI ``--rm``：只清**槽位已空**的标的（被别人接手的槽位不动——那是别人的状态），
且 ``status`` 是在途真单（``is_live_state``）时**不清**（清了等于丢掉这笔委托的跟踪与
终态通知）。重挂同一标的**不清**状态：与 CLI ``--arm`` / 控制面 PUT 同口径——当日已触发
过的标的要重新武装得走 ``POST /qmt-sltp/reset``，这是 P1.3「当日一次触发」的既有语义。

刻意不做的事
------------
* **不写审计表**：``outcomes()`` 给出与 ``decision_ledger_store.build_records(outcomes=…)``
  同形的映射，落库由轮次调度（P2.5）统一做——一张表一个写入者；
* **不定多 agent 的仲裁**：两个 agent 抢同一标的时，先挂的持有、后来的每轮可见地被拒。
  多模型竞争语义（谁该赢）是 P2.7 的事，本层不猜；
* **不写 ``invalidation``**：它不在执行器词表里（写进去会被静默丢掉），落库位是决策审计表
  （P2.1d）。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from backend.services.live_trading.services import sltp_executor as executor
from backend.shared.decision.watch_map import WatchPlan, WatchRule

logger = logging.getLogger(__name__)

#: 拒绝理由（进决策审计表的 ``reject_reason``，**每条都要能指认下一步动作**）
REJECT_NO_OWNER = (
    "agent 为空：无法确定规则归属，本轮不写规则（空 agent 会让所有无名写者共用一组）"
)
REJECT_MANUAL_HOLD = (
    "该标的已有人工规则（owner 为空）：人工优先，本轮 watch 不覆盖——"
    "要交还给决策层请删掉那条人工规则（qmt_sltp_ctl.py --rm <symbol>）"
)
REJECT_OTHER_AGENT = (
    "该标的已归属 {holder}：本执行器一个标的只有一个状态位，先挂的持有"
    "（多 agent 仲裁见 P2.7）"
)
REJECT_SAME_SYMBOL = (
    "本标的在本轮已挂过条件位（第 {first} 条）：同标的多条共用同一个状态位，"
    "第二条挂上去也不会触发，故不挂"
)
REJECT_NOT_LANDED = "写入后回读未确认落到规则表"

#: 控制面 API 的规则上限引用执行器（单源）：合并后超上限时**一条都不写**，
#: 因为规则表是「一个标的一个状态位」的平表，撑爆它之后人工改规则会被 API 拒收。
CAP_EXCEEDED = (
    "合并后规则数 {total} 超上限 {max_rules}（他方 {others} + 本轮 {mine}）：本轮不写"
)


@dataclass(frozen=True)
class Conflict:
    """本轮一条 watch 没能挂上（被别人占着 / 同标的多条）及其理由。"""

    index: int
    symbol: str
    reason: str
    holder: str = ""


@dataclass(frozen=True)
class WatchWriteResult:
    """一轮写入的结果（**结论、理由、留痕三件都在这**）。"""

    owner: str = ""
    plan: WatchPlan = field(default_factory=WatchPlan)
    #: 已落库并回读确认过的标的
    armed: tuple[str, ...] = ()
    #: 本轮整组替换摘掉、且状态已清理的旧标的
    removed: tuple[str, ...] = ()
    conflicts: tuple[Conflict, ...] = ()
    #: 落库意图存在但回读没确认到的 ``(标的, 具体原因)``（**不能记成 armed**）
    unverified: tuple[tuple[str, str], ...] = ()
    #: 轮次级失败（读不到 / 写不进 / 回读失败 / 超上限）：本轮结论一律「未挂上」
    errors: tuple[str, ...] = ()
    #: 回读校验发现的、**不属于某一条 watch** 的问题（他方规则被改写/消失、旧规则没被替换）
    problems: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        """**写入这一层**干成没干成：没失败、没「回读没确认到」、也没动到别人的规则。

        单看 ``errors`` 是不够的：写被静默丢弃（``RedisClient.set`` 吞异常）时失败
        是以「回读没确认到」的形式出现的，这时 ``errors`` 是空的——调用方要是拿
        ``ok`` 当「写成功了」，正好漏掉这一种。

        ``conflicts`` **刻意不进这个判断**：人工优先、别家先到先得、同标的多条——
        都是本层按既定规则做出的判断，不是「没写成」。要问「有没有哪条没挂上」看
        ``conflicts``（逐条带理由）与 ``outcomes()``（进审计表的那份）。
        """
        return not (self.errors or self.unverified or self.problems)

    def outcomes(self) -> dict[int, dict[str, Any]]:
        """序号 → 审计结果（``armed``/``reject_reason``/``notes``，与 P2.1d 契约同形）。

        以 :meth:`WatchPlan.outcomes` 为底（比例三态的注记原样保留），再按本层的事实改判：

        * 轮次级失败 → 本轮**每条** watch 改判「未挂上」（写不进的规则不许记成挂上了），
          但保留它自己的注记；
        * 冲突 / 回读未确认 → 只改判那一条，理由写具体（回读没确认的不许留白）。
        """
        out = dict(self.plan.outcomes())
        if self.errors:
            reason = "；".join(self.errors)
            for w in self.plan.rules:
                base = out.get(w.index) or {}
                out[w.index] = {
                    "armed": False,
                    "reject_reason": reason,
                    "notes": list(base.get("notes") or []),
                }
        for c in self.conflicts:
            base = out.get(c.index) or {}
            out[c.index] = {
                "armed": False,
                "reject_reason": c.reason,
                "notes": list(base.get("notes") or []),
            }
        for symbol, detail in self.unverified:
            for w in self.plan.rules:
                if w.symbol != symbol:
                    continue
                base = out.get(w.index) or {}
                out[w.index] = {
                    "armed": False,
                    "reject_reason": f"{REJECT_NOT_LANDED}：{detail}",
                    "notes": list(base.get("notes") or []),
                }
        return out

    def summary(self) -> dict[str, Any]:
        return {
            "owner": self.owner,
            "armed": list(self.armed),
            "removed": list(self.removed),
            "conflicts": [
                {"symbol": c.symbol, "holder": c.holder, "reason": c.reason}
                for c in self.conflicts
            ],
            "unverified": [{"symbol": s, "reason": r} for s, r in self.unverified],
            "errors": list(self.errors),
            "problems": list(self.problems),
            "notes": list(self.notes),
        }


def _resolve(
    plan: WatchPlan, others_by_symbol: dict[str, dict[str, Any]], owner: str
) -> tuple[list[tuple[WatchRule, dict[str, Any]]], list[Conflict]]:
    """计划 → ``(可挂的, 冲突)``（纯函数：喂给它的规则表就是全部输入）。

    落库形态用 ``normalize_rule`` 现算：**校验的、比对落库结果的是同一份**，
    避免「库里是 A、内存里比自己写的 B」这种对不上的比对。

    **归属在这里盖章**：``watch_map`` 按执行器词表造规则、不知道归属（也没法知道——
    词表外的键会被静默丢掉），所以 ``owner`` 一律由本层写成自己那组。少盖这一下，
    规则会以 ``owner=""`` 落库：下一轮看它是「人工规则」，按人工优先**再也不敢动**，
    决策层那组从此既替换不掉也删不掉（回读校验会先把它判成未落库）。
    """
    armed: list[tuple[WatchRule, dict[str, Any]]] = []
    conflicts: list[Conflict] = []
    seen: dict[str, int] = {}
    for w in plan.rules:
        rule = executor.normalize_rule(w.rule)
        rule["owner"] = owner
        symbol = str(rule.get("symbol") or "")
        holder = others_by_symbol.get(symbol)
        if holder is not None:
            holder_owner = str(holder.get("owner") or "")
            conflicts.append(
                Conflict(
                    index=w.index,
                    symbol=symbol,
                    holder=holder_owner,
                    reason=REJECT_MANUAL_HOLD
                    if not holder_owner
                    else REJECT_OTHER_AGENT.format(holder=holder_owner),
                )
            )
            continue
        if symbol in seen:
            conflicts.append(
                Conflict(
                    index=w.index,
                    symbol=symbol,
                    holder=owner,
                    reason=REJECT_SAME_SYMBOL.format(first=seen[symbol]),
                )
            )
            continue
        seen[symbol] = w.index
        armed.append((w, rule))
    return armed, conflicts


def _verify(
    after: dict[str, Any],
    *,
    owner: str,
    others: list[dict[str, Any]],
    armed: list[tuple[WatchRule, dict[str, Any]]],
    mine_old: list[str],
) -> tuple[list[tuple[str, str]], list[str]]:
    """回读校验 → ``(没确认到的 (标的, 原因), 轮次级问题)``。

    两侧都过 ``normalize_rule`` 再比：拿未归一的字典比，浮点/大小写/标的写法的差异
    会伪装成「写入被改写」。
    """
    unverified: list[tuple[str, str]] = []
    problems: list[str] = []
    table = {
        str(r.get("symbol") or ""): r for r in (after.get("rules") or [])
    }  # 回读结果（read_config_strict 已过 merge_config，规则是归一形态）
    want = {str(rule.get("symbol") or ""): rule for _, rule in armed}

    for symbol, rule in want.items():
        got = table.get(symbol)
        if got is None:
            unverified.append((symbol, "回读的表里没有这条规则"))
        elif str(got.get("owner") or "") != owner:
            unverified.append(
                (symbol, f"归属被改写成 {got.get('owner')!r}（期望 {owner!r}）")
            )
        elif executor.normalize_rule(got) != executor.normalize_rule(rule):
            unverified.append((symbol, "落库内容与写入内容不一致"))

    for before in others:
        symbol = str(before.get("symbol") or "")
        got = table.get(symbol)
        if got is None:
            problems.append(
                f"他方规则在写入后消失：{symbol}（owner={before.get('owner')!r}）"
            )
        elif executor.normalize_rule(got) != executor.normalize_rule(before):
            problems.append(f"他方规则在写入后被改写：{symbol}")

    for symbol in mine_old:
        if symbol not in want and symbol in table:
            problems.append(f"旧规则未被整组替换掉：{symbol}")
    return unverified, problems


def _clear_states(
    redis: Any, symbols: list[str], state_rules: dict[str, Any]
) -> tuple[list[str], list[str]]:
    """摘掉旧规则后清理状态 → ``(已清, 因在途而保留)``。"""
    if not symbols:
        return [], []
    live = [s for s in symbols if executor.is_live_state(state_rules.get(s))]
    cleared = [s for s in symbols if s not in live]
    if cleared:
        state = executor.load_state(redis)
        executor.save_state(redis, state, removed=set(cleared))
    return cleared, live


def write_watch_plan(
    redis: Any,
    plan: WatchPlan,
    *,
    agent: str,
    max_rules: int = executor.MAX_RULES,
) -> WatchWriteResult:
    """把一轮 ``watch`` 计划写成规则表的一张组（读-改-写，整组替换）。

    :param agent: 决策 agent 名（= 归属标记的取值来源）；空 = **拒绝写入**。
    :param max_rules: 合并后的表容量上限（默认执行器单源常量）。
    """
    owner = executor.llm_owner(agent)
    if not owner:
        return WatchWriteResult(plan=plan, errors=(REJECT_NO_OWNER,))

    try:
        cfg = executor.read_config_strict(redis)
    except Exception as exc:  # noqa: BLE001 - 读不到就**不写**，绝不当空表处理
        return WatchWriteResult(
            owner=owner, plan=plan, errors=(f"读取规则配置失败（本轮不写）：{exc}",)
        )

    existing = list(cfg.get("rules") or [])
    mine_old = [
        str(r.get("symbol") or "")
        for r in existing
        if str(r.get("owner") or "") == owner
    ]
    others = [r for r in existing if str(r.get("owner") or "") != owner]
    others_by_symbol = {str(r.get("symbol") or ""): r for r in others}

    armed, conflicts = _resolve(plan, others_by_symbol, owner)
    total = len(others) + len(armed)
    if total > max_rules:
        return WatchWriteResult(
            owner=owner,
            plan=plan,
            conflicts=tuple(conflicts),
            errors=(
                CAP_EXCEEDED.format(
                    total=total,
                    max_rules=max_rules,
                    others=len(others),
                    mine=len(armed),
                ),
            ),
        )

    wanted = others + [rule for _, rule in armed]
    try:
        executor.save_config(redis, {**cfg, "rules": wanted})
    except Exception as exc:  # noqa: BLE001
        return WatchWriteResult(
            owner=owner,
            plan=plan,
            conflicts=tuple(conflicts),
            errors=(f"写入规则配置失败：{exc}",),
        )

    # 写后回读：RedisClient.set 会静默吞掉写失败，不校验就只能「相信」
    try:
        after = executor.read_config_strict(redis)
    except Exception as exc:  # noqa: BLE001
        return WatchWriteResult(
            owner=owner,
            plan=plan,
            conflicts=tuple(conflicts),
            errors=(f"写入后回读失败，无法确认规则已落库：{exc}",),
        )

    unverified, problems = _verify(
        after, owner=owner, others=others, armed=armed, mine_old=mine_old
    )

    notes: list[str] = list(plan.notes)
    if not cfg.get("enabled"):
        notes.append(
            "止盈止损执行器总开关未开：规则已落库但不会触发（qmt_sltp_ctl.py --enable）"
        )
    if conflicts:
        notes.append(f"本轮 {len(conflicts)} 条 watch 未挂上（逐条理由见审计表）")
    # 轮次级问题必须进留痕（审计表的 notes 是唯一持久面）：他方规则被动过、
    # 旧规则没被替换掉——这些都不是某一条 watch 的事，但都是要有人看见的事。
    notes.extend(f"回读校验发现：{p}" for p in problems)

    # 状态：只处理「槽位已空」的标的（被别人接手的槽位不动），且在途真单不清
    final_symbols = {str(r.get("symbol") or "") for r in wanted}
    to_clear = [s for s in mine_old if s and s not in final_symbols]
    cleared: list[str] = []
    # None = 没读到 → 跳过一切状态判断（清不掉旧状态只是小事，按状态不对而误清才是大事）
    state_rules: dict[str, Any] | None = None
    if to_clear or armed:
        try:
            raw_state = executor.read_key_strict(redis, executor.STATE_KEY)
        except Exception as exc:  # noqa: BLE001
            notes.append(f"规则状态读不到（跳过状态检查与清理）：{exc}")
        else:
            state_rules = dict((raw_state or {}).get("rules") or {})
            if str((raw_state or {}).get("date") or "") != executor.trade_date_str():
                state_rules = {}  # 跨日：昨日状态不参与判定（load_state 同口径）

    if state_rules is not None:
        stuck = [
            str(rule.get("symbol") or "")
            for _, rule in armed
            if not executor.is_retryable(state_rules.get(str(rule.get("symbol") or "")))
        ]
        if stuck:
            notes.append(
                "以下标的今日已触发过，重挂的规则不会再次触发"
                f"（重新武装走 POST /qmt-sltp/reset）：{', '.join(stuck)}"
            )
        if to_clear:
            cleared, live = _clear_states(redis, to_clear, state_rules)
            if live:
                notes.append(
                    f"旧规则已摘但状态保留（在途委托仍在跟踪）：{', '.join(live)}"
                )
            if cleared:
                notes.append(f"已清理摘除标的的状态：{', '.join(cleared)}")

    if problems:
        logger.error(
            "[WatchWriter] 回读校验发现问题 owner=%s: %s", owner, "；".join(problems)
        )
    if unverified:
        logger.error(
            "[WatchWriter] 未确认落库 owner=%s: %s",
            owner,
            "；".join(f"{s}（{r}）" for s, r in unverified),
        )
    if not problems and not unverified:
        logger.info(
            "[WatchWriter] owner=%s 落库 %d 条 / 摘除 %d 条 / 冲突 %d 条",
            owner,
            len(armed),
            len(cleared),
            len(conflicts),
        )

    bad = {symbol for symbol, _ in unverified}
    return WatchWriteResult(
        owner=owner,
        plan=plan,
        armed=tuple(
            str(r.get("symbol") or "")
            for _, r in armed
            if str(r.get("symbol") or "") not in bad
        ),
        removed=tuple(cleared),
        conflicts=tuple(conflicts),
        unverified=tuple(unverified),
        errors=(),
        problems=tuple(problems),
        notes=tuple(notes),
    )


__all__ = [
    "CAP_EXCEEDED",
    "REJECT_MANUAL_HOLD",
    "REJECT_NOT_LANDED",
    "REJECT_NO_OWNER",
    "REJECT_OTHER_AGENT",
    "REJECT_SAME_SYMBOL",
    "Conflict",
    "WatchWriteResult",
    "write_watch_plan",
]
