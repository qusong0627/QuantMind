#!/usr/bin/env python3
"""P5 步骤 3 切换工具：隔壁 ``data/live_watch.json`` → 本仓止盈止损规则表。

**为什么单独一个工具、而不是「照着文件在控制面点一遍」**：切换只有一次窗口，规则没
挂上 = 当日**持仓裸奔**（迁移计划风险清单里的 CRITICAL）。人手搬的失效形态是「看起来
搬完了」——它没有任何信号；本工具把每一家、每一条的去向都印出来，并落一份存档。

用法::

    # 1) 预演（默认）：读规则表 + 读隔壁文件，**一行不写**，印出逐家处置
    python backend/scripts/migrate_legacy_watch.py
    # 2) 落库（逐家整组替换 qmt:sltp:executor:config 的 rules）+ 存档
    python backend/scripts/migrate_legacy_watch.py --apply --record <存档路径>
    # 3) 开闸前复验：存档里说挂上了的，现在还在不在（逐条比 owner 与价位）
    python backend/scripts/migrate_legacy_watch.py --verify --record <存档路径>

退出码：``0`` 全清 / ``1`` **要人看一眼**（有冲突、有被拒、源文件空、有告警）/
``2`` 环境或参数错误（文件读不到、名册对不上、``--apply`` 缺 ``--record``、存档在仓里）。

纪律（每一条都对应一种「跑完看起来正常」的失效）
------------------------------------------------
1. **名册闸**：文件里的每个 agent 都必须在名册（``QM_DECISION_LLM_ROSTER``）里。迁移写下的
   ``owner="llm:<agent>"`` 由谁整组替换，是由名册决定的；名册外的名字 = 那一组规则**永远
   没人认领**（既不会被撤也不会跟着新分析走），比没挂上更坏——它**看起来有保护**。
2. **预演与实际同一份判定**：预演调 ``watch_writer.resolve_plan``（实际落库用的同一个
   函数），差分用例钉住两者；本工具不自己再写一遍「人工优先 / 别家先到先得」。
3. **空集不写**：某家一条规则都没有时**整组保留**（与决策层一字不差）。整组替换在空集上
   会把该家已挂的规则全摘掉——重复跑一次这个工具就把上次挂好的止损清了。
4. **存档不许进仓**：``--record`` 里带着模型写的完整证据链（含持仓成本与仓位），公开仓
   不能收。判据与 P3-② 落地区**共用** ``shared/migration_paths.artifact_refusal``
   （仓库树 + git 工作树两条并列——容器里没有 ``.git``，只问 git 会静默放行）。
5. **先武装再开闸**：本工具只写规则。执行器总开关关着时规则不会触发——这正是切换顺序
   要的，工具会把它印在脸上。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.services.live_trading.services import sltp_executor as executor  # noqa: E402
from backend.shared.decision import legacy_watch as lw  # noqa: E402
from backend.shared.decision.watch_writer import write_watch_plan  # noqa: E402
from backend.shared.migration_paths import artifact_refusal  # noqa: E402

EXIT_OK = 0
EXIT_ATTENTION = 1
EXIT_USAGE = 2

#: 隔壁守护计划文件（默认路径；容器/他机上一律显式传 --file）
DEFAULT_FILE = "/home/zbox/quant-Trader/data/live_watch.json"

CONFIG_KEY = "qmt:sltp:executor:config"


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _load_file(path: Path) -> tuple[Any, str | None]:
    """读隔壁文件 → ``(doc, 错误)``（读不到就报错，**绝不当作空表**）。

    把「文件不存在/读失败」当 ``{}`` 处理，是这个工具最危险的静默降级：空表 ⇒ 每家的
    规则集为空 ⇒ 与「空集不写」叠加 ⇒ **什么都没发生，且退出码 0**。操作员在切换窗口里
    看到「全清」，以为守护计划已经搬过去了——而持仓当日裸奔。
    """
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        return None, f"读不到 {path}：{exc}"
    try:
        return json.loads(raw), None
    except json.JSONDecodeError as exc:
        return None, f"{path} 不是合法 JSON：{exc}"


def _roster_agents() -> tuple[tuple[str, ...], str]:
    """名册里的 agent（唯一事实源：P2.9 的 ``resolve_roster`` / ``resolve_config``）。

    名册没开时退回单家配置的模型名——那正是决策轮届时会用的那个 ``owner``
    （``decision_round`` 的 agent 也取自 ``binding.model``，再过 ``normalize_agent``）。
    """
    from backend.shared.decision_llm_client import (
        ENV_ROSTER,
        resolve_config,
        resolve_roster,
    )
    from backend.shared.order_contract import normalize_agent

    if str(os.getenv(ENV_ROSTER) or "").strip():
        models = [c.model for c in resolve_roster()]
        return tuple(normalize_agent(m) for m in models), f"名册 {len(models)} 家"
    model = resolve_config().model
    return (normalize_agent(model),), f"单家（{model}，未开 {ENV_ROSTER}）"


def _print_plan(watch: lw.LegacyWatch, preview: lw.Preview, note: str) -> None:
    print(f"[名册] {note}")
    print(
        f"[计划] 源 {watch.source_rules} 条 → 逐家处置如下"
        f"（表 {preview.table_before} → {preview.table_after} 条 / 上限 {preview.cap}）"
    )
    # strict：预演与源分组必须一一对应。少印一家在本工具里等于**静默少搬一家**——
    # 宁可当场炸，也不要一份看起来「每家都印了」但实际缺一家的报告。
    for group, agent_preview in zip(watch.groups, preview.agents, strict=True):
        head = f"  · {group.agent}: 源 {len(group.rules)} 条"
        if agent_preview.kept:
            print(
                f"{head} —— 本轮无规则：整组保留 {list(agent_preview.kept)}（不写空集）"
            )
            continue
        print(
            f"{head} → 挂上 {len(agent_preview.armed)} {list(agent_preview.armed)}"
            f" / 未挂上 {len(agent_preview.conflicts)}"
            f" / 落库前被拒 {len(agent_preview.rejected)}"
            + (
                f" / 摘除 {list(agent_preview.removed)}"
                if agent_preview.removed
                else ""
            )
        )
        for conflict in agent_preview.conflicts:
            holder = f"（现持有：{conflict.holder}）" if conflict.holder else ""
            print(f"      ✗ {conflict.symbol} 未挂上——{conflict.reason}{holder}")
        for rejection in agent_preview.rejected:
            print(
                f"      ✗ {rejection.code or '（无 code）'} 未挂上——{rejection.reason}"
            )
        if agent_preview.overflow:
            print(f"      ✗ {agent_preview.overflow}")
    for problem in watch.problems:
        print(f"  [问题] {problem}")
    # 这里不再重复印 ``preview.attention()``：它的内容就是上面逐条印过的冲突、超限与
    # 问题（退出码与存档仍然按它算），同一件事印两遍会让操作员以为出了两回事。


def _rule_row(rule: lw.LegacyRule, agent_preview: lw.PreviewAgent) -> dict[str, Any]:
    """一条源规则 + 它在规则表里的归宿（``expected_rule`` = 落库内容的逐字副本）。

    匹配用的是 ``normalize_symbol`` 之后的标的（``plan_watch`` 造规则时已经归一过），
    所以「文件里写的是后缀式、表里是前缀式」这类写法差异不会让复验误报缺失。
    """
    want = executor.normalize_symbol(str(rule.code))
    expected = next(
        (dict(r) for r in agent_preview.arms if str(r.get("symbol") or "") == want),
        None,
    )
    return {
        "agent": rule.agent,
        "index": rule.index,
        "code": rule.code,
        "stop_loss": rule.decision.stop_loss,
        "take_profit": rule.decision.take_profit,
        "move_stop": rule.decision.move_stop,
        "pct": rule.decision.pct.value if rule.decision.pct.is_given else None,
        "pct_state": rule.decision.pct.state,
        "created_ts": rule.created_ts,
        # 原样保留：规则表词表里没有 reason 字段，那段证据链只在这里
        "reason": rule.reason,
        "armed": expected is not None,
        "expected_rule": expected,
    }


def _record_doc(
    *,
    path: Path,
    digest: str,
    watch: lw.LegacyWatch,
    preview: lw.Preview,
    roster_note: str,
    applied: dict[str, Any] | None,
    executor_enabled: bool,
    attention: list[str],
) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    by_agent = {g.agent: g for g in watch.groups}
    for agent_preview in preview.agents:
        group = by_agent.get(agent_preview.agent)
        for rule in group.rules if group else ():
            rows.append(_rule_row(rule, agent_preview))
    return {
        "kind": "legacy-watch-migration",
        "source": {
            "path": str(path),
            "sha256": digest,
            "agents": list(watch.agents()),
            "source_rules": watch.source_rules,
            "problems": list(watch.problems),
            "errors": list(watch.errors),
        },
        "roster": roster_note,
        "executor_enabled": executor_enabled,
        "preview": preview.summary(),
        "applied": applied,
        "rules": rows,
        "verdict": {
            "attention": list(preview.attention()) + list(attention),
            "ok": not (preview.attention() or attention),
        },
    }


def _diff_armed(preview: lw.Preview, applied: dict[str, Any]) -> list[str]:
    """预演说挂上的 vs 实际读回挂上的——**两者不一致必须吵**。

    注意 ``result.armed`` 是「已落库**并回读确认过**」的那部分，所以这里的差集有三种
    成因，都要人看一眼：回读没确认到（``unverified``，另有单独一行）、读-改-写之间
    表被改过（人工改规则、别家轮次）、预演与实际分叉（那就是本工具的 bug）。
    """
    out: list[str] = []
    for agent_preview in preview.agents:
        actual = applied.get(agent_preview.agent) or {}
        if "skipped" in actual:
            continue
        want, got = set(agent_preview.armed), set(actual.get("armed") or ())
        if want - got:
            out.append(
                f"{agent_preview.agent}: 预演说挂上但实际没挂上 {sorted(want - got)}"
                f"（回读没确认到 / 表被并发改过 / 预演与实际分叉）"
            )
        if got - want:
            out.append(
                f"{agent_preview.agent}: 实际挂上的比预演多 {sorted(got - want)}"
                f"（同样是「两处算法分叉」这一类，方向相反）"
            )
    return out


def _verify(args: argparse.Namespace, redis: Any) -> int:
    """复验：存档里说挂上了的，现在还在不在（逐条比 ``expected_rule``）。

    不是「再跑一遍预演」：预演看的是**将要发生**什么，复验看的是**现在表里有什么**。
    两者在读-改-写之间有并发写时会给出不同答案，而开闸前要确认的恰恰是后者。
    """
    try:
        doc = json.loads(Path(args.record).expanduser().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"[复验] 读不到存档 {args.record}：{exc}", file=sys.stderr)
        return EXIT_USAGE
    stored_sha = str(doc.get("source", {}).get("sha256") or "")
    if args.expect_source_sha and args.expect_source_sha != stored_sha:
        print(
            f"[复验] 注意：--expect-source-sha={args.expect_source_sha} 与存档里的 "
            f"{stored_sha} 不同（源文件换过；复验只查规则表，不算失败）"
        )
    try:
        cfg = executor.read_config_strict(redis)
    except Exception as exc:  # noqa: BLE001
        print(f"[复验] 读规则表失败（{CONFIG_KEY}）：{exc}", file=sys.stderr)
        return EXIT_USAGE
    by_symbol = {str(r.get("symbol") or ""): r for r in (cfg.get("rules") or [])}

    expected = [r for r in (doc.get("rules") or []) if r.get("armed")]
    missing: list[dict[str, Any]] = []
    drifted: list[tuple[dict[str, Any], str]] = []
    for row in expected:
        want = row.get("expected_rule") or {}
        symbol = str(want.get("symbol") or row.get("code") or "")
        live = by_symbol.get(symbol)
        if live is None:
            missing.append(row)
            continue
        why = _rule_diff(want, live)
        if why:
            drifted.append((row, why))
    for row in missing:
        print(f"  ✗ {row.get('code')}（{row.get('agent')}）不在规则表里")
    for row, why in drifted:
        print(f"  ✗ {row.get('code')}（{row.get('agent')}）被改过：{why}")
    print(
        f"[复验] 存档 {len(expected)} 条已挂 → 在表 {len(expected) - len(missing)} 条 / "
        f"缺失 {len(missing)} / 被改 {len(drifted)}；表内共 {len(by_symbol)} 条"
    )
    return EXIT_ATTENTION if (missing or drifted) else EXIT_OK


def _rule_diff(want: dict[str, Any], live: dict[str, Any]) -> str:
    """两条规则的关键字段差异（空串 = 一致）。

    只比执行器词表（``DEFAULT_RULE``）里的键：读时还会派生 ``rejected_rules`` 之类的
    诊断字段，各算一次必然不等，拿它们比会天天误报。
    """
    for key in sorted(executor.DEFAULT_RULE):
        a, b = want.get(key), live.get(key)
        if isinstance(a, (int, float)) or isinstance(b, (int, float)):
            if a is None or b is None:
                if (a is None) != (b is None):
                    return f"{key}={b!r}（存档 {a!r}）"
                continue
            if abs(float(a) - float(b)) > 1e-9:
                return f"{key}={b!r}（存档 {a!r}）"
        elif a != b:
            return f"{key}={b!r}（存档 {a!r}）"
    return ""


def _record_refusal(record_path: Path) -> str | None:
    """存档落点不合规时返回理由（``None`` = 放行）。判据与 P3-② 落地区**同一份**
    （``shared/migration_paths.artifact_refusal``：仓库树 + git 工作树两条并列）。

    第二条（git）单独用是不行的：**容器里没有 ``.git``**，只问 git 会静默放行，而
    操作员在容器里跑本工具是最自然的路径（规则表在容器的 Redis 里）。这条防线漏掉
    的后果是：一份带持仓成本与仓位判断的存档躺在 ``/app`` 里，下一次 ``git add``
    就进了公开仓。
    """
    return artifact_refusal(record_path, project_root=PROJECT_ROOT)


def _connect() -> Any | None:
    """取规则表所在的 Redis（**与实盘决策轮同一个客户端**：``trade_shared.deps``）。

    换成别的库/别的包装 = 迁移写进一座表、执行器读另一座表，而两边都会正常返回——
    这是本仓已经吃过一次的坑（``broker:selected`` 读错库），所以这里只有这一条路。
    连不上返回 ``None``（调用方按环境错误退出），不让 FastAPI 的 503 异常以栈的形式
    糊在操作员脸上。
    """
    try:
        from backend.services.trade_shared.deps import get_redis

        return get_redis()
    except Exception as exc:  # noqa: BLE001
        print(f"[迁移] 连不上 Redis（规则表在 trade 库）：{exc}", file=sys.stderr)
        return None


def _run(args: argparse.Namespace) -> int:
    redis = _connect()
    if redis is None:
        return EXIT_USAGE

    if args.verify:
        return _verify(args, redis)

    path = Path(args.file).expanduser()
    doc, err = _load_file(path)
    if err is not None:
        print(f"[迁移] {err}", file=sys.stderr)
        return EXIT_USAGE
    digest = _sha256(path)

    watch = lw.parse_legacy_watch(doc)
    if not watch.ok:
        for error in watch.errors:
            print(f"[迁移] 拒收：{error}", file=sys.stderr)
        return EXIT_USAGE

    if not watch.rules():
        # 「一条规则都没有」（``{}`` / 各家的列表都是空的）是这个工具**最容易被读成
        # 成功**的输入：预演一圈什么都不印，退出码若为 0，操作员会把它读成「守护计划
        # 已迁移」。而现实里它多半意味着隔壁换文件了/被清过了（实测隔壁停跑后这个文件
        # 就成了 ``{}``）——那正是当日持仓裸奔的现场。注意这与「有规则但一条都挂不上」
        # 是两件事：后者在预演里逐家吵（整组保留 + 本轮没有更新）。
        print(
            "[迁移] 源文件里一条规则都没有（0 个 agent，或各家的列表都是空的）："
            "这次迁移等于**什么都没做**。\n"
            "  切换窗口里这不是「搬完了」——先确认隔壁确实是这个状态（进程已停、文件没被"
            "清空或换过），再决定下一步。",
            file=sys.stderr,
        )
        return EXIT_ATTENTION

    try:
        known, roster_note = _roster_agents()
    except Exception as exc:  # noqa: BLE001 决策 LLM 没配 = 名册也没有 = 归属无人认领
        print(
            f"[迁移] 取不到名册/单家配置（{type(exc).__name__}: {exc}）："
            "迁移写下的每条规则都带 owner=llm:<agent>，而将来替换它的是决策轮的那个 "
            "agent——配置读不到就不该往下走。",
            file=sys.stderr,
        )
        return EXIT_USAGE

    missing = lw.roster_mismatches(watch.agents(), known)
    if missing:
        print(
            f"[迁移] 拒收：文件里的 {list(missing)} 不在{roster_note}"
            f"（可选 {list(known)}）。\n"
            "  owner=llm:<agent> 的规则组由名册里那家每轮整组替换；名册外的名字写下去"
            "就是没人认领的一组——既不会被撤也不会跟着新分析走，**看起来有保护**。\n"
            "  先配好 QM_DECISION_LLM_ROSTER（P2.9）再跑本工具。",
            file=sys.stderr,
        )
        return EXIT_USAGE

    try:
        cfg = executor.read_config_strict(redis)
    except Exception as exc:  # noqa: BLE001 读不到就**不写**（读-改-写会抹掉整表）
        print(f"[迁移] 读规则表失败（{CONFIG_KEY}）：{exc}", file=sys.stderr)
        return EXIT_USAGE
    table = list(cfg.get("rules") or [])
    executor_enabled = bool(cfg.get("enabled"))

    plans = lw.plan_groups(watch)
    preview = lw.simulate_apply(plans, table)
    _print_plan(watch, preview, roster_note)
    if not executor_enabled:
        print(
            "  [注意] 执行器总开关未开（qmt:sltp:executor:config.enabled=false）："
            "规则不会触发。开闸是切换顺序里的**下一步**（qmt_sltp_ctl.py --enable）。"
        )

    if not args.apply:
        print("[计划] 一行未写。落库：--apply --record <存档路径>（存档不许在仓里）")
        return EXIT_ATTENTION if preview.attention() else EXIT_OK

    if not args.record:
        print(
            "[迁移] --apply 必须带 --record <path>：逐条规则的来源与去向要留档"
            "（规则表词表里没有 reason 字段，那段证据链只在这里）",
            file=sys.stderr,
        )
        return EXIT_USAGE
    record_path = Path(args.record).expanduser()
    why = _record_refusal(record_path)
    if why is not None:
        print(
            f"[迁移] 存档 {record_path} {why}：拒绝执行。\n"
            "  存档含模型写的完整证据链（持仓成本/仓位/板块判断），公开仓不能收。\n"
            "  宿主上落在 data/legacy/quanttrader/（data/ 是符号链接，解析后在工作树"
            "外）；容器里则写 /tmp 再用 docker cp 取出来——容器内 /app 就是仓库树。",
            file=sys.stderr,
        )
        return EXIT_USAGE

    # ── 落库：逐家整组替换，走**决策层同一个**写入端（读-改-写 + 写后回读）─────
    applied: dict[str, Any] = {}
    attention: list[str] = []
    for plan in plans:
        if not lw.has_rules(plan):
            applied[plan.agent] = {"skipped": "本轮无规则：整组保留（不写空集）"}
            continue
        result = write_watch_plan(redis, plan, agent=plan.agent)
        applied[plan.agent] = result.summary()
        if not result.ok:
            attention.append(
                f"{plan.agent}: 未全部落库（errors={list(result.errors)} "
                f"problems={list(result.problems)}）"
            )
        for symbol, reason in result.unverified:
            attention.append(f"{plan.agent}: {symbol} 回读没确认到——{reason}")
    attention.extend(_diff_armed(preview, applied))

    doc_out = _record_doc(
        path=path,
        digest=digest,
        watch=watch,
        preview=preview,
        roster_note=roster_note,
        applied=applied,
        executor_enabled=executor_enabled,
        attention=attention,
    )
    record_path.parent.mkdir(parents=True, exist_ok=True)
    record_path.write_text(
        json.dumps(doc_out, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    print(f"[迁移] 存档：{record_path}")
    print(
        f"[迁移] 挂上 {preview.armed_total} 条 / 未挂上 {preview.conflicts_total} 条"
        f" / 表 {preview.table_before} → {preview.table_after} 条"
    )
    for item in attention:
        print(f"  [要人看] {item}")
    return EXIT_ATTENTION if (preview.attention() or attention) else EXIT_OK


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="P5 步骤 3：隔壁 live_watch.json → 本仓止盈止损规则表"
    )
    parser.add_argument(
        "--file", default=DEFAULT_FILE, help="隔壁守护计划文件（默认隔壁仓内路径）"
    )
    parser.add_argument("--apply", action="store_true", help="落库（默认只预演）")
    parser.add_argument(
        "--verify", action="store_true", help="复验存档里的规则还在不在"
    )
    parser.add_argument("--record", default="", help="存档路径（--apply/--verify 用）")
    parser.add_argument(
        "--expect-source-sha",
        default="",
        help="复验时若给了就比对存档里的源文件哈希（不同只提示，不算失败）",
    )
    args = parser.parse_args(list(argv) if argv is not None else None)
    if args.apply and args.verify:
        print("[迁移] --apply 与 --verify 互斥", file=sys.stderr)
        return EXIT_USAGE
    if args.verify and not args.record:
        print(
            "[复验] 需要 --record <path>（存档里才有「应该挂上哪些」）", file=sys.stderr
        )
        return EXIT_USAGE
    if args.expect_source_sha and not args.verify:
        print("[迁移] --expect-source-sha 只对 --verify 有意义", file=sys.stderr)
        return EXIT_USAGE
    try:
        return _run(args)
    except KeyboardInterrupt:
        print(
            "[迁移] 中断（已写入的部分保持原样；重跑前先看 --verify）", file=sys.stderr
        )
        return EXIT_USAGE


if __name__ == "__main__":
    sys.exit(main())
