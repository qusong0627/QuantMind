"""市场状态与动态仓位服务"""

import logging
import os
from typing import Any, Dict, Optional, Tuple

import pandas as pd
from backend.services.engine.qlib_app.utils.structured_logger import StructuredTaskLogger

# 尝试导入本地的模拟 qlib 模块
try:
    from backend.services.engine.qlib_mock import D

    logger = logging.getLogger(__name__)
    task_logger = StructuredTaskLogger(logger, "MarketStateService")
    task_logger.info("module_source", "使用本地模拟 qlib 模块", source="qlib_mock")
except ImportError:
    # 如果本地模拟模块不存在，尝试导入真实的 qlib
    try:
        from qlib.data import D

        logger = logging.getLogger(__name__)
        task_logger = StructuredTaskLogger(logger, "MarketStateService")
        task_logger.info("module_source", "使用真实 qlib 模块", source="qlib")
    except ImportError:
        logger = logging.getLogger(__name__)
        task_logger = StructuredTaskLogger(logger, "MarketStateService")
        task_logger.error("module_import_failed", "无法导入 qlib 模块")

        # 创建一个模拟的 D 类

        class D:
            @staticmethod
            def features(instruments, fields, start_time, end_time):
                task_logger.warning("mock_features", "使用模拟的 D.features")
                return None

        D = D()

DEFAULT_THRESHOLDS = {
    "ret_up": 0.02,
    "ret_down": -0.02,
    "vol_high": 0.03,
    "volume_ratio_high": 1.2,
}

DEFAULT_POSITION_BY_STATE = {
    "bull": 1.0,
    "neutral": 0.7,
    "bear": 0.3,
}

DEFAULT_WINDOW = 20


