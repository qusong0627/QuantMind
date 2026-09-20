#!/usr/bin/env python3
"""策略控制台验收报告（T-RC-15/19/20，只读仪器）。

回答的是「这台交易台现在可信吗」——把控制台赖以成立的几条后端契约抓成证据。
每项只读、无副作用，随时可跑：

  A 模式一致性   —— 活跃策略的 mode 合法且与请求口径不矛盾；无活跃策略时不谎报档位
  B 市场闸门     —— 活跃策略市场 vs 策略声明的市场；存储层 market 过滤不泄漏他市场策略
  C 运行日志     —— 运行流可读、条目有 level/stage/source、游标可增量推进
  D 心跳守护     —— 托管循环（sim_hosted / manual_execution / sentinel_push）心跳新鲜度
  E 热更新       —— 配置版本与历史完整、运行身份（code_str/run_id）未被热更新抹掉、
                    生效时机与最近周期时间自洽
  F 风控口径     —— 运行快照 execution_config 与策略参数 execution_config 不分叉（D9 回归门）
  G 停止留痕     —— 运行流里能查到停止记录（没停过则如实 N/A）

用法:
    python backend/scripts/trading_console_acceptance.py                 # 人类可读
    python backend/scripts/trading_console_acceptance.py --json          # 机器可读
    python backend/scripts/trading_console_acceptance.py --user 10000001 --tenant default

退出码：0=无 FAIL；1=存在 FAIL（可直接进 CI）。

**零项参与即 FAIL**：所有检查项都拿到 N/A 时整体判 FAIL——「一项都没验」与
「全部通过」在退出码上必须区分得开，否则这台仪器只会制造虚假安心。
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

CST = timezone(timedelta(hours=8))

#: 守护条关注的循环——与 `real_trading_lifecycle._GUARDIAN_JOB_KEYS` 同一集合。
#: 前端守护条读的就是这三个，验收也只看这三个，避免「界面说活着、验收说死了」。
GUARDIAN_JOB_KEYS = ("sim_hosted", "manual_execution", "sentinel_push")

VALID_MODES = {"REAL", "SHADOW", "SIMULATION"}

#: 心跳年龄上限（秒）——与 JobSpec 的 ttl 语义一致：超过 2×ttl 视为不新鲜。
HEARTBEAT_STALE_FACTOR = 2


# ── 纯函数（可单测）────────────────────────────────────────────────────


def grade_mode(payload: dict[str, Any] | None) -> tuple[str, str]:
    """A 模式一致性。无活跃策略时**不是 OK**——没有可比对的运行态，如实 N/A。"""
    data = payload if isinstance(payload, dict) else {}
    if not data:
        return "N/A", "无活跃策略（未启动），档位无从判定"
    mode = str(data.get("mode") or "").strip().upper()
    if not mode:
        return "WARN", "活跃策略载荷缺 mode：界面只能靠缺省值猜运行档位"
    if mode not in VALID_MODES:
        return "FAIL", f"mode={mode} 不在 {sorted(VALID_MODES)} 内，界面与执行口径都可能错判"
    return "OK", f"mode={mode}"


def grade_market_gate(
    active_market: str,
    declared_market: str | None,
    *,
    foreign_leak: int,
    checked_strategies: int,
) -> tuple[str, str]:
    """B 市场闸门：策略声明 vs 运行市场不分叉；存储层过滤不泄漏他市场策略。"""
    if checked_strategies == 0:
        return "N/A", "库中无策略，过滤与闸门无从验证"
    if foreign_leak > 0:
        return "FAIL", f"market 过滤泄漏 {foreign_leak} 条他市场策略（跨市场混入正是 D4 的原样）"
    if declared_market and declared_market != active_market:
        return "FAIL", f"运行市场 {active_market} ≠ 策略声明市场 {declared_market}（行情/信号口径可能全错）"
    if not declared_market:
        return "WARN", f"运行市场 {active_market}（策略未声明 market，闸门不判定）"
    return "OK", f"运行市场与策略声明一致（{active_market}），过滤 {checked_strategies} 条零泄漏"


def grade_runtime_logs(entries: list[dict[str, Any]], *, has_active: bool) -> tuple[str, str]:
    """C 运行日志可用性：有条目且字段齐 → OK；运行中却一条没有 → FAIL（静默）。"""
    if not entries:
        if has_active:
            return "FAIL", "策略运行中但运行流为空——「界面无日志」与「真的没发生事」分不开"
        return "N/A", "无活跃策略且运行流为空（未跑过就没有日志，不算缺陷）"
    missing_level = [e for e in entries if not str(e.get("level") or "").strip()]
    missing_stage = [e for e in entries if not str(e.get("stage") or "").strip()]
    if missing_level:
        return "FAIL", f"{len(missing_level)}/{len(entries)} 条缺 level：面板无法分级着色"
    if missing_stage:
        return "WARN", f"{len(missing_stage)}/{len(entries)} 条缺 stage：阶段过滤会漏掉它们"
    return "OK", f"运行流 {len(entries)} 条，level/stage/source 齐备"


def grade_heartbeats(rows: list[dict[str, Any]] | None) -> tuple[str, str]:
    """D 心跳：三个守护循环的判定与体检 C07 同源（同一个 read_heartbeats）。"""
    if not rows:
        return "FAIL", "心跳采集返回空——守护条会显示「不可用」，用户无从判断策略是否还在跑"
    states = {str(r.get("key")): str(r.get("state")) for r in rows if isinstance(r, dict)}
    if states.get("sim_hosted") == "ok" or states.get("manual_execution") == "ok":
        alive = [k for k, v in states.items() if v == "ok"]
        return "OK", f"托管循环有心跳：{', '.join(alive)}"
    stale = [k for k, v in states.items() if v == "stale"]
    if stale:
        return "WARN", f"心跳超时：{', '.join(stale)}（循环可能卡住）"
    return "N/A", f"托管循环未启用或未启动（{states}）——无策略在跑时属正常"


def grade_hot_update(payload: dict[str, Any] | None, latest_cycle_at: str | None) -> tuple[str, str]:
    """E 热更新：版本/历史/身份三件事。生效时机与最近周期自洽。"""
    data = payload if isinstance(payload, dict) else {}
    if not data:
        return "N/A", "无活跃策略，热更新无从验证"
    version = data.get("config_version")
    if not isinstance(version, int) or version < 1:
        return "WARN", f"config_version={version!r}：热更新计数缺失，界面无法显示「配置 vN」"
    # 身份必须还在——热更新只改配置，抹掉 code_str/run_id 会让账本与原运行实例断链
    identity = [k for k in ("code_str", "run_id", "started_at") if k not in data]
    if identity:
        return "FAIL", f"热更新后丢失运行身份字段 {identity}（账本断链）"
    history = data.get("config_history")
    if version > 1 and not isinstance(history, list):
        return "FAIL", f"config_version={version} 却没有 config_history：变更无从回溯"
    if version > 1 and isinstance(history, list) and len(history) < version - 1:
        return "WARN", f"config_history 条数 {len(history)} < 版本增量 {version - 1}（历史上限或写入失败）"
    updated = str(data.get("config_updated_at") or "").strip()
    if not updated:
        return "WARN", f"config_version={version} 但缺 config_updated_at：面板无法判断新旧版本"
    if latest_cycle_at and _parse_ts(updated) and _parse_ts(latest_cycle_at):
        pending = _parse_ts(latest_cycle_at) < _parse_ts(updated)
        return (
            "OK",
            f"v{version}，{'下个周期待生效' if pending else '已在本轮生效'}（最近周期 {latest_cycle_at}）",
        )
    return "OK", f"v{version}，更新于 {updated}"


def grade_risk_divergence(divergence: dict[str, Any] | None, *, has_active: bool) -> tuple[str, str]:
    """F 风控口径（D9 回归门）：快照与策略参数分叉 = 止损看着配了却不触发。"""
    if divergence is None:
        if has_active:
            return "WARN", "有活跃策略但无法比对风控口径（策略未声明 execution_config）"
        return "N/A", "无活跃策略，口径比对本就不适用"
    if divergence.get("diverged"):
        fields = divergence.get("fields") or {}
        names = ", ".join(sorted(fields.keys())) or "-"
        return "FAIL", f"风控口径分叉（{names}）：界面显示的止损与实际读到的不是一回事"
    return "OK", "运行快照与策略参数的风控口径一致"


def grade_stop_audit(entries: list[dict[str, Any]]) -> tuple[str, str]:
    """G 停止留痕：运行流里 stage=stop 的记录。没停过如实 N/A。"""
    stops = [e for e in entries if str(e.get("stage") or "") == "stop"]
    if not stops:
        return "N/A", "运行流中无停止记录（本次窗口没停过策略）"
    last = stops[-1]
    line = str(last.get("line") or "")
    if "原因" not in line:
        return "WARN", f"停止记录存在但未带原因：{line[:60]}"
    return "OK", f"停止留痕 {len(stops)} 条，最近一条：{line[:70]}"


def finalize_verdicts(verdicts: dict[str, dict[str, str]]) -> tuple[dict[str, dict[str, str]], bool, dict[str, int]]:
    """收口：判定 has_fail 并施加「零项参与即 FAIL」护栏。

    单独抽出来是因为这条护栏正是本仪器唯一的防伪部件——它若失效，整台仪器会在
    「一项都没验到」时照样返回 0，看起来比绿还绿。纯函数，可直接单测。
    """
    out = dict(verdicts)
    coverage = {
        "total": len(out),
        "participated": sum(1 for v in out.values() if v["level"] != "N/A"),
    }
    has_fail = any(v["level"] == "FAIL" for v in out.values())
    if coverage["participated"] == 0:
        out["Z_coverage"] = {
            "level": "FAIL",
            "message": "全部检查项均为 N/A：未验证任何一条契约，本次运行不构成通过",
        }
        has_fail = True
    return out, has_fail, coverage


def _parse_ts(value: str | None) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None


# ── 采集（只读）───────────────────────────────────────────────────────


def _read_active_payload(user_id: str, tenant_id: str) -> dict[str, Any]:
    from backend.services.live_trading.routers.real_trading_utils import (
        _read_active_strategy_raw,
    )
    from backend.services.trade_shared.redis_client import get_redis

    raw = _read_active_strategy_raw(get_redis(), tenant_id, user_id)
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _load_strategy(strategy_id: str, user_id: str) -> dict[str, Any] | None:
    """策略仓储 `get` 是 async——仪器整体同步，这里用 asyncio.run 桥一次。"""
    import asyncio

    from backend.shared.strategy_storage import get_strategy_storage_service

    try:
        return asyncio.run(
            get_strategy_storage_service().get(strategy_id, user_id=user_id)
        )
    except Exception:  # noqa: BLE001 - 仪器不得因单点失败整体崩
        return None


def collect_market_gate(user_id: str, tenant_id: str, active: dict[str, Any], market: str) -> dict[str, Any]:
    """策略库 market 过滤是否泄漏他市场策略（D4 的服务端那一半）。"""
    from backend.shared.active_strategy_market import (
        active_strategy_market,
        strategy_declared_market,
    )

    active_market = active_strategy_market(active) if active else market
    declared: str | None = None
    strategy_id = str(active.get("strategy_id") or "").strip()
    if strategy_id:
        declared = strategy_declared_market(_load_strategy(strategy_id, user_id))

    foreign_leak = 0
    checked = 0
    try:
        from backend.shared.strategy_storage import get_strategy_storage_service

        rows = get_strategy_storage_service().list(user_id, market=market)
        checked = len(rows)
        # CN 口径下 'A' 是同一市场的另一种写法，不算泄漏（与存储层 where 子句同口径）
        same_as_requested = {market, "A"} if market == "CN" else {market}
        for row in rows:
            params = (row or {}).get("parameters") or {}
            row_market = str(params.get("market") or "").strip().upper()
            if row_market and row_market not in same_as_requested:
                foreign_leak += 1
    except Exception:  # noqa: BLE001
        checked = 0

    return {
        "active_market": active_market,
        "declared_market": declared,
        "foreign_leak": foreign_leak,
        "checked_strategies": checked,
    }


def collect_runtime_logs(tenant_id: str, user_id: str, limit: int = 200) -> dict[str, Any]:
    from backend.services.live_trading.services.runtime_log_stream import runtime_log_stream

    data = runtime_log_stream.fetch_scope_entries(
        tenant_id=tenant_id, user_id=user_id, after_id="0-0", limit=limit
    )
    entries = data.get("entries") or []
    next_id = data.get("next_id") or "0-0"
    # 游标增量读：从 next_id 之后再读一次，验证不重复也不报错
    tail = runtime_log_stream.fetch_scope_entries(
        tenant_id=tenant_id, user_id=user_id, after_id=next_id, limit=limit
    )
    return {
        "entries": entries,
        "next_id": next_id,
        "tail_count": len(tail.get("entries") or []),
        "state": runtime_log_stream.read_state(tenant_id=tenant_id, user_id=user_id),
    }


def collect_heartbeats() -> list[dict[str, Any]] | None:
    from backend.shared.scheduler_registry import read_heartbeats

    try:
        return read_heartbeats(GUARDIAN_JOB_KEYS)
    except Exception:  # noqa: BLE001
        return None


def build_report(user_id: str, tenant_id: str, market: str) -> dict[str, Any]:
    now = datetime.now(tz=CST)
    active = _read_active_payload(user_id, tenant_id)
    logs = collect_runtime_logs(tenant_id, user_id)
    entries = logs["entries"]
    heartbeats = collect_heartbeats()
    gate = collect_market_gate(user_id, tenant_id, active, market)

    divergence: dict[str, Any] | None = None
    try:
        from backend.services.live_trading.routers.real_trading_lifecycle import (
            _execution_config_divergence,
        )

        strategy = None
        strategy_id = str(active.get("strategy_id") or "").strip()
        if strategy_id:
            strategy = _load_strategy(strategy_id, user_id)
        divergence = _execution_config_divergence(active_data=active, strategy=strategy)
    except Exception:  # noqa: BLE001
        divergence = None

    verdicts: dict[str, dict[str, str]] = {}

    def _v(grade: tuple[str, str]) -> dict[str, str]:
        level, message = grade
        return {"level": level, "message": message}

    verdicts["A_mode"] = _v(grade_mode(active or None))
    verdicts["B_market_gate"] = _v(
        grade_market_gate(
            gate["active_market"],
            gate["declared_market"],
            foreign_leak=gate["foreign_leak"],
            checked_strategies=gate["checked_strategies"],
        )
    )
    verdicts["C_runtime_logs"] = _v(grade_runtime_logs(entries, has_active=bool(active)))
    verdicts["D_heartbeat"] = _v(grade_heartbeats(heartbeats))
    # 状态键是 `updated_at`——/status 把它映射成 latest_cycle.at；此处直接读原始键，
    # 写成 `at` 会让「生效时机自洽」这一路恒真（拿不到最近周期时间 → 走 else 分支永远 OK）。
    verdicts["E_hot_update"] = _v(
        grade_hot_update(
            active or None,
            (logs.get("state") or {}).get("updated_at") if logs.get("state") else None,
        )
    )
    verdicts["F_risk_divergence"] = _v(grade_risk_divergence(divergence, has_active=bool(active)))
    verdicts["G_stop_audit"] = _v(grade_stop_audit(entries))

    verdicts, has_fail, coverage = finalize_verdicts(verdicts)

    return {
        "generated_at": now.isoformat(),
        "user_id": user_id,
        "tenant_id": tenant_id,
        "market": market,
        "active_strategy": bool(active),
        "has_fail": has_fail,
        "coverage": coverage,
        "verdicts": verdicts,
        "details": {
            "market_gate": gate,
            "runtime_logs": {
                "count": len(entries),
                "next_id": logs["next_id"],
                "tail_count": logs["tail_count"],
                "sample": entries[-3:],
                "state": logs["state"],
            },
            "heartbeats": heartbeats,
            "divergence": divergence,
        },
    }


def _print_human(report: dict[str, Any]) -> None:
    icon = {"OK": "✓", "WARN": "!", "FAIL": "✗", "N/A": "—"}
    print(
        f"策略控制台验收报告  {report['generated_at']}  "
        f"user={report['user_id']} tenant={report['tenant_id']} market={report['market']}"
        f"  活跃策略={'是' if report['active_strategy'] else '否'}"
    )
    print("-" * 78)
    for key, v in report["verdicts"].items():
        print(f" {icon.get(v['level'], '?')} {key:<20} {v['message']}")
    cov = report.get("coverage") or {}
    total, participated = int(cov.get("total") or 0), int(cov.get("participated") or 0)
    if total and participated < total:
        # 未跑策略时大部分检查必然是 N/A——把「验了几项」写在脸上，避免把退出码 0
        # 误读成「运行态契约都过了」。要全绿请先启动一个策略再跑本仪器。
        print(
            f"\n 覆盖度：{participated}/{total} 项参与 "
            f"（{total - participated} 项因无活跃策略/闭市标 N/A，未构成验证；"
            f"启动一个策略后重跑可提升覆盖）"
        )
    details = report["details"]
    hb = details.get("heartbeats") or []
    if hb:
        print("\n 心跳：" + "，".join(
            f"{r.get('name')}={r.get('state')}(age={r.get('age')}s)" for r in hb
        ))
    rl = details.get("runtime_logs") or {}
    if rl.get("count"):
        print(f" 运行流：{rl['count']} 条，游标 next_id={rl['next_id']}，从游标再读 {rl['tail_count']} 条")
        for e in rl.get("sample") or []:
            print(f"   · [{e.get('level')}/{e.get('stage')}] {str(e.get('line'))[:70]}")
    gate = details.get("market_gate") or {}
    if gate:
        print(
            f" 市场闸门：运行={gate.get('active_market')} 声明={gate.get('declared_market') or '未声明'} "
            f"过滤 {gate.get('checked_strategies')} 条 泄漏 {gate.get('foreign_leak')}"
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="策略控制台验收报告（只读）")
    parser.add_argument("--user", default="10000001", help="用户 ID（默认实盘账户）")
    parser.add_argument("--tenant", default="default", help="租户 ID")
    parser.add_argument("--market", default="CN", help="页签市场口径（CN/HK/US/CRYPTO/FUTURES）")
    parser.add_argument("--json", action="store_true", help="JSON 输出")
    args = parser.parse_args(argv)

    report = build_report(args.user, args.tenant, args.market.upper())
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    else:
        _print_human(report)
    return 1 if report["has_fail"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
