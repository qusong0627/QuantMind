#!/usr/bin/env python3
"""盘后 23:00（北京）复盘 + 明日备选链编排（幂等）。

链：news_review（新闻情绪）→ daily_review（复盘 + 持仓 --watch）→ pick_candidates（明日候选池）
数据全走本机 QuantDB（duckdb 直查 parquet）+ PG；daily_review 缺推理信号时自动补跑。

⚠️ L2 因子 T+1：QuantDB l2_factors/l1_l2_factors 每天 ~00:31（本机 JST）落地。
   推理门禁依赖 l1_l2_factors → 当晚 23:00（北京）复盘可出，但「明日备选」必须等 L2 落地。
   → 两段式调度（本机 JST）：
     · 00:00（=北京 23:00）：本脚本（复盘；L2 未落地时备选步骤优雅跳过）
     · 00:35（=北京 23:35）：本脚本 --picks-only --wait-l2-min 30（等 L2 → 补推理 → 明日备选）

幂等：当日 news/stats/picks 已存在 → 跳过对应步骤（--force 重跑）。
持仓：读 BayMax-Trader logs/live_ledger.json 全部 agent 持仓 → daily_review --watch 传入。

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
import json
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
QDB = DATA / "quantdb"
LEDGER = Path("/home/zbox/BayMax-Trader/logs/live_ledger.json")
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


def load_holdings() -> list[str]:
    """全部 agent 持仓代码（去重保序）。"""
    if not LEDGER.exists():
        return []
    try:
        data = json.loads(LEDGER.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return []
    codes: list[str] = []
    for agent in data.get("agents", {}).values():
        for code in agent.get("positions", {}):
            if code not in codes:
                codes.append(code)
    return codes


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


def step_review(trade_date: str, watch: str, args) -> None:
    """复盘 + 持仓深度分析。"""
    stats_out = REVIEW_DIR / f"{trade_date[:4]}-{trade_date[4:6]}-{trade_date[6:]}_stats.json"
    if not args.force and stats_out.exists():
        log("② daily_review 已产出，跳过")
        return
    log("② daily_review" + (" --watch " + watch if watch else ""))
    cmd = [sys.executable, str(REVIEW_SCRIPT), "--date", trade_date]
    if watch:
        cmd += ["--watch", watch]
    run(cmd, args.dry_run)


def step_picks(trade_date: str, args) -> None:
    """明日备选：等 L2 → 补推理 → pick_candidates（失败自动补跑推理重试一次）。"""
    picks_out = PICKS_DIR / f"{trade_date}_picks.json"
    if not args.force and picks_out.exists():
        log("③ pick_candidates 已产出，跳过")
        return

    # L2 因子 T+1（~00:31 JST 落地），未落地时按模式处理
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
            log("③ L2 因子 T+1 未落地（正常），备选由 00:35 --picks-only cron 完成")
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
        holdings = load_holdings()
        watch = ",".join(holdings)
        if holdings:
            log(f"持仓（--watch）：{watch}")
        else:
            log("无持仓文件，复盘不带 --watch")
        step_news(trade_date, args)
        step_review(trade_date, watch, args)

    step_picks(trade_date, args)
    log("完成。")


if __name__ == "__main__":
    main()
