# coding: utf-8
"""Per-request account_type resolution from BIGQMT_ACCOUNT_TYPE_MAP.

By default, BigQmtOrderGateway and BigQmtPositionProvider use a fixed
account_type set at init time (``self.account_type = "STOCK"``). In a
single-account deployment this is correct. In a multi-account deployment
(one QMT process serving STOCK + FUTURE), the gateway is shared and
self.account_type must match the *request's* account_id, not the gateway's
default.

BIGQMT_ACCOUNT_TYPE_MAP is a dict {account_id: account_type} in the local
config. When it is present, account_type_for() returns the mapped type;
otherwise it falls back to the gateway's own default (the *default_type*
argument), preserving full backward compatibility.

Usage in gateway methods::

    # Before (fixed account_type):
    rows = query(account_id, self.account_type, "ORDER", strategy_name)

    # After (per-request resolution):
    rows = query(account_id, self._resolve_account_type(account_id), "ORDER", strategy_name)
"""

import importlib


_ACCOUNT_TYPE_MAP = None  # None = not loaded yet; {} = loaded but empty


def _load_map():
    """Load BIGQMT_ACCOUNT_TYPE_MAP from local config, or {} if absent.

    Uses importlib.import_module (not reload) to avoid:
    1. Re-executing a config module that holds credentials.
    2. QMT sandbox monkeypatched importlib (issue noted in PR #135 review):
       the sandbox wraps importlib.reload with a custom loader that may fail
       on C++ callback threads with SystemError.
    Hot config changes should go through reload_deployment() → gateway init,
    which reconstructs the entire object graph.
    """
    global _ACCOUNT_TYPE_MAP
    try:
        cfg = importlib.import_module("bigqmt_signal_trader_local_config")
        _ACCOUNT_TYPE_MAP = dict(getattr(cfg, "BIGQMT_ACCOUNT_TYPE_MAP", {}) or {})
    except Exception:
        _ACCOUNT_TYPE_MAP = {}
    return _ACCOUNT_TYPE_MAP


def get_account_type_map():
    """The loaded map, loading it first if needed."""
    if _ACCOUNT_TYPE_MAP is None:
        _load_map()
    return _ACCOUNT_TYPE_MAP


def account_type_for(account_id, default_type="STOCK"):
    """account_type string for *account_id*, falling back to *default_type*.

    This is the core lookup: if BIGQMT_ACCOUNT_TYPE_MAP maps the account_id
    to a type, return it; otherwise return default_type unchanged.
    """
    if not account_id:
        return default_type
    mapping = get_account_type_map()
    return mapping.get(str(account_id), default_type)


def reload():
    """Force-reload the map (e.g. after config change)."""
    global _ACCOUNT_TYPE_MAP
    _ACCOUNT_TYPE_MAP = None
    return get_account_type_map()
