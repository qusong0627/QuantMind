"""LLM 弹性调度：主备、熔断、重试、限流。"""

from __future__ import annotations

import json
import os
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Any, Dict, List, Tuple
from collections.abc import Callable


def _load_ide_config_api_key() -> str:
    """从 AI-IDE 的 config.json 读取用户配置的 API Key"""
    data_dirs = [
        os.getenv("AI_IDE_DATA_DIR"),
        "/app/data",  # Docker 容器内
        os.path.join(os.path.dirname(__file__), "data"),
    ]
    for data_dir in data_dirs:
        if not data_dir:
            continue
        config_path = os.path.join(data_dir, "config.json")
        if os.path.exists(config_path):
            try:
                with open(config_path, encoding="utf-8") as f:
                    config = json.load(f)
                    if config.get("qwen_api_key"):
                        return config["qwen_api_key"]
            except Exception:
                pass
    return ""


class LLMRateLimitError(RuntimeError):
    """LLM 调用触发限流。"""


@dataclass
class _CircuitState:
    failures: int = 0
    opened_until: float = 0.0


class ResilientLLMRouter:
    """同步 LLM 代码生成调度器。"""

    def __init__(
        self,
        provider_factories: dict[str, Callable[[], Any]] | None = None,
        max_retries: int | None = None,
        retry_base_seconds: float | None = None,
        failure_threshold: int | None = None,
        circuit_open_seconds: float | None = None,
        rate_limit_rpm: int | None = None,
        max_concurrency: int | None = None,
    ) -> None:
        self._provider_factories = provider_factories or self._build_default_factories()
        self._providers: dict[str, Any] = {}
        self._providers_lock = threading.Lock()

        self.max_retries = max_retries if max_retries is not None else int(os.getenv("LLM_PROVIDER_MAX_RETRIES", "2"))
        self.retry_base_seconds = (
            retry_base_seconds if retry_base_seconds is not None else float(os.getenv("LLM_RETRY_BASE_SECONDS", "0.5"))
        )
        self.failure_threshold = (
            failure_threshold if failure_threshold is not None else int(os.getenv("LLM_CIRCUIT_FAILURE_THRESHOLD", "3"))
        )
        self.circuit_open_seconds = (
            circuit_open_seconds
            if circuit_open_seconds is not None
            else float(os.getenv("LLM_CIRCUIT_OPEN_SECONDS", "30"))
        )
        self.rate_limit_rpm = (
            rate_limit_rpm if rate_limit_rpm is not None else int(os.getenv("LLM_RATE_LIMIT_RPM", "120"))
        )
        self.max_concurrency = (
            max_concurrency if max_concurrency is not None else int(os.getenv("LLM_MAX_CONCURRENCY", "4"))
        )

        self._circuits: dict[str, _CircuitState] = {name: _CircuitState() for name in self._provider_factories.keys()}
        self._circuits_lock = threading.Lock()

        self._request_times = deque()
        self._rate_lock = threading.Lock()
        self._semaphore = threading.BoundedSemaphore(max(1, self.max_concurrency))

    @staticmethod
    def _build_default_factories() -> dict[str, Callable[[], Any]]:
        from ..llm.deepseek import DeepseekLLM
        from ..llm.qwen import QwenLLM

        return {
            "qwen": QwenLLM,
            "deepseek": DeepseekLLM,
        }

    def _provider_order(self, preferred: str | None = None) -> list[str]:
        preferred_name = (
            (preferred or os.getenv("LLM_PROVIDER_FORCE") or os.getenv("LLM_PROVIDER") or "qwen").strip().lower()
        )
        fallback_env = os.getenv("LLM_FALLBACK_PROVIDERS", "").strip()
        if fallback_env:
            fallbacks = [x.strip().lower() for x in fallback_env.split(",") if x.strip()]
        else:
            fallbacks = ["deepseek"] if preferred_name == "qwen" else ["qwen"]

        ordered = [preferred_name] + fallbacks
        uniq: list[str] = []
        for name in ordered:
            if name in self._provider_factories and name not in uniq:
                uniq.append(name)
        return uniq

    def _get_provider(self, name: str) -> Any:
        with self._providers_lock:
            if name not in self._providers:
                self._providers[name] = self._provider_factories[name]()
            return self._providers[name]

    def _acquire_rate_limit(self) -> None:
        if self.rate_limit_rpm <= 0:
            return
        now = time.time()
        with self._rate_lock:
            while self._request_times and now - self._request_times[0] > 60:
                self._request_times.popleft()
            if len(self._request_times) >= self.rate_limit_rpm:
                raise LLMRateLimitError(
                    f"LLM rate limit exceeded: {len(self._request_times)}/{self.rate_limit_rpm} per 60s"
                )
            self._request_times.append(now)

    def _circuit_open(self, name: str) -> bool:
        with self._circuits_lock:
            return self._circuits[name].opened_until > time.time()

    def _record_success(self, name: str) -> None:
        with self._circuits_lock:
            self._circuits[name] = _CircuitState()

    def _record_failure(self, name: str) -> None:
        with self._circuits_lock:
            state = self._circuits[name]
            state.failures += 1
            if state.failures >= self.failure_threshold:
                state.opened_until = time.time() + self.circuit_open_seconds

    def generate_code(
        self, prompt: str, preferred: str | None = None, mode: str = "simple", api_key: str | None = None
    ) -> tuple[str, dict[str, Any]]:
        self._acquire_rate_limit()
        providers = self._provider_order(preferred)
        if not providers:
            raise RuntimeError("No LLM providers available")

        # 优先使用传入的 api_key，其次从 config.json 读取
        effective_api_key = api_key or _load_ide_config_api_key()

        reasons: list[str] = []
        acquired = self._semaphore.acquire(timeout=30)
        if not acquired:
            raise LLMRateLimitError("LLM concurrency limit reached")
        try:
            for provider_name in providers:
                if self._circuit_open(provider_name):
                    reasons.append(f"{provider_name}: circuit_open")
                    continue

                try:
                    provider = self._get_provider(provider_name)
                except Exception as e:  # noqa: BLE001
                    self._record_failure(provider_name)
                    reasons.append(f"{provider_name}: init_failed={e}")
                    continue

                last_exc: Exception | None = None
                for attempt in range(max(1, self.max_retries)):
                    try:
                        code, meta = provider.generate_code(prompt, mode=mode, api_key=effective_api_key)
                        self._record_success(provider_name)
                        merged = dict(meta or {})
                        merged["provider"] = provider_name
                        merged["retry_attempt"] = attempt
                        return code, merged
                    except Exception as e:  # noqa: BLE001
                        last_exc = e
                        if attempt < self.max_retries - 1:
                            time.sleep(self.retry_base_seconds * (2**attempt))
                        continue

                self._record_failure(provider_name)
                reasons.append(f"{provider_name}: failed={last_exc}")

            raise RuntimeError(f"All providers failed: {'; '.join(reasons)}")
        finally:
            self._semaphore.release()


_router_instance: ResilientLLMRouter | None = None


def get_resilient_llm_router() -> ResilientLLMRouter:
    global _router_instance
    if _router_instance is None:
        _router_instance = ResilientLLMRouter()
    return _router_instance
