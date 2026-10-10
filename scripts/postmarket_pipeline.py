#!/usr/bin/env python3
"""盘后复盘 + 明日备选链编排（幂等）。

链：news_review（新闻情绪）→ daily_review（复盘 + 持仓 --watch）→ pick_candidates（明日候选池）
数据全走本机 QuantDB（duckdb 直查 parquet）+ PG；daily_review 缺推理信号时自动补跑。

⚠️ L2 因子 T+1：QuantDB l2_factors/l1_l2_factors 每天凌晨落地
   （2026-10-10 实测 ~01:18 北京）。推理门禁依赖 l1_l2_factors。
   → 两段式调度（宿主 crontab，本机 JST 周二~周六）：
     · 04:00（=北京 03:00）：本脚本全量（复盘 + 备选）
     · 05:00（=北京 04:00）：本脚本 --picks-only --wait-l2-min 30（补跑）

幂等：当日 news/stats 已存在 → 跳过对应步骤（--force 重跑）；picks 恒重算
（产物按 buy_date 命名，按数据日判存在会错位跳过——见 step_picks docstring）。
持仓：读 PG ``real_account_snapshots``（决策轮同源并集）→ daily_review --watch 传入；
快照停更/缺失/读失败时**空段 + 报告显式标注**（--watch-note），绝不按旧名单编复盘
（T3-1，2026-10-10：旧实现读 BayMax live_ledger.json，该文件 2026-09-29 已停更）。

用法：
  python3 postmarket_pipeline.py                  # 最新交易日（复盘；备选随 L2 就绪情况）
  python3 postmarket_pipeline.py --picks-only     # 只跑明日备选（推理 + 候选池）
  python3 postmarket_pipeline.py --picks-only --wait-l2-min 30   # 等 L2 分区落地最多 30 分钟
  python3 postmarket_pipeline.py --date 20260831
  python3 postmarket_pipeline.py --force          # 强制重跑全部
  python3 postmarket_pipeline.py --dry-run        # 只打印步骤不执行
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    # cron 从 $HOME 起跑（cwd 不是仓库）：持仓同源读取要 import backend.*，靠它兜底。
    sys.path.insert(0, str(ROOT))
DATA = ROOT / "data"
QDB = DATA / "quantdb"
REVIEW_DIR = DATA / "reports" / "daily_review"
PICKS_DIR = DATA / "reports" / "stock_picks"
REVIEW_SCRIPT = ROOT / "skills" / "daily-review" / "scripts" / "daily_review.py"
NEWS_SCRIPT = ROOT / "skills" / "daily-review" / "scripts" / "news_review.py"
PICKS_SCRIPT = ROOT / "skills" / "stock-picks" / "scripts" / "pick_candidates.py"
TRIGGER_SCRIPT = ROOT / "skills" / "daily-review" / "scripts" / "trigger_inference.py"
# 推理门禁数据集（L2 T+1，每天 ~00:31 JST 落地）
L2_PART = lambda d: QDB / "6_ml_datasets" / "l2_factors" / f"dt={d}" / "data.parquet"


def log(msg: str) -> None:
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


def run(cmd: list[str], dry_run: bool) -> None:
    log("$ " + " ".join(str(c) for c in cmd))
    if dry_run:
        return
    r = subprocess.run(cmd, cwd=ROOT)
    if r.returncode != 0:
        sys.exit(f"步骤失败（{r.returncode}）：{' '.join(str(c) for c in cmd)}")


def resolve_trade_date() -> str:
    """最新交易日 = daily_unadjusted 分区最大 dt。"""
    import duckdb  # noqa: PLC0415 宿主已装，延迟导入加速 --dry-run

    db = duckdb.connect()
    base = QDB / "1_kline_data" / "daily_unadjusted"
    return str(db.execute(
        f"SELECT max(dt) FROM read_parquet('{base}/dt=*/data.parquet', hive_partitioning=true)"
    ).fetchone()[0])


_CST = timezone(timedelta(hours=8))  # 北京墙钟=UTC+8（无夏令时）；不走 zoneinfo 免 tzdata


def _pg_connect():
    """盘后链 PG 连接（psycopg2）：与 skills 侧 ``inference_signals.pg_connect``
    **同一 env 契约**（``POSTGRES_*``，缺省回环）——crontab 头部把 ``POSTGRES_HOST``
    指到 DB 实际发布地址（回环 5432 无监听，见 crontab 注释，2026-10-08 修过一次）。"""
    import psycopg2  # noqa: PLC0415 宿主已装；延迟导入让 --picks-only 路径不碰它

    return psycopg2.connect(
        host=os.getenv("POSTGRES_HOST", "127.0.0.1"),
        port=int(os.getenv("POSTGRES_PORT", "5432")),
        user=os.getenv("POSTGRES_USER", "quantmind"),
        password=os.getenv("POSTGRES_PASSWORD", "quantmind2026"),
        dbname=os.getenv("POSTGRES_DB", "quantmind"),
        connect_timeout=5,
    )


def _read_snapshot_positions() -> tuple[dict, dict]:
    """实盘快照持仓 ``(prefix 持仓表, 源元信息)``——决策轮同源（同表同口径）。

    ``active_source`` 不传：复盘只用**代码集合**（表里没有成交量/出处列），活跃券商
    只影响「同票取谁的量」，不影响并集本身；顺带免掉宿主链对交易库 Redis 的依赖。
    读失败抛异常，由 load_holdings 标注成注记。"""
    from backend.services.trade.services.decision_round_core import (  # noqa: PLC0415
        ENV_ACCOUNT_USER,
        TENANT_ID,
    )
    from backend.shared.real_positions import load_real_positions_sync  # noqa: PLC0415
    from backend.shared.simulation_account_keys import (  # noqa: PLC0415
        resolve_db_account_user,
    )

    return load_real_positions_sync(
        TENANT_ID, resolve_db_account_user(ENV_ACCOUNT_USER), connect=_pg_connect
    )


def _snapshot_cst(raw: object) -> datetime | None:
    """merge 元信息里的 ``snapshot_at``（ISO 串，实测 naive **UTC**）→ aware 北京时间。

    折算成北京墙钟再比日期，是为了扛住午夜边界：复盘跑在凌晨（cron 03:00 北京），
    写侧凌晨的整点快照其 UTC 日期还停在**前一天**——按 UTC 日期比会把「刚刚还在写」
    的活源误判成停更。
    """
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(str(raw))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(_CST)


def holdings_for_review(positions: dict, meta: dict, trade_date: str) -> tuple[list[str], str]:
    """快照（merge 产物）→ 复盘用的 ``(watch 代码表[后缀式], 报告注记)``（纯函数）。

    新鲜度门（审计 H1）：快照必须覆盖复盘日（北京时间口径，见 ``_snapshot_cst``）——
    判停更时返回**空表 + 显式标注**，绝不拿旧名单编持仓段。缺失/`None` 时间同判。
    新鲜时注记快照时刻与并集来源；相对停更（60 分钟口径）被 merge 剔除的源也点名，
    不静默蒸发一整个账户。
    """
    from backend.shared.stock_utils import StockCodeUtil  # noqa: PLC0415

    sources = (meta or {}).get("sources") or {}
    if not sources:
        return [], (
            "⚠️ 实盘快照源缺失：real_account_snapshots 无记录"
            "（盘后链未取到任何账户快照），持仓段留空。"
        )
    ts = _snapshot_cst(meta.get("snapshot_at"))
    trade_day = datetime.strptime(trade_date, "%Y%m%d").date()
    if ts is None or ts.date() < trade_day:
        shown = f"{ts:%Y-%m-%d}" if ts else "时间未知"
        return [], (
            f"⚠️ 持仓源 {shown} 停更：实盘快照最新一条早于复盘日 "
            f"{trade_day:%Y-%m-%d}（快照未随当日更新），持仓段留空——绝不按旧名单编复盘。"
        )
    codes = sorted({StockCodeUtil.to_suffix(sym) for sym in positions})
    kept = [s for s, m in sources.items() if not (m or {}).get("stale")]
    dropped = [s for s, m in sources.items() if (m or {}).get("stale")]
    note = (
        f"持仓源：实盘快照 real_account_snapshots（{ts:%Y-%m-%d %H:%M} 北京，"
        f"{'/'.join(kept)} 并集，{len(codes)} 只）"
    )
    if dropped:
        note += f"；{'/'.join(dropped)} 相对停更未并入"
    return codes, note


def load_holdings(trade_date: str, *, read_positions=None) -> tuple[list[str], str]:
    """复盘持仓 ``(watch 代码表, 报告注记)``。

    旧实现读 BayMax-Trader 的 live_ledger.json——那份文件 2026-09-29 起停更，复盘却
    每晚照它编持仓段（2026-10-10 审计 H1 实锤的僵尸账本）；现源 = PG
    ``real_account_snapshots``（决策轮同源）。读取失败不炸链：返回空表 + 显式注记
    （宁空段不编）。``read_positions`` 注入点：测试换替身，默认真连库。
    """
    read = read_positions or _read_snapshot_positions
    try:
        positions, meta = read()
    except Exception as exc:  # noqa: BLE001 持仓源不可读不许带走整条盘后链
        return [], f"⚠️ 实盘快照读取失败（{type(exc).__name__}: {exc}），持仓段留空。"
    return holdings_for_review(positions, meta, trade_date)


def step_news(trade_date: str, args) -> None:
    """新闻情绪（宿主失败回退容器）。"""
    news_out = REVIEW_DIR / f"{trade_date[:4]}-{trade_date[4:6]}-{trade_date[6:]}_news.json"
    if not args.force and news_out.exists():
        log("① news_review 已产出，跳过")
        return
    log("① news_review")
    cmd = [sys.executable, str(NEWS_SCRIPT), "--date", trade_date]
    if args.dry_run:
        run(cmd, True)
        return
    if subprocess.run(cmd, cwd=ROOT).returncode == 0:
        return
    if not shutil.which("docker"):
        sys.exit("① news_review 宿主失败且无 docker")
    log("宿主执行失败，回退容器 docker cp + exec")
    run(["docker", "cp", str(NEWS_SCRIPT), "quantmind:/tmp/news_review.py"], False)
    run(["docker", "exec", "-w", "/app", "quantmind", "python3",
         "/tmp/news_review.py", "--date", trade_date], False)


def step_review(trade_date: str, watch: str, watch_note: str, args) -> None:
    """复盘 + 持仓深度分析。"""
    stats_out = REVIEW_DIR / f"{trade_date[:4]}-{trade_date[4:6]}-{trade_date[6:]}_stats.json"
    if not args.force and stats_out.exists():
        log("② daily_review 已产出，跳过")
        return
    log("② daily_review" + (" --watch " + watch if watch else ""))
    cmd = [sys.executable, str(REVIEW_SCRIPT), "--date", trade_date]
    if watch:
        cmd += ["--watch", watch]
    if watch_note:
        cmd += ["--watch-note", watch_note]
    run(cmd, args.dry_run)


def step_picks(trade_date: str, args) -> None:
    """明日备选：等 L2 → 补推理 → pick_candidates（失败自动补跑推理重试一次）。

    恒重算、不做「已产出」跳过：产物按 buy_date（信号日=目标交易日）命名，而这里的
    ``trade_date`` 是数据截止日——按 ``{trade_date}_picks.json`` 判存在会**错位跳过**
    次日的产出（2026-10-10 实锤：10-09 的补救池文件令 10-12 的池在 04:00/05:00 两跑
    都被跳过，周一空池）。该步幂等、无 LLM 成本，重算最稳。
    """
    # L2 因子 T+1（凌晨落地），未落地时按模式处理
    if not L2_PART(trade_date).exists():
        if args.wait_l2_min > 0 and not args.dry_run:
            for i in range(args.wait_l2_min):
                log(f"等 L2 落地 {i + 1}/{args.wait_l2_min} 分钟…")
                time.sleep(60)
                if L2_PART(trade_date).exists():
                    break
            if not L2_PART(trade_date).exists():
                sys.exit(f"③ L2 分区 {L2_PART(trade_date)} 超时未落地")
        elif not args.picks_only:
            log("③ L2 因子 T+1 未落地（正常），备选由 05:00（JST）--picks-only cron 完成")
            return
        # picks-only 且未等待：直接尝试，推理门禁会失败并给出明确错误

    cmd = [sys.executable, str(PICKS_SCRIPT), "--data-date", trade_date,
           "--top", str(args.top), "--json"]
    if args.dry_run:
        run(cmd, True)
        return
    log(f"③ pick_candidates --top {args.top}")
    r = subprocess.run(cmd, cwd=ROOT)
    if r.returncode == 0:
        return
    log("③ 失败，疑似缺当日推理批次 → 容器内补跑 trigger_inference")
    if not shutil.which("docker"):
        sys.exit(f"③ pick_candidates 失败且无 docker 可补推理：{r.returncode}")
    run(["docker", "cp", str(TRIGGER_SCRIPT), "quantmind:/tmp/trigger_inference.py"], False)
    run(["docker", "exec", "-w", "/app", "quantmind", "python3",
         "/tmp/trigger_inference.py", "--date", trade_date], False)
    log("③ 重试 pick_candidates")
    run(cmd, False)


def main() -> None:
    ap = argparse.ArgumentParser(description="盘后复盘 + 明日备选链")
    ap.add_argument("--date", help="交易日 YYYYMMDD（默认最新交易日）")
    ap.add_argument("--force", action="store_true", help="已产出也重跑")
    ap.add_argument("--dry-run", action="store_true", help="只打印将执行的步骤")
    ap.add_argument("--picks-only", action="store_true", help="只跑明日备选（推理 + 候选池）")
    ap.add_argument("--wait-l2-min", type=int, default=0, help="等 L2 分区落地最多 N 分钟")
    ap.add_argument("--top", type=int, default=30, help="候选池规模（默认 30）")
    args = ap.parse_args()

    trade_date = args.date or resolve_trade_date()
    log(f"交易日：{trade_date}")

    if not args.picks_only:
        holdings, holdings_note = load_holdings(trade_date)
        watch = ",".join(holdings)
        if holdings:
            log(f"持仓（--watch {len(holdings)} 只）：{watch}")
        else:
            log("持仓段留空，复盘不带 --watch")
        if holdings_note:
            log(f"持仓注记：{holdings_note}")
        step_news(trade_date, args)
        step_review(trade_date, watch, holdings_note, args)

    step_picks(trade_date, args)
    log("完成。")


if __name__ == "__main__":
    main()
