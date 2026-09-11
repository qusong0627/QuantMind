#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""校验生成的 A 股策略模板（在 quantmind 容器内运行）。

校验内容：
1. 模板 .py 能被 exec，且提供 STRATEGY_CONFIG / get_strategy_config。
2. 策略类可实例化（内置类走 module_path 反射，自定义类走 namespace）。
3. 统计被 strip_unsupported_kwargs 静默丢弃的 kwargs（f_* 落到不支持的类上是典型问题）。
4. f_* 是否真的进入 fundamental_constraints。

用法（容器内）：
    docker exec quantmind python /app/scripts/validate_ashare_templates.py
"""

from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path

import pandas as pd

from backend.services.engine.qlib_app.schemas.backtest import QlibStrategyParams

TEMPLATE_DIR = Path("/app/strategy_templates")
if not TEMPLATE_DIR.exists():
    TEMPLATE_DIR = Path(__file__).resolve().parents[1] / "strategy_templates"

# 运行时由 Runtime 层用真实预测替换 <PRED>；校验时用空 Series 占位即可完成实例化
_DUMMY_SIGNAL = pd.Series(
    dtype="float64",
    index=pd.MultiIndex.from_arrays([[], []], names=["datetime", "instrument"]),
)
# 这些类不接受 only_tradable（kwargs 直接透传 BaseStrategy）
_NO_ONLY_TRADABLE = {"RedisCrashBuyDipStrategy"}

# f_* → FundamentalAligner 真实列名（features_daily 51 列中可用于过滤的部分）
_FEATURES_DAILY_COLUMNS = {
    "close", "ma5", "ma10", "ma20", "ma60", "ma_gap_5", "ma_gap_10", "ma_gap_20",
    "rsi_6", "rsi_14", "kdj_k", "kdj_d", "kdj_j", "macd_dif", "macd_dea", "macd_hist",
    "vol_std_5", "vol_std_20", "vol_std_60", "vol_atr_14", "vol_to_ma5", "vol_to_ma20",
    "volume_ma_3", "amount_ma_5", "volume_trend_3d", "return_1d", "return_3d",
    "return_5d", "return_10d", "return_20d", "return_60d", "pct_change", "beta_20",
    "Symbol_val", "close_val", "total_capital", "circulating_capital", "total_mv",
    "float_mv", "net_profit_ttm", "revenue_ttm", "equity", "annual_net_profit",
    "pe_ttm", "pe_static", "pb", "ps_ttm", "dividend_rate", "time", "symbol",
}
# 未来收益列：禁止用作过滤（标签泄漏）
_LEAKY_COLUMNS = {"return_1d", "return_3d", "return_5d", "return_10d", "return_20d", "return_60d"}


def _live_features_columns() -> set[str]:
    """从实际 features_daily 分区读列名；读不到就退回硬编码集合。"""
    try:
        import pyarrow.parquet as pq

        from backend.shared.fundamental_aligner import fundamental_aligner as fa

        day_dirs = sorted(fa.features_daily_path.glob("dt=*"))
        if not day_dirs:
            return set()
        files = sorted(day_dirs[-1].glob("*.parquet"))
        if not files:
            return set()
        return set(pq.read_table(files[0]).column_names)
    except Exception:  # noqa: BLE001 - 数据目录不可达时退回静态清单
        return set()


def _load_config(code: str) -> dict:
    namespace: dict = {}
    exec(compile(code, "<template>", "exec"), namespace)  # noqa: S102 - 模板代码由本仓库生成
    if callable(namespace.get("get_strategy_config")):
        return namespace["get_strategy_config"](), namespace
    if callable(namespace.get("get_strategy_instance")):
        return {"instance": namespace["get_strategy_instance"]()}, namespace
    config = namespace.get("STRATEGY_CONFIG")
    if not config:
        raise ValueError("未找到 STRATEGY_CONFIG / get_strategy_config")
    return config, namespace


def _resolve_class(config: dict, namespace: dict):
    class_name = config.get("class")
    if class_name in namespace:
        return namespace[class_name], True
    module_path = config.get("module_path")
    if not module_path:
        raise ValueError(f"类 {class_name} 不在 namespace 且无 module_path")
    module = importlib.import_module(module_path)
    return getattr(module, class_name), False


def main() -> int:
    # 打补丁：记录被静默丢弃的 kwargs
    import backend.services.engine.qlib_app.utils.recording_strategy as rs
    import backend.services.engine.qlib_app.utils.extended_strategies as es

    dropped_log: dict[str, list[str]] = {}
    original = rs.strip_unsupported_kwargs

    def spy(cls, kwargs, *, strategy_name=""):
        before = set(kwargs)
        result = original(cls, kwargs, strategy_name=strategy_name)
        gone = sorted(before - set(kwargs))
        if gone:
            dropped_log.setdefault(strategy_name or cls.__name__, []).extend(gone)
        return result

    rs.strip_unsupported_kwargs = spy
    es.strip_unsupported_kwargs = spy

    errors: list[str] = []
    warnings: list[str] = []
    checked = 0

    live_columns = _live_features_columns()
    features_columns = live_columns or _FEATURES_DAILY_COLUMNS
    print(
        f"features_daily 列来源：{'实时分区' if live_columns else '静态清单'}（{len(features_columns)} 列）"
    )

    for json_path in sorted(TEMPLATE_DIR.glob("as*.json")):
        template_id = json_path.stem
        py_path = json_path.with_suffix(".py")
        meta = json.loads(json_path.read_text(encoding="utf-8"))
        if not py_path.exists():
            errors.append(f"{template_id}: 缺少 .py")
            continue

        try:
            config, namespace = _load_config(py_path.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{template_id}: 加载失败 {exc!r}")
            continue

        if "instance" in config:
            errors.append(f"{template_id}: 使用了 get_strategy_instance（本批应统一用 STRATEGY_CONFIG）")
            continue

        kwargs = dict(config.get("kwargs") or {})
        f_keys = {k: v for k, v in kwargs.items() if k.startswith("f_")}
        for key in f_keys:
            column = key[2:].rsplit("_", 1)[0]
            if column in _LEAKY_COLUMNS:
                errors.append(f"{template_id}: {key} 使用未来收益列 {column}（标签泄漏）")
            elif column not in features_columns:
                errors.append(f"{template_id}: {key} 对应的列 {column} 不存在于 features_daily（会被静默跳过）")

        # UI 参数必须能通过回测请求 schema，否则 AI-IDE 点回测直接 422
        param_defaults = {p["name"]: p["default"] for p in meta.get("params", [])}
        schema_params = {
            name: value
            for name, value in param_defaults.items()
            if name in QlibStrategyParams.model_fields
        }
        try:
            QlibStrategyParams(**schema_params)
        except Exception as exc:  # noqa: BLE001
            detail = str(exc).splitlines()
            errors.append(f"{template_id}: UI 参数超出 QlibStrategyParams 约束 -> {detail[1:3] or detail[:1]}")
        for param in meta.get("params", []):
            low, high = param.get("min"), param.get("max")
            value = param.get("default")
            if low is not None and value is not None and value < low:
                errors.append(f"{template_id}: 参数 {param['name']} 默认值 {value} < min {low}")
            if high is not None and value is not None and value > high:
                errors.append(f"{template_id}: 参数 {param['name']} 默认值 {value} > max {high}")

        dropped_log.clear()
        try:
            cls, _is_local = _resolve_class(config, namespace)
            # signal 占位符 <PRED> 由 Runtime 替换；直接实例化需换成真实 Series
            run_kwargs = dict(kwargs)
            if run_kwargs.get("signal") == "<PRED>":
                run_kwargs["signal"] = _DUMMY_SIGNAL
            instance = cls(**run_kwargs)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{template_id}: 实例化失败 {exc!r}")
            continue

        checked += 1
        dropped = sorted({k for keys in dropped_log.values() for k in keys})
        if dropped:
            errors.append(f"{template_id}: kwargs 被静默丢弃 {dropped}")

        constraints = getattr(instance, "fundamental_constraints", None)
        if f_keys:
            if constraints is None:
                errors.append(f"{template_id}: 含 f_* 但策略类无 FundamentalFilterMixin（f_* 被丢弃）")
            else:
                expected = {k[2:]: v for k, v in f_keys.items()}
                if constraints != expected:
                    errors.append(
                        f"{template_id}: fundamental_constraints 不一致 期望={expected} 实际={constraints}"
                    )
                unknown = [c for c in constraints if c.rsplit("_", 1)[0] not in features_columns]
                if unknown:
                    errors.append(f"{template_id}: 过滤列不存在 {unknown}")

        class_name = config.get("class")
        if kwargs.get("only_tradable") is not True and class_name not in _NO_ONLY_TRADABLE:
            warnings.append(f"{template_id}: only_tradable 未显式置 True")
        if kwargs.get("signal") != "<PRED>":
            errors.append(f"{template_id}: signal 不是 <PRED>")

        # 与 JSON params 的默认值一致性（UI 滑块参数）
        param_defaults = {p["name"]: p["default"] for p in meta.get("params", [])}
        for key, value in kwargs.items():
            if key in param_defaults and param_defaults[key] != value:
                errors.append(
                    f"{template_id}: JSON 参数 {key} 默认值 {param_defaults[key]!r} != kwargs {value!r}"
                )

    print(f"校验模板 {checked} 个")
    if warnings:
        print(f"\n警告 {len(warnings)} 条：")
        for item in warnings:
            print("  -", item)
    if errors:
        print(f"\n错误 {len(errors)} 条：")
        for item in errors:
            print("  -", item)
        return 1
    print("\n全部通过 ✅")
    return 0


if __name__ == "__main__":
    sys.exit(main())
