"""插件配置：``config/factor_mining/plugins.yaml``（可缺省）+ env 热覆盖。

缺省即全部内置默认——配置文件不存在时 cost_rate 取
``factor_research.analysis.COST_RATE``（单一出处），评估器全开。
运维改阈值优先走 env（免重建镜像）：

  QM_MINING_PLUGINS_CONFIG  配置文件路径（默认 config/factor_mining/plugins.yaml）
  QM_MINING_COST_RATE       扣成本口径（研究用）
  QM_MINING_GATES_MODE      门禁全局升级：strict → 全部 hard；soft → 全部 soft
  QM_MINING_GATES_DISABLED  门禁逐条关闭（逗号分隔 key），优先于 yaml
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

import yaml

logger = logging.getLogger(__name__)

_ENV_CONFIG_PATH = "QM_MINING_PLUGINS_CONFIG"
_ENV_COST_RATE = "QM_MINING_COST_RATE"
_ENV_GATES_MODE = "QM_MINING_GATES_MODE"
_ENV_GATES_DISABLED = "QM_MINING_GATES_DISABLED"
DEFAULT_CONFIG_REL = Path("config") / "factor_mining" / "plugins.yaml"

_VALID_GATE_MODES = ("soft", "hard")


def _repo_root() -> Path:
    # backend/services/engine/mining_plugins/config.py → parents[4] = 仓库根
    return Path(__file__).resolve().parents[4]


def config_path() -> Path:
    env = os.environ.get(_ENV_CONFIG_PATH)
    return Path(env) if env else _repo_root() / DEFAULT_CONFIG_REL


def load_plugin_config() -> dict:
    """读取插件配置；文件缺失/损坏一律退回默认（不拦回测，只告警）。"""
    cfg: dict = {
        "cost_rate": None,
        "evaluators": {},
        "gates": {},
        "scoring": {},
        "cleanup": {},
    }
    try:
        path = config_path()
        if path.exists():
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            if isinstance(data, dict):
                if data.get("cost_rate") is not None:
                    cfg["cost_rate"] = float(data["cost_rate"])
                if isinstance(data.get("evaluators"), dict):
                    cfg["evaluators"] = data["evaluators"]
                if isinstance(data.get("gates"), dict):
                    cfg["gates"] = data["gates"]
                if isinstance(data.get("scoring"), dict):
                    cfg["scoring"] = data["scoring"]
                if isinstance(data.get("cleanup"), dict):
                    cfg["cleanup"] = data["cleanup"]
    except Exception as exc:  # noqa: BLE001 — 配置坏不拦回测
        logger.warning(
            "mining_plugins 配置读取失败(%s)，退回默认: %s", config_path(), exc
        )
    return cfg


def get_cost_rate() -> float:
    """扣成本口径（双边）：env > yaml > factor_research.analysis.COST_RATE。"""
    env = os.environ.get(_ENV_COST_RATE)
    if env:
        try:
            return float(env)
        except ValueError:
            logger.warning("QM_MINING_COST_RATE=%r 非法，忽略", env)
    cfg_rate = load_plugin_config().get("cost_rate")
    if cfg_rate is not None:
        return float(cfg_rate)
    from backend.services.engine.factor_research.analysis import COST_RATE

    return float(COST_RATE)


def get_enabled_names(all_names) -> set[str]:
    """按 ``evaluators: {name: false}`` 配置过筛；未列名 = 启用。"""
    ev = load_plugin_config().get("evaluators") or {}
    return {n for n in all_names if ev.get(n, True) is not False}


def get_gate_settings(name: str, descriptor=None) -> dict:
    """单门禁三级解析（阈值/模式/开关），不改门禁自身代码。

    mode：env ``QM_MINING_GATES_MODE``（strict→hard / soft→soft，全局覆盖）
    > yaml ``gates.<name>.mode`` > ``descriptor.default_mode``（默认 soft）。
    enabled：yaml ``enabled: false`` 或 env ``QM_MINING_GATES_DISABLED``
    含该 key → 关（env 优先，运维热关）。
    threshold：yaml 值（float）；缺失返回 None → 调用方回落插件默认阈值。
    """
    entry = (load_plugin_config().get("gates") or {}).get(name)
    if not isinstance(entry, dict):
        entry = {}
    mode = entry.get("mode")
    if mode not in _VALID_GATE_MODES:
        mode = getattr(descriptor, "default_mode", None)
    if mode not in _VALID_GATE_MODES:
        mode = "soft"
    env_mode = (os.environ.get(_ENV_GATES_MODE) or "").strip().lower()
    if env_mode == "strict":
        mode = "hard"
    elif env_mode == "soft":
        mode = "soft"

    enabled = entry.get("enabled", True) is not False
    disabled_env = {
        s.strip()
        for s in (os.environ.get(_ENV_GATES_DISABLED) or "").split(",")
        if s.strip()
    }
    if name in disabled_env:
        enabled = False

    threshold = entry.get("threshold")
    if threshold is not None:
        try:
            threshold = float(threshold)
        except (TypeError, ValueError):
            logger.warning("门禁 %s 阈值 %r 非法，回落默认", name, threshold)
            threshold = None
    return {"enabled": enabled, "mode": mode, "threshold": threshold}
