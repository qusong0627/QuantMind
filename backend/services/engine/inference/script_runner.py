"""
InferenceScriptRunner
=====================
执行模型目录中用户编写的 inference.py 推理脚本，解析输出并写库/发布信号。

inference.py 规范
-----------------
调用方式：
    python inference.py --date YYYY-MM-DD

平台注入环境变量：
    DATABASE_URL   PostgreSQL 连接串
    MODEL_DIR      模型目录绝对路径
    TRADE_DATE     推理日期（同 --date 参数）
    OUTPUT_FORMAT  固定值 json

stdout 输出（JSON 数组，每项含 symbol 和 score）：
    [{"symbol": "sh600519", "score": 0.82}, ...]

exit code：
    0  = 成功
    1  = 致命错误
    2  = 数据质量不足，触发兜底模型推理
    其他非零 = 失败
"""

from __future__ import annotations

# 并发推理时，多个 worker 会同时执行 _persist_and_publish 写 engine_signal_scores
import logging
import json
import math
import os
import re
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Optional

import exchange_calendars as xcals
from sqlalchemy import create_engine, text

# QuantDB factor describe 结果缓存（TTL 300s）
# describe() 会全量扫描 parquet 求 min/max/schema，单次 ~2.6s；
# 预检/就绪检查高频调用时缓存以避免阻塞单 worker 事件循环。
_DESCRIBE_TTL = 300.0
_describe_cache: dict[str, tuple[float, Any]] = {}
_describe_lock = threading.Lock()


def _cached_describe(reader, source: str) -> Any:
    now = time.monotonic()
    with _describe_lock:
        hit = _describe_cache.get(source)
        if hit and now - hit[0] < _DESCRIBE_TTL:
            return hit[1]
    status = reader.describe(source)
    with _describe_lock:
        _describe_cache[source] = (time.monotonic(), status)
    return status
from sqlalchemy.orm import sessionmaker

from backend.services.engine.services.event_stream import EngineSignalStreamPublisher
from backend.shared.stock_utils import StockCodeUtil

logger = logging.getLogger(__name__)

# 并发推理时，多个 worker 会同时执行 _persist_and_publish 写 engine_signal_scores
# （DELETE 历史 + DELETE 当日 + INSERT 大量行）。并发写同一张表会触发 PostgreSQL
# 锁竞争，导致 worker 卡死。用模块级锁把写库串行化：推理子进程仍并发执行，
# 只有最终写库串行，既保留加速又避免锁冲突。
_INFER_PERSIST_LOCK = __import__("threading").Lock()


def _resolve_quantdb_data_dir() -> str:
    """QuantDB 因子数据目录：QUANTDB_DATA_DIR → QM_QUANTDB_DATA_DIR → hub 统一解析。

    历史默认值 /app/data/quantdb 在容器内不存在（真实挂载为 /data/quantdb），
    曾导致 quantdb_factors 模型推理前置检查永远失败、推理按钮置灰。
    """
    for key in ("QUANTDB_DATA_DIR", "QM_QUANTDB_DATA_DIR"):
        val = os.getenv(key, "").strip()
        if val and Path(val).is_dir():
            return val
    try:
        from backend.services.engine.data_platform.quantdb_hub import _resolve_data_dir
        return str(_resolve_data_dir())
    except Exception:  # noqa: BLE001
        return "/data/quantdb"


def _resolve_market_factor_data_dir(meta: dict) -> str:
    """按模型 metadata.context.market 解析因子数据根目录（HK→quanthk 等）。"""
    try:
        from backend.services.engine.data_platform.quantdb_factor_reader import (
            market_data_dir, normalize_market,
        )
        market = normalize_market(
            str((meta.get("context") or {}).get("market") or "CN")
        )
        path = market_data_dir(market)
        return str(path) if path.is_dir() else _resolve_quantdb_data_dir()
    except Exception:  # noqa: BLE001
        return _resolve_quantdb_data_dir()

_PARQUET_TEMPLATE_MARKERS = (
    "QuantMind Parquet 数据源推理脚本 (inference.py 模板)",
    "QuantMind Parquet 数据源推理脚本\n=================================\n由训练流水线自动生成",
)

# 融合模型脚本特征（ensemble_src 模板头）。融合脚本也需要每次推理前
# 从最新模板同步（DL 源模型委派、窗口截断等修复），但标记与 parquet 模板不同，
# 必须单独识别，否则会被 parquet 模板误覆盖。
_ENSEMBLE_SRC_MARKERS = (
    "QuantMind 融合模型推理脚本 (inference_ensemble_src.py)",
    "融合模型推理脚本 (inference_ensemble_src.py)",
)

# 默认超时 3600 秒（1 小时），可通过环境变量覆盖
_SCRIPT_TIMEOUT_SEC = int(os.getenv("INFERENCE_SCRIPT_TIMEOUT_SEC", "3600"))
_DEFAULT_FEATURE_DIM = int(os.getenv("INFERENCE_DEFAULT_FEATURE_DIM", "48"))
_MIN_READY_SYMBOLS = int(os.getenv("INFERENCE_MIN_READY_SYMBOLS", "3000"))
_MIN_READY_RATIO = float(os.getenv("INFERENCE_MIN_READY_RATIO", "0.9"))
_MIN_READY_FLOOR = int(os.getenv("INFERENCE_MIN_READY_FLOOR", "100"))
_PREDICTION_RETENTION_DAYS = int(os.getenv("INFERENCE_PREDICTION_RETENTION_DAYS", "730"))

# Redis 标记键：记录当日推理已完成
_COMPLETED_REDIS_KEY_PREFIX = "qm:inference:completed"


@dataclass
class ExecutionResult:
    success: bool
    exit_code: int
    stdout: str
    stderr: str
    signals_count: int = 0
    run_id: str = ""
    error: str = ""
    signals: list[dict] = field(default_factory=list)
    fallback_used: bool = False  # True = alpha158 兜底脚本实际执行
    fallback_reason: str = ""  # 触发兜底的原因描述
    failure_stage: str = ""  # main_script/fallback_script/output_parse
    active_model_id: str = ""
    active_data_source: str = ""
    data_trade_date: str = ""
    prediction_trade_date: str = ""


