"""RDLoop 包装器 — 将 RD-Agent 的 FactorRDLoop 适配到 QuantMind

提供统一接口:
- 接收 MarketAdapter 配置市场参数
- 启动/监控/取消 RDLoop
- 从日志中提取因子结果
"""

from __future__ import annotations

import asyncio
import logging
import os
import pickle
import re
import shutil
import time
from datetime import date
from pathlib import Path
from typing import Any

import pandas as pd

from .market_adapters import get_adapter, list_markets
from .market_adapters.base import MarketAdapter

logger = logging.getLogger(__name__)

# QuantDB 富化列白名单（仅 A 股）：{源列名: 输出列名}，输出列在 h5 中加 "$" 前缀。
# 这些列会随 daily_pv.h5 一起提供给 RD-Agent，LLM 可直接在因子表达式中引用
# （$<输出列名>），使因子能用上 QuantDB 的预计算技术指标/估值/资金流/筹码等富数据。
_ENRICH_FEATURES_DAILY: dict[str, str] = {
    "rsi_14": "rsi_14",
    "macd_hist": "macd_hist",
    "vol_atr_14": "atr_14",
    "beta_20": "beta_20",
    "pe_ttm": "pe_ttm",
    "pb": "pb",
    "ps_ttm": "ps_ttm",
    "dividend_rate": "div_yield",
    "total_mv": "total_mv",
    "float_mv": "float_mv",
    "net_profit_ttm": "np_ttm",
}
_ENRICH_L1: dict[str, str] = {
    "turn_5": "turn_5",
    "turn_20": "turn_20",
    "turn_z_20": "turn_z_20",
    "amt_net_flow_5": "netflow_5",
    "amt_net_flow_20": "netflow_20",
    "mfi_14": "mfi_14",
    "obv_slope_20": "obv_slope",
    "vol_parkinson_20": "parkinson_20",
    "tech_bb_width": "bb_width",
    "tech_bb_pos": "bb_pos",
    "tech_adx_14": "adx_14",
    "tech_max_drawdown_20": "maxdd_20",
    "fun_bp": "bp",
    "fun_ep": "ep",
    "fun_roe": "roe",
    "fun_peg": "peg",
    "fun_np_growth": "np_growth",
    "chip_profit_ratio_20": "chip_profit_20",
    "style_idio_vol_20": "idio_vol_20",
    "ind_strength_20": "ind_strength",
    "concept_hot_score": "concept_hot",
}
_ENRICH_OUTPUT_COLUMNS: tuple[str, ...] = tuple(
    _ENRICH_FEATURES_DAILY.values()
) + tuple(_ENRICH_L1.values())


