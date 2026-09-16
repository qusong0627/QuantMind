"""热集实时推理服务（T-P6-08）：常驻 15s 节拍 → ONNX 批量 → Signal 契约 source=realtime。

数据流（每周期）：
  热集（远端行情 Redis ``qm:hot_set:symbols``）
    → 快照拉取（``market:snapshot:*`` pipeline 批量）
    → 基线行（T-1 特征快照 parquet 按日缓存；**纯数字 symbol**）
    → live 覆盖（T-P6-07 增量引擎；仅白名单列——口径裁定证据见细案 T-P6-08）
    → 特征矩阵（缺列填 fill_values）→ ONNX session（导出链 onnx_exporter，缓存）
    → 分数 → `POST /engine/runs/{run_id}/signal-ready`（现成契约端点，同进程直调）
      run_id = ``rt-{model_id}-{YYYYMMDD}``（当日稳定；ON CONFLICT 幂等）

门控（Redis ``qm:realtime:infer:config``，**默认关**——P6 纪律：新链路默认关）：
  enabled / model_dir / cadence_s / override_whitelist(逗号分隔) / tenant_id / user_id
配置每周期热读——启用/停用无需重启。

纪律：单周期异常只计数不退出（引擎循环永续）；发布失败如实进 last_error；
override 白名单默认**空**（不覆盖任何快照列——量纲漂移零风险），开启需显式列名。
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from collections.abc import Callable

import numpy as np

from backend.shared.feature_incremental import TIER_COLUMNS, Window, compute_tier

logger = logging.getLogger(__name__)

CST = timezone(timedelta(hours=8))
CONFIG_KEY = "qm:realtime:infer:config"
SNAPSHOT_DIR = "/app/db/feature_snapshots"
DEFAULT_SNAPSHOT_PARQUET = f"{SNAPSHOT_DIR}/model_features_{datetime.now(tz=CST).year}.parquet"
OVERRIDE_GUARD_NOTE = (
    "override 白名单默认空：快照列与批量定义仅部分同口径（2026-09-17 实测裁定），"
    "开启覆盖须显式列名并过金样"
)


def _now() -> datetime:
    return datetime.now(tz=CST)


def _digits(symbol: str) -> str:
    """后缀式/前缀式 → 纯数字（快照 parquet 与 engine_signal_scores 口径）。"""
    s = str(symbol or "").strip().upper()
    for suf in (".SH", ".SZ", ".BJ"):
        if s.endswith(suf):
            return s[: -len(suf)]
    if s[:2] in ("SH", "SZ", "BJ") and s[2:].isdigit():
        return s[2:]
    return s


def snapshot_key(symbol: str) -> str | None:
    """任意形态 → ``market:snapshot:{prefix.lower()}``（与 collector 写入面同构）。"""
    s = str(symbol or "").strip().upper()
    if "." in s:
        code, _, mk = s.partition(".")
        if mk in ("SH", "SZ", "BJ") and code.isdigit():
            return f"market:snapshot:{mk.lower()}{code}"
        return None
    if s[:2] in ("SH", "SZ", "BJ") and s[2:].isdigit():
        return f"market:snapshot:{s[:2].lower()}{s[2:]}"
    if s.isdigit() and len(s) == 6:
        mk = "SH" if s[0] in "69" else ("BJ" if s[0] in "48" else "SZ")
        return f"market:snapshot:{mk.lower()}{s}"
    return None


class RealtimeInferConfig:
    __slots__ = ("enabled", "model_dir", "cadence_s", "override_whitelist", "tenant_id", "user_id")

    def __init__(
        self,
        *,
        enabled: bool = False,
        model_dir: str = "",
        cadence_s: float = 15.0,
        override_whitelist: tuple[str, ...] = (),
        tenant_id: str = "default",
        user_id: str = "admin",
    ) -> None:
        self.enabled = enabled
        self.model_dir = model_dir
        self.cadence_s = max(3.0, float(cadence_s))
        self.override_whitelist = tuple(override_whitelist)
        self.tenant_id = tenant_id
        self.user_id = user_id

    @classmethod
    def from_mapping(cls, raw: dict[str, Any] | None) -> RealtimeInferConfig:
        raw = raw or {}
        truthy = {"1", "true", "yes", "on"}
        whitelist = tuple(
            c.strip() for c in str(raw.get("override_whitelist") or "").split(",") if c.strip()
        )
        try:
            cadence = float(raw.get("cadence_s") or 15.0)
        except (TypeError, ValueError):
            cadence = 15.0
        return cls(
            enabled=str(raw.get("enabled") or "").strip().lower() in truthy,
            model_dir=str(raw.get("model_dir") or "").strip(),
            cadence_s=cadence,
            override_whitelist=whitelist,
            tenant_id=str(raw.get("tenant_id") or "default").strip() or "default",
            user_id=str(raw.get("user_id") or "admin").strip() or "admin",
        )


def _load_config_sync() -> RealtimeInferConfig:
    try:
        import os

        import redis as redis_lib

        client = redis_lib.Redis(
            host=os.getenv("REDIS_HOST") or "redis",
            port=int(os.getenv("REDIS_PORT", "6379")),
            db=int(os.getenv("REDIS_DB", "0")),
            password=os.getenv("REDIS_PASSWORD") or None,
            decode_responses=True, socket_connect_timeout=2, socket_timeout=2,
        )
        raw = client.hgetall(CONFIG_KEY) or {}
        client.close()
        return RealtimeInferConfig.from_mapping(raw)
    except Exception:  # noqa: BLE001 - 配置读取失败=未启用
        return RealtimeInferConfig()


class RealtimeInferenceService:
    """常驻热集推理（线程安全的 status；计算在线程池、发布在事件循环）。"""

    def __init__(
        self,
        *,
        config_loader: Callable[[], RealtimeInferConfig] | None = None,
        hot_set_fetcher: Callable[[], list[str]] | None = None,
        snapshot_fetcher: Callable[[list[str]], dict[str, dict[str, Any]]] | None = None,
        baseline_loader: Callable[[list[str], date], dict[str, dict[str, Any]]] | None = None,
        publisher: Callable[[dict[str, Any], RealtimeInferConfig], Any] | None = None,
    ) -> None:
        self._config_loader = config_loader or _load_config_sync
        self._hot_set_fetcher = hot_set_fetcher
        self._snapshot_fetcher = snapshot_fetcher
        self._baseline_loader = baseline_loader
        self._publisher = publisher
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self._lock = threading.Lock()
        self._engine = None  # IncrementalFeatureEngine（惰性）
        self._bootstrapped: set[str] = set()  # 已引导历史的标的（引擎键=热集原生码）
        self._sessions: dict[str, Any] = {}  # model_dir → ort session
        self._baselines: dict[str, dict[str, dict[str, Any]]] = {}  # "YYYYMMDD|model" → rows
        self._baseline_day: str | None = None
        self.counters: dict[str, Any] = {
            "cycles": 0, "published": 0, "scores": 0, "skipped": 0,
            "last_ms": None, "last_error": None, "last_run_id": None,
            "last_cycle_at": None, "last_scores": 0,
        }

    # ── 供测注入的默认实现 ────────────────────────────────────────────

    def _default_hot_set(self) -> list[str]:
        from backend.shared.remote_quote_config import make_sync_client

        client = make_sync_client()
        if client is None:
            return []
        try:
            return sorted(client.smembers("qm:hot_set:symbols") or [])
        finally:
            try:
                client.close()
            except Exception:  # noqa: BLE001
                pass

    def _default_snapshots(self, symbols: list[str]) -> dict[str, dict[str, Any]]:
        from backend.shared.remote_quote_config import make_sync_client

        client = make_sync_client()
        if client is None:
            return {}
        out: dict[str, dict[str, Any]] = {}
        try:
            for start in range(0, len(symbols), 200):
                chunk = symbols[start: start + 200]
                keys = [(sym, snapshot_key(sym)) for sym in chunk]
                keys = [(sym, key) for sym, key in keys if key]
                pipe = client.pipeline(transaction=False)
                for _sym, key in keys:
                    pipe.hgetall(key)
                for (sym, _key), data in zip(keys, pipe.execute(), strict=False):
                    if data:
                        out[sym] = data
            return out
        finally:
            try:
                client.close()
            except Exception:  # noqa: BLE001
                pass

    def _default_baseline(self, symbols: list[str], day: date) -> dict[str, Any]:
        """T-1 特征行 + 价格历史（同一次 parquet 读取）；parquet 为纯数字 symbol。

        返回 {"rows": {digits: {feature: value}}, "history": {digits: DataFrame(尾 45 行)}}——
        history 供增量引擎引导（快车道任何特征都需要价格窗口，仅特征行不够）。
        """
        import pandas as pd

        meta = _read_metadata(Path(self._current_model_dir()))
        cols = list(meta.get("feature_columns") or [])
        path = Path(DEFAULT_SNAPSHOT_PARQUET)
        if not cols or not path.is_file():
            return {"rows": {}, "history": {}}
        raw = ["symbol", "trade_date", "open", "high", "low", "close", "volume", "amount"]
        df = pd.read_parquet(path, columns=raw + [c for c in cols if c not in raw])
        df = df[df["symbol"].isin([_digits(s) for s in symbols])]
        df["trade_date"] = pd.to_datetime(df["trade_date"])
        past = df[df["trade_date"].dt.date < day]
        if past.empty:
            return {"rows": {}, "history": {}}
        latest = past["trade_date"].max()
        rows: dict[str, dict[str, Any]] = {}
        history: dict[str, Any] = {}
        for sym, g in past.groupby("symbol"):
            g = g.sort_values("trade_date")
            history[str(sym)] = g[raw].tail(45).reset_index(drop=True)
            last = g[g["trade_date"] == latest]
            if not last.empty:
                row = last.iloc[-1]
                rows[str(sym)] = {c: row.get(c) for c in cols}
        return {"rows": rows, "history": history}

    async def _default_publish(self, payload: dict[str, Any], cfg: RealtimeInferConfig) -> None:
        from backend.services.engine.routers.realtime_contract import (
            FeatureReadyRequest,
            SignalReadyRequest,
            SignalScoreItem,
            mark_feature_ready,
            mark_signal_ready,
        )

        await mark_feature_ready(
            payload["run_id"],
            FeatureReadyRequest(
                tenant_id=cfg.tenant_id,
                user_id=cfg.user_id,
                trade_date=payload["trade_date"],
                model_name=payload["model_id"],
                model_version=payload["model_version"],
                feature_version=payload["feature_version"],
                feature_dim=payload["feature_dim"],
                expected_symbols=payload["expected_symbols"],
                ready_symbols=payload["ready_symbols"],
                missing_symbols=payload["missing_symbols"],
                source="realtime",
                quality=payload.get("quality") or {},
            ),
        )
        await mark_signal_ready(
            payload["run_id"],
            SignalReadyRequest(
                tenant_id=cfg.tenant_id,
                user_id=cfg.user_id,
                trade_date=payload["trade_date"],
                model_version=payload["model_version"],
                feature_version=payload["feature_version"],
                scores=[
                    SignalScoreItem(
                        symbol=item["symbol"],
                        fusion_score=item["fusion_score"],
                        score_rank=item["score_rank"],
                        universe_tag="hot_set",
                        market="CN",
                        quality=item.get("quality") or {},
                    )
                    for item in payload["scores"]
                ],
            ),
        )

    def _current_model_dir(self) -> str:
        with self._lock:
            return getattr(self, "_model_dir", "")

    # ── 周期计算（同步；由 to_thread 调用）────────────────────────────

    def build_cycle(self) -> dict[str, Any] | None:
        """单周期：热集 → 快照 → 基线+覆盖 → 矩阵 → 分数 → 载荷（不发布）。"""
        cfg = self._config_loader()
        if not cfg.enabled or not cfg.model_dir:
            return None
        with self._lock:
            self._model_dir = cfg.model_dir
        model_dir = Path(cfg.model_dir)
        meta = _read_metadata(model_dir)
        cols = list(meta.get("feature_columns") or [])
        fill = meta.get("fill_values") or {}
        if not cols:
            raise RuntimeError(f"metadata.feature_columns 为空: {model_dir}")

        hot = (self._hot_set_fetcher or self._default_hot_set)()
        if not hot:
            raise RuntimeError("热集为空（远端 Redis 不可读或未构建）")
        snapshots = (self._snapshot_fetcher or self._default_snapshots)(hot)
        today = _now().date()
        bundle = (self._baseline_loader or self._default_baseline)(hot, today)
        baseline = bundle.get("rows") or {}
        histories = bundle.get("history") or {}

        session = self._ensure_session(model_dir, len(cols))
        input_name = session.get_inputs()[0].name

        if self._engine is None:
            from backend.services.engine.inference.incremental_features import (
                IncrementalFeatureEngine,
            )

            self._engine = IncrementalFeatureEngine()

        override = set(cfg.override_whitelist) & set(TIER_COLUMNS) & set(cols)
        x = np.empty((len(hot), len(cols)), dtype=np.float32)
        ready = missing = overridden = 0
        symbols_digits: list[str] = []
        for i, sym in enumerate(hot):
            digits = _digits(sym)
            symbols_digits.append(digits)
            row = dict(baseline.get(digits) or {})
            if sym not in self._bootstrapped:
                hist = histories.get(digits)
                if hist is not None and len(hist):
                    try:
                        self._engine.bootstrap(sym, hist)
                    except Exception as exc:  # noqa: BLE001 - 单标的引导失败不拖垮周期
                        logger.debug("bootstrap 失败 %s: %s", sym, exc)
                self._bootstrapped.add(sym)
            snap = snapshots.get(sym)
            if snap:
                self._engine.on_snapshot(sym, snap)
            live = None
            if override:
                try:
                    live = self._engine.compute(sym)
                except Exception as exc:  # noqa: BLE001 - 单标的失败不拖垮周期
                    logger.debug("live 特征失败 %s: %s", sym, exc)
            for j, col in enumerate(cols):
                val = row.get(col)
                if live is not None and col in override:
                    lv = live.get(col)
                    if lv is not None and np.isfinite(lv):
                        val = lv
                        overridden += 1
                if val is None or (isinstance(val, float) and np.isnan(val)):
                    val = fill.get(col, 0.0)
                    missing += 1
                x[i, j] = float(val)
            if row:
                ready += 1
        out = session.run(None, {input_name: x})[0]
        scores = np.asarray(out, dtype=float).reshape(-1)
        order = np.argsort(-scores)
        ranks = np.empty(len(scores), dtype=int)
        ranks[order] = np.arange(1, len(scores) + 1)
        items = [
            {
                "symbol": symbols_digits[i],
                "fusion_score": float(scores[i]),
                "score_rank": int(ranks[i]),
                "quality": {"live": bool(override), "overridden_cols": len(override)},
            }
            for i in range(len(hot))
        ]
        model_id = model_dir.name
        return {
            "run_id": f"rt-{model_id}-{today.strftime('%Y%m%d')}",
            "trade_date": today,
            "model_id": model_id,
            "model_version": str(meta.get("model_version") or model_id),
            "feature_version": str(meta.get("factor_catalog_version") or meta.get("feature_version") or "default"),
            "feature_dim": len(cols),
            "expected_symbols": len(hot),
            "ready_symbols": ready,
            "missing_symbols": missing,
            "overridden_cells": overridden,
            "quality": {"override_whitelist": sorted(override), "note": OVERRIDE_GUARD_NOTE},
            "scores": items,
        }

    def _ensure_session(self, model_dir: Path, n_features: int) -> Any:
        key = str(model_dir)
        with self._lock:
            cached = self._sessions.get(key)
        if cached is not None:
            return cached
        import onnxruntime as ort

        onnx_path = model_dir / "model.onnx"
        if not onnx_path.is_file():
            from backend.services.engine.inference.onnx_exporter import export_model_to_onnx

            report = export_model_to_onnx(model_dir, output_path=onnx_path)
            if not report.get("ok"):
                raise RuntimeError(f"ONNX 导出失败: {report.get('reason')}")
        session = ort.InferenceSession(str(onnx_path), providers=["CPUExecutionProvider"])
        got = int(session.get_inputs()[0].shape[1] or 0)
        if got not in (0, n_features):
            raise RuntimeError(f"ONNX 输入维度 {got} ≠ feature_columns {n_features}")
        with self._lock:
            self._sessions[key] = session
        return session

    # ── 循环 ────────────────────────────────────────────────────────

    async def run_forever(self) -> None:
        logger.info("[realtime-infer] service loop started")
        while not self._stop.is_set():
            cfg = self._config_loader()
            try:
                if cfg.enabled and cfg.model_dir:
                    t0 = time.monotonic()
                    payload = await asyncio.to_thread(self.build_cycle)
                    if payload:
                        publisher = self._publisher or self._default_publish
                        result = publisher(payload, cfg)
                        if asyncio.iscoroutine(result):
                            await result
                        with self._lock:
                            self.counters["published"] += 1
                            self.counters["scores"] += len(payload["scores"])
                            self.counters["last_scores"] = len(payload["scores"])
                            self.counters["last_run_id"] = payload["run_id"]
                            self.counters["last_error"] = None
                    with self._lock:
                        self.counters["cycles"] += 1
                        self.counters["last_ms"] = round((time.monotonic() - t0) * 1000, 1)
                        self.counters["last_cycle_at"] = _now().isoformat()
            except Exception as exc:  # noqa: BLE001 - 循环永续
                with self._lock:
                    self.counters["skipped"] += 1
                    self.counters["last_error"] = f"{type(exc).__name__}: {exc}"
                logger.warning("[realtime-infer] 周期失败: %s", exc)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=cfg.cadence_s)
            except asyncio.TimeoutError:
                pass

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._stop = asyncio.Event()
            self._task = asyncio.get_running_loop().create_task(
                self.run_forever(), name="realtime-inference"
            )

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=3)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                self._task.cancel()

    def status(self) -> dict[str, Any]:
        cfg = self._config_loader()
        with self._lock:
            out = {
                "enabled": cfg.enabled,
                "model_dir": cfg.model_dir,
                "cadence_s": cfg.cadence_s,
                "override_whitelist": list(cfg.override_whitelist),
                "loop_alive": bool(self._task and not self._task.done()),
                "cached_models": list(self._sessions.keys()),
                **dict(self.counters),
            }
        return out


def _read_metadata(model_dir: Path) -> dict[str, Any]:
    meta_path = model_dir / "metadata.json"
    if not meta_path.is_file():
        raise RuntimeError(f"metadata.json 不存在: {model_dir}")
    return json.loads(meta_path.read_text(encoding="utf-8"))


_default_service: RealtimeInferenceService | None = None


def default_service() -> RealtimeInferenceService:
    global _default_service
    if _default_service is None:
        _default_service = RealtimeInferenceService()
    return _default_service
