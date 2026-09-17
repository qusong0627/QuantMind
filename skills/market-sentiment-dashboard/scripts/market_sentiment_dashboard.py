#!/usr/bin/env python3
"""市场情绪仪表盘：盘面状态 + 情绪温度（quantdb 昨日全景）+ 实时口径提示。

输出 JSON 三节事实，供 AI 生成「市场情绪报告」。口径纪律见 SKILL.md。
"""
import glob
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]  # dsh/skills/<name>/scripts → 仓库根
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))


def quantdb_breadth() -> dict | None:
    roots = (
        "/home/zbox/projects/quantmind/data/quantdb/5_technical_derived/market_sentiment",
        "/quantmind/data/quantdb/5_technical_derived/market_sentiment",
        "/data/quantdb/5_technical_derived/market_sentiment",
    )
    root = next((p for p in roots if os.path.isdir(p)), None)
    if not root:
        return None
    ds = sorted(int(os.path.basename(f).split("=")[1])
                for f in glob.glob(f"{root}/dt=*"))
    if not ds:
        return None
    try:
        import duckdb

        df = duckdb.connect().execute(
            f"SELECT momentum_1d, buy_pressure, sell_pressure "
            f"FROM read_parquet('{root}/dt={ds[-1]}')").df()
    except Exception:  # noqa: BLE001
        return None
    if df.empty:
        return None
    mom = df["momentum_1d"].dropna()
    total_all = int(len(mom))
    mom = mom[mom.abs() <= 10]
    total = int(len(mom))
    if total < 1 or total / max(total_all, 1) < 0.9:
        return {"source": f"dt={ds[-1]}", "ok": False, "note": "极端值占比过高，视为脏数据"}
    up, down = int((mom > 0).sum()), int((mom < 0).sum())
    if (up + down) / total < 0.8:
        return {"source": f"dt={ds[-1]}", "ok": False, "note": "涨跌覆盖不完整"}
    return {
        "ok": True, "source": f"dt={ds[-1]}", "total": total,
        "up": up, "down": down,
        "up_down_ratio": round(up / max(down, 1), 2),
        "momentum_mean": round(float(mom.mean()), 3),
        "buy_pressure": round(float(df["buy_pressure"].mean()), 3)
        if "buy_pressure" in df else None,
        "sell_pressure": round(float(df["sell_pressure"].mean()), 3)
        if "sell_pressure" in df else None,
        "is_today": False,  # EOD 口径，明确标注
    }


def market_state() -> dict:
    try:
        from market_state import build_market_state

        try:
            from agent_tools.brokers.tdx_bridge import TdxBridgeBroker

            return {"ok": True, "state": build_market_state(TdxBridgeBroker())}
        except Exception as exc:  # noqa: BLE001 桥/依赖不可用 → 降级（无桥分支）
            return {"ok": True, "degraded": True,
                    "state": build_market_state(None) + f"（桥不可用降级：{exc}）"}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}


def main() -> int:
    out = {"market_state": market_state(), "sentiment": quantdb_breadth(),
           "realtime_note": "盘中实时温度使用：量比>1.5/五档失衡±强信号/5分钟涨速±1%"
                            "（与分析提示词同口径）；不使用 quantdb 昨日温度冒充实时；"
                            "桥 get_zdt_data 仅支持单股状态、全市场家数未验证（2026-09-03 探针结论）。"}
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())