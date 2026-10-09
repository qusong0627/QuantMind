#!/usr/bin/env python3
"""日志治理（P1-3）：滚动清理产物目录（默认保留 30 天）+ 过期任务清理。
用法：python scripts/log_cleanup.py [--days 30] [--dry]
cron：每周日 北京 05:00。
"""
import argparse
import shutil
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# 产物目录（按 mtime 清理旧文件，保留目录结构）
# 2026-09-29 随 keeper 迁入 QuantMind：baymax 独有目录（debates/daily_report/
# min_snapshots）不再列入；keeper 自身的 stdout/stderr 落在 logs/keepers/。
TARGETS = [
    ROOT / "logs" / "keepers",         # keeper 定时任务日志（迁入后新增）
    ROOT / "logs" / "review",          # 盘后复盘
    ROOT / "logs" / "night_pool",      # 晚间研究（大脑待改接 QM dsh，见 night_pool_agent.py）
    ROOT / "logs" / "budget",          # 风险预算明细
]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--dry", action="store_true")
    a = ap.parse_args()
    cutoff = time.time() - a.days * 86400
    removed = 0
    for d in TARGETS:
        if not d.is_dir():
            continue
        for f in d.rglob("*"):
            if f.is_file() and f.stat().st_mtime < cutoff:
                if not a.dry:
                    f.unlink()
                print(f"{'[dry] ' if a.dry else ''}清理 {f.relative_to(ROOT)}")
                removed += 1
    # analysis_jobs 终态 >3 天清理
    jf = ROOT / "logs" / "analysis_jobs.jsonl"
    if jf.is_file():
        import json

        # 逐行容错 + errors="replace"（含中文的追加写队列，批 10/13）：旧实现里一行坏
        # JSON 直接抛穿 main()，周日的 cron 每周必崩；撕裂的多字节字符同样致命。
        try:
            text = jf.read_text(encoding="utf-8", errors="replace")
        except OSError:
            text = ""
        rows, bad = [], 0
        for l in text.splitlines():
            if not l.strip():
                continue
            try:
                r = json.loads(l)
            except json.JSONDecodeError:
                r = None
            if not isinstance(r, dict):
                bad += 1
                continue
            if r.get("status") == "pending" or (r.get("done_ts") or "") >= \
                    time.strftime("%Y-%m-%dT%H:%M", time.localtime(time.time() - 3 * 86400)):
                rows.append(r)
        if bad:
            print(f"⚠️ analysis_jobs 有 {bad} 行无法解析，已跳过（其余 {len(rows)} 条保留）")
        # 行数对不上（含坏行）就重写一次 = 顺手把坏行从盘上剔除，不做二次读盘
        if not a.dry and len(rows) != sum(1 for _ in text.splitlines() if _.strip()):
            jf.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n",
                          encoding="utf-8")
    print(f"✅ {'dry-run: ' if a.dry else ''}共清理 {removed} 个过期文件（>{a.days} 天）")
    return 0


if __name__ == "__main__":
    sys.exit(main())