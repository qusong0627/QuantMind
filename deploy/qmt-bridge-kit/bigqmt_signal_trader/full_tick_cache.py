"""Demand-driven Redis cache for Big QMT full tick snapshots."""

import hashlib
import json
import pickle
import time

from .code_utils import EXCHANGE_TOKENS, normalize_stock_code


# Whole-exchange tokens, futures included: a token that normalize_stock_code
# would reject must not reach it (issue #95).
MARKET_CODES = set(EXCHANGE_TOKENS)
DEMAND_KEY_TEMPLATE = "bigqmt:full_tick:demand:{account_id}"
CACHE_KEY_TEMPLATE = "bigqmt:full_tick:cache:{account_id}:{request_id}"


def _decode_text(value):
    if isinstance(value, bytes):
        return value.decode("utf-8")
    return str(value)


def _loads_json(value):
    return json.loads(_decode_text(value))


def normalize_full_tick_codes(codes):
    normalized = []
    seen = set()
    for code in codes or []:
        text = str(code or "").strip()
        if not text:
            continue
        if text.upper() in MARKET_CODES:
            item = text.upper()
        else:
            # Pass the raw code through: futures symbol case is exchange
            # convention and normalize_stock_code preserves it (issue #95).
            # Uppercasing here would undo that before it ever gets the chance.
            item = normalize_stock_code(text)
        if item not in seen:
            seen.add(item)
            normalized.append(item)
    return sorted(normalized)


def full_tick_request_id(codes):
    normalized = normalize_full_tick_codes(codes)
    digest = hashlib.sha1("|".join(normalized).encode("utf-8")).hexdigest()
    return digest[:20]


def full_tick_demand_key(account_id):
    return DEMAND_KEY_TEMPLATE.format(account_id=str(account_id or ""))


def full_tick_cache_key(account_id, codes=None, request_id=None):
    rid = str(request_id or full_tick_request_id(codes or []))
    return CACHE_KEY_TEMPLATE.format(account_id=str(account_id or ""), request_id=rid)


def _dump_snapshot(payload):
    return pickle.dumps(payload, protocol=4)


def _load_snapshot(raw):
    if not raw:
        return None
    try:
        return pickle.loads(raw)
    except Exception:
        try:
            return _loads_json(raw)
        except Exception:
            return None


def request_full_tick_cache(redis_client, account_id, codes, demand_ttl_seconds=10, cache_ttl_seconds=10):
    normalized = normalize_full_tick_codes(codes)
    if not normalized:
        raise ValueError("full tick codes are required")
    now = time.time()
    request_id = full_tick_request_id(normalized)
    payload = {
        "request_id": request_id,
        "codes": normalized,
        "requested_at_ts": now,
        "expires_at_ts": now + float(demand_ttl_seconds),
        "cache_ttl_seconds": float(cache_ttl_seconds),
    }
    key = full_tick_demand_key(account_id)
    redis_client.hset(key, request_id, json.dumps(payload, ensure_ascii=False, sort_keys=True))
    try:
        redis_client.expire(key, max(30, int(float(demand_ttl_seconds) * 3)))
    except Exception:
        pass
    return payload


def write_full_tick_cache(redis_client, account_id, codes, data, cache_ttl_seconds=10):
    normalized = normalize_full_tick_codes(codes)
    request_id = full_tick_request_id(normalized)
    now = time.time()
    payload = {
        "request_id": request_id,
        "codes": normalized,
        "updated_at_ts": now,
        "updated_at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now)),
        "data": data or {},
    }
    key = full_tick_cache_key(account_id, request_id=request_id)
    ttl = int(max(1, float(cache_ttl_seconds)))
    redis_client.setex(key, ttl, _dump_snapshot(payload))
    return payload


def read_full_tick_cache(redis_client, account_id, codes, max_age_seconds=10):
    normalized = normalize_full_tick_codes(codes)
    key = full_tick_cache_key(account_id, codes=normalized)
    snapshot = _load_snapshot(redis_client.get(key))
    if not isinstance(snapshot, dict):
        return None
    if normalize_full_tick_codes(snapshot.get("codes") or []) != normalized:
        return None
    updated_at = float(snapshot.get("updated_at_ts") or 0)
    if updated_at <= 0:
        return None
    if time.time() - updated_at > float(max_age_seconds):
        return None
    data = snapshot.get("data")
    return data if isinstance(data, dict) else None


def wait_full_tick_cache(redis_client, account_id, codes, max_age_seconds=10, wait_seconds=3.5, poll_interval_seconds=0.2):
    deadline = time.time() + max(0.0, float(wait_seconds))
    while True:
        data = read_full_tick_cache(redis_client, account_id, codes, max_age_seconds=max_age_seconds)
        if data is not None:
            return data
        if time.time() >= deadline:
            return None
        time.sleep(max(0.05, float(poll_interval_seconds)))


def iter_active_full_tick_demands(redis_client, account_id, demand_ttl_seconds=10, max_requests=8):
    key = full_tick_demand_key(account_id)
    raw_mapping = redis_client.hgetall(key) or {}
    now = time.time()
    active = []
    for field, raw_payload in list(raw_mapping.items()):
        field_text = _decode_text(field)
        try:
            payload = _loads_json(raw_payload)
        except Exception:
            try:
                redis_client.hdel(key, field_text)
            except Exception:
                pass
            continue
        expires_at = float(payload.get("expires_at_ts") or 0)
        if expires_at <= now:
            try:
                redis_client.hdel(key, field_text)
            except Exception:
                pass
            continue
        codes = normalize_full_tick_codes(payload.get("codes") or [])
        if not codes:
            try:
                redis_client.hdel(key, field_text)
            except Exception:
                pass
            continue
        payload["codes"] = codes
        payload["cache_ttl_seconds"] = float(payload.get("cache_ttl_seconds") or demand_ttl_seconds)
        active.append(payload)
    active.sort(key=lambda item: float(item.get("requested_at_ts") or 0), reverse=True)
    return active[: int(max_requests)]


def _demand_is_market(codes):
    return any(str(code).strip().upper() in MARKET_CODES for code in codes or [])


def refresh_full_tick_cache(
    redis_client,
    context_info,
    account_id,
    demand_ttl_seconds=10,
    cache_ttl_seconds=10,
    max_requests=8,
    kind=None,
    max_wall_seconds=None,
):
    """Refresh cached snapshots for active demands.

    ``kind`` selects which demands to refresh: ``None`` (all), ``"symbol"``
    (only symbol-list demands), or ``"market"`` (only whole-market demands such
    as SH/SZ/BJ/HK). ``max_wall_seconds`` caps how long one refresh round may run
    on the caller (strategy) thread; the in-flight demand always completes and at
    least one demand is always refreshed before the budget can cut the round.
    """
    started_at = time.time()
    refreshed = 0
    for demand in iter_active_full_tick_demands(
        redis_client,
        account_id,
        demand_ttl_seconds=demand_ttl_seconds,
        max_requests=max_requests,
    ):
        codes = demand.get("codes") or []
        is_market = _demand_is_market(codes)
        if kind == "symbol" and is_market:
            continue
        if kind == "market" and not is_market:
            continue
        if max_wall_seconds and refreshed and (time.time() - started_at) > float(max_wall_seconds):
            break
        tick_data = context_info.get_full_tick(codes) or {}
        ttl = demand.get("cache_ttl_seconds") or cache_ttl_seconds
        write_full_tick_cache(redis_client, account_id, codes, tick_data, cache_ttl_seconds=ttl)
        refreshed += 1
    return refreshed
