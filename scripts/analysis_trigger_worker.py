#!/usr/bin/env python3
"""手动分析触发 worker（宿主 cron 每分钟）：消费「立即分析」任务台账的 pending。

链路：前端「立即分析」→ api 写任务行（``/data/logs/analysis_jobs.jsonl``，
台账实现 ``backend/services/agent_arena/analysis_ledger.py``）→ 本 worker 拉起
执行脚本：
  - ``type=news`` → ``scripts/news_brief.py``（QM 原生新闻管线，不受交易日闸门限制）
  - ``type=live`` → ``scripts/live_model_analysis.py``（模型对话轮；**只出观点
    不下单**——上游「时段内可真下单 / 盘外 --force」语义不恢复，故无窗口分支）

管线自身有 flock 防叠跑（live_model_analysis 的 .live_model_analysis.lock、
news_brief 的增量游标）；忙碌时任务留 pending 下轮自动补跑。台账读写一律经
ledger 模块（跨进程 flock + 原子替换），本文件不直接碰文件。
"""

import fcntl
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from backend.services.agent_arena import analysis_ledger as ledger  # noqa: E402

LOCK = ledger.jobs_file().parent / ".analysis_worker.lock"
TIMEOUT_S = 900
BJ = timezone(timedelta(hours=8))


def _now() -> str:
    return datetime.now(BJ).isoformat(timespec="seconds")


def _pick_pending(is_trading_day: bool) -> dict | None:
    """取下一个可跑任务：新闻优先且不受闸门限制；live 仅交易日可跑。

    台账是追加型小文件（任务个位数量级），每次重读保持与并发追加一致
    （不在锁外缓存整表再写回，避免覆盖 api 刚追加的行）。
    """
    rows = ledger.load_jobs()
    pend = [r for r in rows if r.get("status") == "pending"]
    news = [r for r in pend if r.get("type") == "news"]
    if news:
        return news[0]
    if is_trading_day:
        live = [r for r in pend if r.get("type", "live") != "news"]
        if live:
            return live[0]
    return None


def _holiday_note_once(why: str, n_held: int) -> None:
    """休市提示每日只打一次（防每分钟 cron 刷屏）。"""
    day_key = _now()[:10]
    note = LOCK.parent / ".worker_holiday_note"
    try:
        seen = note.read_text(encoding="utf-8").strip() == day_key
    except OSError:
        seen = False
    if not seen:
        try:
            note.write_text(day_key, encoding="utf-8")
        except OSError:
            pass
        print(f"[{_now()}] {why}，{n_held} 个分析任务保持 pending（下个交易日补跑）")


def _cmd_for(job: dict) -> list[str]:
    if job.get("type") == "news":
        return [sys.executable, str(ROOT / "scripts" / "news_brief.py")]
    agents = job.get("agents") or "all"
    names = "" if agents == "all" else ",".join(agents)
    cmd = [sys.executable, str(ROOT / "scripts" / "live_model_analysis.py")]
    if names:
        cmd += ["--agents", names]
    return cmd


def main() -> int:
    is_trading_day = True
    try:
        from trading_cal import is_trading_day as _is_td, why_not

        bj_today = datetime.now(BJ).date()
        if bj_today.weekday() < 5:
            is_trading_day = _is_td(bj_today)
            if not is_trading_day:
                held = sum(
                    1
                    for r in ledger.load_jobs()
                    if r.get("status") == "pending" and r.get("type", "live") != "news"
                )
                if held:
                    _holiday_note_once(why_not(bj_today), held)
    except Exception:  # noqa: BLE001 日历不可用 → 维持原行为（按交易日跑）
        pass

    if _pick_pending(is_trading_day) is None:
        return 0

    try:
        lock = LOCK.open("a")
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return 0  # 上一轮未结束，保持 pending 下轮再试

    try:
        while True:
            job = _pick_pending(is_trading_day)
            if job is None:
                break
            ledger.update_job(job["id"], status="running", start_ts=_now())
            cmd = _cmd_for(job)
            try:
                proc = subprocess.run(
                    cmd, capture_output=True, text=True, timeout=TIMEOUT_S
                )
                out = (proc.stdout or "") + (proc.stderr or "")
                tail = " | ".join(out.strip().splitlines()[-2:])[:200]
                if "已有分析实例在跑" in out:
                    ledger.update_job(
                        job["id"], status="pending", note="分析实例正忙，留待下轮补跑"
                    )
                    print(f"[{_now()}] {job['id']} 忙，回 pending")
                    break  # 管线在忙，本轮到这为止（下分钟再试）
                elif proc.returncode == 0:
                    ledger.update_job(
                        job["id"], status="done", done_ts=_now(), note=tail or "ok"
                    )
                    print(f"[{_now()}] {job['id']} → done: {tail[:120]}")
                else:
                    ledger.update_job(
                        job["id"],
                        status="failed",
                        done_ts=_now(),
                        note=tail or f"rc={proc.returncode}",
                    )
                    print(
                        f"[{_now()}] {job['id']} → failed(rc={proc.returncode}): "
                        f"{tail[:120]}"
                    )
            except subprocess.TimeoutExpired:
                ledger.update_job(
                    job["id"],
                    status="failed",
                    done_ts=_now(),
                    note=f"超时（>{TIMEOUT_S}s）",
                )
                print(f"[{_now()}] {job['id']} 超时（>{TIMEOUT_S}s）")
        removed = ledger.cleanup_old()
        if removed:
            print(f"[{_now()}] 清理 {removed} 条过期任务")
        return 0
    finally:
        try:
            fcntl.flock(lock, fcntl.LOCK_UN)
        except OSError:
            pass


if __name__ == "__main__":
    sys.exit(main())
