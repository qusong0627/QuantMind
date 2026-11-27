#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""启动质检：QMT 缓存数据 + 关键参数 + 运行时健康。

由 `启动交易台.cmd` 在 TDX/QMT 服务启动后调用；也可手工：

    C:\\Program Files\\Python312\\python.exe cache_quality_check.py --wait 15

退出码：0=OK，2=WARN，1=FAIL（一键启动继续，但状态会标红）。
只读 Redis/配置/Parquet，不下单、不重启任何服务。
"""

from __future__ import annotations

import argparse
import json
import socket
import sys
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

CN_TZ = timezone(timedelta(hours=8))
REQUIRED_COLUMNS = {
    "bar1m": {
        "date", "symbol", "minute_ts", "open", "high", "low", "close",
        "volume", "amount", "vwap", "cum_volume", "cum_amount", "source", "final",
    },
    "snapshot3s": {
        "date", "ts_ms", "symbol", "last", "open", "high", "low", "pre_close",
        "volume", "amount", "bid1", "ask1", "bid_vol1", "ask_vol1", "source",
        "quality",
    },
    "tick_hot": {
        "date", "ts_ms", "symbol", "last", "volume", "amount", "bid1", "ask1",
        "source",
    },
}
SESSIONS = ((9, 15, 11, 35), (12, 55, 15, 5))


class Issue:
    __slots__ = ("level", "code", "message")

    def __init__(self, level: str, code: str, message: str):
        self.level = level
        self.code = code
        self.message = message

    def __repr__(self) -> str:
        return "%s %s: %s" % (self.level, self.code, self.message)


def _nested(cfg, path: str, default=None):
    node = cfg
    for part in path.split("."):
        if not isinstance(node, dict) or part not in node:
            return default
        node = node[part]
    return node


def _bool(value, default=False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    return str(value).strip().lower() not in {"", "0", "false", "no", "off"}


def _num(value, default=0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def in_session(now: datetime | None = None) -> bool:
    dt = now or datetime.now(CN_TZ)
    if dt.weekday() >= 5:
        return False
    hm = (dt.hour, dt.minute)
    return any((sh, sm) <= hm < (eh, em) for sh, sm, eh, em in SESSIONS)


def evaluate_config(cfg: dict, *, pyarrow_version: str | None) -> list[Issue]:
    issues: list[Issue] = []
    account = str(cfg.get("account_id") or "").strip()
    if not account:
        issues.append(Issue("FAIL", "account", "bridge_config.account_id 为空"))
    if not str(_nested(cfg, "redis.host", "")).strip():
        issues.append(Issue("FAIL", "redis", "bridge_config.redis.host 为空"))

    interval = _num(_nested(cfg, "rpc.trade_min_interval_s", 3.0), 3.0)
    if interval < 1.0:
        issues.append(Issue("WARN", "rpc.interval", "trade_min_interval_s=%.1f < 1.0" % interval))
    burst = _num(_nested(cfg, "rpc.trade_burst", 3), 3)
    if burst < 1 or burst > 10:
        issues.append(Issue("WARN", "rpc.burst", "trade_burst=%.0f 超出 1-10" % burst))
    read_timeout = _num(_nested(cfg, "rpc.trade_read_timeout_s", 8.0), 8.0)
    if read_timeout > 15.0:
        issues.append(Issue("WARN", "rpc.read_timeout", "trade_read_timeout_s=%.1f > 15" % read_timeout))
    market_backlog = _num(_nested(cfg, "budget.market_preempt_queue", 2), 2)
    if market_backlog > 3:
        issues.append(Issue("WARN", "budget.market", "market_preempt_queue=%.0f > 3" % market_backlog))
    order_backlog = _num(_nested(cfg, "budget.order_preempt_queue", 3), 3)
    if order_backlog < 1:
        issues.append(Issue("WARN", "budget.order", "order_preempt_queue < 1"))

    if not _bool(_nested(cfg, "quotes.enabled", True), True):
        issues.append(Issue("WARN", "quotes", "quotes.enabled=false（热集行情轮询关闭）"))
    if not _bool(_nested(cfg, "sweep.enabled", True), True):
        issues.append(Issue("WARN", "sweep", "sweep.enabled=false（全市场兜底关闭）"))
    if not _bool(_nested(cfg, "today_cache.enabled", True), True):
        issues.append(Issue("WARN", "cache.enabled", "today_cache.enabled=false（一键启动期待缓存开启）"))
    if not _bool(_nested(cfg, "today_cache.snapshot3s_enabled", True), True):
        issues.append(Issue("WARN", "cache.snapshot3s", "snapshot3s_enabled=false"))
    if _bool(_nested(cfg, "source_http.allow_orders", False), False):
        issues.append(
            Issue("WARN", "source_http.orders", "source_http.allow_orders=true（HTTP 下单后门，确认是否必要）")
        )
    if not _bool(_nested(cfg, "watchdog.prefer_bridge_restart", True), True):
        issues.append(Issue("WARN", "watchdog", "prefer_bridge_restart=false（风暴时无法自动解锁）"))

    max_amount = _num(_nested(cfg, "risk.max_order_amount", 0), 0)
    if max_amount <= 0:
        issues.append(Issue("WARN", "risk.amount", "risk.max_order_amount<=0（金额风控关闭）"))
    max_per_min = _num(_nested(cfg, "risk.max_orders_per_min", 0), 0)
    if max_per_min <= 0:
        issues.append(Issue("WARN", "risk.freq", "risk.max_orders_per_min<=0（频率风控关闭）"))
    if pyarrow_version is None:
        issues.append(Issue("WARN", "pyarrow", "pyarrow 不可用，缓存降级为 jsonl.gz"))
    return issues


def evaluate_runtime(metrics: dict, *, session: bool) -> list[Issue]:
    issues: list[Issue] = []
    state = str(metrics.get("strategy_state") or "").strip().lower()
    if state != "ok":
        level = "FAIL" if session else "WARN"
        issues.append(Issue(level, "strategy", "strategy_state=%s" % (state or "unknown")))
    pos_age = _num(metrics.get("positions_age_s", -1), -1)
    asset_age = _num(metrics.get("asset_age_s", -1), -1)
    if session:
        if pos_age < 0 or pos_age > 60:
            issues.append(Issue("FAIL", "pos_age", "positions_age_s=%s" % pos_age))
        if asset_age < 0 or asset_age > 180:
            issues.append(Issue("FAIL", "asset_age", "asset_age_s=%s" % asset_age))
    elif pos_age > 0 and pos_age > 1800:
        issues.append(Issue("WARN", "pos_age", "positions_age_s=%.0f（盘外）" % pos_age))
    queue = _num(metrics.get("queue_len", 0), 0)
    if queue > 5:
        issues.append(Issue("WARN", "queue", "queue_len=%.0f > 5" % queue))
    return issues


def evaluate_cache(
    metrics: dict,
    *, session: bool, source_ages: list[float] | None = None,
) -> list[Issue]:
    issues: list[Issue] = []
    enabled = _num(metrics.get("cache_enabled", 0), 0)
    degraded = _num(metrics.get("cache_degraded", 0), 0)
    if enabled != 1:
        issues.append(Issue("FAIL", "cache.enabled", "cache_enabled=0"))
        return issues
    if degraded:
        issues.append(Issue("WARN", "cache.degraded", "cache_degraded=1（非 Parquet）"))
    errors = _num(metrics.get("cache_errors", 0), 0)
    dropped = _num(metrics.get("cache_dropped", 0), 0)
    queue = _num(metrics.get("cache_queue", 0), 0)
    if errors:
        issues.append(Issue("WARN", "cache.errors", "cache_errors=%.0f" % errors))
    if dropped:
        issues.append(Issue("WARN", "cache.dropped", "cache_dropped=%.0f" % dropped))
    if queue > 50:
        issues.append(Issue("WARN", "cache.queue", "cache_queue=%.0f > 50" % queue))
    if not session:
        return issues

    latest = _num(metrics.get("cache_latest_symbols", 0), 0)
    if latest < 1000:
        issues.append(Issue("FAIL", "cache.latest", "cache_latest_symbols=%.0f < 1000" % latest))
    ingest_age = _num(metrics.get("cache_last_ingest_age_s", -1), -1)
    if ingest_age < 0 or ingest_age > 15:
        issues.append(Issue("FAIL", "cache.ingest_age", "cache_last_ingest_age_s=%s" % ingest_age))
    hot = _num(metrics.get("cache_hot_symbols", 0), 0)
    if hot < 1:
        issues.append(Issue("WARN", "cache.hot", "cache_hot_symbols=0"))
    snapshot_rounds = _num(metrics.get("cache_snapshot3s_rounds", 0), 0)
    if snapshot_rounds < 1:
        issues.append(Issue("WARN", "cache.snapshot3s", "cache_snapshot3s_rounds=0"))
    if source_ages is not None:
        if not source_ages:
            issues.append(Issue("FAIL", "cache.source_keys", "盘中无 market:snapshot:qmt:* 来源键"))
        elif min(source_ages) > 10.0:
            issues.append(
                Issue("FAIL", "cache.source_fresh", "来源键最年轻 %.1fs > 10s" % min(source_ages))
            )
    return issues


def evaluate_parquet(parts: dict, *, session: bool) -> list[Issue]:
    issues: list[Issue] = []
    for kind, info in (parts or {}).items():
        if not info:
            if session:
                issues.append(Issue("WARN", "parquet." + kind, "今日尚无 %s part（首 part 约 5 分钟）" % kind))
            continue
        if info.get("rows", 0) <= 0:
            issues.append(Issue("FAIL", "parquet." + kind, "%s 最新 part 行数为 0" % kind))
        missing = set(REQUIRED_COLUMNS.get(kind, ())) - set(info.get("columns") or [])
        if missing:
            issues.append(
                Issue("FAIL", "parquet." + kind, "%s 缺列: %s" % (kind, ",".join(sorted(missing))))
            )
        if info.get("bad_rows"):
            issues.append(
                Issue("WARN", "parquet." + kind, "%s 样例异常行 %s" % (kind, info["bad_rows"]))
            )
    return issues


def evaluate_divergence(
    pairs: list[dict],
    *,
    now: float | None = None,
    max_age_s: float = 10.0,
    threshold_pct: float = 1.0,
) -> tuple[list[Issue], dict]:
    """TDX/QMT 同标的价差质检（只统计两边都新鲜的样本）。"""
    now = time.time() if now is None else float(now)
    fresh = []
    for pair in pairs or []:
        qmt_last, tdx_last = pair.get("qmt_last"), pair.get("tdx_last")
        qmt_ts, tdx_ts = pair.get("qmt_ts"), pair.get("tdx_ts")
        try:
            qmt_last = float(qmt_last)
            tdx_last = float(tdx_last)
            qmt_ts = float(qmt_ts)
            tdx_ts = float(tdx_ts)
        except (TypeError, ValueError):
            continue
        if qmt_last <= 0 or tdx_last <= 0:
            continue
        if now - qmt_ts > max_age_s or now - tdx_ts > max_age_s:
            continue
        base = max(abs(qmt_last), abs(tdx_last), 1e-9)
        fresh.append(abs(qmt_last - tdx_last) / base * 100.0)
    summary = {
        "pairs": len(fresh),
        "max_pct": round(max(fresh), 3) if fresh else None,
        "median_pct": round(sorted(fresh)[len(fresh) // 2], 3) if fresh else None,
        "over_threshold": sum(1 for diff in fresh if diff > threshold_pct),
    }
    issues: list[Issue] = []
    if not fresh:
        issues.append(Issue("WARN", "divergence.none", "盘中无 TDX/QMT 同源可比样本"))
    elif summary["over_threshold"]:
        issues.append(
            Issue(
                "WARN",
                "divergence",
                "%d/%d 只 TDX/QMT 价差 > %.1f%%（max=%.2f%%）"
                % (
                    summary["over_threshold"],
                    summary["pairs"],
                    threshold_pct,
                    summary["max_pct"],
                ),
            )
        )
    return issues, summary


def _suffix_to_prefix(symbol: str) -> str:
    text = str(symbol or "").strip().upper()
    if "." not in text:
        return text.lower()
    code, exch = text.split(".", 1)
    return (exch[:2].lower() + code)


def _price_from_hash(row: dict) -> float | None:
    for key in ("last", "Now", "price"):
        try:
            value = float(row.get(key))
        except (TypeError, ValueError):
            continue
        if value > 0:
            return value
    return None


def _ts_from_hash(row: dict) -> float | None:
    for key in ("ts", "timestamp"):
        try:
            return float(row.get(key))
        except (TypeError, ValueError):
            continue
    return None


def collect_divergence(redis_cfg: dict, limit: int = 20) -> list[dict]:
    """采样 QMT 来源键，与同标的 TDX 来源键/canonical 比较最新价。"""
    import redis

    host = redis_cfg.get("host", "127.0.0.1")
    port = int(redis_cfg.get("port", 6379))
    password = redis_cfg.get("password") or None
    qmt_db = int(redis_cfg.get("db", 5))
    tdx_db = int(redis_cfg.get("tdx_db", 3))
    qmt = redis.Redis(host=host, port=port, db=qmt_db, password=password,
                      decode_responses=True, socket_timeout=3)
    tdx = redis.Redis(host=host, port=port, db=tdx_db, password=password,
                      decode_responses=True, socket_timeout=3)
    pairs: list[dict] = []
    try:
        cursor = 0
        keys: list[str] = []
        while len(keys) < limit:
            cursor, batch = qmt.scan(cursor=cursor, match="market:snapshot:qmt:*", count=200)
            keys.extend(batch or [])
            if cursor == 0:
                break
        for key in keys[:limit]:
            symbol = key.split("market:snapshot:qmt:", 1)[-1]
            qmt_row = qmt.hgetall(key)
            if not qmt_row:
                continue
            prefix = _suffix_to_prefix(symbol)
            tdx_row = tdx.hgetall("market:snapshot:tdx:%s" % prefix)
            if not tdx_row:
                tdx_row = tdx.hgetall("market:snapshot:%s" % prefix)
            if not tdx_row:
                continue
            pairs.append(
                {
                    "symbol": symbol,
                    "qmt_last": _price_from_hash(qmt_row),
                    "tdx_last": _price_from_hash(tdx_row),
                    "qmt_ts": _ts_from_hash(qmt_row),
                    "tdx_ts": _ts_from_hash(tdx_row),
                }
            )
    finally:
        for client in (qmt, tdx):
            try:
                client.close()
            except Exception:  # noqa: BLE001
                pass
    return pairs


def _check_http(url: str) -> dict | None:
    try:
        with urllib.request.urlopen(url, timeout=3) as resp:
            return json.loads(resp.read().decode("utf-8", "replace"))
    except Exception:  # noqa: BLE001
        return None


def _port_open(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=2):
            return True
    except OSError:
        return False


def _metrics(redis_cfg: dict) -> dict:
    import redis

    client = redis.Redis(
        host=redis_cfg.get("host", "127.0.0.1"),
        port=int(redis_cfg.get("port", 6379)),
        db=int(redis_cfg.get("db", 5)),
        password=redis_cfg.get("password") or None,
        decode_responses=True,
        socket_timeout=3,
    )
    try:
        return dict(client.hgetall("qm:qmt:pipe:metrics") or {})
    finally:
        try:
            client.close()
        except Exception:  # noqa: BLE001
            pass


def _source_ages(redis_cfg: dict, limit: int = 20) -> list[float]:
    import redis

    client = redis.Redis(
        host=redis_cfg.get("host", "127.0.0.1"),
        port=int(redis_cfg.get("port", 6379)),
        db=int(redis_cfg.get("db", 5)),
        password=redis_cfg.get("password") or None,
        decode_responses=True,
        socket_timeout=3,
    )
    now = time.time()
    ages: list[float] = []
    try:
        cursor = 0
        seen = 0
        while seen < limit:
            cursor, keys = client.scan(cursor=cursor, match="market:snapshot:qmt:*", count=200)
            for key in keys:
                ts = client.hget(key, "ts")
                try:
                    ages.append(max(0.0, now - float(ts)))
                except (TypeError, ValueError):
                    pass
                seen += 1
                if seen >= limit:
                    break
            if cursor == 0:
                break
    finally:
        try:
            client.close()
        except Exception:  # noqa: BLE001
            pass
    return ages


def _parquet_parts(bridge_dir: Path, cache_dir: Path | None) -> dict:
    if cache_dir is None:
        cache_dir = bridge_dir.parent / "data" / "cache" / "qmt"
    date = datetime.now(CN_TZ).strftime("%Y%m%d")
    out = {}
    try:
        import pyarrow.parquet as pq
    except Exception:  # noqa: BLE001
        return {kind: None for kind in REQUIRED_COLUMNS}
    for kind in REQUIRED_COLUMNS:
        base = cache_dir / ("date=%s" % date) / ("type=%s" % kind)
        if not base.is_dir():
            out[kind] = None
            continue
        files = sorted(base.glob("hour=*/part-*.parquet"), key=lambda p: p.stat().st_mtime)
        if not files:
            out[kind] = None
            continue
        path = files[-1]
        try:
            pf = pq.ParquetFile(str(path))
            columns = list(pf.schema_arrow.names)
            rows = int(pf.metadata.num_rows)
            sample = pf.read_row_group(0).slice(0, 50).to_pylist() if pf.num_row_groups else []
            bad = 0
            for row in sample:
                last = row.get("last", row.get("close"))
                try:
                    if last is not None and float(last) <= 0:
                        bad += 1
                except (TypeError, ValueError):
                    bad += 1
            out[kind] = {
                "path": str(path),
                "rows": rows,
                "columns": columns,
                "bad_rows": bad,
            }
        except Exception as exc:  # noqa: BLE001
            out[kind] = {"path": str(path), "rows": 0, "columns": [], "bad_rows": 0,
                         "error": str(exc)}
    return out


def _ensure_venv_path() -> None:
    """桥的 redis/pyarrow 在 C:\\xmt\\venv\\Lib\\site-packages（Program Files 解释器）。"""
    venv = r"C:\xmt\venv\Lib\site-packages"
    if venv not in sys.path:
        sys.path.insert(0, venv)


def _pyarrow_version() -> str | None:
    _ensure_venv_path()
    try:
        import pyarrow

        return str(pyarrow.__version__)
    except Exception:  # noqa: BLE001
        return None


def _print(issues: list[Issue], metrics: dict, cfg: dict, parts: dict,
           pyarrow_version: str | None, divergence: dict | None = None) -> str:
    fails = [i for i in issues if i.level == "FAIL"]
    warns = [i for i in issues if i.level == "WARN"]
    status = "FAIL" if fails else ("WARN" if warns else "OK")
    print("[DIAG] status=%s fail=%d warn=%d" % (status, len(fails), len(warns)))
    print(
        "[DIAG] account=%s redis=%s:%s/%s"
        % (
            cfg.get("account_id") or "-",
            _nested(cfg, "redis.host", "-"),
            _nested(cfg, "redis.port", "-"),
            _nested(cfg, "redis.db", "-"),
        )
    )
    print(
        "[DIAG] rpc interval=%.1fs burst=%.0f read_timeout=%.1fs"
        % (
            _num(_nested(cfg, "rpc.trade_min_interval_s", 3), 3),
            _num(_nested(cfg, "rpc.trade_burst", 3), 3),
            _num(_nested(cfg, "rpc.trade_read_timeout_s", 8), 8),
        )
    )
    print(
        "[DIAG] risk max_order=%.0f max_per_min=%.0f deviation=%.1f%% apply_sells=%s"
        % (
            _num(_nested(cfg, "risk.max_order_amount", 0), 0),
            _num(_nested(cfg, "risk.max_orders_per_min", 0), 0),
            _num(_nested(cfg, "risk.max_deviation_pct", 0), 0),
            _bool(_nested(cfg, "risk.apply_to_sells", False), False),
        )
    )
    print(
        "[DIAG] flags quotes=%s tap=%s sweep=%s cache=%s snap3s=%s allow_orders=%s"
        % (
            int(_bool(_nested(cfg, "quotes.enabled", True), True)),
            int(_bool(_nested(cfg, "tap.enabled", False), False)),
            int(_bool(_nested(cfg, "sweep.enabled", True), True)),
            int(_bool(_nested(cfg, "today_cache.enabled", True), True)),
            int(_bool(_nested(cfg, "today_cache.snapshot3s_enabled", True), True)),
            int(_bool(_nested(cfg, "source_http.allow_orders", False), False)),
        )
    )
    print("[DIAG] pyarrow=%s" % (pyarrow_version or "MISSING"))
    if divergence is not None:
        print(
            "[DIAG] divergence pairs=%s median=%s%% max=%s%% over1=%s"
            % (
                divergence.get("pairs"),
                divergence.get("median_pct"),
                divergence.get("max_pct"),
                divergence.get("over_threshold"),
            )
        )
    print(
        "[DIAG] runtime strategy=%s pos_age=%ss asset_age=%ss queue=%s errors=%s"
        % (
            metrics.get("strategy_state", "-"),
            metrics.get("positions_age_s", "-"),
            metrics.get("asset_age_s", "-"),
            metrics.get("queue_len", "-"),
            metrics.get("errors", "-"),
        )
    )
    print(
        "[DIAG] cache enabled=%s degraded=%s latest=%s last_ingest=%ss hot=%s "
        "hot_stale=%s snap3s_rounds=%s parts=%s queue=%s errors=%s"
        % (
            metrics.get("cache_enabled", "-"),
            metrics.get("cache_degraded", "-"),
            metrics.get("cache_latest_symbols", "-"),
            metrics.get("cache_last_ingest_age_s", "-"),
            metrics.get("cache_hot_symbols", "-"),
            metrics.get("cache_hot_stale_symbols", "-"),
            metrics.get("cache_snapshot3s_rounds", "-"),
            metrics.get("cache_parts", "-"),
            metrics.get("cache_queue", "-"),
            metrics.get("cache_errors", "-"),
        )
    )
    for kind, info in (parts or {}).items():
        if info:
            print(
                "[DIAG] parquet %s rows=%s cols=%s bad=%s"
                % (kind, info.get("rows"), len(info.get("columns") or []), info.get("bad_rows"))
            )
    for issue in issues:
        print("[%s] %s: %s" % (issue.level, issue.code, issue.message))
    return status


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="QMT startup quality check")
    parser.add_argument("--wait", type=float, default=15.0)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--skip-parquet", action="store_true")
    args = parser.parse_args(argv)
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass

    base = Path(__file__).resolve().parent
    bridge_dir = base / "qmt-external-bridge"
    _ensure_venv_path()
    cfg_path = bridge_dir / "bridge_config.json"
    try:
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        print("[DIAG] status=FAIL fail=1 warn=0")
        print("[FAIL] config: 读取 %s 失败: %s" % (cfg_path, exc))
        return 1

    session = in_session()
    metrics: dict = {}
    try:
        metrics = _metrics(cfg.get("redis") or {})
    except Exception as exc:  # noqa: BLE001
        print("[DIAG] status=FAIL fail=1 warn=0")
        print("[FAIL] redis: 读取指标失败: %s" % exc)
        return 1

    if session and args.wait > 0:
        deadline = time.time() + max(0.0, args.wait)
        while time.time() < deadline:
            if _num(metrics.get("cache_latest_symbols", 0), 0) >= 1000:
                break
            time.sleep(2)
            try:
                metrics = _metrics(cfg.get("redis") or {})
            except Exception:  # noqa: BLE001
                pass

    source_ages = None
    if session:
        try:
            source_ages = _source_ages(cfg.get("redis") or {})
        except Exception:  # noqa: BLE001
            source_ages = []

    divergence_pairs: list[dict] = []
    div_issues: list[Issue] = []
    div_summary = None
    if session:
        try:
            divergence_pairs = collect_divergence(cfg.get("redis") or {})
        except Exception:  # noqa: BLE001
            divergence_pairs = []
        div_issues, div_summary = evaluate_divergence(divergence_pairs)

    parts = {}
    if not args.skip_parquet:
        cache_dir_cfg = _nested(cfg, "today_cache.dir", "../data/cache/qmt")
        cache_dir = Path(cache_dir_cfg)
        if not cache_dir.is_absolute():
            cache_dir = (bridge_dir / cache_dir).resolve()
        parts = _parquet_parts(bridge_dir, cache_dir)

    pyarrow_version = _pyarrow_version()
    issues = []
    issues += evaluate_config(cfg, pyarrow_version=pyarrow_version)
    issues += evaluate_runtime(metrics, session=session)
    issues += evaluate_cache(metrics, session=session, source_ages=source_ages)
    issues += evaluate_parquet(parts, session=session)
    issues += div_issues

    status = _print(issues, metrics, cfg, parts, pyarrow_version, div_summary)
    if args.json:
        print(json.dumps(
            {
                "status": status,
                "session": session,
                "pyarrow": pyarrow_version,
                "issues": [{"level": i.level, "code": i.code, "message": i.message} for i in issues],
                "metrics": metrics,
                "parts": parts,
            },
            ensure_ascii=False,
            default=str,
        ))
    if status == "FAIL":
        return 1
    if status == "WARN":
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
