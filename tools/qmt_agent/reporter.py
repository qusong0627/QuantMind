"""HTTP reporter that forwards data to the quantmind bridge backend."""
from __future__ import annotations

import logging
import importlib.util
import sys
from pathlib import Path
from typing import Any

import requests

try:
    from .auth import AuthManager
    from .config import AgentConfig
except ImportError:
    _MODULE_DIR = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))

    def _load_local_module(module_name: str):
        qualified_name = f"qmt_agent_local_{module_name}"
        module = sys.modules.get(qualified_name)
        if module is not None:
            return module
        module_path = _MODULE_DIR / f"{module_name}.py"
        spec = importlib.util.spec_from_file_location(qualified_name, module_path)
        if spec is None or spec.loader is None:
            raise ImportError(f"Cannot load local module {module_name} from {module_path}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[qualified_name] = module
        spec.loader.exec_module(module)
        return module

    AuthManager = _load_local_module("auth").AuthManager  # type: ignore[attr-defined]
    AgentConfig = _load_local_module("config").AgentConfig  # type: ignore[attr-defined]

logger = logging.getLogger("qmt_agent")

class BridgeReporter:
    def __init__(self, cfg: AgentConfig, auth: AuthManager):
        self.cfg = cfg
        self.auth = auth

    def _post_json(self, endpoint: str, payload: dict[str, Any]) -> None:
        url = f"{self.cfg.api_base_url.rstrip('/')}/{endpoint.lstrip('/')}"
        # 资产上报日志由于内容较多，改为 debug 级别，其余保留 info
        if "account" in endpoint:
            logger.debug("bridge report -> %s payload=%s", endpoint, payload)
        else:
            logger.info("bridge report -> %s payload=%s", endpoint, payload)
            
        try:
            # 将超时时间从 15s 缩短至 5s，防止单次网络挂起导致整个 Agent 步调变慢
            resp = requests.post(
                url,
                json=payload,
                headers=self.auth.authorization_header(),
                timeout=5,
            )
            if resp.status_code >= 400:
                logger.warning(
                    "bridge report <- %s status=%s body=%s",
                    endpoint,
                    resp.status_code,
                    resp.text[:1000],
                )
            resp.raise_for_status()
            
            if "account" not in endpoint:
                logger.info("bridge report <- %s status=%s", endpoint, resp.status_code)
        except requests.exceptions.RequestException as exc:
            # 捕获网络异常，防止其向上抛出拖死调用线程
            logger.error("bridge report network error for %s: %s", endpoint, exc)
            # 对于关键上报，我们可以选择重新抛出或静默，但在 Agent 模式下，通常由外部循环控制重试
            raise

    def report_account(self, payload: dict[str, Any]) -> None:
        self._post_json("internal/strategy/bridge/account", payload)

    def report_heartbeat(self, payload: dict[str, Any]) -> None:
        self._post_json("internal/strategy/bridge/heartbeat", payload)

    def report_execution(self, payload: dict[str, Any]) -> None:
        self._post_json("internal/strategy/bridge/execution", payload)

