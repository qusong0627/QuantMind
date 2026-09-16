"""TdxAiData IPC 协议与纯函数口径（worker 与 client 共用）。

- 传输：unix socket + JSONL；请求 ``{"id","method","params"}``；
  响应 ``{"id","ok","result","error","meta"}``，error 形如
  ``{"code","message","raw_code","retry_after_s"}``。
- 口径纯函数：周期映射、count→区间换算（原生 count 模式该 token 不可用）、
  K 线归一（NaN 残缺行剔除——限流/缺数时接口会返回残缺行）。
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta
from typing import Any

KNOWN_METHODS = {
    "ping",
    "status",
    "get_quote",
    "get_klines",
    "get_klines_batch",
    "get_minute_data",
    "get_tick_data",
    "subscription_status",
    "hot_set_sync",
}

_PERIOD_MAP = {
    "daily": "1d",
    "weekly": "1w",
    "monthly": "1mon",
    "1m": "1m",
    "5m": "5m",
    "10m": "10m",
    "15m": "15m",
    "30m": "30m",
    "1h": "1h",
    "45d": "45d",
    "1q": "1q",
    "1y": "1y",
}
_BAR_MINUTES = {"1m": 1, "5m": 5, "10m": 10, "15m": 15, "30m": 30, "1h": 60}
_DAY_MARGIN = {"1d": 2, "1w": 15, "1mon": 45, "45d": 90, "1q": 130, "1y": 520}


class ProtocolError(RuntimeError):
    """IPC 报文不符合契约。"""


def period_of(interval: str) -> str:
    """interval → tqs 周期串（未知原样透传，由 SDK 报错码 5）。"""
    return _PERIOD_MAP.get(str(interval or "").strip(), str(interval or "").strip())


def start_for_count(period: str, count: int, end: str) -> str:
    """count → 区间模式 start_time（原生 count 参数该 token 不可用）。

    分钟周期按 bar 时长×2 裕量回推；日历周期按根数×日裕量回推（含周末/节假日）。
    """
    minutes = _BAR_MINUTES.get(period)
    try:
        end_dt = datetime.strptime(end, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        end_dt = datetime.now()
    if minutes:
        start_dt = end_dt - timedelta(minutes=minutes * int(count) * 2 + 30)
    else:
        start_dt = end_dt - timedelta(days=_DAY_MARGIN.get(period, 2) * int(count))
    return start_dt.strftime("%Y-%m-%d %H:%M:%S")


def now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# ── K 线归一 ────────────────────────────────────────────────────────


def _dates(data: dict) -> list[str]:
    """日期序列：优先显式 ``_dates``（测试/兜底），否则取任一字段的索引。"""
    explicit = data.get("_dates")
    if isinstance(explicit, list):
        return [str(d) for d in explicit]
    sample = None
    for key, value in data.items():
        if key.startswith("_"):
            continue
        if hasattr(value, "index"):
            sample = value
            break
    if sample is None:
        return []
    return [
        str(d.date()) if hasattr(d, "date") else str(d) for d in list(sample.index)
    ]


def _val(data: dict, field: str, symbol: str, i: int) -> Any:
    """从 ``{field: DataFrame|{symbol: [...]}|list}`` 取第 i 个值（逐形态试取）。"""
    f = data.get(field)
    if f is None:
        return None
    # pandas DataFrame：f[symbol] → Series → .iloc[i]
    try:
        return f[symbol].iloc[i]
    except (KeyError, TypeError, IndexError, AttributeError):
        pass
    # 单列 DataFrame（无 symbol 列名）
    try:
        iloc = getattr(f, "iloc", None)
        if iloc is not None:
            return iloc[i]
    except (TypeError, IndexError):
        pass
    # dict：{symbol: [...]}
    if isinstance(f, dict):
        try:
            return f.get(symbol, [])[i]
        except IndexError:
            return None
    # list / 其他可下标
    try:
        return f[i]
    except (TypeError, IndexError, KeyError):
        return None


def bars_from_market_data(data: dict, symbol: str) -> list[dict[str, Any]]:
    """tqs.get_market_data 结果 → 统一 bar 列表；close 无效（NaN/None/≤0）行剔除。"""
    if not isinstance(data, dict):
        return []
    bars: list[dict[str, Any]] = []
    dates = _dates(data)
    for i in range(len(dates)):
        close = _val(data, "Close", symbol, i)
        try:
            c = float(close)
            valid = math.isfinite(c) and c > 0
        except (TypeError, ValueError):
            valid = False
        if not valid:
            continue
        bars.append(
            {
                "date": dates[i],
                "open": _val(data, "Open", symbol, i),
                "high": _val(data, "High", symbol, i),
                "low": _val(data, "Low", symbol, i),
                "close": c,
                "volume": _val(data, "Volume", symbol, i),
                "amount": _val(data, "Amount", symbol, i),
            }
        )
    return bars


# ── 订阅推送解析（2026-09-16 真机样本定形）─────────────────────────
#
# 推送形如：{"Error":"","ErrorId":0,
#   "ResultSets":[{"ColDes":["code","decimal","price",...,"bid5","bid_vol5"],
#                  "Content":[[...一格式对齐 ColDes...], ...]}]}
# 39 列全景（A 股）：price/pre_close/open/high/low/refresh_time/volume/limit_up/
# limit_down/cage_up/cage_down/after_hours_flag/tomorrow_limit_up(tomorrow_limit_down)/
# sdunit_status/seal_amount/ask1..5/ask_vol1..5/bid1..5/bid_vol1..5。

_PUSH_NUMERIC = frozenset(
    {
        "decimal", "price", "pre_close", "open", "high", "low", "volume",
        "bond_match_price", "limit_up", "limit_down", "cage_up", "cage_down",
        "tomorrow_limit_up", "tomorrow_limit_down", "seal_amount",
    }
) | {f"ask{i}" for i in range(1, 6)} | {f"ask_vol{i}" for i in range(1, 6)} | {
    f"bid{i}" for i in range(1, 6)
} | {f"bid_vol{i}" for i in range(1, 6)}


def _to_number(value: Any) -> float | None:
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def parse_push_payload(raw: Any) -> list[dict[str, Any]]:
    """订阅推送载荷 → 归一记录列表（每行一只标的）。

    - ``ErrorId != 0`` → ProtocolError（携带服务端 Error 文本）；
    - 数值列按 _PUSH_NUMERIC 强转（失败给 None，绝不假值）；其余列原样字符串；
    - ``refresh_time`` 形如 "153053" → 同时给 ``refresh_hms="15:30:53"``；
    - 缺 code 的行跳过。
    """
    import json

    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode("utf-8", errors="replace")
    if isinstance(raw, str):
        try:
            payload = json.loads(raw)
        except (TypeError, ValueError) as exc:
            raise ProtocolError(f"推送非 JSON: {exc}") from exc
    elif isinstance(raw, dict):
        payload = raw
    else:
        raise ProtocolError(f"推送类型不支持: {type(raw).__name__}")

    error_id = int(payload.get("ErrorId") or 0)
    if error_id != 0:
        raise ProtocolError(f"推送错误 ErrorId={error_id}: {payload.get('Error')}")

    records: list[dict[str, Any]] = []
    for result_set in payload.get("ResultSets") or []:
        cols = [str(c) for c in (result_set.get("ColDes") or [])]
        for row in result_set.get("Content") or []:
            item: dict[str, Any] = {}
            for idx, col in enumerate(cols):
                value = row[idx] if idx < len(row) else None
                if col in _PUSH_NUMERIC:
                    item[col] = _to_number(value)
                else:
                    item[col] = None if value is None else str(value)
            symbol = str(item.get("code") or item.get("Code") or "").strip()
            if not symbol:
                continue
            item["symbol"] = symbol
            rt = item.get("refresh_time")
            if isinstance(rt, str) and len(rt) == 6 and rt.isdigit():
                item["refresh_hms"] = f"{rt[:2]}:{rt[2:4]}:{rt[4:]}"
            records.append(item)
    return records


# ── 错误映射 ────────────────────────────────────────────────────────


def map_sdk_error(message: object) -> tuple[str, str]:
    """SDK 异常/打印文本 → 统一错误 code。"""
    text = str(message or "")
    low = text.lower()
    if "insufficient" in low or "code=13" in low or "错误码 13" in text:
        return "rate_limited", f"Token Insufficient（配额不足，冷却窗口后重试）: {text[:80]}"
    if "不是对象" in text or "invalid parameter" in low or "code=6" in low or "错误码 6" in text:
        return "invalid_params", f"参数错误: {text[:120]}"
    if "不可用" in text or "not loaded" in low or "动态库" in text:
        return "sdk_unavailable", f"SDK 不可用: {text[:120]}"
    return "call_failed", text[:200] or "TdxAiData 调用失败"


# ── 报文校验 ────────────────────────────────────────────────────────


def encode_request(req_id: int, method: str, params: dict | None = None) -> bytes:
    if method not in KNOWN_METHODS:
        raise ProtocolError(f"未知方法: {method}")
    import json

    payload = {"id": int(req_id), "method": method, "params": params or {}}
    return (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")


def parse_request(raw: bytes | str) -> dict:
    import json

    try:
        req = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise ProtocolError(f"请求非 JSON: {exc}") from exc
    if not isinstance(req, dict) or "id" not in req or "method" not in req:
        raise ProtocolError("请求缺 id/method")
    if req["method"] not in KNOWN_METHODS:
        raise ProtocolError(f"未知方法: {req['method']}")
    if not isinstance(req.get("params") or {}, dict):
        raise ProtocolError("params 必须为对象")
    return req


def parse_response(raw: bytes | str) -> dict:
    import json

    try:
        resp = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise ProtocolError(f"响应非 JSON: {exc}") from exc
    if not isinstance(resp, dict) or "id" not in resp or "ok" not in resp:
        raise ProtocolError("响应缺 id/ok")
    return resp