class InferenceScriptRunner:
    """执行模型目录中的 inference.py 并处理结果。

    执行顺序：
    1. 主模型推理脚本（默认 inference.py）
       - exit 0 → 成功
       - exit 1 → 致命失败，返回错误
       - exit 2 → 数据质量不足，自动执行兜底模型脚本
    2. 兜底模型推理脚本（默认 inference.py）
       - exit 0 → 兜底成功，结果标记 fallback_used=True
       - 非 0   → 兜底失败，返回错误
    """

    # exit code 2: 数据质量不足，触发兜底
    _EXIT_DATA_QUALITY = 2

    def __init__(
        self,
        models_production: str | None = None,
        *,
        primary_model_dir: str | None = None,
        fallback_model_dir: str | None = None,
        primary_data_dir: str | None = None,
        fallback_data_dir: str | None = None,
        primary_model_id: str | None = None,
        fallback_model_id: str | None = None,
        primary_script_name: str | None = None,
        fallback_script_name: str | None = None,
        enable_fallback: bool = True,
    ):
        self.enable_fallback = enable_fallback
        # `models_production` 为历史兼容参数，等价于 primary_model_dir。
        resolved_primary = (
            primary_model_dir
            or models_production
            or os.getenv("MODELS_PRODUCTION", "/app/models/production/model_qlib")
        )
        self.primary_model_dir = Path(resolved_primary)
        self.fallback_model_dir = Path(
            fallback_model_dir
            or os.getenv(
                "MODELS_FALLBACK_PRODUCTION", "/app/models/production/alpha158"
            )
        )
        self.primary_data_dir = self._normalize_provider_uri(
            str(primary_data_dir or os.getenv("QLIB_PRIMARY_DATA_PATH", ""))
        )
        self.fallback_data_dir = self._normalize_provider_uri(
            str(
                fallback_data_dir
                or os.getenv("QLIB_FALLBACK_DATA_PATH", "")
            ),
            prefer_alpha158=True,
        )
        self.primary_model_id = str(
            primary_model_id or os.getenv("PRIMARY_MODEL_ID", "model_qlib")
        )
        self.fallback_model_id = str(
            fallback_model_id or os.getenv("FALLBACK_MODEL_ID", "alpha158")
        )
        self.primary_script_name = str(
            primary_script_name or os.getenv("INFERENCE_PRIMARY_SCRIPT", "inference.py")
        )
        self.fallback_script_name = str(
            fallback_script_name
            or os.getenv("INFERENCE_FALLBACK_SCRIPT", "inference.py")
        )

    @staticmethod
    def _normalize_provider_uri(
        provider_uri: str, *, prefer_alpha158: bool = False
    ) -> str:
        """
        规范化 Qlib provider uri，避免相对路径在子进程 cwd 下被错误解析。

        规则：
        1) 若能在候选路径中命中真实目录，返回该绝对路径；
        2) 相对路径默认转换为 /app/<path>；
        3) prefer_alpha158 时在候选列表头部追加 metadata 中的默认路径。
        """
        raw = str(provider_uri or "").strip()
        if not raw:
            from backend.shared.qlib_paths import resolve_qlib_provider_uri
            raw = resolve_qlib_provider_uri()

        candidates: list[Path] = []
        p = Path(raw)
        if p.is_absolute():
            candidates.append(p)
        else:
            candidates.append(Path("/app") / p)
            candidates.append(p)

        seen = set()
        for c in candidates:
            key = str(c)
            if key in seen:
                continue
            seen.add(key)
            if c.exists():
                return str(c)

        if p.is_absolute():
            return raw
        return str(Path("/app") / p)

    # ------------------------------------------------------------------
    # 公开方法
    # ------------------------------------------------------------------

    def check_script_exists(self) -> bool:
        """检查主模型推理脚本是否存在。"""
        script = self.primary_model_dir / self.primary_script_name
        return script.is_file()

    def check_fallback_script_exists(self) -> bool:
        """检查兜底模型推理脚本是否存在。"""
        script = self.fallback_model_dir / self.fallback_script_name
        return script.is_file()

    def _resolve_expected_feature_dim(self) -> int:
        """
        解析主模型期望特征维度。
        优先级：
        1) metadata.json 中 feature_count
        2) feature_schema.json 中 features 长度
        3) inference.py 顶部注释中的“XX 特征”
        4) 环境变量默认值（48）
        """
        metadata_path = self.primary_model_dir / "metadata.json"
        if metadata_path.is_file():
            try:
                meta = json.loads(metadata_path.read_text(encoding="utf-8"))
                for key in ("feature_count", "feature_dim", "input_dim"):
                    val = meta.get(key)
                    if isinstance(val, int) and val > 0:
                        return val
                feature_columns = meta.get("feature_columns")
                if isinstance(feature_columns, list) and feature_columns:
                    return len(feature_columns)
                input_spec = meta.get("input_spec")
                if isinstance(input_spec, dict):
                    tensor_shape = input_spec.get("tensor_shape")
                    if isinstance(tensor_shape, list) and len(tensor_shape) >= 3:
                        try:
                            dim = int(tensor_shape[2] or 0)
                            if dim > 0:
                                return dim
                        except Exception:
                            pass
                model_info = meta.get("model_info") if isinstance(meta, dict) else None
                if isinstance(model_info, dict):
                    for key in ("feature_count", "feature_dim", "input_dim"):
                        val = model_info.get(key)
                        if isinstance(val, int) and val > 0:
                            return val
                    feature_columns = model_info.get("feature_columns")
                    if isinstance(feature_columns, list) and feature_columns:
                        return len(feature_columns)
            except Exception:
                pass

        schema_path = self.primary_model_dir / "feature_schema.json"
        if schema_path.is_file():
            try:
                schema = json.loads(schema_path.read_text(encoding="utf-8"))
                if isinstance(schema, dict):
                    for key in ("features", "feature_columns", "columns"):
                        cols = schema.get(key)
                        if isinstance(cols, list) and cols:
                            return len(cols)
            except Exception:
                pass

        main_script = self.primary_model_dir / self.primary_script_name
        if main_script.is_file():
            try:
                text_part = main_script.read_text(encoding="utf-8", errors="ignore")[
                    :4000
                ]
                match = re.search(r"(\d+)\s*特征", text_part)
                if match:
                    dim = int(match.group(1))
                    if dim > 0:
                        return dim
            except Exception:
                pass

        return _DEFAULT_FEATURE_DIM

    def _read_primary_metadata(self) -> dict:
        """读取主模型 metadata.json，失败返回空字典。"""
        meta_path = self.primary_model_dir / "metadata.json"
        if meta_path.is_file():
            try:
                return json.loads(meta_path.read_text(encoding="utf-8"))
            except Exception:
                pass
        return {}

    def _try_deploy_parquet_template(self, script_path: Path) -> bool:
        """
        当 parquet 模型缺少 inference.py 时，自动从内置模板写入。
        成功返回 True，失败返回 False（不影响主流程）。
        """
        template_path = Path(__file__).parent / "templates" / "inference_parquet.py"
        if not template_path.is_file():
            logger.warning(
                "[InferenceScriptRunner] parquet 推理模板不存在: %s", template_path
            )
            return False
        try:
            script_path.parent.mkdir(parents=True, exist_ok=True)
            script_path.write_text(
                template_path.read_text(encoding="utf-8"), encoding="utf-8"
            )
            logger.info(
                "[InferenceScriptRunner] 已自动写入 parquet 推理脚本: %s", script_path
            )
            return True
        except Exception as exc:
            logger.warning("[InferenceScriptRunner] 自动写入推理脚本失败: %s", exc)
            return False

    def _try_deploy_ensemble_src_template(self, script_path: Path) -> bool:
        """融合模型 inference.py：从 ensemble_src 模板写入/同步。"""
        template_path = (
            Path(__file__).parent / "templates" / "inference_ensemble_src.py"
        )
        if not template_path.is_file():
            logger.warning(
                "[InferenceScriptRunner] 融合推理模板不存在: %s", template_path
            )
            return False
        try:
            script_path.parent.mkdir(parents=True, exist_ok=True)
            script_path.write_text(
                template_path.read_text(encoding="utf-8"), encoding="utf-8"
            )
            logger.info(
                "[InferenceScriptRunner] 已同步融合推理脚本: %s", script_path
            )
            return True
        except Exception as exc:
            logger.warning("[InferenceScriptRunner] 同步融合推理脚本失败: %s", exc)
            return False

    @staticmethod
    def _is_managed_parquet_template(script_path: Path) -> bool:
        if not script_path.is_file():
            return False
        try:
            text = script_path.read_text(encoding="utf-8", errors="ignore")
            if not any(marker in text for marker in _PARQUET_TEMPLATE_MARKERS):
                return False
            # 融合模板的 _sync_member_script 注释里也含 parquet marker 字符串，
            # 必须用 ensemble marker 排除，否则融合脚本会被误覆写成 parquet 模板。
            return not any(marker in text for marker in _ENSEMBLE_SRC_MARKERS)
        except Exception:
            return False

    def _ensure_parquet_template_script(self, script_path: Path) -> bool:
        """为平台托管的 parquet inference.py 同步最新模板。"""
        if not script_path.is_file():
            return self._try_deploy_parquet_template(script_path)

        if not self._is_managed_parquet_template(script_path):
            return True

        return self._try_deploy_parquet_template(script_path)

    @staticmethod
    def _resolve_primary_active_data_source(primary_meta: dict[str, object]) -> str:
        data_source = str(primary_meta.get("data_source") or "").lower()
        if data_source == "parquet":
            return str(
                primary_meta.get("data_dir")
                or os.getenv("MODEL_TRAINING_DATA_DIR", "/app/db/feature_snapshots")
            )
        if data_source == "quantdb_factors":
            return _resolve_market_factor_data_dir(primary_meta)
        try:
            from backend.shared.qlib_paths import resolve_qlib_provider_uri
            return resolve_qlib_provider_uri("CN")
        except Exception:
            return str(os.getenv("QLIB_PRIMARY_DATA_PATH", "db/qlib_data"))

    def _query_quantdb_readiness(self, trade_date: str) -> dict:
        meta = self._read_primary_metadata()
        try:
            from backend.services.engine.data_platform.quantdb_factor_reader import QuantDBFactorReader
            data_dir = Path(_resolve_market_factor_data_dir(meta))
            reader = QuantDBFactorReader(data_dir)
            source = str(meta.get("factor_source") or "l1_l2_factors")
            # describe() 会全量扫描 parquet 求 min/max，开销大；做 TTL 缓存避免每次预检 2.6s
            status = _cached_describe(reader, source)
            if not status.ready:
                return {
                    "ready": False,
                    "detail": (
                        f"QuantDB {source} 不可用: "
                        + (", ".join(status.missing_required) or status.reason or "未知原因")
                    ),
                }
            # 越界时返回最新可用日期，供 precheck/run 自动回退到数据覆盖范围
            if trade_date:
                if status.max_date and trade_date > status.max_date:
                    return {
                        "ready": False,
                        "detail": f"QuantDB {source} 最新可用 {status.max_date}，请求 {trade_date} 超出数据范围",
                        "latest_available_date": status.max_date,
                    }
                if status.min_date and trade_date < status.min_date:
                    return {
                        "ready": False,
                        "detail": f"QuantDB {source} 最早 {status.min_date}，请求 {trade_date} 早于数据起点",
                        "latest_available_date": status.min_date,
                    }
            schema_hash = str(meta.get("factor_schema_hash") or "")
            if schema_hash and schema_hash != status.schema_hash:
                return {"ready": False, "detail": "QuantDB schema hash differs from model metadata"}
            missing = [
                source for source in (meta.get("factor_field_sources") or {}).values()
                if source not in status.columns
            ]
            return {"ready": not missing, "detail": "ok" if not missing else f"missing mapped fields: {missing[:5]}"}
        except Exception as exc:
            return {"ready": False, "detail": f"QuantDB unavailable: {exc}"}

    def _query_parquet_readiness(self, trade_date: str) -> dict:
        """
        Parquet 数据源就绪检查。
        当模型 metadata.json 中 data_source=parquet 时使用，
        检查对应年份的 parquet 文件是否存在且含有目标日期的数据。
        """
        meta = self._read_primary_metadata()
        # 解析 parquet 数据目录（优先 metadata 中的 data_dir，否则用默认路径）
        parquet_dir = Path(
            meta.get("data_dir")
            or os.getenv("MODEL_TRAINING_DATA_DIR", "/app/db/feature_snapshots")
        )

        # Market-aware parquet file resolution
        market = ""
        ctx = meta.get("context")
        if isinstance(ctx, dict):
            market = str(ctx.get("market", "")).upper()

        _MARKET_PARQUET: dict[str, str] = {
            "HK": "model_features_hk.parquet",
            "US": "model_features_us.parquet",
            "CRYPTO": "model_features_crypto.parquet",
            "FUTURES": "model_features_futures.parquet",
        }

        parquet_path = None
        if market in _MARKET_PARQUET:
            p = parquet_dir / _MARKET_PARQUET[market]
            if p.exists():
                parquet_path = p

        if parquet_path is None:
            year = int(trade_date[:4])
            parquet_path = parquet_dir / f"model_features_{year}.parquet"

        if not parquet_path.exists():
            # Legacy fallback
            parquet_path = parquet_dir / "model_features.parquet"

        if not parquet_path.exists():
            return {
                "ready": False,
                "detail": f"parquet 文件不存在: {parquet_path}",
            }

        # 快速检查：读取 trade_date 列验证日期存在性（只读 trade_date 列，避免全量加载）
        try:
            import pandas as pd  # noqa: PLC0415

            df = pd.read_parquet(parquet_path, columns=["trade_date"], engine="pyarrow")
            df["trade_date"] = pd.to_datetime(df["trade_date"]).dt.strftime("%Y-%m-%d")
            rows = int((df["trade_date"] == trade_date).sum())
            ready = rows > 0
            result = {
                "ready": ready,
                "detail": (
                    f"parquet={parquet_path.name}, date={trade_date}, rows={rows}"
                    + ("" if ready else " (该日期无数据)")
                ),
            }
            if not ready:
                # 返回最新可用日期，供 precheck 自动回退
                latest = df["trade_date"].max()
                if latest:
                    result["latest_available_date"] = latest
                    result["detail"] += f", 最新可用={latest}"
            return result
        except Exception as exc:
            return {"ready": False, "detail": f"parquet 读取失败: {exc}"}

    def _query_qlib_readiness(self, trade_date: str) -> dict:
        """
        Qlib 二进制数据源就绪检查。
        检查 calendars/day.txt 是否包含目标日期。
        """
        provider_uri = self.primary_data_dir
        calendar_path = Path(provider_uri) / "calendars" / "day.txt"

        if not calendar_path.exists():
            return {
                "ready": False,
                "detail": f"qlib 日历文件不存在: {calendar_path}",
            }

        try:
            content = calendar_path.read_text(encoding="utf-8")
            if trade_date in content:
                return {
                    "ready": True,
                    "detail": f"qlib_data={provider_uri}, date={trade_date} (已在日历中找到)",
                }
            else:
                last_date = (
                    content.strip().splitlines()[-1] if content.strip() else "empty"
                )
                return {
                    "ready": False,
                    "detail": f"qlib_data={provider_uri}, date={trade_date} (未在日历中找到，最后日期={last_date})",
                }
        except Exception as exc:
            return {"ready": False, "detail": f"qlib 日历读取失败: {exc}"}

    @staticmethod
    def _resolve_ready_threshold(total_rows: int) -> int:
        if total_rows <= 0:
            return _MIN_READY_SYMBOLS
        ratio = min(max(_MIN_READY_RATIO, 0.0), 1.0)
        abs_target = min(_MIN_READY_SYMBOLS, total_rows)
        ratio_target = int(math.ceil(total_rows * ratio))
        required = min(abs_target, ratio_target)
        required = max(_MIN_READY_FLOOR, required)
        return min(required, total_rows)

    @staticmethod
    def _resolve_prediction_trade_date(data_trade_date: str, market: str = "A") -> str:
        """
        统一口径：
        - data_trade_date：用于读取特征的数据交易日 (T)
        - prediction_trade_date：信号生效交易日 (T+1)
        """
        from datetime import date as _date, timedelta

        market_upper = (market or "A").upper()

        # 加密货币 7×24，T+1 自然日
        if market_upper == "CRYPTO":
            try:
                d = _date.fromisoformat(str(data_trade_date)[:10])
                return (d + timedelta(days=1)).isoformat()
            except Exception:
                return str(data_trade_date)

        try:
            import exchange_calendars as xcals

            _MARKET_XCAL = {"A": "XSHG", "HK": "XHKG", "US": "XNYS"}
            xcal_name = _MARKET_XCAL.get(market_upper, "XSHG")
            cal = xcals.get_calendar(xcal_name)
            # 将输入日期转换为下一个交易日
            nxt = cal.next_session(data_trade_date)
            return (
                nxt.date().isoformat()
                if hasattr(nxt, "date")
                else str(nxt).split(" ")[0]
            )
        except Exception as e:
            logger.warning(
                f"[InferenceScriptRunner] 计算预测日期失败，回退到 T+1 自然日: {e}"
            )
            # 兜底：如果日历解析失败，至少加 1 天（自然日）
            from datetime import datetime, timedelta

            dt = datetime.strptime(data_trade_date, "%Y-%m-%d")
            return (dt + timedelta(days=1)).strftime("%Y-%m-%d")

    def _query_dimension_readiness(self, trade_date: str, expected_dim: int) -> dict:
        # 使用同步驱动进行就绪度查询
        sync_db_url = os.getenv("DATABASE_URL", "")
        if "+asyncpg" in sync_db_url:
            sync_db_url = sync_db_url.replace("+asyncpg", "+psycopg2")
        if not sync_db_url.startswith("postgresql"):
            sync_db_url = (
                "postgresql+psycopg2://quantmind:quantmind2026@localhost:5432/quantmind"
            )
        sync_engine = create_engine(sync_db_url, pool_pre_ping=True, future=True)
        SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=sync_engine)

        db = SessionLocal()
        try:
            # 切换为查询 stock_daily_latest，只要有基础行情数据即视为就绪
            row = (
                db.execute(
                    text(
                        """
                        SELECT
                            COUNT(*) FILTER (WHERE trade_date = :trade_date) AS total_rows,
                            COUNT(*) FILTER (WHERE trade_date = :trade_date AND close > 0) AS ready_rows
                        FROM stock_daily_latest
                        """
                    ),
                    {"trade_date": trade_date},
                )
                .mappings()
                .first()
            )

            total_rows = int((row or {}).get("total_rows") or 0)
            ready_rows = int((row or {}).get("ready_rows") or 0)
            required_ready = self._resolve_ready_threshold(total_rows)
            ready = total_rows > 0 and ready_rows >= required_ready
            detail = (
                f"trade_date={trade_date}, table=stock_daily_latest, "
                f"total_rows={total_rows}, ready_rows={ready_rows}, "
                f"required_ready={required_ready}"
            )
            return {"ready": ready, "detail": detail}
        except Exception as exc:
            return {"ready": False, "detail": f"dimension_readiness_query_error={exc}"}
        finally:
            db.close()

    # ------------------------------------------------------------------
    # 兜底执行
    # ------------------------------------------------------------------

    def _get_python_executable(self) -> str:
        """解析容器内正确的 Python 解释器路径。"""
        # 1. 优先检查环境变量
        env_py = os.getenv("PYTHON_EXECUTABLE")
        if env_py and Path(env_py).exists():
            return env_py
        # 2. 检查常用的容器内路径
        for p in [
            "/usr/local/bin/python3",
            "/usr/bin/python3",
            "/usr/local/bin/python",
            "/usr/bin/python",
        ]:
            if Path(p).exists():
                return p
        # 3. 兜底使用 sys.executable
        return sys.executable

    def _get_subprocess_env(self) -> dict:
        """构造子进程运行环境，确保路径和库能被正确找到。"""
        env = os.environ.copy()
        # 确保 /usr/local/bin 在 PATH 中，许多 pip 包安装在这里
        if "/usr/local/bin" not in env.get("PATH", ""):
            env["PATH"] = "/usr/local/bin:" + env.get("PATH", "")

        # 强制设置 PYTHONPATH
        curr_python_path = env.get("PYTHONPATH", "")
        if "/app" not in curr_python_path:
            env["PYTHONPATH"] = f"/app:{curr_python_path}".strip(":")

        return env

    def _execute_fallback(
        self,
        date: str,
        tenant_id: str,
        user_id: str,
        redis_client,
        run_id: str,
        v10_stderr: str,
        fallback_reason: str,
        prediction_trade_date: str,
    ) -> ExecutionResult:
        """执行 inference_alpha158.py 兜底推理脚本。"""
        fallback_path = self.fallback_model_dir / self.fallback_script_name
        if not fallback_path.is_file():
            return ExecutionResult(
                success=False,
                exit_code=self._EXIT_DATA_QUALITY,
                stdout="",
                stderr=v10_stderr,
                error=f"v10 数据质量不足且兜底脚本不存在: {fallback_path}",
                run_id=run_id,
                fallback_used=False,
                fallback_reason=fallback_reason,
                failure_stage="fallback_script",
                active_model_id=self.fallback_model_id,
                active_data_source=self.fallback_data_dir,
                data_trade_date=date,
                prediction_trade_date=prediction_trade_date,
            )

        env = self._get_subprocess_env()
        env.update(
            {
                "MODEL_DIR": str(self.fallback_model_dir),
                "TRADE_DATE": date,
                "OUTPUT_FORMAT": "json",
                "QLIB_PROVIDER_URI": self.fallback_data_dir,
            }
        )

        out_file = self.fallback_model_dir / f"fallback_{run_id}.json"

        try:
            from backend.shared.notification_publisher import publish_notification

            publish_notification(
                user_id="system",
                tenant_id="default",
                title="触发 Alpha158 兜底模型",
                content=f"由于 [{fallback_reason}] 触发了兜底机制，请尽快排查主模型和数据状态。",
                type="system",
                level="error",
            )
        except Exception as e:
            logger.warning("[InferenceScriptRunner] 发布兜底告警通知失败: %s", e)

        python_exec = self._get_python_executable()
        try:
            # 增加环境诊断
            diag_cmd = [
                python_exec,
                "-c",
                "import sys, os; print(f'SUB_PATH: {sys.path}'); import qlib; print(f'QLIB_OK: {qlib.__file__}')",
            ]
            diag_proc = subprocess.run(
                diag_cmd, capture_output=True, text=False, env=env, timeout=10
            )
            logger.info(
                f"[InferenceScriptRunner] 子进程环境诊断: stdout={diag_proc.stdout.decode('utf-8', errors='replace').strip()}, stderr={diag_proc.stderr.decode('utf-8', errors='replace').strip()}"
            )

            cmd = [
                python_exec,
                str(fallback_path),
                "--date",
                date,
                "--output",
                str(out_file),
            ]
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=False,
                cwd=str(self.fallback_model_dir),
                env=env,
                timeout=_SCRIPT_TIMEOUT_SEC,
            )
        except subprocess.TimeoutExpired as exc:
            return ExecutionResult(
                success=False,
                exit_code=-1,
                stdout=(exc.stdout or b"").decode("utf-8", errors="replace"),
                stderr=(exc.stderr or b"").decode("utf-8", errors="replace"),
                error=f"alpha158 兜底脚本超时 ({_SCRIPT_TIMEOUT_SEC}s)",
                run_id=run_id,
                fallback_used=True,
                fallback_reason=fallback_reason,
                failure_stage="fallback_script",
                active_model_id=self.fallback_model_id,
                active_data_source=self.fallback_data_dir,
                data_trade_date=date,
                prediction_trade_date=prediction_trade_date,
            )
        except Exception as exc:
            return ExecutionResult(
                success=False,
                exit_code=-1,
                stdout="",
                stderr="",
                error=f"alpha158 兜底脚本启动失败: {exc}",
                run_id=run_id,
                fallback_used=True,
                fallback_reason=fallback_reason,
                failure_stage="fallback_script",
                active_model_id=self.fallback_model_id,
                active_data_source=self.fallback_data_dir,
                data_trade_date=date,
                prediction_trade_date=prediction_trade_date,
            )

        fb_stdout = (proc.stdout or b"").decode("utf-8", errors="replace")
        fb_stderr = (
            v10_stderr + "\n--- alpha158 fallback ---\n" + (proc.stderr or b"").decode("utf-8", errors="replace")
        ).strip()
        fb_exitcode = proc.returncode

        if fb_exitcode != 0:
            logger.error(
                f"[InferenceScriptRunner] alpha158 兜底脚本失败 exit={fb_exitcode}, run_id={run_id}"
            )
            return ExecutionResult(
                success=False,
                exit_code=fb_exitcode,
                stdout=fb_stdout,
                stderr=fb_stderr,
                error=f"alpha158 兜底脚本返回非零退出码: {fb_exitcode}",
                run_id=run_id,
                fallback_used=True,
                fallback_reason=fallback_reason,
                failure_stage="fallback_script",
                active_model_id=self.fallback_model_id,
                active_data_source=self.fallback_data_dir,
                data_trade_date=date,
                prediction_trade_date=prediction_trade_date,
            )

        signals = self._parse_signals(str(out_file))
        if signals is None:
            return ExecutionResult(
                success=False,
                exit_code=0,
                stdout=fb_stdout,
                stderr=fb_stderr,
                error="alpha158 兜底未能写入合法的 JSON 信号数组",
                run_id=run_id,
                fallback_used=True,
                fallback_reason=fallback_reason,
                failure_stage="fallback_script",
                active_model_id=self.fallback_model_id,
                active_data_source=self.fallback_data_dir,
                data_trade_date=date,
                prediction_trade_date=prediction_trade_date,
            )

        logger.info(
            f"[InferenceScriptRunner] alpha158 兜底成功，{len(signals)} 条信号, run_id={run_id}"
        )
        self._persist_and_publish(
            run_id,
            prediction_trade_date,
            tenant_id,
            user_id,
            signals,
            active_model_id=self.fallback_model_id,
            data_trade_date=date,
            market="CN",  # alpha158 兜底仅 CN 链路
        )

        if redis_client is not None:
            try:
                redis_client.set(
                    f"{_COMPLETED_REDIS_KEY_PREFIX}:{prediction_trade_date}",
                    run_id,
                    ex=86400,
                )
            except Exception as exc:
                logger.warning(f"[InferenceScriptRunner] 写 Redis 完成标记失败: {exc}")

        return ExecutionResult(
            success=True,
            exit_code=0,
            stdout=fb_stdout,
            stderr=fb_stderr,
            signals_count=len(signals),
            run_id=run_id,
            signals=signals,
            fallback_used=True,
            fallback_reason=fallback_reason,
            active_model_id=self.fallback_model_id,
            active_data_source=self.fallback_data_dir,
            data_trade_date=date,
            prediction_trade_date=prediction_trade_date,
        )

    def execute(
        self,
        date: str,
        tenant_id: str = "default",
        user_id: str = "system",
        redis_client=None,
        symbols: list[str] | None = None,
    ) -> ExecutionResult:
        """
        执行 inference.py 脚本，解析信号输出，写库并发布 Redis Stream。

        Parameters
        ----------
        date        : 推理日期，格式 YYYY-MM-DD
        tenant_id   : 租户 ID（用于写库和信号流）
        user_id     : 用户 ID
        redis_client: 可选 Redis 客户端，用于写完成标记
        symbols     : 可选股票代码子集（前缀式，如 ["SH600036"]）。非空时仅对匹配
                      的股票落库与返回，用于「单股补推」；为 None 时全市场（原行为）。
        """
        script_path = self.primary_model_dir / self.primary_script_name
        primary_meta = self._read_primary_metadata()
        model_market = str((primary_meta.get("context") or {}).get("market") or "A").upper()
        prediction_trade_date = self._resolve_prediction_trade_date(date, market=model_market)
        data_source = str(primary_meta.get("data_source") or "").lower()
        active_data_source = self._resolve_primary_active_data_source(primary_meta)
        if not script_path.is_file():
            # parquet 数据源模型：自动写入模板脚本，无需手动部署
            if data_source in ("parquet", "quantdb_factors") and self._try_deploy_parquet_template(
                script_path
            ):
                logger.info(
                    "[InferenceScriptRunner] parquet 模型自动注入推理脚本: %s",
                    script_path,
                )
            else:
                run_id = f"run_{date.replace('-', '')}_{uuid.uuid4().hex[:8]}"
                fallback_reason = f"主模型推理脚本不存在: {script_path}"
                logger.warning(
                    "[InferenceScriptRunner] 主模型脚本缺失，触发 alpha158 兜底, run_id=%s, reason=%s",
                    run_id,
                    fallback_reason,
                )
                if not self.enable_fallback or model_market not in ("CN", "A"):
                    return ExecutionResult(
                        success=False,
                        exit_code=1,
                        stdout="",
                        stderr="",
                        error=fallback_reason,
                        run_id=run_id,
                        failure_stage="main_script",
                        active_model_id=self.primary_model_id,
                    )
                return self._execute_fallback(
                    date=date,
                    tenant_id=tenant_id,
                    user_id=user_id,
                    redis_client=redis_client,
                    run_id=run_id,
                    v10_stderr=fallback_reason,
                    fallback_reason=fallback_reason,
                    prediction_trade_date=prediction_trade_date,
                )

        if data_source in ("parquet", "quantdb_factors"):
            # 模板选择以 metadata 为准（marker 嗅探会被覆写过的脚本骗过：
            # 融合脚本一旦被 parquet 模板覆写，头标记就变成了 parquet，
            # 永远无法自愈）。融合模型强制同步 ensemble_src 模板。
            primary_meta = self._read_primary_metadata()
            is_ensemble_model = bool(
                primary_meta.get("is_ensemble")
                or str(primary_meta.get("model_type") or "").lower() == "ensemble"
                or str(primary_meta.get("framework") or "").lower() == "ensemble"
            )
            if is_ensemble_model:
                if not self._try_deploy_ensemble_src_template(script_path):
                    logger.warning(
                        "[InferenceScriptRunner] 融合模型模板同步失败，继续使用现有脚本: %s",
                        script_path,
                    )
            elif not self._ensure_parquet_template_script(script_path):
                logger.warning(
                    "[InferenceScriptRunner] parquet 模型模板同步失败，继续使用现有脚本: %s",
                    script_path,
                )

        run_id = f"run_{date.replace('-', '')}_{uuid.uuid4().hex[:8]}"
        logger.info(
            f"[InferenceScriptRunner] 启动推理脚本, run_id={run_id}, date={date}"
        )

        expected_dim = self._resolve_expected_feature_dim()

        # 判断数据源：针对不同存储引擎执行对应的就绪检查
        if data_source == "parquet":
            readiness = self._query_parquet_readiness(trade_date=date)
        elif data_source == "quantdb_factors":
            readiness = self._query_quantdb_readiness(trade_date=date)
        elif data_source in ("qlib", "qlib_bin", "bin"):
            readiness = self._query_qlib_readiness(trade_date=date)
        else:
            # 默认回退到数据库维度检查（兼容旧模型）
            readiness = self._query_dimension_readiness(
                trade_date=date, expected_dim=expected_dim
            )

        logger.info(
            "[InferenceScriptRunner] 数据源就绪检查: source=%s, ready=%s, detail=%s",
            data_source or "default_db",
            readiness.get("ready"),
            readiness.get("detail"),
        )

        if not readiness.get("ready", False):
            fallback_reason = f"主模型维度门禁未通过: {readiness.get('detail', 'N/A')}"
            logger.warning(
                "[InferenceScriptRunner] 主模型数据维度不足，触发 alpha158 兜底, run_id=%s, reason=%s",
                run_id,
                fallback_reason,
            )
            if not self.enable_fallback or model_market not in ("CN", "A"):
                return ExecutionResult(
                    success=False,
                    exit_code=1,
                    stdout="",
                    stderr="",
                    error=fallback_reason,
                    run_id=run_id,
                    failure_stage="main_script",
                    active_model_id=self.primary_model_id,
                )
            return self._execute_fallback(
                date=date,
                tenant_id=tenant_id,
                user_id=user_id,
                redis_client=redis_client,
                run_id=run_id,
                v10_stderr=fallback_reason,
                fallback_reason=fallback_reason,
                prediction_trade_date=prediction_trade_date,
            )

        # 注入平台环境变量
        env = self._get_subprocess_env()
        # Resolve raw source directory.  Direct QuantDB models never fall back
        # to model_features_*.parquet.
        primary_meta = self._read_primary_metadata()
        parquet_data_dir = (
            _resolve_market_factor_data_dir(primary_meta)
            if data_source == "quantdb_factors" else str(
                primary_meta.get("data_dir")
                or os.getenv("MODEL_TRAINING_DATA_DIR", "/app/db/feature_snapshots")
            )
        )
        env.update(
            {
                "MODEL_DIR": str(self.primary_model_dir),
                "TRADE_DATE": date,
                "OUTPUT_FORMAT": "json",
                "QLIB_PROVIDER_URI": self.primary_data_dir,
                "MODEL_TRAINING_DATA_DIR": parquet_data_dir,
            }
        )

        # 执行子进程
        out_file = self.primary_model_dir / f"main_{run_id}.json"
        python_exec = self._get_python_executable()
        model_dir = self.primary_model_dir
        try:
            cmd = [
                python_exec,
                str(script_path),
                "--date",
                date,
                "--output",
                str(out_file),
            ]
            # 把 legacy "A" 归一化为 "CN"，其余市场原样传给推理脚本
            cli_market = model_market if model_market != "A" else "CN"
            if cli_market in ("US", "HK", "CRYPTO", "FUTURES"):
                cmd += ["--market", cli_market]
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=False,
                cwd=str(model_dir),
                env=env,
                timeout=_SCRIPT_TIMEOUT_SEC,
            )
        except subprocess.TimeoutExpired as exc:
            logger.error(
                f"[InferenceScriptRunner] 脚本超时 ({_SCRIPT_TIMEOUT_SEC}s), run_id={run_id}"
            )
            return ExecutionResult(
                success=False,
                exit_code=-1,
                stdout=(exc.stdout or b"").decode("utf-8", errors="replace"),
                stderr=(exc.stderr or b"").decode("utf-8", errors="replace"),
                error=f"脚本执行超时（{_SCRIPT_TIMEOUT_SEC}s）",
                run_id=run_id,
                failure_stage="main_script",
                active_model_id=self.primary_model_id,
                active_data_source=self.primary_data_dir,
            )
        except Exception as exc:
            logger.error(f"[InferenceScriptRunner] 脚本启动失败: {exc}")
            return ExecutionResult(
                success=False,
                exit_code=-1,
                stdout="",
                stderr="",
                error=str(exc),
                run_id=run_id,
                failure_stage="main_script",
                active_model_id=self.primary_model_id,
                active_data_source=self.primary_data_dir,
            )

        stdout = (proc.stdout or b"").decode("utf-8", errors="replace")
        stderr = (proc.stderr or b"").decode("utf-8", errors="replace")
        exit_code = proc.returncode

        if exit_code != 0:
            # exit code 2 = 数据质量不足 → 尝试 alpha158 兜底
            if exit_code == self._EXIT_DATA_QUALITY:
                fallback_reason = (
                    stderr.strip().splitlines()[-1]
                    if stderr.strip()
                    else "v10 数据质量不足"
                )
                logger.warning(
                    f"[InferenceScriptRunner] v10 数据质量不足 (exit=2)，启动 alpha158 兜底, run_id={run_id}"
                )
                if not self.enable_fallback or model_market not in ("CN", "A"):
                    return ExecutionResult(
                        success=False,
                        exit_code=exit_code,
                        stdout=stdout,
                        stderr=stderr,
                        error=fallback_reason,
                        run_id=run_id,
                        failure_stage="main_script",
                        active_model_id=self.primary_model_id,
                    )
                return self._execute_fallback(
                    date=date,
                    tenant_id=tenant_id,
                    user_id=user_id,
                    redis_client=redis_client,
                    run_id=run_id,
                    v10_stderr=stderr,
                    fallback_reason=fallback_reason,
                    prediction_trade_date=prediction_trade_date,
                )

            logger.error(
                f"[InferenceScriptRunner] 脚本异常退出 exit_code={exit_code}, run_id={run_id}\nstderr: {stderr[:500]}"
            )
            return ExecutionResult(
                success=False,
                exit_code=exit_code,
                stdout=stdout,
                stderr=stderr,
                error=f"脚本返回非零退出码: {exit_code}",
                run_id=run_id,
                failure_stage="main_script",
                active_model_id=self.primary_model_id,
                active_data_source=self.primary_data_dir,
                data_trade_date=date,
                prediction_trade_date=prediction_trade_date,
            )

        # 解析信号
        signals = self._parse_signals(str(out_file))
        if signals is None:
            return ExecutionResult(
                success=False,
                exit_code=exit_code,
                stdout=stdout,
                stderr=stderr,
                error='输出文件不存在或不是合法的 JSON 信号数组，期望格式：[{"symbol":"...","score":0.0},...]',
                run_id=run_id,
                failure_stage="output_parse",
                active_model_id=self.primary_model_id,
                active_data_source=self.primary_data_dir,
            )

        # 单股补推：非空 symbols 时仅保留目标股票，后续落库/返回只针对这些股票。
        # 推理脚本仍对全池出分（不改子进程与模板），只裁剪写库与返回，
        # 从而 predict-stock 能立即读到该股的真实分数，又不破坏全市场批量路径。
        # partial_applied=True 时落库走按 symbol 缩小删除范围的局部覆盖，
        # 避免单股补推把当日全市场信号桶整桶删成 1 条。
        partial_applied = False
        if symbols:
            target_norm = set()
            for s in symbols:
                try:
                    target_norm.add(StockCodeUtil.to_prefix(s))
                except Exception:
                    target_norm.add(str(s).strip())
            kept = []
            dropped = 0
            for sig in signals:
                try:
                    sig_sym = StockCodeUtil.to_prefix(str(sig.get("symbol") or ""))
                except Exception:
                    sig_sym = str(sig.get("symbol") or "")
                if sig_sym in target_norm:
                    kept.append(sig)
                else:
                    dropped += 1
            if kept:
                signals = kept
                partial_applied = True
                logger.info(
                    "[InferenceScriptRunner] 单股补推过滤: %d/%d 条入选 (target=%s), run_id=%s",
                    len(kept), len(kept) + dropped, sorted(target_norm), run_id,
                )
            else:
                logger.warning(
                    "[InferenceScriptRunner] 单股补推未命中任何目标股票 (target=%s)，保留全量信号避免空库, run_id=%s",
                    sorted(target_norm), run_id,
                )

        logger.info(
            f"[InferenceScriptRunner] 解析到 {len(signals)} 条信号, run_id={run_id}"
        )

        # 写库 + 发布 Redis Stream（partial=单股补推时局部覆盖，不整桶删除）
        # universe_tag 标注市场（HK 推理 → 'HK'），老行 NULL=CN；A 股路径值 'CN' 与历史等价
        persist_market = "CN" if model_market in ("A", "") else model_market
        self._persist_and_publish(
            run_id,
            prediction_trade_date,
            tenant_id,
            user_id,
            signals,
            active_model_id=self.primary_model_id,
            data_trade_date=date,
            partial=partial_applied,
            market=persist_market,
        )

        # 写 Redis 完成标记
        if redis_client is not None:
            try:
                redis_client.set(
                    f"{_COMPLETED_REDIS_KEY_PREFIX}:{prediction_trade_date}",
                    run_id,
                    ex=86400,
                )
            except Exception as exc:
                logger.warning(
                    f"[InferenceScriptRunner] 写 Redis 完成标记失败（不影响主流程）: {exc}"
                )

        return ExecutionResult(
            success=True,
            exit_code=0,
            stdout=stdout,
            stderr=stderr,
            signals_count=len(signals),
            run_id=run_id,
            signals=signals,
            active_model_id=self.primary_model_id,
            active_data_source=active_data_source,
            data_trade_date=date,
            prediction_trade_date=prediction_trade_date,
        )

    # ------------------------------------------------------------------
    # 内部方法
    # ------------------------------------------------------------------

    @staticmethod
    def _resolve_signal_sides(
        scores: list[float],
        consensus_list: list[int] | None = None,
        confidence_list: list[float] | None = None,
        buy_pct: float = 0.20,
        sell_pct: float = 0.20,
        min_buy_score: float = 0.2,
        max_sell_score: float = -0.2,
        min_consensus: int = 4,
        min_confidence: float = 0.3,
    ) -> list[str]:
        """双指标信号逻辑：百分比排名 + 共识度 + 绝对方向闸门 + 置信度。

        逻辑：
        - Top buy_pct 百分位 AND score > min_buy_score AND consensus >= min_consensus → BUY
        - Bottom sell_pct 百分位 AND score < max_sell_score AND consensus >= min_consensus → SELL
        - 分歧太大 (consensus < min_consensus) 或低置信 (confidence < min_confidence) → HOLD
        - 其余 → HOLD

        confidence_list 为 None 时跳过置信度门槛（兼容旧版）。
        """
        import numpy as np

        if not scores:
            return []

        arr = np.array(scores)
        n = len(arr)

        # 计算百分位阈值
        buy_threshold = np.percentile(arr, (1 - buy_pct) * 100)
        sell_threshold = np.percentile(arr, sell_pct * 100)

        # 生成信号（百分位 + 方向 + 共识度 + 置信度 四重约束）
        has_consensus = consensus_list is not None and len(consensus_list) == n
        has_confidence = confidence_list is not None and len(confidence_list) == n
        sides = []
        for i, s in enumerate(scores):
            # 置信度门槛（None 视为低置信 → HOLD，避免 NoneType 比较异常）
            conf = confidence_list[i] if has_confidence else None
            low_conf = conf is not None and conf < min_confidence
            if has_confidence and conf is None:
                low_conf = True
            is_buy = s >= buy_threshold and s > min_buy_score
            is_sell = s <= sell_threshold and s < max_sell_score
            if (has_consensus and consensus_list[i] < min_consensus) or low_conf:
                sides.append("HOLD")  # 分歧太大或低置信
            elif is_buy:
                sides.append("BUY")
            elif is_sell:
                sides.append("SELL")
            else:
                sides.append("HOLD")

        # 统计日志
        buy_count = sides.count("BUY")
        sell_count = sides.count("SELL")
        hold_count = sides.count("HOLD")
        logger.info(
            f"SignalLogic] 双指标+方向信号: BUY={buy_count}({buy_count/n*100:.1f}%), "
            f"SELL={sell_count}({sell_count/n*100:.1f}%), HOLD={hold_count}({hold_count/n*100:.1f}%), "
            f"buy_threshold={buy_threshold:.4f} (min_buy={min_buy_score}), "
            f"sell_threshold={sell_threshold:.4f} (max_sell={max_sell_score}), "
            f"score_range=[{arr.min():.4f}, {arr.max():.4f}], mean={arr.mean():.4f}"
        )

        return sides

    @staticmethod
    def _get_st_symbols() -> set[str]:
        """从数据库获取当前 ST/*ST 股票代码集合。"""
        try:
            import os
            from sqlalchemy import create_engine, text as sql_text

            db_url = os.getenv("DATABASE_URL", "")
            if "+asyncpg" in db_url:
                db_url = db_url.replace("+asyncpg", "+psycopg2")
            if not db_url.startswith("postgresql"):
                return set()
            engine = create_engine(db_url, pool_pre_ping=True)
            with engine.begin() as conn:
                rows = conn.execute(sql_text(
                    "SELECT DISTINCT symbol FROM stock_daily_latest "
                    "WHERE is_st = 1 AND trade_date >= CURRENT_DATE - INTERVAL '30 days'"
                )).fetchall()
            engine.dispose()
            return {r[0] for r in rows}
        except Exception:
            return set()

    @staticmethod
    def _normalize_code(code: str) -> str:
        """提取纯 6 位数字代码，去掉 SH/SZ/BJ 前缀和 .SH/.SZ/.BJ 后缀。"""
        c = code.strip().upper()
        for prefix in ("SH", "SZ", "BJ"):
            if c.startswith(prefix):
                c = c[len(prefix):]
                break
        c = c.split(".")[0]
        return c

    @staticmethod
    def _normalize_signal_symbols(
        signals_sorted: list[dict],
    ) -> tuple[list[str], list[str]]:
        """返回 (raw_symbols, plain_symbols)。

        engine_signal_scores.symbol 约定为纯数字（600519，历史全库一致）。
        QuantDB 直读模型的 symbol 是前缀（SH600097）或后缀（600097.SH），
        落库/信号流/position_score 一律用归一后的纯数字；原始格式仅保留
        给行情 Redis 查价（需要市场前缀定位 stock:{code}.SH）。
        """
        raw = [str(s["symbol"]) for s in signals_sorted]
        return raw, [InferenceScriptRunner._normalize_code(x) for x in raw]

    @staticmethod
    def _is_st_symbol(symbol: str, st_symbols: set[str], st_normalized: set[str] | None = None) -> bool:
        """判断股票代码是否为 ST。"""
        if symbol in st_symbols:
            return True
        if st_normalized is None:
            st_normalized = {InferenceScriptRunner._normalize_code(s) for s in st_symbols}
        sym_code = InferenceScriptRunner._normalize_code(symbol)
        return sym_code in st_normalized if sym_code else False

    @staticmethod
    def _parse_signals(file_path: str) -> list[dict] | None:
        """从指定的 json 文件读取信号数组，解析成功后自动删除文件。返回 None 表示失败。"""
        p = Path(file_path)
        if not p.is_file():
            return None
        try:
            with open(p, encoding="utf-8") as f:
                data = json.load(f)
        except json.JSONDecodeError:
            return None
        finally:
            try:
                p.unlink()
            except Exception:
                pass

        if not isinstance(data, list):
            return None

        # 获取 ST 股票列表（预计算标准化代码集合）
        st_symbols = InferenceScriptRunner._get_st_symbols()
        st_normalized = {InferenceScriptRunner._normalize_code(s) for s in st_symbols}

        valid = []
        for item in data:
            if isinstance(item, dict) and "symbol" in item and "score" in item:
                try:
                    symbol = str(item["symbol"]).strip().upper()
                    # 1. 排除 B 股: 上海 B (900xxx), 深圳 B (200xxx)
                    if symbol.startswith("SH900") or symbol.startswith("SZ200"):
                        continue
                    if ".SH" in symbol and symbol.startswith("900"):
                        continue
                    if ".SZ" in symbol and symbol.startswith("200"):
                        continue
                    # 处理无前缀的纯数字
                    if symbol.isdigit() and len(symbol) == 6:
                        if symbol.startswith("900") or symbol.startswith("200"):
                            continue

                    # 2. 排除北交所: BJ 前缀或 .BJ 后缀，或数字开头 (43, 83, 87, 88)
                    if symbol.startswith("BJ") or ".BJ" in symbol:
                        continue
                    if symbol.startswith(("43", "83", "87", "88", "92")):
                        continue

                    # 3. 排除指数代码: SH000xxx, SZ399xxx 等
                    if symbol.startswith("SH000") or symbol.startswith("SZ399"):
                        continue
                    if symbol.startswith("000") and symbol.endswith(".SH"):
                        continue
                    if symbol.startswith("399") and symbol.endswith(".SZ"):
                        continue

                    # 4. 排除 ST/*ST 股票（代码匹配 + 名称匹配）
                    sym_code = InferenceScriptRunner._normalize_code(symbol)
                    if sym_code and sym_code in st_normalized:
                        continue
                    # 名称包含 ST 的也要排除（兜底）
                    name = str(item.get("name", "")).upper()
                    if "ST" in name and ("*" in name or name.startswith("ST")):
                        continue

                    valid.append(
                        {
                            "symbol": str(item["symbol"]),
                            "score": float(item["score"]),
                            "consensus": int(item.get("consensus", 0)),
                            "zfusion": float(item.get("zfusion", 0.0)),
                            "detail": item.get("detail", {}),
                        }
                    )
                except (ValueError, TypeError):
                    pass
        return valid if valid else None

    @staticmethod
    def _normalize_model_bucket(model_id: str | None) -> str:
        raw = str(model_id or "").strip().lower()
        if not raw:
            return "inference_script"
        slug = re.sub(r"[^a-z0-9_]+", "_", raw).strip("_")
        return slug[:48] if slug else "inference_script"

    @classmethod
    def _resolve_feature_version(cls, model_id: str | None) -> str:
        return f"script_v1_{cls._normalize_model_bucket(model_id)}"

    def _persist_and_publish(
        self,
        run_id: str,
        prediction_trade_date: str,
        tenant_id: str,
        user_id: str,
        signals: list[dict],
        *,
        active_model_id: str | None = None,
        data_trade_date: str | None = None,
        partial: bool = False,
        market: str | None = None,
    ) -> None:
        """
        将推理结果写入 engine_signal_scores 并发布到 Redis Stream。

        存储策略：按模型桶覆盖（同 tenant/user/date/model），保证同日不同模型可并存。
        partial=True（单股补推）时只覆盖目标 symbol 的行，不整桶删除当日全市场信号。

        Args:
            data_trade_date: 推理日期（数据截止日期），若不传则默认等于 prediction_trade_date
        """
        # 按分数降序排列，排名越靠前分数越高
        signals_sorted = sorted(signals, key=lambda x: x["score"], reverse=True)
        # symbol 统一归一为纯数字（落库/信号流/position_score 约定），
        # raw 保留原始格式给行情 Redis 查价
        raw_symbols, symbols = InferenceScriptRunner._normalize_signal_symbols(
            signals_sorted
        )
        scores = [s["score"] for s in signals_sorted]
        consensus_list = [s.get("consensus", 0) for s in signals_sorted]
        zfusion_list = [s.get("zfusion", 0.0) for s in signals_sorted]
        detail_list = [s.get("detail", {}) for s in signals_sorted]
        confidence_list = [s.get("confidence") for s in signals_sorted]
        signal_sides = self._resolve_signal_sides(
            scores, consensus_list, confidence_list
        )
        feature_dim = max(1, self._resolve_expected_feature_dim())
        model_name = str(active_model_id or self.primary_model_id or "inference_script")
        feature_version = self._resolve_feature_version(model_name)
        # 推理日期默认等于预测日期（兼容旧调用）
        inference_date = data_trade_date or prediction_trade_date

        # shared.database 的 SessionLocal 在 asyncpg URL 下会触发 greenlet 错误，
        # 这里显式构造一个同步驱动会话，仅用于脚本写库链路。
        sync_db_url = os.getenv("DATABASE_URL", "")
        if "+asyncpg" in sync_db_url:
            sync_db_url = sync_db_url.replace("+asyncpg", "+psycopg2")
        if not sync_db_url.startswith("postgresql"):
            sync_db_url = (
                "postgresql+psycopg2://postgres:password@localhost:5432/quantmind"
            )
        sync_engine = create_engine(sync_db_url, pool_pre_ping=True, future=True)
        SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=sync_engine)

        db = SessionLocal()
        try:
            with _INFER_PERSIST_LOCK:
                self._persist_locked(
                    db=db,
                    run_id=run_id,
                    prediction_trade_date=prediction_trade_date,
                    tenant_id=tenant_id,
                    user_id=user_id,
                    signals=signals,
                    symbols=symbols,
                    scores=scores,
                    consensus_list=consensus_list,
                    zfusion_list=zfusion_list,
                    detail_list=detail_list,
                    confidence_list=confidence_list,
                    feature_dim=feature_dim,
                    model_name=model_name,
                    feature_version=feature_version,
                    inference_date=inference_date,
                    signal_sides=signal_sides,
                    raw_symbols=raw_symbols,
                    partial=partial,
                    market=market,
                )
            # 信号落库后：按当日截面算 position_score（凯利仓位建议）写回 quality JSONB。
            # 失败不影响主流程（信号已落库），仅告警。
            try:
                from backend.services.engine.inference.position_signal import (
                    compute_position_scores,
                    batch_update_quality,
                )
                preds = compute_position_scores(symbols, scores, signal_sides)
                batch_update_quality(db, run_id, tenant_id, user_id, preds)
            except Exception as exc:
                logger.warning("[InferenceScriptRunner] position_score 计算失败（信号已落库，忽略）: %s", exc)
        except Exception as exc:
            logger.error(f"[InferenceScriptRunner] 写库失败: {exc}")
            db.rollback()
        finally:
            db.close()
            try:
                sync_engine.dispose()
            except Exception:
                pass

        # 发布信号到 Redis Stream（失败不影响主流程）
        try:
            signal_events = [
                {
                    "signal_id": f"{run_id}-{idx:04d}",
                    "client_order_id": f"coid-{run_id}-{idx:04d}",
                    "symbol": sym,
                    "score": score,
                    "quantity": 100,
                    "price": 0.0,
                }
                for idx, (sym, score) in enumerate(zip(symbols, scores, strict=True))
            ]
            publisher = EngineSignalStreamPublisher()
            publisher.mark_latest_run(
                tenant_id=tenant_id,
                user_id=str(user_id),
                run_id=run_id,
            )
            published = publisher.publish_signals(
                tenant_id=tenant_id,
                user_id=str(user_id),
                run_id=run_id,
                trace_id=run_id,
                signal_source="inference_script",
                signals=signal_events,
            )
            logger.info(
                f"[InferenceScriptRunner] 已发布 {published} 条信号, run_id={run_id}"
            )

            # === 推送到通达信 (Top N 选股: 板块 + 预警 + 消息) ===
            if os.getenv("ENABLE_TDX_PUSH", "").strip().lower() == "true":
                try:
                    # A 股专属推送：HK/US 等市场信号不推通达信（读取侧已有 CN 门，源头再挡一次）
                    if market and str(market).upper() not in ("A", "CN", ""):
                        logger.info(
                            "[InferenceScriptRunner] 非 A 股市场跳过 TDX 选股推送 market=%s run=%s",
                            market,
                            run_id,
                        )
                        return
                    import asyncio

                    from backend.services.live_trading.services.tdx_signal_push_service import (
                        tdx_signal_pusher,
                    )

                    async def _push_to_tdx():
                        result = await tdx_signal_pusher.build_push_payload(
                            tenant_id=tenant_id,
                            user_id=str(user_id),
                            run_id=run_id,
                        )
                        if result.get("success"):
                            logger.info(
                                "[InferenceScriptRunner] 已推送 %d 只选股到通达信 (run=%s)",
                                result.get("pushed", 0),
                                run_id,
                            )
                        else:
                            logger.warning(
                                "[InferenceScriptRunner] 通达信选股推送未成功: %s",
                                result.get("error") or "未知原因",
                            )

                    try:
                        asyncio.get_running_loop()
                    except RuntimeError:
                        # celery 同步上下文: 没有运行中的事件循环
                        asyncio.run(_push_to_tdx())
                    else:
                        asyncio.create_task(_push_to_tdx())

                    # === 滚动买卖检查 (分数>2.2买/掉下2.2卖/大盘MA20过滤) ===
                    # A 股专属链路：HK/US 等市场信号不推通达信（market 参数即 universe_tag 市场）
                    if market and str(market).upper() not in ("A", "CN", ""):
                        logger.info(
                            "[InferenceScriptRunner] 非 A 股市场跳过 TDX 滚动推送 "
                            "market=%s run=%s",
                            market,
                            run_id,
                        )
                        return
                    from backend.services.live_trading.services.tdx_rolling_trade_service import (
                        tdx_rolling_trader,
                    )

                    async def _run_rolling_push():
                        result = await tdx_rolling_trader.run_rolling_push(
                            tenant_id=tenant_id,
                            user_id=str(user_id),
                            run_id=run_id,
                        )
                        if result.get("success"):
                            logger.info(
                                "[InferenceScriptRunner] 滚动买卖推送完成 run=%s buys=%d sells=%d",
                                run_id,
                                len(result.get("buys") or []),
                                len(result.get("sells") or []),
                            )
                        else:
                            logger.warning(
                                "[InferenceScriptRunner] 滚动买卖推送未成功: %s",
                                result.get("error") or "未知原因",
                            )

                    try:
                        asyncio.get_running_loop()
                    except RuntimeError:
                        asyncio.run(_run_rolling_push())
                    else:
                        asyncio.create_task(_run_rolling_push())
                except Exception as exc:
                    logger.warning(
                        f"[InferenceScriptRunner] 推送通达信失败（不影响结果）: {exc}"
                    )
        except Exception as exc:
            logger.warning(
                f"[InferenceScriptRunner] 信号发布失败（不影响 DB 结果）: {exc}"
            )

    def _persist_locked(
        self,
        *,
        db,
        run_id: str,
        prediction_trade_date: str,
        tenant_id: str,
        user_id: str,
        signals: list[dict],
        symbols: list[str],
        scores: list[float],
        feature_dim: int,
        model_name: str,
        feature_version: str,
        inference_date: str,
        signal_sides: list[str],
        consensus_list: list[int] | None = None,
        zfusion_list: list[float] | None = None,
        detail_list: list[dict] | None = None,
        confidence_list: list[float] | None = None,
        raw_symbols: list[str] | None = None,
        partial: bool = False,
        market: str | None = None,
    ) -> None:
        """写库逻辑（在 _INFER_PERSIST_LOCK 保护下执行）。

        并发推理时多个 worker 会同时写 engine_signal_scores / engine_feature_runs /
        qm_research_candidate_snapshot。PostgreSQL 对并发 DELETE+INSERT 大量行会
        发生锁竞争甚至卡死，因此把整个写库串行化。推理子进程仍并发执行，仅写库串行。
        """
        prediction_day = date.fromisoformat(prediction_trade_date)
        retention_floor = (
            prediction_day - timedelta(days=max(1, _PREDICTION_RETENTION_DAYS))
        ).isoformat()

        # ── Step 0.1: 清理超出保留期的历史数据（默认 30 天）────────────
        db.execute(
            text("""
                DELETE FROM engine_signal_scores
                WHERE tenant_id = :tenant_id
                  AND user_id = :user_id
                  AND model_version = 'inference_script'
                  AND feature_version = :feature_version
                  AND trade_date < :retention_floor
            """),
            {
                "tenant_id": tenant_id,
                "user_id": user_id,
                "feature_version": feature_version,
                "retention_floor": retention_floor,
            },
        )
        db.execute(
            text("""
                DELETE FROM engine_feature_runs
                WHERE tenant_id = :tenant_id
                  AND user_id = :user_id
                  AND source = 'inference_script'
                  AND feature_version = :feature_version
                  AND trade_date < :retention_floor
            """),
            {
                "tenant_id": tenant_id,
                "user_id": user_id,
                "feature_version": feature_version,
                "retention_floor": retention_floor,
            },
        )

        # ── Step 0.2: 删除当日旧推理结果（覆盖策略）───────────────────
        # partial（单股补推）时只删目标 symbol 的旧行，保留当日全市场信号。
        # 注意：unique 键含 run_id，新 run 会另插一行，同 symbol 当日可能并存多行，
        # 读侧按最新 created_at 取最新（与个股分数曲线口径一致）。
        _sym_filter = " AND symbol = ANY(:partial_symbols)" if partial else ""
        db.execute(
            text(f"""
                DELETE FROM engine_signal_scores
                WHERE trade_date    = :trade_date
                  AND tenant_id    = :tenant_id
                  AND user_id      = :user_id
                  AND model_version = 'inference_script'
                  AND feature_version = :feature_version
                  {_sym_filter}
            """),
            {
                "trade_date": prediction_trade_date,
                "tenant_id": tenant_id,
                "user_id": user_id,
                "feature_version": feature_version,
                "partial_symbols": symbols if partial else None,
            },
        )
        if partial:
            logger.info(
                f"[InferenceScriptRunner] 单股补推局部覆盖: 仅替换 {len(symbols)} 只标的的旧信号, run_id={run_id}"
            )
        else:
            # 同步清除旧 feature_runs 记录（保留最新 run_id）
            db.execute(
                text("""
                    DELETE FROM engine_feature_runs
                    WHERE trade_date = :trade_date
                      AND tenant_id  = :tenant_id
                      AND user_id    = :user_id
                      AND source     = 'inference_script'
                      AND feature_version = :feature_version
                """),
                {
                    "trade_date": prediction_trade_date,
                    "tenant_id": tenant_id,
                    "user_id": user_id,
                    "feature_version": feature_version,
                },
            )
        logger.info(
            f"[InferenceScriptRunner] 已清除 {prediction_trade_date} 旧推理数据(模型桶={feature_version}, partial={partial}), run_id={run_id}"
        )

        # ── Step 1: 写入本次 feature run 记录（单股补推不覆盖全市场 feature_runs）─
        if not partial:
            db.execute(
            text("""
                INSERT INTO engine_feature_runs (
                    run_id, tenant_id, user_id, trade_date, model_name, model_version,
                    feature_version, feature_dim, status, expected_symbols, ready_symbols,
                    source, created_at, updated_at
                ) VALUES (
                    :run_id, :tenant_id, :user_id, :trade_date,
                    :model_name, 'inference_script',
                    :feature_version, :feature_dim, 'signal_ready',
                    :n, :n, 'inference_script', NOW(), NOW()
                )
                ON CONFLICT (run_id) DO UPDATE SET
                    model_name = EXCLUDED.model_name,
                    feature_version = EXCLUDED.feature_version,
                    status = 'signal_ready', updated_at = NOW()
            """),
            {
                "run_id": run_id,
                "tenant_id": tenant_id,
                "user_id": user_id,
                "trade_date": prediction_trade_date,
                "n": len(signals),
                "model_name": model_name,
                "feature_version": feature_version,
                "feature_dim": feature_dim,
            },
        )

        # ── Step 2: 批量写入信号评分（含 signal_side 和 expected_price）──────────
        import redis as redis_lib

        redis_host = os.getenv("REMOTE_QUOTE_REDIS_HOST", "redis")
        redis_port = int(os.getenv("REMOTE_QUOTE_REDIS_PORT", "6379"))
        redis_password = os.getenv(
            "REMOTE_QUOTE_REDIS_PASSWORD", ""
        ) or None
        try:
            quote_redis = redis_lib.Redis(
                host=redis_host,
                port=redis_port,
                password=redis_password,
                decode_responses=True,
                socket_timeout=2,
            )
            quote_redis.ping()
            logger.info(
                f"[InferenceScriptRunner] 已连接行情 Redis: {redis_host}:{redis_port}"
            )
        except Exception as redis_err:
            logger.warning(
                f"[InferenceScriptRunner] 无法连接行情 Redis: {redis_err}, 价格将缺失"
            )
            quote_redis = None

        score_sql = text("""
            INSERT INTO engine_signal_scores (
                run_id, tenant_id, user_id, trade_date, symbol,
                model_version, feature_version, universe_tag,
                light_score, tft_score, fusion_score, risk_weight, regime,
                signal_side, expected_price, quality, created_at
            ) VALUES (
                :run_id, :tenant_id, :user_id, :trade_date, :symbol,
                'inference_script', :feature_version, :universe_tag,
                NULL, NULL, :score, 1.0, 'normal',
                :signal_side, :expected_price, CAST(:quality AS jsonb), NOW()
            )
            ON CONFLICT (tenant_id, user_id, trade_date, symbol, model_version, feature_version, run_id)
            DO UPDATE SET
                fusion_score = EXCLUDED.fusion_score,
                signal_side = EXCLUDED.signal_side,
                expected_price = EXCLUDED.expected_price,
                quality = EXCLUDED.quality
        """)
        # universe_tag 市场标注：HK/US 等非 CN 写入市场大写；A 股路径 'CN'（老行 NULL 视为 CN）
        universe_tag = "CN" if market in (None, "", "A") else str(market).upper()
        has_consensus = consensus_list is not None and len(consensus_list) == len(symbols)
        has_zfusion = zfusion_list is not None and len(zfusion_list) == len(symbols)
        has_detail = detail_list is not None and len(detail_list) == len(symbols)
        for idx, (sym, score) in enumerate(zip(symbols, scores, strict=True)):
            expected_price = None
            signal_side = signal_sides[idx]
            # 构建 quality JSONB
            quality_parts = {"consensus": consensus_list[idx]} if has_consensus else {}
            if has_zfusion:
                quality_parts["zfusion"] = round(zfusion_list[idx], 6)
            if has_detail:
                quality_parts["detail"] = detail_list[idx]
            if confidence_list is not None and idx < len(confidence_list) and confidence_list[idx] is not None:
                quality_parts["confidence"] = round(float(confidence_list[idx]), 4)
            quality = json.dumps(quality_parts) if quality_parts else None
            if quote_redis:
                try:
                    # symbols 已归一为纯数字，行情 Redis 查价需要原始
                    # 市场前缀定位（stock:{code}.SH），raw_symbols 兜底原样
                    raw_sym0 = raw_symbols[idx] if raw_symbols else sym
                    raw_sym = (
                        raw_sym0.replace("SH", "").replace("SZ", "").replace("BJ", "")
                    )
                    if raw_sym0.startswith("SH"):
                        redis_key = f"stock:{raw_sym}.SH"
                    elif raw_sym0.startswith("SZ"):
                        redis_key = f"stock:{raw_sym}.SZ"
                    elif raw_sym0.startswith("BJ") or raw_sym.startswith("920"):
                        redis_key = f"stock:{raw_sym}.BJ"
                    else:
                        redis_key = f"stock:{raw_sym0}"
                    now_price = quote_redis.hget(redis_key, "Now")
                    if now_price:
                        expected_price = float(now_price)
                except Exception as e:
                    logger.debug(
                        f"[InferenceScriptRunner] 获取 {sym} 价格失败: {e}"
                    )
            db.execute(
                score_sql,
                {
                    "run_id": run_id,
                    "tenant_id": tenant_id,
                    "user_id": user_id,
                    "trade_date": prediction_trade_date,
                    "symbol": sym,
                    "feature_version": feature_version,
                    "universe_tag": universe_tag,
                    "score": score,
                    "signal_side": signal_side,
                    "expected_price": expected_price,
                    "quality": quality,
                },
            )
        if quote_redis:
            try:
                quote_redis.close()
            except Exception:
                pass

        # ── Step 3: 写入投研平台候选池快照 ────────────────────────────────
        db.execute(
            text("""
                DELETE FROM qm_research_candidate_snapshot
                WHERE run_id = :run_id
                  AND tenant_id = :tenant_id
                  AND user_id = :user_id
            """),
            {
                "run_id": run_id,
                "tenant_id": tenant_id,
                "user_id": user_id,
            },
        )

        # 计算分数排名（分数越高排名越靠前）
        scored_signals = sorted(
            [(sym, score) for sym, score in zip(symbols, scores, strict=True)],
            key=lambda x: x[1],
            reverse=True,
        )
        rank_map = {sym: idx + 1 for idx, (sym, score) in enumerate(scored_signals)}

        candidate_sql = text("""
            INSERT INTO qm_research_candidate_snapshot (
                tenant_id, user_id, run_id, model_id,
                data_trade_date, prediction_trade_date,
                symbol, fusion_score, score_rank,
                signal_side, expected_price, universe_tag,
                confidence_level, created_at, updated_at
            ) VALUES (
                :tenant_id, :user_id, :run_id, :model_id,
                :data_trade_date, :prediction_trade_date,
                :symbol, :fusion_score, :score_rank,
                :signal_side, :expected_price, :universe_tag,
                :confidence_level, NOW(), NOW()
            )
            ON CONFLICT (tenant_id, user_id, run_id, symbol)
            DO UPDATE SET
                model_id = EXCLUDED.model_id,
                data_trade_date = EXCLUDED.data_trade_date,
                prediction_trade_date = EXCLUDED.prediction_trade_date,
                fusion_score = EXCLUDED.fusion_score,
                score_rank = EXCLUDED.score_rank,
                signal_side = EXCLUDED.signal_side,
                expected_price = EXCLUDED.expected_price,
                universe_tag = EXCLUDED.universe_tag,
                confidence_level = EXCLUDED.confidence_level,
                updated_at = NOW()
        """)
        for idx, (sym, score) in enumerate(zip(symbols, scores, strict=True)):
            signal_side = signal_sides[idx]
            if signal_side == "BUY":
                confidence_level = "high"
            elif signal_side == "SELL":
                confidence_level = "watch"
            else:
                confidence_level = "medium"
            # 获取 expected_price（从之前 Redis 查询的结果）
            expected_price_val = None
            if quote_redis:
                try:
                    raw_sym = sym.replace("SH", "").replace("SZ", "").replace("BJ", "")
                    if sym.startswith("SH"):
                        redis_key = f"stock:{raw_sym}.SH"
                    elif sym.startswith("SZ"):
                        redis_key = f"stock:{raw_sym}.SZ"
                    elif sym.startswith("BJ") or sym.startswith("920"):
                        redis_key = f"stock:{raw_sym}.BJ"
                    else:
                        redis_key = f"stock:{sym}"
                    now_price = quote_redis.hget(redis_key, "Now")
                    if now_price:
                        expected_price_val = float(now_price)
                except Exception:
                    pass
            db.execute(
                candidate_sql,
                {
                    "tenant_id": tenant_id,
                    "user_id": user_id,
                    "run_id": run_id,
                    "model_id": model_name,
                    "data_trade_date": inference_date,  # 推理日期（数据截止日期）
                    "prediction_trade_date": prediction_trade_date,
                    "symbol": sym,
                    "fusion_score": score,
                    "score_rank": rank_map.get(sym, 999999),
                    "signal_side": signal_side,
                    "expected_price": expected_price_val,
                    "universe_tag": "默认候选池",
                    "confidence_level": confidence_level,
                },
            )
        logger.info(
            f"[InferenceScriptRunner] 写入 {len(signals)} 条投研候选池快照, run_id={run_id}"
        )

        db.commit()
        logger.info(
            f"[InferenceScriptRunner] 写入 {len(signals)} 条信号, run_id={run_id}"
        )
