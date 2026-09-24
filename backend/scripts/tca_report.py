#!/usr/bin/env python3
"""执行损耗 TCA 读数面 —— 「下单这件事，比决策时点的价贵了多少」。

取数在 ``backend.shared.exec_cost_source``（读时拼接 ``trades`` ⨝ ``orders`` ⨝
``qm_decision_ledger``），口径在 ``backend.shared.exec_cost``（纯算术，与 F2 保真度
共用同一条 ``slip_bps``）。本文件只做三件事：**组装读数、渲染纪律、落盘**。

判读纪律（与影子账、决策记分卡同一套）
--------------------------------------

* **样本 <30 只展示不结论**（``MIN_SAMPLE``，同 ``decision_ledger``／隔壁
  ``decision_track`` 同值同语义）。这条纪律必须出现在**终端输出里**：隔壁把它写进了
  scorecard 的字段、`print_report` 从不打印，读终端的人只看到一行光秃秃的均值；
* **"不可定价"与"0 bps"分开计数**——没有基准价时滑点算不出来，印成 `0.00` 会被读成
  "执行完美"。没数据就是没数据，报告说"无样本"；
* **成交率分母含零成交终态**——只数成交笔数会把"挂十单成一单"读成 100%；
* **正号 = 比基准差**（买贵了/卖便宜了），两侧同义，符号约定在 ``exec_cost`` 里写死。

已知边界（报告里原样印出，不藏进文档）
--------------------------------------

1. **手续费不在账**：QMT 通道落库时 ``commission``/``stamp_duty`` 写死 0.0
   （``qmt_exec_reconciler`` 的 Trade 组装），桥只回报价量 → 本读数是**价差损耗**，
   不含佣金/印花税/过户费；报告带**实测计数**印这句（窗口内几笔带费用），
   通道日后补了费用字段，这句话会自己变准；
2. **``fill_ts`` 是轮询观察到成交的时刻**（QMT 成交轮询 2s），不是交易所成交时刻
   → "提交→成交确认"这一列有秒级粒度误差；
3. **基准价 ``orders.ref_price`` 是后补的字段**（见 ``order_contract.ORDER_COLUMNS``）：
   字段上线前的历史成交没有基准价 → 进"不可定价"，**不拿成交价倒推假基准**；
4. **路径由幂等号优先识别**：``orders.remarks`` 会被成交回报覆盖（``apply_execution_report``
   的 `order.remarks = msg`），认不出来的一律计 ``unknown``，不猜。

用法::

    python backend/scripts/tca_report.py                     # 近 30 天，打印
    python backend/scripts/tca_report.py --days 0            # 全历史
    python backend/scripts/tca_report.py --days 90 --push    # 摘要推送（可选）
    python backend/scripts/tca_report.py --no-save           # 只打印不落盘

输出（``--out`` 目录，默认 ``data/reports/tca/``）::

    {YYYY-MM-DD}_tca.json   结构化读数（供后续消费）
    {YYYY-MM-DD}_tca.md     人读报告（同终端输出）
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.shared.exec_cost import merge_orders, summarize  # noqa: E402
from backend.shared.exec_cost_source import (  # noqa: E402
    CST,
    LoadResult,
    cst_day_bounds,
    load_samples,
)

logger = logging.getLogger(__name__)

EXIT_OK = 0
EXIT_ATTENTION = 1
EXIT_USAGE = 2

#: 与 ``decision_ledger.MIN_SAMPLE`` 同值同语义：30 以下的均值不配当结论。
MIN_SAMPLE = 30

#: 样本不足时的告警文案（测试按字面量钉住，改文案要连带改测试——刻意的）。
SMALL_SAMPLE_NOTE = f"⚠ 样本<{MIN_SAMPLE}，只展示不做结论"

#: 默认窗口（自然日）。与隔壁 TCA 报告的默认读数窗口一致。
DEFAULT_DAYS = 30

#: 默认账户（``QM_DECISION_ACCOUNT_USER_ID`` 同源，见 ``main``）。
ENV_ACCOUNT_USER = "QM_DECISION_ACCOUNT_USER_ID"

#: QMT 成交轮询间隔（``qmt_exec_poller.POLL_INTERVAL_SECONDS``）——边界②的粒度。
POLL_INTERVAL_SECONDS = 2.0


# ── 组装 ────────────────────────────────────────────────────────────
def reports_dir() -> Path:
    """报告目录（``QM_REPORTS_DIR`` 可覆盖，同 ``decision_ledger``）。"""
    return (
        Path(os.getenv("QM_REPORTS_DIR", str(PROJECT_ROOT / "data" / "reports"))) / "tca"
    )


def build_report(
    loaded: LoadResult,
    *,
    days: int,
    tenant_id: str,
    user_id: str,
    generated: str | None = None,
) -> dict[str, Any]:
    """``LoadResult`` → 读数 dict（**纯函数**：不碰库、不碰盘）。

    ``rows`` 是**合并后**的委托行（一笔单一行），``sample`` 是它的汇总。
    两者都进 JSON：``sample`` 给人读结论，``rows`` 给下钻（"那笔 300bp 是谁"）。
    """
    rows = merge_orders(loaded.samples)
    sample = summarize(rows, zero_fill_orders=loaded.zero_fill_orders)
    fees = [r for r in rows if _fees_of(r) > 0]
    return {
        "generated": generated
        or datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S"),
        "days": int(days),
        "tenant_id": str(tenant_id),
        "user_id": str(user_id),
        "window": list(loaded.window),
        "n_rows": len(loaded.samples),
        "n_orders": len(rows),
        "n_priced": sample["n"],
        "n_unpriced": sample["n_unpriced"],
        "n_zero_fill": int(loaded.zero_fill_orders or 0),
        "n_fees": len(fees),
        "fees_total": round(sum(_fees_of(r) for r in rows), 2),
        "coverage": dict(loaded.coverage),
        "sample": sample,
        "rows": rows,
        "caliber": (
            "正号=比基准差（买贵/卖便宜）；加权按成交额；"
            "基准=orders.ref_price（决策链取价时刻）；成交率分母含零成交终态"
        ),
    }


async def collect(
    *,
    days: int,
    tenant_id: str,
    user_id: str,
    today: date | None = None,
) -> dict[str, Any]:
    """取数 + 组装（CLI 的 async 入口；测试注入 ``load_samples`` 即可脱离库）。"""
    day = today or datetime.now(CST).date()
    loaded = await load_samples(
        days=days, tenant_id=tenant_id, user_id=user_id, today=day
    )
    if loaded.coverage.get("n_orphan"):
        # 拼不上订单的成交仍是真金白银（钱花了），但读不到基准/限价/决策时刻。
        # 它们不进样本，必须让运维看得见——这是"少了一笔"和"这笔拼不上"的区别。
        logger.warning(
            "[TCA] 有 %d 笔成交拼不上订单行（已排除在样本外，见报告 coverage）",
            loaded.coverage["n_orphan"],
        )
    return build_report(loaded, days=days, tenant_id=tenant_id, user_id=user_id)


def _fees_of(row: dict[str, Any]) -> float:
    value = row.get("fees")
    if isinstance(value, bool) or value is None:
        return 0.0
    try:
        out = float(value)
    except (TypeError, ValueError):
        return 0.0
    return out if out == out else 0.0


# ── 渲染 ────────────────────────────────────────────────────────────
def _fmt(value: Any, unit: str = "", nd: int = 2) -> str:
    """``None`` → ``—``（**不是 0**：缺口和 0 是两件事，见模块 docstring）。"""
    if value is None:
        return "—"
    try:
        return f"{float(value):,.{nd}f}{unit}"
    except (TypeError, ValueError):
        return "—"


def render(rep: dict[str, Any]) -> list[str]:
    """读数 → 人读行（含判读纪律与已知边界）。行尾一律不带空格。"""
    win = rep.get("window") or []
    cov = rep.get("coverage") or {}
    sample = rep.get("sample") or {}
    days = int(rep.get("days") or 0)
    span = f"近 {days} 天" if days > 0 else "全历史"
    # 两个"窗口"必须分开叫：`days` 是**取数**范围，`window` 是**取到的成交**落在哪几天。
    # 合成一句「窗口 09-10 ~ 09-11（近 30 天）」读者只会觉得自相矛盾，而真实情况
    # （"近 30 天里只有这两天有成交"）恰恰是读数时要看见的事实。
    win_s = f"{win[0]} ~ {win[-1]}" if win else "无（窗口内没有成交）"

    lines = [
        f"📉 执行损耗 TCA · {rep.get('generated') or ''}",
        f"账户 {rep.get('tenant_id')}/{rep.get('user_id')} · 取数 {span} · 成交区间 {win_s}",
        (
            f"成交 {rep.get('n_rows', 0)} 笔 → 委托 {rep.get('n_orders', 0)} 笔 · "
            f"可定价 {rep.get('n_priced', 0)} · 不可定价 {rep.get('n_unpriced', 0)} · "
            f"零成交终态 {rep.get('n_zero_fill', 0)} 笔"
        ),
    ]
    # 覆盖计数：只印非零项（全零时这一行消失，读的人不必扫一串 0）
    counters = [
        ("非真单", cov.get("n_not_real")),
        ("拼不上订单", cov.get("n_orphan")),
        ("合成成交(柜面均价)", cov.get("n_synth")),
        ("缺基准价", cov.get("n_missing_ref")),
        ("缺限价", cov.get("n_missing_limit")),
        ("路径未识别", cov.get("n_unknown_path")),
    ]
    shown = [f"{name} {int(n)}" for name, n in counters if n]
    if shown:
        # 「按成交笔」不是废话：上面那行的成交/委托数是**合并后**的委托数，而这几项
        # 是**合并前**的逐笔计数——不标注口径，读者会把两个数直接相减。
        lines.append("覆盖（按成交笔）：" + " · ".join(shown))

    if not sample.get("n"):
        lines.append("按路径/方向：**无样本**（没有可定价的成交——基准价缺失时不做结论）")
    else:
        lines.append("按路径/方向（**正 = 比基准差**；加权按成交额）：")
        lines.append(f"  {'分组':<22}{'n':>4}{'加权':>10}{'中位':>10}{'p10':>9}{'p90':>9}")
        for key, group in (sample.get("groups") or {}).items():
            if not group.get("n"):
                continue
            lines.append(
                f"  {key:<22}{group['n']:>4}{_fmt(group.get('slip_bps_w')):>10}"
                f"{_fmt(group.get('slip_bps_med')):>10}{_fmt(group.get('slip_bps_p10')):>9}"
                f"{_fmt(group.get('slip_bps_p90')):>9}"
            )
        lines.append(
            f"  合计 加权 {_fmt(sample.get('slip_bps_w'))} bps · "
            f"中位 {_fmt(sample.get('slip_bps_med'))} bps · "
            f"缓冲用掉中位 {_fmt(sample.get('cushion_used_med'))}% · "
            f"成交额 ¥{_fmt(sample.get('notional'), '', 0)}"
        )
        if sample.get("n") < MIN_SAMPLE:
            lines.append(f"  {SMALL_SAMPLE_NOTE}（当前 n={sample['n']}）")

    if sample.get("fill_rate") is not None:
        lines.append(
            f"成交率 {sample['fill_rate'] * 100:.1f}%（成交 {rep.get('n_orders', 0)} / "
            f"委托 {sample.get('n_orders', 0)} 笔；分母含零成交终态）"
        )
    if sample.get("decide_to_submit_min_med") is not None:
        lines.append(
            f"延迟：决策→提交 中位 {sample['decide_to_submit_min_med']:.1f} 分钟（轮级）· "
            f"提交→成交确认 中位 {_fmt(sample.get('submit_to_fill_min_med'))} 分钟"
            f"（轮询粒度 {POLL_INTERVAL_SECONDS:g}s）"
        )

    lines.append(
        f"判读：每组样本 <{MIN_SAMPLE} 只展示不做结论；正号=比基准差；"
        "「不可定价」与「零损耗」是两件事（前者是没测到，后者才是结论）"
    )
    lines.append(
        f"已知边界：①**手续费不在账**（窗口内 {rep.get('n_fees', 0)}/{rep.get('n_orders', 0)} "
        f"笔带费用，合计 ¥{_fmt(rep.get('fees_total'), '', 2)}——QMT 通道落库写 0.0，"
        "桥只回报价量）→ 本读数是价差损耗，不含佣金/印花税；"
        f"②fill_ts 是轮询**观察到**成交的时刻（粒度 {POLL_INTERVAL_SECONDS:g}s），"
        "不是交易所成交时刻；③基准价 ref_price 为后补字段，上线前的历史成交无基准价 "
        "→ 进不可定价，**不拿成交价倒推假基准**；④路径按幂等号识别，备注会被成交回报"
        "覆盖，认不出的计 unknown。"
    )
    return lines


def push_line(rep: dict[str, Any]) -> str:
    """推送摘要一行（没有可读数字时如实说没有，不许印 0 bps）。"""
    sample = rep.get("sample") or {}
    head = (
        f"执行损耗 TCA {rep.get('n_orders', 0)} 笔委托 · "
        f"可定价 {rep.get('n_priced', 0)} · 零成交 {rep.get('n_zero_fill', 0)}"
    )
    if not sample.get("n"):
        return head + "（无样本：基准价缺失，暂无可读数字）"
    body = (
        f"：加权 {_fmt(sample.get('slip_bps_w'))} bps"
        f" · 中位 {_fmt(sample.get('slip_bps_med'))} bps"
        f"（n={sample['n']}，样本 <{MIN_SAMPLE} 仅供参考）"
    )
    return head + body


# ── 落盘 ────────────────────────────────────────────────────────────
def _atomic_write(path: Path, text: str) -> None:
    """临时文件 + ``os.replace``：读到一半的 JSON 比没有更坏。"""
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def write_report(
    rep: dict[str, Any], out_dir: Path, *, stamp: str
) -> tuple[Path, Path]:
    """落盘 ``{stamp}_tca.json`` + ``{stamp}_tca.md``（同日重跑覆盖，报告是快照）。"""
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / f"{stamp}_tca.json"
    md_path = out_dir / f"{stamp}_tca.md"
    _atomic_write(
        json_path, json.dumps(rep, ensure_ascii=False, indent=2, default=str)
    )
    _atomic_write(md_path, "\n".join(render(rep)) + "\n")
    return json_path, md_path


# ── 推送 ────────────────────────────────────────────────────────────
def _push(rep: dict[str, Any], *, user_id: str, tenant_id: str) -> bool:
    """站内通知（**尽力而为**：推送失败不许把一个已经算好的报告变成失败）。

    与 ``real_mirror_service.notify`` 走同一个发布器（落库 → 前端通知中心）。
    """
    try:
        import asyncio

        from backend.shared.notification_publisher import publish_notification_async

        delivered = asyncio.run(
            publish_notification_async(
                user_id=user_id,
                tenant_id=tenant_id,
                title="执行损耗 TCA",
                content=push_line(rep),
                type="trading",
                level="info",
            )
        )
    except Exception as exc:  # noqa: BLE001 —— 通知失败不阻断读数
        logger.warning("[TCA] 通知推送失败: %s", exc)
        return False
    return bool(delivered)


# ── CLI ─────────────────────────────────────────────────────────────
def _add_window(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--days",
        type=int,
        default=DEFAULT_DAYS,
        help=f"回溯自然日（含今天；0=全历史，默认 {DEFAULT_DAYS}）",
    )
    parser.add_argument("--tenant", default="default", help="租户（默认 default）")
    parser.add_argument(
        "--user",
        default="",
        help=f"账户 user_id（默认与决策轮同源：resolve_db_account_user({ENV_ACCOUNT_USER})）",
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="执行损耗 TCA 读数面（P1.6）")
    _add_window(parser)
    parser.add_argument("--out", help="输出目录（默认 data/reports/tca/）")
    parser.add_argument("--no-save", action="store_true", help="只打印，不落盘")
    parser.add_argument("--json", action="store_true", help="只输出 JSON（给程序消费）")
    parser.add_argument("--push", action="store_true", help="把摘要推送到站内通知")
    args = parser.parse_args(argv)

    from backend.shared.simulation_account_keys import resolve_db_account_user

    user_id = args.user or resolve_db_account_user(ENV_ACCOUNT_USER)

    import asyncio

    try:
        rep = asyncio.run(
            collect(days=args.days, tenant_id=args.tenant, user_id=user_id)
        )
    except (ValueError, OSError, RuntimeError) as exc:
        print(f"[TCA] 环境/参数错误：{exc}")
        return EXIT_USAGE

    if args.json:
        print(json.dumps(rep, ensure_ascii=False, indent=2, default=str))
    else:
        print("\n".join(render(rep)))

    if args.push:
        _push(rep, user_id=user_id, tenant_id=args.tenant)

    if not args.no_save:
        out_dir = Path(args.out) if args.out else reports_dir()
        stamp = str(rep["generated"])[:10]
        json_path, md_path = write_report(rep, out_dir, stamp=stamp)
        if not args.json:
            print(f"\n[TCA] 已落盘 {json_path} / {md_path}")

    # 退出码：样本不足是**注意**不是错误（脚本/cron 可据此决定要不要推给人看）
    if (rep.get("sample") or {}).get("n", 0) < MIN_SAMPLE:
        return EXIT_ATTENTION
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
