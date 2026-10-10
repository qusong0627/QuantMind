"""A股市场适配器"""

from __future__ import annotations

import json
import os
from pathlib import Path

from . import register_adapter
from .base import BacktestConfig, DataConfig, MarketAdapter


@register_adapter
class AShareAdapter(MarketAdapter):
    """A股 (CSI300) 市场适配器"""

    market_id = "a_share"
    market_name = "A股"
    description = "中国 A 股市场 (CSI300)，Qlib Alpha158 因子集"

    def get_data_config(self) -> DataConfig:
        return DataConfig(
            provider_uri=self.get_qlib_provider_uri(),
            data_dir=self._get_quantdb_dir() or "/data/qlib/cn_data",
            calendar="day",
            market="csi300",
        )

    def get_qlib_provider_uri(self) -> str:
        # 统一走 qlib_paths 解析（固定目录 /data/qlib/cn_data 优先）
        try:
            from backend.shared.qlib_paths import resolve_qlib_provider_uri
            return resolve_qlib_provider_uri("CN")
        except Exception:
            pass
        # Fallback: QuantDB parquet 构建的 Qlib 缓存
        quantdb_dir = self._get_quantdb_dir()
        if quantdb_dir:
            qlib_cache = os.path.join(quantdb_dir, ".qlib_cache", "cn_data")
            if os.path.isdir(qlib_cache) and os.path.isfile(
                os.path.join(qlib_cache, "calendars", "day.txt")
            ):
                return qlib_cache
        # Fallback to the canonical fixed qlib directory
        container_path = "/data/qlib/cn_data"
        if os.path.isdir(container_path):
            return container_path
        host_path = os.path.join(
            os.getenv("PROJECT_ROOT", "/opt/quantmind"),
            "data", "qlib", "cn_data",
        )
        return host_path

    @staticmethod
    def _get_quantdb_dir() -> str | None:
        """获取 QuantDB 数据目录路径。"""
        quantdb_dir = os.getenv("QM_QUANTDB_DATA_DIR", "").strip()
        if quantdb_dir and os.path.isdir(quantdb_dir):
            return quantdb_dir
        for d in ("/data/quantdb", "/app/data/quantdb"):
            if os.path.isdir(d):
                return d
        return None

    def get_backtest_config(self) -> BacktestConfig:
        return BacktestConfig(
            annualization_days=252,
            limit_threshold=0.1,
            commission_rate=0.001,
            min_commission=5.0,
            region="cn",
            needs_adjustment_factor=True,
        )

    def get_factor_set(self) -> dict[str, str]:
        """从 QuantDB L1 因子动态加载因子集（每类 5 个代表性因子）。

        若 QuantDB 不可用则回退到 Alpha158(20) 子集。
        """
        try:
            from backend.services.engine.data_platform.quantdb_hub import QuantDBDataHub
            hub = QuantDBDataHub.get_instance()
            if hub.available:
                categories = hub.fetch_l1_factor_categories()
                cat_list = categories.get("categories", [])
                if cat_list:
                    result = {}
                    for cat in cat_list:
                        for feat in cat.get("sample_features", [])[:5]:
                            result[feat] = cat["name"] + "类因子"
                    return result if result else self._fallback_factors()
        except Exception:
            pass
        return self._fallback_factors()

    def generate_base_factors_json(self, output_dir: str) -> str | None:
        """生成 RD-Agent 可读取的 base_factors.json 文件。

        从 feature catalog 读取 L1/L2 因子名列表，写入符合 RD-Agent 格式的
        JSON 文件（feature_name -> expression/description）。供 LLM 在因子挖掘时
        参考已有因子作为构建基础。

        L2 高频因子（资金流/微观结构/已实现波动率）在 QuantDB 已预计算为日频值，
        T-MV-07 起已全量接入 daily_pv.h5——这里给出「可直接引用」的完整列清单
        （$<真列名>，列清单与数据面同一单源、中文释义取自 quantdb_factor_dictionary）；
        另有少量 Qlib 日频近似模板（MicroSeed_*），仅作概念改造种子，不冒充 L2 实列。

        Returns:
            生成的文件路径，失败返回 None。
        """
        json_path = os.path.join(output_dir, "base_factors.json")
        try:
            from backend.services.engine.data_platform.quantdb_hub import QuantDBDataHub
            hub = QuantDBDataHub.get_instance()
            if not hub.available:
                return None

            categories = hub.fetch_l1_factor_categories()
            if not categories.get("categories"):
                return None

            # 从 catalog 文件读取完整 feature 列表
            catalog_path = (
                Path(__file__).resolve().parents[5]
                / "config" / "features" / "model_training_feature_catalog_v1.json"
            )
            all_features: list[dict] = []
            if catalog_path.exists():
                import json as _json
                with open(catalog_path, encoding="utf-8") as f:
                    catalog = _json.load(f)
                for cat in catalog.get("categories", []):
                    cat_name = cat.get("name", "因子")
                    for feat in cat.get("features", []):
                        all_features.append({**feat, "_category": cat_name})

            factors: dict[str, str] = {}

            # Alpha158 表达式模板：给 LLM 提供可直接仿写的 Qlib 语法范例
            known_expr = self._fallback_factors()
            factors.update(known_expr)

            # 微观结构概念的日频近似模板（MicroSeed_*）：T-MV-07 起 L2 真列已可直接
            # 引用（见下方 $ 清单），本组仅作改造种子
            micro_seed = self._l2_factor_expressions()
            factors.update(micro_seed)

            # QuantDB 已接入 daily_pv.h5 的预计算列：可在因子表达式中直接引用
            for out_col, desc in self._usable_quantdb_columns().items():
                factors[out_col] = (
                    f"${out_col}（QuantDB 预计算列，可直接在表达式中引用；{desc}）"
                )

            for feat in all_features:
                feat_key = feat.get("key", "")
                if not feat_key or feat_key in factors:
                    continue
                # catalog 中剩余因子（L1 预计算值）：给 LLM 提供分类/含义/公式作为参考
                parts = [feat.get("_category", "因子")]
                desc = (feat.get("description") or "").strip()
                if desc:
                    parts.append(desc)
                formula = (feat.get("formula") or "").strip()
                if formula:
                    parts.append(f"公式: {formula}")
                factors[feat_key] = (
                    " | ".join(parts) + "（QuantDB 其余预计算列未接入 h5，仅供参考实现思路）"
                )

            if not factors:
                return None

            os.makedirs(output_dir, exist_ok=True)
            with open(json_path, "w", encoding="utf-8") as f:
                import json as _json
                _json.dump(factors, f, ensure_ascii=False, indent=2)
            import logging
            logging.getLogger(__name__).info(
                "[%s] Generated base_factors.json: %d factors (micro seeds: %d) -> %s",
                self.market_id, len(factors), len(micro_seed), json_path,
            )
            return json_path

        except Exception as e:
            import logging
            logging.getLogger(__name__).warning(
                "[%s] Failed to generate base_factors.json: %s", self.market_id, e
            )
            return None

    @staticmethod
    def _l2_factor_expressions() -> dict[str, str]:
        """微观结构概念的 Qlib 日频近似模板（T-MV-07 起定位为种子，非 L2 实列）。

        L2 真列（micro_*/flow_*/vol_* 等）已全量接入 daily_pv.h5，LLM 应优先
        直接引用 $<真列名>；本组把日频 OHLCV+amount 可近似的概念翻译成 Qlib
        表达式，供在意念层面改造/组合/调参。命名以 MicroSeed_ 前缀与 L2 实列
        区分——不可近似的高频量（档口深度、VPIN 桶等）不列入，它们的真列在
        上面的 $ 清单里。
        """
        return {
            # ---- 已实现波动率族（L2 高频波动率的日频近似）----
            "MicroSeed_RV5": "Std($close/Ref($close,1)-1, 5) * Sqrt(252)",
            "MicroSeed_RV20": "Std($close/Ref($close,1)-1, 20) * Sqrt(252)",
            "MicroSeed_Jump": "Abs($close/Ref($close,1)-1) - Std($close/Ref($close,1)-1, 20)",
            "MicroSeed_RetSkew": "Mean(Power($close/Ref($close,1)-1, 3), 20) / Power(Std($close/Ref($close,1)-1, 20), 3)",
            "MicroSeed_RetKurt": "Mean(Power($close/Ref($close,1)-1, 4), 20) / Power(Std($close/Ref($close,1)-1, 20), 4)",
            "MicroSeed_VolPersist": "Corr(Std($close/Ref($close,1)-1, 5), Ref(Std($close/Ref($close,1)-1, 5), 5), 20)",
            "MicroSeed_UpDnVolRatio": "Sum($volume * ($close>Ref($close,1)), 5) / (Sum($volume * ($close<Ref($close,1)), 5) + 1)",
            # ---- 资金流族（L2 逐单资金流的日频近似）----
            "MicroSeed_FlowNetRatio": "Sum($amount * ($close-Ref($close,1))/Ref($close,1), 5) / Sum($amount, 5)",
            "MicroSeed_FlowLarge": "Sum($amount * ($close>Ref($close,1)) * ($volume > Mean($volume, 20)), 10) / Sum($amount, 10)",
            "MicroSeed_FlowImbalance": "(Sum($volume * ($close>=Ref($close,1)), 10) - Sum($volume * ($close<Ref($close,1)), 10)) / Sum($volume, 10)",
            "MicroSeed_FlowConsistency": "Corr($close/Ref($close,1)-1, $volume/Ref($volume,1)-1, 10)",
            "MicroSeed_MFI": "Mean($amount * ($close-Ref($close,1))/Ref($close,1), 14) / (Std($amount, 14) + 1e-12)",
            "MicroSeed_BigTradeRatio": "Sum($volume * ($volume > Mean($volume, 20) * 2), 10) / Sum($volume, 10)",
            # ---- 微观结构族（L2 档口/价差的日频近似）----
            "MicroSeed_Amihud": "Mean(Abs($close/Ref($close,1)-1)/($amount + 1e-12), 20)",
            "MicroSeed_PriceImpact": "Abs($close/Ref($close,1)-1) / (Log($volume/Ref($volume,1)+1) + 1e-12)",
            "MicroSeed_KyleLambda": "Cov($close/Ref($close,1)-1, $volume/Ref($volume,1)-1, 20) / (Var($volume/Ref($volume,1)-1, 20) + 1e-12)",
            "MicroSeed_SpreadProxy": "($high - $low) / ($close + 1e-12)",
            "MicroSeed_BidAskVolRatio": "Sum($volume * ($close>Ref($close,1)), 5) / Sum($volume * ($close<Ref($close,1)), 5)",
            "MicroSeed_OpenGap": "($open - Ref($close, 1)) / Ref($close, 1)",
            "MicroSeed_BuyPressure": "Sum(($close-$low)/($high-$low+1e-12) * $volume, 10) / Sum($volume, 10)",
            "MicroSeed_SellPressure": "Sum(($high-$close)/($high-$low+1e-12) * $volume, 10) / Sum($volume, 10)",
            "MicroSeed_OrderToxicity": "Abs($close/Ref($close,1)-1) * $volume / (Mean(Abs($close/Ref($close,1)-1), 20) * Mean($volume, 20) + 1e-12)",
            "MicroSeed_InformedRatio": "Sum(Abs($close/Ref($close,1)-1) * $volume, 5) / (Std(Abs($close/Ref($close,1)-1), 20) * Sum($volume, 5) + 1e-12)",
            "MicroSeed_JumpCount": "Sum(Abs($close/Ref($close,1)-1) > Mean(Abs($close/Ref($close,1)-1), 20) * 3, 20)",
        }

    @staticmethod
    def _usable_quantdb_columns() -> dict[str, str]:
        """已接入 daily_pv.h5 的 QuantDB 预计算列（输出列名 -> 简述）。

        与 rd_loop_wrapper._ENRICH_* 白名单保持一致，避免提示与数据漂移；
        T-MV-07 起并入 L2 微观结构真列（见 ``_l2_referenceable_columns``）。
        """
        desc = {
            "rsi_14": "14日RSI",
            "macd_hist": "MACD柱",
            "atr_14": "14日ATR波动",
            "beta_20": "20日市场beta",
            "parkinson_20": "20日Parkinson波动率",
            "bb_width": "布林带宽度",
            "bb_pos": "收盘价在布林带位置",
            "adx_14": "14日ADX趋势强度",
            "maxdd_20": "20日最大回撤",
            "idio_vol_20": "20日特质波动率",
            "obv_slope": "OBV斜率",
            "turn_5": "5日换手率",
            "turn_20": "20日换手率",
            "turn_z_20": "换手率z-score",
            "mfi_14": "14日资金流指标",
            "netflow_5": "5日净资金流",
            "netflow_20": "20日净资金流",
            "pe_ttm": "市盈率TTM",
            "pb": "市净率",
            "ps_ttm": "市销率TTM",
            "div_yield": "股息率",
            "ep": "盈利收益率",
            "bp": "账面市值比",
            "roe": "净资产收益率",
            "peg": "PEG",
            "np_growth": "净利润增速",
            "np_ttm": "净利润TTM",
            "total_mv": "总市值",
            "float_mv": "流通市值",
            "chip_profit_20": "20日筹码获利比例",
            "ind_strength": "行业相对强度",
            "concept_hot": "概念热度",
        }
        try:
            from backend.services.engine.rd_agent.rd_loop_wrapper import (
                _ENRICH_FEATURES_DAILY,
                _ENRICH_L1,
            )
            names = list(_ENRICH_FEATURES_DAILY.values()) + list(_ENRICH_L1.values())
        except Exception:
            names = list(desc.keys())
        result = {n: desc.get(n, "QuantDB 预计算特征") for n in names}
        result.update(AShareAdapter._l2_referenceable_columns())
        return result

    @staticmethod
    def _l2_referenceable_columns() -> dict[str, str]:
        """L2 微观结构真列名 -> 中文释义（T-MV-07 正名）。

        列清单现场读盘（rd_loop_wrapper.l2_factor_columns，与挖掘数据面同一
        单源——「列清单与目录一致」），释义取自 quantdb_factor_dictionary
        （与研究界面同一字典源）。任一步失败返回 {}：L2 提示降级，不拦挖掘。
        """
        try:
            from backend.services.engine.data_platform.quantdb_factor_dictionary import (
                definition_for,
            )
            from backend.services.engine.rd_agent.rd_loop_wrapper import l2_factor_columns

            quantdb_dir = AShareAdapter._get_quantdb_dir() or ""
            out: dict[str, str] = {}
            for col in l2_factor_columns(quantdb_dir):
                defn = definition_for(col)
                out[col] = (
                    f"L2 微观结构列（{defn.get('category_name', '微观结构')}）："
                    f"{defn.get('display_name', col)}"
                )
            return out
        except Exception:
            return {}

    @staticmethod
    def _fallback_factors() -> dict[str, str]:
        """Alpha158 风格默认因子集兜底（K线形态 + 动量 + 均线 + 波动率 + 量价 + 趋势强度）。"""
        return {
            "KMID": "($close - $open) / $open",
            "KLEN": "($high - $low) / $open",
            "KMID2": "($close - $open) / ($high - $low + 1e-12)",
            "KUP": "($high - Max($open, $close)) / $open",
            "KUP2": "($high - Max($open, $close)) / ($high - $low + 1e-12)",
            "KLOW": "(Min($open, $close) - $low) / $open",
            "KLOW2": "(Min($open, $close) - $low) / ($high - $low + 1e-12)",
            "KSFT": "(2 * $close - $high - $low) / $open",
            "KSFT2": "(2 * $close - $high - $low) / ($high - $low + 1e-12)",
            "ROC5": "Ref($close, 5) / $close - 1",
            "ROC10": "Ref($close, 10) / $close - 1",
            "ROC20": "Ref($close, 20) / $close - 1",
            "ROC30": "Ref($close, 30) / $close - 1",
            "ROC60": "Ref($close, 60) / $close - 1",
            "ROC120": "Ref($close, 120) / $close - 1",
            "ROC250": "Ref($close, 250) / $close - 1",
            "MA5": "Mean($close, 5) / $close",
            "MA10": "Mean($close, 10) / $close",
            "MA20": "Mean($close, 20) / $close",
            "MA30": "Mean($close, 30) / $close",
            "MA60": "Mean($close, 60) / $close",
            "STD5": "Std($close, 5) / $close",
            "STD10": "Std($close, 10) / $close",
            "STD20": "Std($close, 20) / $close",
            "STD30": "Std($close, 30) / $close",
            "STD60": "Std($close, 60) / $close",
            # 量价相关（A股量价背离是最经典 alpha 来源，原兜底集缺失）
            "CORR5": "Corr(Log($close), Log($volume + 1), 5)",
            "CORR10": "Corr(Log($close), Log($volume + 1), 10)",
            "CORR20": "Corr(Log($close), Log($volume + 1), 20)",
            "VOLCH5": "Log($volume / Ref($volume, 5))",
            "VOLCH10": "Log($volume / Ref($volume, 10))",
            # 趋势强度（线性回归斜率/拟合度/残差）
            "BETA20": "Slope($close, 20) / $close",
            "BETA60": "Slope($close, 60) / $close",
            "RSQR20": "Rsquare($close, 20)",
            "RESI20": "Resi($close, 20) / $close",
        }

    def get_prop_setting_class(self) -> str:
        return "rdagent.app.qlib_rd_loop.conf.FactorBasePropSetting"

    def get_env_overrides(self) -> dict[str, str]:
        # API key resolution: AI_IDE_LLM_API_KEY > AI_IDE_API_KEY > OPENAI_API_KEY
        api_key = (
            os.getenv("AI_IDE_LLM_API_KEY")
            or os.getenv("AI_IDE_API_KEY")
            or os.getenv("OPENAI_API_KEY", "")
        )
        return {
            "QLIB_PROVIDER_URI": self.get_qlib_provider_uri(),
            "OPENAI_BASE_URL": os.getenv("OPENAI_BASE_URL", ""),
            "OPENAI_API_KEY": api_key,
            "CHAT_MODEL": os.getenv("CHAT_MODEL", ""),
            "REASONING_MODEL": os.getenv("CHAT_MODEL", ""),
            "CHAT_STREAM": "false",
            "CHAT_MAX_TOKENS": os.getenv("CHAT_MAX_TOKENS", "8000"),
            "CHAT_TEMPERATURE": os.getenv("CHAT_TEMPERATURE", "0.3"),
        }
