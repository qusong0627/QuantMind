"""Bridge session authentication manager."""
from __future__ import annotations

import logging
import importlib.util
import sys
import threading
import time
from pathlib import Path

import requests

try:
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

    AgentConfig = _load_local_module("config").AgentConfig  # type: ignore[attr-defined]

logger = logging.getLogger("qmt_agent")

class AuthManager:
    def __init__(self, cfg: AgentConfig):
        self.cfg = cfg
        self._lock = threading.RLock()
        self.token = ""
        self.expires_at = 0.0

    def bootstrap(self, force_rebind: bool = False) -> None:
        access_key = str(self.cfg.access_key or "").strip()
        secret_key = str(self.cfg.secret_key or "").strip()
        account_id = str(self.cfg.account_id or "").strip()
        client_fingerprint = str(self.cfg.client_fingerprint or "").strip()
        client_version = str(self.cfg.client_version or "").strip()
        hostname = str(self.cfg.hostname or "").strip()

        def _do_request(rebind: bool) -> "requests.Response":
            return requests.post(
                f"{self.cfg.api_base_url.rstrip('/')}/internal/strategy/bridge/session",
                json={
                    "access_key": access_key,
                    "secret_key": secret_key,
                    "agent_type": "qmt",
                    "account_id": account_id,
                    "client_fingerprint": client_fingerprint,
                    "client_version": client_version,
                    "hostname": hostname,
                    "force_rebind": rebind,
                },
                timeout=15,
            )

        resp = _do_request(force_rebind)
        if resp.status_code == 409 and not force_rebind:
            logger.warning("bridge session 409 conflict, retrying with force_rebind=True")
            resp = _do_request(True)
        resp.raise_for_status()
        data = resp.json()
        ws_url = str(data.get("ws_url") or "").strip()
        if ws_url:
            # 服务端返回的 ws_url 优先，避免本地配置与服务端路由漂移导致 bridge_agent_offline。
            self.cfg.server_url = ws_url
        with self._lock:
            self.token = data["bridge_session_token"]
            self.expires_at = time.time() + int(data["expires_in"])
        logger.info("bridge session created, expires_in=%s", data["expires_in"])

    def refresh_if_needed(self) -> bool:
        with self._lock:
            current_token = self.token
            should_refresh = bool(
                current_token
                and time.time() >= self.expires_at - self.cfg.renew_before_seconds
            )
        if not should_refresh:
            return False

        resp = requests.post(
            f"{self.cfg.api_base_url.rstrip('/')}/internal/strategy/bridge/session/refresh",
            headers={"Authorization": f"Bearer {current_token}"},
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        ws_url = str(data.get("ws_url") or "").strip()
        if ws_url:
            self.cfg.server_url = ws_url
        with self._lock:
            self.token = data["bridge_session_token"]
            self.expires_at = time.time() + int(data["expires_in"])
        logger.info("bridge session refreshed, expires_in=%s", data["expires_in"])
        return True

    def authorization_header(self) -> dict[str, str]:
        with self._lock:
            token = self.token
        return {"Authorization": f"Bearer {token}"}