class RDLoopWrapper:
    """封装 RD-Agent FactorRDLoop，提供 QuantMind 兼容接口"""

    def __init__(self, market: str = "a_share") -> None:
        self.adapter: MarketAdapter = get_adapter(market)
        if self.adapter is None:
            raise ValueError(f"Unknown market: {market}. Available: {[m['market_id'] for m in list_markets()]}")
        self.market = market
        self._loop = None
        self._running = False
        self._cancelled = False

    @property
    def market_name(self) -> str:
        return self.adapter.market_name

    def _configure_env(self, task_log_dir: str) -> dict[str, str]:
        """从 MarketAdapter 构建环境变量"""
        env_overrides = self.adapter.get_env_overrides()
        env = {
            **os.environ,
            **env_overrides,
            "LOG_TRACE_PATH": task_log_dir,
            "PYTHONPATH": os.getenv("PYTHONPATH") or "/app",
        }
        # 设置数据文件路径环境变量
        data_file = "/app/alphaagent/scenarios/qlib/experiment/factor_data_template/daily_pv_all.h5"
        if os.path.exists(data_file):
            env["FACTOR_DATA_PATH"] = data_file
        # Ensure critical LLM settings are present
        if not env.get("OPENAI_BASE_URL"):
            env["OPENAI_BASE_URL"] = os.getenv("OPENAI_BASE_URL", "")
        if not env.get("OPENAI_API_KEY"):
            env["OPENAI_API_KEY"] = os.getenv("AI_IDE_LLM_API_KEY", "")
        if not env.get("CHAT_MODEL"):
            env["CHAT_MODEL"] = os.getenv("CHAT_MODEL", "")
        # litellm needs provider prefix for non-OpenAI models
        model = env.get("CHAT_MODEL", "")
        if model and not model.startswith(("openai/", "azure/", "anthropic/", "huggingface/")):
            env["CHAT_MODEL"] = f"openai/{model}"
        env["REASONING_MODEL"] = env.get("CHAT_MODEL", "")
        env["CHAT_STREAM"] = "false"
        # 回测数据从 2016 年开始 (默认 2008 太慢)
        env.setdefault("QLIB_FACTOR_TRAIN_START", os.getenv("QLIB_FACTOR_TRAIN_START", "2016-01-01"))
        env.setdefault("QLIB_FACTOR_VALID_START", os.getenv("QLIB_FACTOR_VALID_START", "2021-01-01"))
        env.setdefault("QLIB_FACTOR_VALID_END", os.getenv("QLIB_FACTOR_VALID_END", "2022-12-31"))
        env.setdefault("QLIB_FACTOR_TEST_START", os.getenv("QLIB_FACTOR_TEST_START", "2023-01-01"))
        env.setdefault("QLIB_FACTOR_TEST_END", os.getenv("QLIB_FACTOR_TEST_END", "2025-12-31"))
        # 因子处理并行数
        env.setdefault("MULTI_PROC_N", os.getenv("MULTI_PROC_N", "4"))
        # 补齐 RD-Agent litellm 后端需要的 LITELLM_ 前缀变量（deepseek 优先）
        from backend.services.engine.rd_agent.llm_env import build_llm_env
        build_llm_env(env)
        return env

    # 需要注入中文/研究方向指令的 prompt key（RD-Agent prompts.yaml 中的顶层键）
    _INJECT_TARGET_KEYS = ("qlib_factor_background", "qlib_quant_background")

    #: 因子池注入文件路径（launcher 在 spawn 前写好、经 env 传入）
    _ENV_POOL_CONTEXT = "QMF_POOL_CONTEXT_PATH"

    def _build_prompt_suffix(self) -> str:
        """构造追加到因子背景 prompt 末尾的中文与研究方向指令。"""
        suffix = (
            "\n\n====== 语言要求 / Language Requirement ======\n"
            "所有因子的 description 字段必须使用中文撰写。"
            "hypothesis 和 reason 也请用中文。\n"
            "All factor descriptions MUST be written in Chinese (中文). "
            "Hypothesis and reason should also be in Chinese.\n"
        )
        direction = getattr(self, "_direction", "")
        if direction:
            suffix += (
                "\n\n====== 研究方向 / Research Direction ======\n"
                f"用户的研究方向/假设: {direction}\n"
                f"User's research direction/hypothesis: {direction}\n"
                "请围绕此方向进行因子探索。Focus factor exploration on this theme.\n"
            )
        suffix += self._pool_context_block()
        return suffix

    def _pool_context_block(self) -> str:
        """历史挖掘记忆段（因子池 top-k 摘要，launcher 交付的文件）。

        只读文件、不做 DB：本函数运行在 RD-Agent 子进程内，子进程里复用主
        进程 DB engine 会跨事件循环炸（旧坑）；池查询全部在 launcher
        （engine 进程）完成，这里只消费交付物。文件缺失/为空 → 空串，
        注入是增益层，任何情况下不拦挖掘。
        """
        path = os.getenv(self._ENV_POOL_CONTEXT, "").strip()
        if not path:
            return ""
        try:
            content = Path(path).read_text(encoding="utf-8").strip()
        except OSError as exc:
            logger.warning("[%s] 池注入文件读取失败(%s): %s", self.market, path, exc)
            return ""
        if not content:
            return ""
        return f"\n\n====== 历史挖掘记忆 / Past Mining Memory ======\n{content}\n"

    def _patch_prompts_for_chinese(self):
        """注入中文与研究方向指令到 RD-Agent 提示词。

        RD-Agent 的 prompt 存于各包内的 prompts.yaml，经 `utils.agent.tpl.load_content`
        每次按需读取（无缓存），因此在该函数返回值上追加指令是唯一可靠的注入点 ——
        早期实现 import `...experiment.prompts` 模块，但该模块并不存在，注入从未生效。
        """
        try:
            from rdagent.utils.agent import tpl as _tpl

            if getattr(_tpl, "_qm_patched", False):
                _tpl._qm_suffix = self._build_prompt_suffix()
                return

            suffix_holder = self._build_prompt_suffix()
            _tpl._qm_suffix = suffix_holder
            original_load = _tpl.load_content
            target_keys = self._INJECT_TARGET_KEYS

            def patched_load(uri: str, *args, **kwargs):
                content = original_load(uri, *args, **kwargs)
                if not isinstance(content, str):
                    return content
                if not any(uri.endswith(f":{k}") for k in target_keys):
                    return content
                if "语言要求" in content:
                    return content
                return content + getattr(_tpl, "_qm_suffix", "")

            _tpl.load_content = patched_load
            _tpl._qm_patched = True
            logger.info(
                "[%s] Patched RD-Agent prompt loader (Chinese + direction=%s)",
                self.market,
                bool(getattr(self, "_direction", "")),
            )
        except Exception as e:
            logger.error("Failed to patch prompts for Chinese: %s", e)

    def _create_loop(self):
        """创建 FactorRDLoop 实例"""
        from rdagent.app.qlib_rd_loop.conf import FactorBasePropSetting
        from rdagent.app.qlib_rd_loop.factor import FactorRDLoop
        from rdagent.core.utils import import_class

        # 注入中文指令
        self._patch_prompts_for_chinese()

        prop_setting_path = self.adapter.get_prop_setting_class()
        prop_cls = import_class(prop_setting_path)
        prop_setting = prop_cls()

        loop = FactorRDLoop(prop_setting)
        return loop

    async def run(
        self,
        loop_n: int = 3,
        task_log_dir: str = "",
        direction: str = "",
    ) -> dict[str, Any]:
        """执行因子挖掘循环

        Args:
            loop_n: 循环轮数
            task_log_dir: 日志输出目录
            direction: 挖掘方向/假设

        Returns:
            包含 factors 和 metadata 的结果字典
        """
        self._running = True
        self._cancelled = False
        self._direction = direction

        try:
            # 配置环境变量
            env = self._configure_env(task_log_dir)
            for k, v in env.items():
                os.environ[k] = v

            # 确保 daily_pv.h5 数据文件可用
            self._ensure_data_file(task_log_dir)

            # RD-Agent workspace / 数据目录对齐：设绝对路径 env 变量 + 切 cwd。
            # RD-Agent 的 FACTOR_COSTEER_SETTINGS.data_folder 与 workspace_path 默认用
            # Path.cwd() 相对解析（import 时冻结），必须显式指到 task_log_dir，
            # 否则因子执行时找不到 daily_pv.h5（默认 workspace 在 /tmp/git_ignore_folder/...）。
            if task_log_dir:
                os.environ["WORKSPACE_PATH"] = task_log_dir
                os.environ["FACTOR_CoSTEER_data_folder"] = os.path.join(
                    task_log_dir, "git_ignore_folder", "factor_implementation_source_data"
                )
                os.environ["FACTOR_CoSTEER_data_folder_debug"] = os.path.join(
                    task_log_dir, "git_ignore_folder", "factor_implementation_source_data_debug"
                )
                os.chdir(task_log_dir)

            logger.info("[%s] RDLoop starting: market=%s, loops=%d, log_dir=%s",
                        self.market, self.adapter.market_name, loop_n, task_log_dir)

            # 创建 RDLoop
            self._loop = self._create_loop()

            # 注入 base_features_path（L1/L2 因子集供 LLM 参考）
            base_features_path = getattr(self, "_base_features_path", None)
            if base_features_path:
                self._loop._init_base_features(base_features_path)
                logger.info("[%s] Loaded base features from %s", self.market, base_features_path)
            step_count = len(self._loop.steps)
            total_steps = loop_n * step_count

            logger.info("[%s] Steps per loop: %d, total steps: %d", self.market, step_count, total_steps)
            logger.info("[%s] Step flow: %s", self.market,
                        " → ".join(getattr(s, '__name__', s.__class__.__name__) for s in self._loop.steps))

            # 运行循环
            t0 = time.time()
            await self._loop.run(step_n=total_steps)
            elapsed = time.time() - t0

            logger.info("[%s] RDLoop completed in %.1fs", self.market, elapsed)

            # 提取结果
            factors = self._extract_factors(task_log_dir)

            return {
                "market": self.market,
                "market_name": self.adapter.market_name,
                "loop_n": loop_n,
                "elapsed_seconds": elapsed,
                "factors": factors,
                "total_factors": len(factors),
                "log_dir": task_log_dir,
                "success": True,
            }

        except asyncio.CancelledError:
            self._cancelled = True
            logger.info("[%s] RDLoop cancelled", self.market)
            return {"market": self.market, "cancelled": True, "factors": [], "success": False}

        except Exception as e:
            logger.exception("[%s] RDLoop failed: %s", self.market, e)
            return {"market": self.market, "error": str(e), "factors": [], "success": False}

        finally:
            self._running = False

    def cancel(self):
        """请求取消运行"""
        self._cancelled = True
        logger.info("[%s] Cancellation requested", self.market)

    def _ensure_data_file(self, task_log_dir: str = ""):
        """确保 daily_pv.h5 数据文件和 Qlib 数据目录在 RD-Agent 期望的位置可用

        RD-Agent 的 subprocess 以 task_log_dir 为 cwd 运行，
        FACTOR_COSTEER_SETTINGS.data_folder (git_ignore_folder/factor_implementation_source_data)
        相对于 cwd 解析。需要在 task_log_dir 下创建该目录并复制数据文件。

        同时确保对应的 Qlib provider_uri 目录可用。
        """
        import shutil

        # 根据市场选择数据源
        market_data_map = {
            "crypto": {
                "source_all": "/app/db/crypto_data/5min_pv.h5",
                "source_debug": "/app/db/crypto_data/5min_pv.h5",
                "qlib_source": "/app/db/qlib_data/crypto_data",
                "qlib_target_name": "crypto_data",
            },
            "hong_kong": {
                "source_all": "/app/db/hk_data/daily_pv.h5",
                "source_debug": "/app/db/hk_data/daily_pv.h5",
                "qlib_source": "/app/db/qlib_data/hk_data",
                "qlib_target_name": "hk_data",
            },
            "us_stock": {
                "source_all": "/app/db/us_data/daily_pv.h5",
                "source_debug": "/app/db/us_data/daily_pv.h5",
                "qlib_source": "/app/db/qlib_data/us_data",
                "qlib_target_name": "us_data",
            },
            "futures": {
                "source_all": "/app/db/futures_data/daily_pv.h5",
                "source_debug": "/app/db/futures_data/daily_pv.h5",
                "qlib_source": "/data/quantfutures/.qlib_cache/futures_data",
                "qlib_target_name": "futures_data",
            },
        }

        # 默认 A 股
        market_cfg = market_data_map.get(self.market, {
            "source_all": "/app/alphaagent/scenarios/qlib/experiment/factor_data_template/daily_pv_all.h5",
            "source_debug": "/app/alphaagent/scenarios/qlib/experiment/factor_data_template/daily_pv_debug.h5",
            "qlib_source": "/data/qlib/cn_data",
            "qlib_target_name": "cn_data",
        })

        source_all = market_cfg["source_all"]
        source_debug = market_cfg["source_debug"]

        # base_dir: RD-Agent subprocess cwd (task log dir)
        base_dir = task_log_dir if task_log_dir else os.getcwd()

        # RD-Agent data folders (relative to subprocess cwd)
        target_all = os.path.join(base_dir, "git_ignore_folder/factor_implementation_source_data/daily_pv.h5")
        target_debug = os.path.join(base_dir, "git_ignore_folder/factor_implementation_source_data_debug/daily_pv.h5")

        # A 股：优先从 QuantDB parquet 生成 .h5，失败则 fallback 到预生成文件
        if self.market == "a_share":
            quantdb_dir = self._resolve_quantdb_dir()
            h5_generated = False
            if quantdb_dir:
                ok_all = self._generate_h5_from_parquet(quantdb_dir, target_all, debug=False)
                ok_debug = self._generate_h5_from_parquet(quantdb_dir, target_debug, debug=True)
                h5_generated = ok_all and ok_debug
            if not h5_generated:
                logger.warning("[%s] H5 generation failed or QuantDB dir missing, falling back to copy", self.market)
                self._copy_h5_source(source_all, target_all, source_debug, target_debug)
        elif self.market == "futures":
            # 期货：从 QuantFutures parquet 生成（无预生成文件，fut_ 前缀 instrument）
            futures_dir = self._resolve_market_data_dir("/data/quantfutures")
            h5_generated = False
            if futures_dir.is_dir():
                ok_all = self._generate_futures_h5(futures_dir, target_all, debug=False)
                ok_debug = self._generate_futures_h5(futures_dir, target_debug, debug=True)
                h5_generated = ok_all and ok_debug
            if not h5_generated:
                logger.warning(
                    "[%s] Futures h5 generation failed or QuantFutures dir missing, falling back to copy",
                    self.market,
                )
                self._copy_h5_source(source_all, target_all, source_debug, target_debug)
        else:
            self._copy_h5_source(source_all, target_all, source_debug, target_debug)

        # Ensure Qlib provider_uri data is available at ~/.qlib/qlib_data/<market>
        qlib_source = market_cfg["qlib_source"]
        qlib_target_name = market_cfg["qlib_target_name"]

        # parquet 单源市场：优先固定目录/各市场本地派生缓存（统一走 qlib_paths）
        market_cache = {
            "a_share": ("CN", ("/data/quantdb", ".qlib_cache", "cn_data")),
            "hong_kong": ("HK", ("/data/quanthk", ".qlib_cache", "hk_data")),
            "us_stock": ("US", ("/data/quantus", ".qlib_cache", "us_data")),
        }
        if self.market in market_cache:
            mkt_key, (base_dir_cfg, cache_sub, leaf) = market_cache[self.market]
            candidates = [Path(base_dir_cfg) / cache_sub / leaf,
                          self._resolve_market_data_dir(base_dir_cfg) / cache_sub / leaf]
            try:
                from backend.shared.qlib_paths import resolve_qlib_provider_uri
                candidates.insert(0, Path(resolve_qlib_provider_uri(mkt_key)))
            except Exception:
                pass
            for candidate in candidates:
                if candidate.is_dir():
                    qlib_source = str(candidate)
                    break

        qlib_target = os.path.expanduser(f"~/.qlib/qlib_data/{qlib_target_name}")
        if os.path.isdir(qlib_source):
            # 修复过期 symlink：若指向错误路径则重建
            need_create = False
            if os.path.islink(qlib_target):
                current_target = os.readlink(qlib_target)
                if os.path.abspath(current_target) != os.path.abspath(qlib_source):
                    logger.info("[%s] Replacing stale symlink: %s -> %s (was %s)",
                                self.market, qlib_target, qlib_source, current_target)
                    os.unlink(qlib_target)
                    need_create = True
            elif os.path.isdir(qlib_target):
                # 真实目录而非 symlink，rename 后建 symlink
                backup = qlib_target + "_backup"
                logger.info("[%s] Replacing real directory with symlink: %s -> %s (backup: %s)",
                            self.market, qlib_target, qlib_source, backup)
                try:
                    if os.path.exists(backup):
                        shutil.rmtree(backup)
                    os.rename(qlib_target, backup)
                    need_create = True
                except Exception as e:
                    logger.warning("[%s] Failed to replace directory with symlink: %s", self.market, e)
            else:
                need_create = True

            if need_create:
                try:
                    os.makedirs(os.path.dirname(qlib_target), exist_ok=True)
                    os.symlink(qlib_source, qlib_target)
                    logger.info("[%s] Created symlink: %s -> %s", self.market, qlib_target, qlib_source)
                except Exception as e:
                    logger.warning("[%s] Failed to create Qlib symlink: %s", self.market, e)

        # A 股：生成 base_factors.json 供 RD-Agent LLM 参考
        if self.market == "a_share" and hasattr(self.adapter, "generate_base_factors_json"):
            data_dir = os.path.join(base_dir, "git_ignore_folder", "factor_implementation_source_data")
            os.makedirs(data_dir, exist_ok=True)
            bf_path = self.adapter.generate_base_factors_json(data_dir)
            if bf_path:
                self._base_features_path = data_dir
                logger.info("[%s] base_factors.json ready at %s", self.market, data_dir)
            else:
                self._base_features_path = None
        else:
            self._base_features_path = None

    def _copy_h5_source(self, source_all, target_all, source_debug, target_debug):
        """从预生成的 h5 文件复制。"""
        for target, source in [(target_all, source_all), (target_debug, source_debug)]:
            if not os.path.exists(source):
                logger.warning("[%s] Source data file not found: %s", self.market, source)
                continue
            target_dir = os.path.dirname(target)
            os.makedirs(target_dir, exist_ok=True)
            if not os.path.exists(target) or os.path.getmtime(source) > os.path.getmtime(target):
                try:
                    shutil.copy2(source, target)
                    logger.info("[%s] Copied data file: %s -> %s", self.market, source, target)
                except Exception as e:
                    logger.warning("[%s] Failed to copy data file to %s: %s", self.market, target, e)

    @staticmethod
    def _resolve_quantdb_dir() -> str | None:
        """解析 QuantDB 数据目录，返回存在的路径或 None。"""
        env_dir = os.getenv("QM_QUANTDB_DATA_DIR", "").strip()
        candidates = [env_dir] if env_dir else []
        candidates += ["/data/quantdb", "/app/data/quantdb"]
        for d in candidates:
            if d and os.path.isdir(d):
                return d
        return None

    @staticmethod
    def _resolve_market_data_dir(container_path: str) -> Path:
        """解析市场本地 parquet 数据目录（容器挂载 /data/quantX）。

        支持 env 覆盖（QM_QUANTHK_DATA_DIR 等）；容器内为 /data/quantX，
        宿主机构回退到项目 data/quantX。返回不存在的候选也不报错，
        由调用方用 .is_dir() 判断。
        """
        env_map = {
            "/data/quanthk": "QM_QUANTHK_DATA_DIR",
            "/data/quantus": "QM_QUANTUS_DATA_DIR",
            "/data/quantbc": "QM_QUANTBC_DATA_DIR",
            "/data/quantdb": "QM_QUANTDB_DATA_DIR",
            "/data/quantfutures": "QM_QUANTFUTURES_DATA_DIR",
        }
        env_name = env_map.get(container_path)
        if env_name:
            env_val = os.getenv(env_name, "").strip()
            if env_val:
                return Path(env_val)
        project_data = Path(__file__).resolve().parents[4] / "data" / Path(container_path).name
        return project_data

    def _generate_h5_from_parquet(self, quantdb_dir: str, output_path: str, *, debug: bool = False) -> bool:
        """从 QuantDB parquet 生成 RD-Agent 期望的 daily_pv.h5 文件。

        H5 格式:
        - Key: "data"
        - Index: MultiIndex [datetime, instrument]
        - Columns: ["$open", "$high", "$low", "$close", "$volume", "$amount",
          "$factor", <QuantDB 富化列 ...>]
        - instrument 格式: Qlib 格式 sh600036

        生成的富化文件先写入共享缓存 ``<quantdb_dir>/.h5_cache/``（按最新分区
        自动失效），再硬链/软链到任务目录，避免每个挖掘任务重复生成 GB 级文件。

        Returns:
            True if h5 file was generated/already current, False on failure.
        """
        cache_dir = os.path.join(quantdb_dir, ".h5_cache")
        cache_path = os.path.join(
            cache_dir, "daily_pv_debug.h5" if debug else "daily_pv_all.h5"
        )

        # 共享缓存命中：直接链接到任务目录（避免重复生成）
        if os.path.exists(cache_path) and self._h5_cache_fresh(quantdb_dir, cache_path):
            logger.info("[%s] h5 cache hit: %s", self.market, cache_path)
            self._link_h5(cache_path, output_path)
            return True

        try:
            from backend.services.engine.data_platform.quantdb_hub import QuantDBDataHub
            hub = QuantDBDataHub(quantdb_dir)
            if not hub.available:
                logger.error("[%s] QuantDBDataHub not available for h5 generation", self.market)
                return self._use_stale_cache(cache_path, output_path)

            # 获取股票列表
            df_stocks = hub.fetch_stock_list()
            if df_stocks.empty:
                logger.error("[%s] No stock list from QuantDB", self.market)
                return self._use_stale_cache(cache_path, output_path)

            symbol_col = "Symbol" if "Symbol" in df_stocks.columns else "symbol"
            symbols = df_stocks[symbol_col].dropna().unique()

            if debug:
                # Debug 模式：只取少量 symbol
                symbols = symbols[:50]

            import numpy as np

            symbols = [str(s) for s in symbols]
            start_d, end_d = date(2020, 1, 1), date(2026, 12, 31)

            # 批量读取：一次查全部 symbol，避免逐股票 N 次分区扫描
            # （早期实现对 ~5400 只股票各查 2 次，单次生成需 40 分钟以上）
            df = hub.fetch_daily_kline_batch(symbols, start_d, end_d, adjust="qfq")
            if df is None or df.empty:
                logger.error("[%s] No K-line data read from QuantDB", self.market)
                return self._use_stale_cache(cache_path, output_path)
            df_unadj = hub.fetch_daily_kline_batch(symbols, start_d, end_d, adjust="none")

            # 前复权价可能为负（高分红股票多年除权后 qfq 价转负），会污染 Qlib 因子
            # 计算，这里整体剔除这些行。
            valid = df["close"].to_numpy(dtype="float64") > 0
            dropped = int((~valid).sum())
            if dropped:
                logger.warning(
                    "[%s] Dropped %d rows with non-positive qfq close (negative 前复权价)",
                    self.market,
                    dropped,
                )
                df = df.loc[valid].reset_index(drop=True)
            if df.empty:
                logger.error("[%s] No positive-price K-line rows from QuantDB", self.market)
                return self._use_stale_cache(cache_path, output_path)

            # 按 (symbol, trade_date) 对齐不复权收盘价以计算 $factor
            if df_unadj is not None and not df_unadj.empty:
                unadj = df_unadj[["symbol", "trade_date", "close"]].rename(
                    columns={"close": "_close_unadj"}
                )
                df = df.merge(unadj, on=["symbol", "trade_date"], how="left")
                factor = np.where(
                    df["_close_unadj"].to_numpy(dtype="float64", na_value=0.0) > 0,
                    df["close"].to_numpy(dtype="float64")
                    / df["_close_unadj"].to_numpy(dtype="float64", na_value=1.0),
                    1.0,
                )
            else:
                factor = np.ones(len(df))

            # 合并 QuantDB 富化列（技术指标/估值/资金流/筹码等），供因子直接引用
            df = self._merge_enrich(
                df, hub, start_d, end_d, None if not debug else symbols
            )

            instruments = [self._to_qlib_symbol(str(s)) for s in df["symbol"]]
            data: dict[str, Any] = {
                "$open": df["open"].to_numpy(dtype="float64"),
                "$high": df["high"].to_numpy(dtype="float64"),
                "$low": df["low"].to_numpy(dtype="float64"),
                "$close": df["close"].to_numpy(dtype="float64"),
                "$volume": df["volume"].to_numpy(dtype="float64"),
                "$factor": factor,
            }
            if "amount" in df.columns:
                data["$amount"] = df["amount"].to_numpy(dtype="float64")
            for out_col in _ENRICH_OUTPUT_COLUMNS:
                if out_col in df.columns:
                    data[f"${out_col}"] = df[out_col].to_numpy(dtype="float32")

            combined = pd.DataFrame(
                data,
                index=pd.MultiIndex.from_arrays(
                    [pd.to_datetime(df["trade_date"]), instruments],
                    names=["datetime", "instrument"],
                ),
            )
            combined = combined.sort_index()

            os.makedirs(cache_dir, exist_ok=True)
            tmp_path = cache_path + ".tmp"
            combined.to_hdf(tmp_path, key="data", mode="w")
            os.replace(tmp_path, cache_path)
            self._link_h5(cache_path, output_path)
            logger.info(
                "[%s] Generated enriched h5 from parquet: %s (%d rows, %d cols)",
                self.market, cache_path, len(combined), combined.shape[1],
            )
            return True

        except Exception as exc:
            logger.error("[%s] Failed to generate h5 from parquet: %s", self.market, exc)
            return self._use_stale_cache(cache_path, output_path)

    def _merge_enrich(
        self,
        df: pd.DataFrame,
        hub: Any,
        start: date,
        end: date,
        symbols: list[str] | None,
    ) -> pd.DataFrame:
        """把 QuantDB 富化列按 (symbol, trade_date) 左连接到 K 线 DataFrame。"""
        for dataset, mapping in (
            ("features_daily", _ENRICH_FEATURES_DAILY),
            ("l1_factors", _ENRICH_L1),
        ):
            try:
                sub = hub.fetch_ml_columns(
                    dataset, list(mapping.keys()), start, end, symbols=symbols
                )
            except Exception as exc:
                logger.warning("[%s] Enrich from %s failed: %s", self.market, dataset, exc)
                continue
            if sub is None or sub.empty:
                logger.warning("[%s] No enrich data from %s", self.market, dataset)
                continue
            sub = sub.rename(columns=mapping)
            sub["trade_date"] = pd.to_datetime(sub["trade_date"])
            keep = [c for c in ("symbol", "trade_date", *mapping.values()) if c in sub.columns]
            df = df.merge(sub[keep], on=["symbol", "trade_date"], how="left")
            logger.info(
                "[%s] Enriched h5 with %d cols from %s", self.market, len(keep) - 2, dataset
            )
        return df

    @staticmethod
    def _latest_partition_mtime(quantdb_dir: str, rel_path: str) -> float:
        """返回某数据集最新分区内 parquet 的最大 mtime（无则 0）。"""
        base = Path(quantdb_dir) / rel_path
        if not base.is_dir():
            return 0.0
        try:
            dirs = [d for d in base.iterdir() if d.is_dir() and d.name.startswith("dt=")]
            if not dirs:
                return 0.0
            latest = max(dirs, key=lambda d: d.name)
            mtimes = [f.stat().st_mtime for f in latest.glob("*.parquet")]
            return max(mtimes) if mtimes else latest.stat().st_mtime
        except OSError:
            return 0.0

    def _h5_cache_fresh(self, quantdb_dir: str, cache_path: str) -> bool:
        """判断共享缓存是否仍是最新（K 线/features_daily/l1 最新分区均不晚于缓存）。"""
        try:
            cache_mtime = os.path.getmtime(cache_path)
        except OSError:
            return False
        sources = (
            "1_kline_data/daily_forward",
            "6_ml_datasets/features_daily",
            "6_ml_datasets/l1_factors",
        )
        latest = max(self._latest_partition_mtime(quantdb_dir, rel) for rel in sources)
        return latest > 0 and cache_mtime >= latest

    @staticmethod
    def _link_h5(src: str, dst: str) -> None:
        """把共享缓存的 h5 链接（优先硬链）到任务目录，失败则复制。"""
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        try:
            if os.path.islink(dst) or os.path.exists(dst):
                os.remove(dst)
        except OSError:
            pass
        for linker in (
            lambda: os.link(src, dst),
            lambda: os.symlink(os.path.abspath(src), dst),
            lambda: shutil.copy2(src, dst),
        ):
            try:
                linker()
                return
            except OSError:
                continue
            except Exception:
                continue
        logger.warning("[RDLoopWrapper] Failed to link/copy h5 %s -> %s", src, dst)

    def _use_stale_cache(self, cache_path: str, output_path: str) -> bool:
        """生成失败时回退到已有（可能过期）缓存，尽量不阻断任务。"""
        if os.path.exists(cache_path):
            logger.warning("[%s] Reusing stale h5 cache: %s", self.market, cache_path)
            self._link_h5(cache_path, output_path)
            return True
        return False

    @staticmethod
    def _to_qlib_symbol(symbol: str) -> str:
        """suffix 格式 600036.SH -> Qlib 格式 sh600036。"""
        s = symbol.strip()
        if "." in s:
            code, exchange = s.split(".", 1)
            return f"{exchange.lower()}{code}"
        return s.lower()

    def _generate_futures_h5(
        self, futures_dir: Path | str, output_path: str, *, debug: bool = False
    ) -> bool:
        """从 QuantFutures parquet 生成 RD-Agent 的 daily_pv.h5。

        与 A 股版区别:
        - instrument 用 fut_ 前缀（对齐 QlibDataBuilder._MARKET_QLIB_PREFIX）
        - 期货无复权概念，$factor 恒为 1
        - 行情从 2016 年起量价才完整，起点对齐 Qlib 因子训练窗口
        """
        import numpy as np

        if os.path.exists(output_path):
            try:
                h5_mtime = os.path.getmtime(output_path)
                kline_dir = Path(futures_dir) / "1_kline_data" / "daily_forward"
                if kline_dir.is_dir():
                    partitions = sorted(d for d in os.listdir(kline_dir) if d.startswith("dt="))
                    if partitions:
                        latest_parquet = kline_dir / partitions[-1] / "data.parquet"
                        if os.path.exists(latest_parquet) and os.path.getmtime(latest_parquet) > h5_mtime:
                            logger.info("[%s] Parquet newer than h5, regenerating: %s", self.market, output_path)
                        else:
                            return True
                    else:
                        return True
                else:
                    return True
            except Exception:
                return True

        try:
            from backend.services.engine.data_platform.quantfutures_hub import (
                QuantFuturesDataHub,
            )

            hub = QuantFuturesDataHub(Path(futures_dir))
            if not hub.available:
                logger.error("[%s] QuantFuturesDataHub not available for h5 generation", self.market)
                return False

            # 期货无 instrument_detail parquet，从 daily_forward 分区推导 symbol 列表
            symbols: set[str] = set()
            kline_dir = Path(futures_dir) / "1_kline_data" / "daily_forward"
            if not kline_dir.is_dir():
                logger.error("[%s] QuantFutures daily_forward missing", self.market)
                return False
            import duckdb

            con = duckdb.connect(config={"memory_limit": "4GB", "threads": "2"})
            try:
                df_syms = con.execute(
                    f"SELECT DISTINCT symbol FROM read_parquet('{kline_dir / 'dt=*' / 'data.parquet'}', hive_partitioning=1)"
                ).fetchdf()
            finally:
                con.close()
            symbols = {str(s) for s in df_syms["symbol"].dropna().unique()}
            if not symbols:
                logger.error("[%s] No futures symbols found in parquet", self.market)
                return False

            symbol_list = sorted(symbols)
            if debug:
                symbol_list = symbol_list[:50]

            # 行情起点对齐 Qlib 因子训练窗口（2016 起量价完整）
            df = hub.fetch_daily_kline_batch(
                symbol_list, date(2016, 1, 1), date(2026, 12, 31), adjust="qfq"
            )
            if df is None or df.empty:
                logger.error("[%s] No futures K-line data read", self.market)
                return False

            # 剔除无效行情行（价格 <= 0 或非有限值会污染因子计算）
            cols = ["open", "high", "low", "close"]
            valid = np.isfinite(df[cols].to_numpy(dtype="float64")).all(axis=1) & (
                df[cols].to_numpy(dtype="float64") > 0
            ).all(axis=1)
            dropped = int((~valid).sum())
            if dropped:
                logger.warning("[%s] Dropped %d invalid futures rows", self.market, dropped)
                df = df.loc[valid].reset_index(drop=True)
            if df.empty:
                logger.error("[%s] No valid futures K-line rows", self.market)
                return False

            instruments = [f"fut_{s}" for s in df["symbol"]]
            combined = pd.DataFrame(
                {
                    "$open": df["open"].to_numpy(dtype="float64"),
                    "$high": df["high"].to_numpy(dtype="float64"),
                    "$low": df["low"].to_numpy(dtype="float64"),
                    "$close": df["close"].to_numpy(dtype="float64"),
                    "$volume": df["volume"].to_numpy(dtype="float64"),
                    "$factor": np.ones(len(df)),
                },
                index=pd.MultiIndex.from_arrays(
                    [pd.to_datetime(df["trade_date"]), instruments],
                    names=["datetime", "instrument"],
                ),
            )
            combined = combined.sort_index()

            os.makedirs(os.path.dirname(output_path), exist_ok=True)
            combined.to_hdf(output_path, key="data", mode="w")
            logger.info(
                "[%s] Generated futures h5 from parquet: %s (%d rows, %d symbols)",
                self.market, output_path, len(combined), len(symbol_list),
            )
            return True

        except Exception as exc:
            logger.error("[%s] Failed to generate futures h5 from parquet: %s", self.market, exc)
            return False

    @property
    def is_running(self) -> bool:
        return self._running

    def _extract_factors(self, log_dir: str) -> list[dict[str, Any]]:
        """从 RD-Agent 日志目录提取因子

        RDLoop 输出结构 (pickle):
        - experiment generation/**/*.pkl → 实验任务 (因子名 + 表达式)
        - coder result/**/*.pkl → 编码结果 (因子代码)
        - feedback/**/*.pkl → 反馈 (IC 等指标)
        """
        log_path = Path(log_dir)
        if not log_path.exists():
            logger.warning("[%s] Log dir not found: %s", self.market, log_dir)
            return []

        pkl_count = len(list(log_path.glob("**/*.pkl")))
        logger.info("[%s] Scanning log dir: %s (%d pkl files)", self.market, log_dir, pkl_count)

        # 1. Factor metadata
        factor_meta: dict[str, dict] = {}
        for pkl_path in sorted(log_path.glob("**/experiment generation/**/*.pkl")):
            try:
                with open(pkl_path, "rb") as f:
                    tasks = pickle.load(f)
                if not isinstance(tasks, list):
                    tasks = [tasks]
                for t in tasks:
                    name = getattr(t, "factor_name", None) or getattr(t, "name", None)
                    if not name:
                        continue
                    factor_meta[name] = {
                        "name": name,
                        "formulation": getattr(t, "factor_formulation", "") or "",
                        "description": getattr(t, "description", "") or "",
                        "category": getattr(t, "category", "") or "",
                    }
            except Exception as e:
                logger.debug("Failed to read %s: %s", pkl_path, e)

        # 2. Factor code —— 直接用 workspace.target_task.factor_name 映射，
        #    不再依赖函数名与因子名一致（LLM 函数命名可能与因子名不同）。
        factor_code: dict[str, str] = {}
        for pkl_path in sorted(log_path.glob("**/coder result/**/*.pkl")):
            try:
                with open(pkl_path, "rb") as f:
                    workspaces = pickle.load(f)
                if not isinstance(workspaces, list):
                    workspaces = [workspaces]
                for ws in workspaces:
                    file_dict = getattr(ws, "file_dict", None) or {}
                    code = file_dict.get("factor.py", "")
                    if not code:
                        for v in file_dict.values():
                            if isinstance(v, str) and "def " in v:
                                code = v
                                break
                    if not code:
                        continue
                    name = ""
                    target = getattr(ws, "target_task", None)
                    if target is not None:
                        name = str(
                            getattr(target, "factor_name", None)
                            or getattr(target, "name", None)
                            or ""
                        ).strip()
                    if not name:
                        fn_match = re.search(r"def\s+(\w+)\s*\(", code)
                        name = (
                            fn_match.group(1).removeprefix("calculate_")
                            if fn_match
                            else f"factor_{len(factor_code)}"
                        )
                    factor_code[name] = code
            except Exception as e:
                logger.debug("Failed to read coder result %s: %s", pkl_path, e)

        # 3. Feedback
        feedback_text = ""
        for pkl_path in sorted(log_path.glob("**/feedback/**/*.pkl")):
            try:
                with open(pkl_path, "rb") as f:
                    fb = pickle.load(f)
                if isinstance(fb, str):
                    feedback_text += fb + "\n"
                elif isinstance(fb, (list, tuple)):
                    for item in fb:
                        if isinstance(item, str):
                            feedback_text += item + "\n"
            except Exception as e:
                logger.debug("Failed to read feedback %s: %s", pkl_path, e)

        # 4. Merge —— 只保留**已完成 coding 阶段**的因子（有代码）。
        #    半成品（如 loop_n 预算截断时第二轮只有 experiment generation、无 coder result）
        #    不落库，避免因子库里出现无代码、无法回测的条目。
        factors: list[dict] = []
        for name, code in sorted(factor_code.items()):
            meta = factor_meta.get(name, {})
            factors.append({
                "name": meta.get("name", name),
                "formulation": meta.get("formulation", ""),
                "description": meta.get("description", ""),
                "category": meta.get("category", ""),
                "code": code,
                "market": self.market,
                "feedback": feedback_text[:5000] if feedback_text else "",
            })

        missing = sorted(set(factor_meta.keys()) - set(factor_code.keys()))
        if missing:
            logger.info(
                "[%s] %d factors generated without code (partial/incomplete loop), skipped: %s",
                self.market, len(missing), missing,
            )

        logger.info("[%s] Extracted %d factors", self.market, len(factors))
        return factors


# ── Runner script entry point (subprocess) ──


def run_factor_mining_subprocess(
    market: str,
    task_id: str,
    user_id: str,
    loop_n: int,
    log_dir: str,
    direction: str = "",
) -> dict[str, Any]:
    """在子进程中执行因子挖掘（用于 launcher 调用）

    这是同步入口点，由 launcher 的 subprocess 调用。
    """
    wrapper = RDLoopWrapper(market=market)
    logger.info("Starting factor mining: market=%s, task=%s, loops=%d", market, task_id, loop_n)
    result = asyncio.run(wrapper.run(
        loop_n=loop_n,
        task_log_dir=log_dir,
        direction=direction,
    ))
    result["task_id"] = task_id
    result["user_id"] = user_id
    return result
