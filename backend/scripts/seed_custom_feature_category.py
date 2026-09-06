"""追加自定义因子目录（含 OHLCV 演示因子），幂等。"""
from __future__ import annotations

import json
from pathlib import Path

JSON_PATH = Path(__file__).resolve().parents[2] / "config" / "features" / "model_training_feature_catalog_v1.json"

DEMOS = [
    {
        "key": "custom_ret_1d",
        "name": "自定义1日涨跌幅",
        "explanation": "当日收盘价相对前收的涨跌幅，演示用（OHLCV直接计算）",
        "formula": "(close / pre_close) - 1",
        "source": "OHLCV演示",
    },
    {
        "key": "custom_amp_1d",
        "name": "自定义1日振幅",
        "explanation": "当日最高最低价相对前收的振幅，演示用",
        "formula": "(high - low) / pre_close",
        "source": "OHLCV演示",
    },
    {
        "key": "custom_close_pos",
        "name": "自定义收盘位置",
        "explanation": "收盘价在当日最高最低区间中的相对位置（0=最低，1=最高），演示用",
        "formula": "(close - low) / (high - low)",
        "source": "OHLCV演示",
    },
    {
        "key": "custom_vol_ratio_5",
        "name": "自定义5日量比",
        "explanation": "当日成交量与近5日均量的比值，演示用",
        "formula": "volume / mean(volume, 5)",
        "source": "OHLCV演示",
    },
    {
        "key": "custom_amt_ma_5",
        "name": "自定义5日成交额均线",
        "explanation": "近5日成交额均值，衡量短期资金活跃度，演示用",
        "formula": "mean(amount, 5)",
        "source": "OHLCV演示",
    },
]


def main() -> None:
    raw = json.loads(JSON_PATH.read_text(encoding="utf-8"))
    cats = raw.get("categories", [])
    if any(c.get("id") == "custom" for c in cats):
        print("custom category already exists, skip")
        return
    max_order = max((int(c.get("order") or 0) for c in cats), default=0)
    feats = []
    for i, d in enumerate(DEMOS, start=1):
        feats.append(
            {
                "feature_id": f"feat_custom_{i:03d}",
                "key": d["key"],
                "description": d["name"],
                "feature_name": d["name"],
                "explanation": d["explanation"],
                "formula": d["formula"],
                "source": d["source"],
                "enabled": True,
                "markets": ["CN", "HK", "US", "CRYPTO", "FUTURES"],
                "order_no": i,
                "default_selected": False,
            }
        )
    cats.append(
        {
            "id": "custom",
            "name": "自建因子",
            "order": max_order + 1,
            "feature_count": len(feats),
            "features": feats,
        }
    )
    raw["feature_count"] = sum(len(c.get("features", [])) for c in cats)
    JSON_PATH.write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"added custom category with {len(feats)} demos, total={raw['feature_count']}")


if __name__ == "__main__":
    main()
