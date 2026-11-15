# ── 移植注（2026-09-30，arena 原生迁移 A3）─────────────────────────────
# 逐字节副本：quant-Trader `backend/services/lab_batch.py`（上游已退役）。
# 改写点一处：BATCH_DIR 默认落点 `parents[2]/data/lab_batch` 在 QM 包结构下
# 会指到 `backend/data/`（错误），改 QM 数据根 `<root>/lab_batch`（A1 已落）；
# env `LAB_BATCH_DIR` 覆盖保留。**落点惰性化**——上游常量还被直接当函数默认
# 参数，def 时绑定后 monkeypatch 都换不动；改 `batch_dir()` 函数 + 调用时
# 解析（out_dir=None 语义与上游「不传就用默认」一致）。跑批脚本
# （lab_batch_backtest.py，B3 迁）与 API 进程按同一落点读写，无需搬运。
# `from . import market_lab` 相对导入原样可用。
# ─────────────────────────────────────────────────────────────────────
"""批量回测：股票池 × 策略 → 逐标的明细 + 策略聚合排名，结果落盘。

与 market_lab 的分工：market_lab 管单标的回测与看盘数据（API 实时调用），
这里管「跑一大批 + 存结果」——由 scripts/lab_batch_backtest.py 驱动（多进程），
API 只读结果文件（data/lab_batch/*.json 已挂进容器 /app/data）。

为什么不做成 API 内的后台任务：单次批量是分钟级 CPU 密集作业，塞进 API 进程
会和实盘接口抢 CPU。跑批走 CLI，界面只读产物。

口径：A股多头单向（现金账户不能做空），后复权（不复权会被除权跳空污染），
费率/滑点/仓位与单标的回测一致（见 lab_strategies/*.py 的 @script.strategy）。
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

from . import market_lab as ml
from backend.services.agent_arena import arena_config

# ---------- 股票池 ----------

INDEX_POOLS: dict[str, tuple[str, str]] = {
    "sz50": ("000016.SH", "上证50"),
    "hs300": ("000300.SH", "沪深300"),
    "zz500": ("000905.SH", "中证500"),
    "zz800": ("000906.SH", "中证800"),
    "zz1000": ("000852.SH", "中证1000"),
    "kc50": ("000688.SH", "科创50"),
    "cyb": ("399006.SZ", "创业板指"),
}

def batch_dir() -> Path:
    """批次产物目录（惰性：env LAB_BATCH_DIR 覆盖，默认 QM 数据根/lab_batch）。

    上游是模块级常量且直接当函数默认参数——默认参数在 def 时求值，常量一旦
    固化（import 时）就再也换不动（测试夹具进程内切 QM_ARENA_DATA_ROOT 会
    打到真实落点）。这里落点一律函数、每次调用重解析，外部行为无差。
    """
    return Path(os.environ.get("LAB_BATCH_DIR")
                or arena_config.data_root() / "lab_batch")


# 跑批脚本与 API 进程同写同读 QM 数据根下的 lab_batch/（一个容器一个根），无需搬运。


# ---------- 提交 / 进度（2026-11-15：界面发起批量回测） ----------
# 跑批必须用宿主 pynecore（容器 Python 3.10 装不了），所以 API 只写队列文件，
# 宿主 worker（scripts/lab_batch_worker.py）消费并跑 CLI；状态落 status/<run_id>.json。
# 这条链路与 Pine 转写 worker 同构：容器不执行回测代码，只做入队与读状态。

class BatchBusy(RuntimeError):
    """已有批次在跑（跑批吃满 CPU，同一时间只允许一个）。"""


def queue_dir() -> Path:
    return batch_dir() / "queue"


def status_dir() -> Path:
    return batch_dir() / "status"


def requests_dir() -> Path:
    return batch_dir() / "requests"


def status_file(run_id: str) -> Path:
    return status_dir() / f"{Path(run_id).name}.json"


def _atomic_json(path: Path, payload: dict) -> None:
    """原子写 JSON。

    ⚠️ 权限：容器里的 API 进程以 root 跑，宿主 worker 以普通用户跑，两者共用
    `<数据根>/lab_batch/`。目录 0777 / 文件 0666 是**必要的**——否则 worker
    删不掉 API 写的队列文件、也替换不了 root 建的 status 文件（2026-11-15 实测
    PermissionError）。单用户本地部署，`/data` 本身不是多租户边界。
    """
    parent = path.parent
    parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(parent, 0o777)
    except OSError:
        pass
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    try:
        os.chmod(tmp, 0o666)
    except OSError:
        pass
    tmp.replace(path)
    try:
        os.chmod(path, 0o666)
    except OSError:
        pass


def write_status(run_id: str, **fields) -> dict:
    """合并写状态文件（worker 逐行更新进度也走这里，原子替换）。"""
    run_id = str(run_id or "").strip()
    if not run_id:
        raise ValueError("run_id 不能为空")
    path = status_file(run_id)
    cur = {}
    if path.is_file():
        try:
            cur = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            cur = {}
    cur.update(fields)
    cur["run_id"] = run_id
    cur["updated"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    _atomic_json(path, cur)
    return cur


def get_status(run_id: str) -> dict:
    """批次状态。优先 status/；老批次没有状态文件但结果已落盘 → done。"""
    path = status_file(run_id)
    if path.is_file():
        return json.loads(path.read_text(encoding="utf-8"))
    result = (batch_dir() / f"{Path(run_id).name}.json")
    if result.is_file():
        try:
            d = json.loads(result.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            d = {}
        return {"run_id": run_id, "status": "done", "stage": "done",
                "pool": d.get("pool"), "pool_label": d.get("pool_label"),
                "universe": d.get("universe"), "counts": d.get("counts"),
                "elapsed_sec": d.get("elapsed_sec"), "error": ""}
    raise FileNotFoundError(f"批次不存在: {run_id}")


def active_run() -> dict | None:
    """当前排队/运行中的批次（队列里或状态为 queued/running）。

    running 状态超过 30 分钟没有心跳更新 → 判定 worker 已退出，标 failed 并放行，
    避免一个崩溃的批次永久堵住队列（worker 每 2 秒写一次进度/心跳）。
    """
    if status_dir().is_dir():
        for p in sorted(status_dir().glob("*.json")):
            try:
                d = json.loads(p.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            status = d.get("status")
            if status not in ("queued", "running"):
                continue
            if status == "running":
                try:
                    ts = datetime.fromisoformat(str(d.get("updated") or ""))
                    if (datetime.now(timezone.utc) - ts).total_seconds() > 1800:
                        d = write_status(
                            d.get("run_id") or p.stem, status="failed", stage="worker",
                            error="worker 超过 30 分钟无心跳（可能已退出）",
                        )
                        continue
                except (TypeError, ValueError):
                    pass
            return d
    if queue_dir().is_dir():
        for p in sorted(queue_dir().glob("*.json")):
            return {"run_id": p.stem, "status": "queued", "stage": "queued"}
    return None


def _normalize_codes(codes: list | None) -> list[str]:
    names = ml._names("cn")
    out, bad, seen = [], [], set()
    for raw in codes or []:
        code = ml.normalize_code(str(raw or ""), "cn")
        if not code or code in seen:
            continue
        seen.add(code)
        if code not in names:
            bad.append(str(raw))
            continue
        out.append(code)
    if bad:
        raise ValueError(f"未知 A 股代码: {', '.join(bad[:10])}"
                         + ("…" if len(bad) > 10 else ""))
    return out


def create_request(pool: str = "hs300", codes: list | None = None,
                   strategies: list | None = None, adj: str = "backward",
                   limit: int = 0, min_bars: int = 0,
                   requested_by: str = "") -> dict:
    """入队一批回测。pool=custom 时用 codes；其余走已有股票池名。"""
    active = active_run()
    if active:
        raise BatchBusy(f"已有批次在跑：{active.get('run_id')}（{active.get('status')}）")

    pool = str(pool or "hs300").strip()
    custom = pool == "custom" or bool(codes)
    if custom:
        codes = _normalize_codes(codes)
        if not codes:
            raise ValueError("自定义池至少给一个 A 股代码")
        pool_id = "custom"
        label = f"自定义（{len(codes)} 只）"
    else:
        if pool not in INDEX_POOLS and pool != "all":
            raise ValueError(
                f"未知股票池: {pool}（可用：{'/'.join(INDEX_POOLS)}/all/custom）"
            )
        codes = []
        pool_id = pool
        label = pool_label(pool)

    strategies = [str(s).strip() for s in (strategies or list(ml.STRATEGIES))]
    strategies = [s for s in strategies if s]
    bad = [s for s in strategies if s not in ml.STRATEGIES]
    if bad:
        raise ValueError(f"未知策略: {', '.join(bad)}")
    if not strategies:
        raise ValueError("至少选一个策略")
    if adj not in ml.ADJ_DIRS:
        raise ValueError(f"未知复权口径: {adj}")
    if limit and limit < 0:
        raise ValueError("limit 不能为负")
    min_bars = int(min_bars or MIN_BARS)
    if min_bars < 60:
        raise ValueError("min_bars 至少 60")

    run_id = new_run_id(pool_id)
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    req = {
        "run_id": run_id,
        "created": now,
        "pool": pool_id,
        "pool_label": label,
        "codes": codes,
        "strategies": strategies,
        "adj": adj,
        "limit": int(limit or 0),
        "min_bars": int(min_bars),
        "requested_by": str(requested_by or ""),
    }
    _atomic_json(queue_dir() / f"{run_id}.json", req)
    status = write_status(
        run_id, status="queued", stage="queued", pool=pool_id,
        pool_label=label, universe=len(codes) if custom else 0,
        total=len(codes) if custom else 0, done=0, rows=0, error="",
        created=now,
    )
    return {"request": req, "status": status}

# 批量回测最少 K 线根数：次新股/长期停牌没有统计意义
MIN_BARS = 250


def _index_members(index_code: str) -> list[str]:
    p = ml._root() / "2_base_sector/index_weights" / f"{index_code}.parquet"
    if not p.is_file():
        raise FileNotFoundError(f"指数成分缺失: {p}")
    import duckdb

    rows = duckdb.connect().execute(
        "SELECT Symbol FROM read_parquet(?) ORDER BY Symbol", [str(p)]).fetchall()
    return [str(s) for (s,) in rows if s]


def _all_stocks(limit: int = 0) -> list[str]:
    """全市场 A 股：剔 ST/退市，按流通市值降序（limit 才有意义）。"""
    p = ml._root() / "2_base_sector/instrument_detail/instrument_detail.parquet"
    import duckdb

    rows = duckdb.connect().execute(
        "SELECT Symbol FROM read_parquet(?) "
        "WHERE (IsSTGP IS NULL OR IsSTGP <> '1') "
        "  AND (IsQuitGP IS NULL OR IsQuitGP <> '1') "
        "  AND Symbol LIKE '%.%' "
        "ORDER BY TRY_CAST(Ltsz AS DOUBLE) DESC NULLS LAST, Symbol",
        [str(p)]).fetchall()
    codes = [str(s) for (s,) in rows if s]
    return codes[:limit] if limit else codes


def _from_file(path: str) -> list[str]:
    """外部清单：JSON 数组（字符串或含 code 的对象）或含 codes/candidates 键的对象。"""
    raw = json.loads(Path(path).expanduser().read_text(encoding="utf-8"))
    if isinstance(raw, dict):
        for key in ("codes", "candidates", "pool", "symbols"):
            if key in raw:
                raw = raw[key]
                break
        else:
            raw = list(raw.keys())
    out = []
    for item in raw:
        if isinstance(item, dict):
            code = item.get("code") or item.get("symbol")
        else:
            code = item
        if code:
            out.append(str(code))
    return out


def resolve_pool(pool: str = "hs300", limit: int = 0) -> list[dict]:
    """股票池名 → [{"code","name"}]。支持 hs300/zz500/...、all、file:<path>。"""
    pool = (pool or "hs300").strip()
    if pool.startswith("file:"):
        codes = _from_file(pool[5:])
    elif pool in INDEX_POOLS:
        codes = _index_members(INDEX_POOLS[pool][0])
    elif pool == "all":
        codes = _all_stocks(limit)
    else:
        raise ValueError(f"未知股票池: {pool}（可用：{'/'.join(INDEX_POOLS)}/all/file:<path>）")

    names = ml._names()
    seen, out = set(), []
    for c in codes:
        code = ml.normalize_code(c)
        if not code or code in seen:
            continue
        seen.add(code)
        out.append({"code": code, "name": names.get(code, "")})
    if limit and not pool.startswith("file:") and pool != "all":
        out = out[:limit]
    return out


def pool_label(pool: str) -> str:
    if pool.startswith("file:"):
        return Path(pool[5:]).name
    if pool == "all":
        return "全市场"
    return INDEX_POOLS.get(pool, (pool, pool))[1]


# ---------- 跑批 ----------


def run_batch(symbols: list[dict], strategy_ids: list[str], adj: str = "backward",
              min_bars: int = MIN_BARS, on_progress=None) -> dict:
    """单进程跑一批：每个标的读一次 K 线 → 所有策略复用。

    返回 {rows, skipped, errors}。rows 每行 = 一个 (标的, 策略) 组合的统计。
    """
    rows: list[dict] = []
    skipped: list[dict] = []
    errors: list[dict] = []
    for i, sym in enumerate(symbols, 1):
        code, name = sym["code"], sym.get("name", "")
        try:
            bars = ml.load_klines(code, adj=adj, limit=0)
        except Exception as exc:  # noqa: BLE001 单标的失败不能拖垮整批
            errors.append({"symbol": code, "error": f"读K线失败: {exc}"})
            continue
        if len(bars) < min_bars:
            skipped.append({"symbol": code, "name": name, "bars": len(bars),
                            "reason": f"K线不足 {min_bars} 根"})
            continue
        for sid in strategy_ids:
            try:
                r = ml._run_strategy_on_bars(sid, bars, code, adj, want_trades=False)
            except Exception as exc:  # noqa: BLE001
                errors.append({"symbol": code, "strategy": sid, "error": str(exc)})
                continue
            rows.append(_row(code, name, sid, r, len(bars)))
        if on_progress:
            on_progress(i, len(symbols), code)
    return {"rows": rows, "skipped": skipped, "errors": errors}


def _num(stats: dict, key: str, field: str = "pct") -> float | None:
    v = (stats.get(key) or {}).get(field)
    return float(v) if isinstance(v, (int, float)) else None


def _row(code: str, name: str, sid: str, result: dict, bars: int) -> dict:
    """统计 CSV → 紧凑一行（批量结果不保留逐笔/净值，体积和速度都不允许）。"""
    st = result["stats"]
    return {
        "symbol": code, "name": name, "strategy": sid,
        "net_pct": _num(st, "Net profit"),
        "buy_hold_pct": _num(st, "Buy & hold return"),
        "dd_pct": _num(st, "Max equity drawdown"),
        "sharpe": _num(st, "Sharpe ratio", "value"),
        "sortino": _num(st, "Sortino ratio", "value"),
        "profit_factor": _num(st, "Profit factor", "value"),
        "win_rate_pct": _num(st, "Percent profitable"),
        "trades": _num(st, "Total trades", "value"),
        "avg_bars": _num(st, "Avg # bars in trades", "value"),
        "bars": bars,
        "start": result["meta"]["start"], "end": result["meta"]["end"],
    }


# ---------- 聚合 ----------


def _median(xs: list[float]) -> float | None:
    if not xs:
        return None
    s = sorted(xs)
    n = len(s)
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2


def _mean(xs: list[float]) -> float | None:
    return sum(xs) / len(xs) if xs else None


def aggregate(rows: list[dict]) -> list[dict]:
    """按策略聚合：均值/中位数/跑赢买入持有的比例 —— 排行榜用这个排序。"""
    by_sid: dict[str, list[dict]] = {}
    for r in rows:
        by_sid.setdefault(r["strategy"], []).append(r)

    out = []
    for sid, rs in by_sid.items():
        nets = [r["net_pct"] for r in rs if r["net_pct"] is not None]
        dds = [r["dd_pct"] for r in rs if r["dd_pct"] is not None]
        sharpes = [r["sharpe"] for r in rs if r["sharpe"] is not None]
        pfs = [r["profit_factor"] for r in rs if r["profit_factor"] is not None]
        wins = [r["win_rate_pct"] for r in rs if r["win_rate_pct"] is not None]
        trades = [r["trades"] for r in rs if r["trades"] is not None]
        beats = [r for r in rs
                 if r["net_pct"] is not None and r["buy_hold_pct"] is not None
                 and r["net_pct"] > r["buy_hold_pct"]]
        comparable = [r for r in rs
                      if r["net_pct"] is not None and r["buy_hold_pct"] is not None]
        out.append({
            "strategy": sid,
            # 结果文件可能比策略注册表旧（策略被删/改名）→ 退回 id，不能让界面 500
            "name": (ml.STRATEGIES.get(sid) or {}).get("name") or sid,
            "symbols": len(rs),
            "mean_net_pct": _mean(nets), "median_net_pct": _median(nets),
            "mean_dd_pct": _mean(dds),
            "mean_sharpe": _mean(sharpes), "mean_profit_factor": _mean(pfs),
            "mean_win_rate_pct": _mean(wins), "mean_trades": _mean(trades),
            "beat_bh_pct": 100.0 * len(beats) / len(comparable) if comparable else None,
        })
    out.sort(key=lambda x: (x["mean_net_pct"] is not None, x["mean_net_pct"] or -1e9),
             reverse=True)
    return out


# ---------- 结果落盘 ----------

def save_run(payload: dict, out_dir: Path | None = None) -> Path:
    out_dir = out_dir or batch_dir()
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{payload['run_id']}.json"
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)          # 原子替换：界面读到的一半文件不会存在
    return path


def new_run_id(pool: str) -> str:
    return f"{time.strftime('%Y%m%d-%H%M%S')}-{pool.replace(':', '_').replace('/', '_')}"


def list_runs(out_dir: Path | None = None, limit: int = 20) -> list[dict]:
    """按时间倒序列出批次（只读头部摘要，不加载全部明细）。"""
    out_dir = out_dir or batch_dir()
    if not out_dir.is_dir():
        return []
    files = sorted(out_dir.glob("*.json"), reverse=True)[:limit]
    out = []
    for f in files:
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        out.append({k: d.get(k) for k in
                    ("run_id", "created", "pool", "pool_label", "adj",
                     "strategies", "universe", "counts")})
    return out


def load_run(run_id: str, out_dir: Path | None = None) -> dict:
    path = (out_dir or batch_dir()) / f"{Path(run_id).name}.json"
    if not path.is_file():
        raise FileNotFoundError(f"批次不存在: {run_id}")
    return json.loads(path.read_text(encoding="utf-8"))
