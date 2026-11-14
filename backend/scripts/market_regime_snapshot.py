"""生成收盘「市场阶段」快照（研究总控 / 交易 agent 共用）。

用法::

    python -m backend.scripts.market_regime_snapshot                 # 最新交易日
    python -m backend.scripts.market_regime_snapshot --date 2026-11-13
    python -m backend.scripts.market_regime_snapshot --date 2026-11-13 --print
    python -m backend.scripts.market_regime_snapshot --universe 000300.SH

落盘：``${QM_MARKET_SNAPSHOT_DIR:-/data/market-analysis}/regime/{date}.json``
（ALL 口径另有 ``latest.json``）。研究总控收盘档会自动 ``ensure_snapshot``，
本脚本用于手工补算/验收。
"""

from __future__ import annotations

import argparse
import sys

from backend.shared.market_stage_snapshot import (
    build_snapshot,
    render_stage_text,
    write_snapshot,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="生成收盘市场阶段快照")
    parser.add_argument("--date", default=None, help="YYYY-MM-DD；省略=最新交易日")
    parser.add_argument("--universe", default="ALL", help="指数宇宙，默认 ALL")
    parser.add_argument(
        "--print", dest="print_text", action="store_true", help="打印渲染文本"
    )
    args = parser.parse_args(argv)

    try:
        snap = build_snapshot(args.date, args.universe)
        path = write_snapshot(snap)
    except Exception as exc:  # noqa: BLE001 - CLI 失败要显式退出
        print(
            f"[market-regime-snapshot] 失败：{type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        return 1
    print(f"[market-regime-snapshot] 写入 {path}")
    if args.print_text:
        print(render_stage_text(snap))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
