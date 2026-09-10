"""Small, dependency-free helpers for Big-QMT quote shape differences."""


OPTION_CODE_SUFFIXES = (".SHO", ".SZO")


def is_option_code(code):
    return str(code or "").strip().upper().endswith(OPTION_CODE_SUFFIXES)


def latest_quote_row(value):
    """Return the newest tick from any ContextInfo market-data shape.

    Big-QMT versions expose the same data as a pandas DataFrame, a list of row
    dicts, a dict of column arrays, or this bridge's serialisable DataFrame
    marker. This module deliberately has no pandas/numpy dependency because it
    also runs inside QMT's embedded Python.
    """
    if value is None:
        return None
    if isinstance(value, dict) and value.get("__bigqmt_type__") == "DataFrame":
        records = value.get("records") or []
        if isinstance(records, (list, tuple)):
            return dict(records[-1]) if records and isinstance(records[-1], dict) else None
        # get_market_data_ex_ori differs across full-QMT builds: some return a
        # list of row dicts, others return a dict of column arrays. The bridge's
        # raw-frame marker preserves either shape in ``records``.
        return latest_quote_row(records)
    if hasattr(value, "iloc") and hasattr(value, "columns"):
        try:
            if len(value.index) <= 0:
                return None
            row = value.iloc[-1]
            return dict(row.to_dict()) if hasattr(row, "to_dict") else dict(row)
        except Exception:
            return None
    if isinstance(value, (list, tuple)):
        if not value:
            return None
        latest = value[-1]
        return dict(latest) if isinstance(latest, dict) else None
    if not isinstance(value, dict):
        return None

    # ContextInfo.subscribe_quote(result_type="list") yields column arrays. A
    # scalar time/price means this is already one tick dict; in that case the
    # bidPrice/askPrice arrays are the five-level ladder and must stay intact.
    timeline = None
    for key in ("time", "stime", "lastPrice"):
        candidate = value.get(key)
        if candidate is not None:
            timeline = candidate
            break
    if isinstance(timeline, (str, bytes)) or not hasattr(timeline, "__len__"):
        return dict(value)
    try:
        if len(timeline) <= 0:
            return None
    except Exception:
        return dict(value)

    row = {}
    for key, column in value.items():
        try:
            row[key] = column.iloc[-1] if hasattr(column, "iloc") else column[-1]
        except Exception:
            row[key] = column
    return row


def find_code_payload(payload, code):
    if not isinstance(payload, dict):
        return None
    if code in payload:
        return payload[code]
    wanted = str(code).upper()
    for key, value in payload.items():
        if str(key).upper() == wanted:
            return value
    return None


def latest_quote_batch(data):
    if not isinstance(data, dict):
        return data
    batch = {}
    for code, value in data.items():
        row = latest_quote_row(value)
        if row is not None:
            batch[code] = row
    return batch