class MarketStateService:
    """计算市场状态与动态风险仓位"""

    def __init__(self) -> None:
        self._config_url = os.getenv("MARKET_CONFIG_URL") or os.getenv("MARKET_STATE_CONFIG_URL")

    def _fetch_remote_config(self, style: str | None) -> dict[str, Any]:
        if not self._config_url:
            return {}

        try:
            import requests
        except Exception:
            task_logger.warning("requests_missing", "requests 未安装，无法拉取市场配置")
            return {}

        params: dict[str, Any] = {}
        if style:
            params["style"] = style

        try:
            resp = requests.get(self._config_url, params=params, timeout=5)
            resp.raise_for_status()
            data = resp.json()
            if isinstance(data, dict):
                return data
        except Exception as exc:
            task_logger.warning("fetch_remote_config_failed", "拉取市场配置失败", error=str(exc))

        return {}

    def _merge_thresholds(self, remote: dict[str, Any]) -> dict[str, float]:
        thresholds = dict(DEFAULT_THRESHOLDS)
        remote_thresholds = remote.get("thresholds")
        if isinstance(remote_thresholds, dict):
            thresholds.update({k: float(v) for k, v in remote_thresholds.items() if v is not None})
        return thresholds

    def _merge_position_map(self, remote: dict[str, Any]) -> dict[str, float]:
        position_by_state = dict(DEFAULT_POSITION_BY_STATE)
        remote_map = remote.get("position_by_state")
        if isinstance(remote_map, dict):
            position_by_state.update({k: float(v) for k, v in remote_map.items() if v is not None})
        return position_by_state

    def resolve_config(
        self,
        style: str | None,
        symbol: str,
        window: int | None,
    ) -> dict[str, Any]:
        remote = self._fetch_remote_config(style)
        merged = {
            "symbol": remote.get("symbol") or symbol,
            "window": int(remote.get("window") or window or DEFAULT_WINDOW),
            "thresholds": self._merge_thresholds(remote),
            "position_by_state": self._merge_position_map(remote),
        }
        if isinstance(remote.get("market_state_series"), dict):
            merged["market_state_series"] = remote.get("market_state_series")
        if isinstance(remote.get("risk_degree_series"), dict):
            merged["risk_degree_series"] = remote.get("risk_degree_series")
        return merged

    def build_risk_degree_series(
        self,
        symbol: str,
        start_date: str,
        end_date: str,
        window: int | None = None,
        style: str | None = None,
        strategy_total_position: float = 1.0,
    ) -> tuple[dict[str, float], dict[str, float]]:
        config = self.resolve_config(style, symbol, window)
        position_by_state = config["position_by_state"]

        remote_risk = config.get("risk_degree_series")
        if isinstance(remote_risk, dict) and remote_risk:
            return (
                self._normalize_risk_series(remote_risk, strategy_total_position),
                position_by_state,
            )

        remote_state = config.get("market_state_series")
        if isinstance(remote_state, dict) and remote_state:
            risk_series = self._risk_from_state_series(remote_state, position_by_state, strategy_total_position)
            return risk_series, position_by_state

        series = self.build_market_state_series(
            config["symbol"],
            start_date,
            end_date,
            config["window"],
            config["thresholds"],
        )
        risk_series = self._risk_from_state_series(series, position_by_state, strategy_total_position)
        return risk_series, position_by_state

    def build_market_state_series(
        self,
        symbol: str,
        start_date: str,
        end_date: str,
        window: int,
        thresholds: dict[str, float],
    ) -> dict[str, str]:
        try:
            df = D.features(
                [symbol],
                ["$close", "$volume"],
                start_time=start_date,
                end_time=end_date,
            )
        except Exception as exc:
            task_logger.warning("fetch_market_data_failed", "拉取市场数据失败", symbol=symbol, error=str(exc))
            return {}

        if df is None or df.empty:
            task_logger.warning("market_data_empty", "市场数据为空", symbol=symbol)
            return {}

        if isinstance(df.index, pd.MultiIndex):
            df = df.reset_index()
        else:
            df = df.reset_index()

        if "instrument" in df.columns:
            df = df[df["instrument"] == symbol]

        if "datetime" in df.columns:
            df["datetime"] = pd.to_datetime(df["datetime"])
            df = df.sort_values("datetime")
        else:
            task_logger.warning("missing_datetime", "市场数据缺少 datetime 字段")
            return {}

        close = df.get("$close")
        volume = df.get("$volume")
        if close is None:
            task_logger.warning("missing_close", "市场数据缺少 $close")
            return {}

        close = close.astype(float)
        if volume is None:
            volume = pd.Series([1.0] * len(close), index=close.index)
        else:
            volume = volume.astype(float)

        daily_ret = close.pct_change()
        roll_ret = close / close.shift(window) - 1.0
        roll_vol = daily_ret.rolling(window).std()
        volume_ratio = volume / volume.rolling(window).mean()

        series: dict[str, str] = {}
        for idx in range(len(df)):
            if idx < window:
                continue
            if idx + 1 >= len(df):
                continue
            date = df.iloc[idx + 1]["datetime"]
            ret = roll_ret.iloc[idx]
            vol = roll_vol.iloc[idx]
            vratio = volume_ratio.iloc[idx]
            state = self._classify_state(ret, vol, vratio, thresholds)
            series[date.strftime("%Y-%m-%d")] = state

        return series

    def _classify_state(
        self,
        ret: float,
        vol: float,
        vratio: float,
        thresholds: dict[str, float],
    ) -> str:
        if pd.isna(ret) or pd.isna(vol):
            return "neutral"
        if ret >= thresholds["ret_up"] and vol <= thresholds["vol_high"]:
            return "bull"
        if ret <= thresholds["ret_down"] and vol >= thresholds["vol_high"]:
            return "bear"
        if vratio >= thresholds["volume_ratio_high"] and ret >= 0:
            return "bull"
        return "neutral"

    def _risk_from_state_series(
        self,
        series: dict[str, Any],
        position_by_state: dict[str, float],
        strategy_total_position: float,
    ) -> dict[str, float]:
        risk_series: dict[str, float] = {}
        for date, state in series.items():
            if isinstance(state, (int, float)):
                risk_series[date] = self._clamp(float(state) * strategy_total_position)
                continue
            mapped = position_by_state.get(str(state), position_by_state.get("neutral", 1.0))
            risk_series[date] = self._clamp(mapped * strategy_total_position)
        return risk_series

    def _normalize_risk_series(self, series: dict[str, Any], strategy_total_position: float) -> dict[str, float]:
        risk_series: dict[str, float] = {}
        for date, val in series.items():
            if val is None:
                continue
            try:
                risk_series[date] = self._clamp(float(val) * strategy_total_position)
            except Exception:
                continue
        return risk_series

    def _clamp(self, value: float) -> float:
        return max(0.0, min(1.0, value))
