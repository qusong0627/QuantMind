"""因子概率板：产物组包/校验/原子落盘/读取（schema ``factor_board/1.1``）。

1.1 增量（P2）：顶层 ``regime``/``dedup`` 两键（**可为 null 但键必须在**）；
``factors[]`` 新增净 IC/衰减/regime 列。前端 REQUIRED_TOP_KEYS 仍是 8 核心
（1.0 客户端对 1.1 产物向后兼容）。

落盘纪律：临时文件 + ``os.replace``（读到一半的 JSON 比没有更坏）；同日重跑覆盖；
``latest.json`` 与 ``{decision_date}.json`` 内容一致。缺失一律 JSON ``null``。
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

import numpy as np
import pandas as pd

from backend.shared.factor_board import config as C
from backend.shared.factor_board import sectors as S

log = logging.getLogger("factor_board.board_io")

_REQUIRED_TOP = (
    "schema_version",
    "meta",
    "factors",
    "horizon",
    "validation",
    "resonance",
    "sectors",
    "stocks",
    "regime",  # 1.1：regime 分桶权重块（可为 null）
    "dedup",  # 1.1：入选内二次去重块（可为 null）
)


def _clean(x, nd: int = 4):
    """NaN/inf → None；float 取 nd 位。JSON 里绝不允许出现 NaN（非标准）。"""
    if x is None:
        return None
    if isinstance(x, (int, np.integer)):
        return int(x)
    try:
        if not np.isfinite(x):
            return None
    except TypeError:
        return x
    return round(float(x), nd)


def _bool_or_none(x):
    """bool/None/NaN → 三态布尔（缺失绝不落成 False）。"""
    if x is None:
        return None
    try:
        if pd.isna(x):
            return None
    except (TypeError, ValueError):
        pass
    return bool(x)


def _int_or_none(x):
    """int/None/NaN → 三态整数。"""
    if x is None:
        return None
    try:
        if pd.isna(x):
            return None
    except (TypeError, ValueError):
        pass
    return int(x)


def sector_payload(q: pd.DataFrame, members: pd.DataFrame, stype: str) -> dict | None:
    """单类板块（行业二级/一级/概念）payload：quantiles + items（按均分降序）。"""
    sm = members[members["SectorType"] == stype]
    if sm.empty:
        return None
    base = q.merge(
        sm.rename(columns={"Symbol": "symbol"}).set_index("symbol")[["SectorName"]],
        left_index=True,
        right_index=True,
        how="inner",
    )
    g = S.sector_table(base)
    if g.empty:
        return None
    g, q67, q33 = S.apply_verdicts(g)
    g = g.sort_values("fac", ascending=False, na_position="last")
    items = []
    for name, r in g.iterrows():
        items.append(
            {
                "name": str(name),
                "n": int(r["n"]),
                "n_scored": int(r.get("n_scored", r["n"])),
                "fac": _clean(r["fac"]),
                "med": _clean(r["med"]),
                "fbreadth": _clean(r["fbreadth"]),
                "mbreadth": _clean(r["mbreadth"]),
                "model_cov": _clean(r["model_cov"]),
                "verdict": str(r["verdict"]),
            }
        )
    return {"q67": _clean(q67), "q33": _clean(q33), "items": items}


def factor_payload(sel: pd.DataFrame, horizon: pd.DataFrame) -> list[dict]:
    """入选因子清单（按 |IC60| 降序），携带持有期画像。

    1.1 增量列（缺失列→None，三态语义，**绝不为 0**）：净 IC（``net_ic60``/
    ``net_win60``/``net_icir60``/``eroded``）、尾窗衰减（``ic20``/``win20``/
    ``decay`` ∈ ok/weak/flipped/insufficient）、regime（``w_raw``/
    ``regime_mult``/``regime_ic``/``regime_n``/``regime_note``）。
    """
    out = []
    hz = horizon if horizon is not None else pd.DataFrame()
    for _, r in sel.sort_values(
        "ic60", key=lambda s: s.abs(), ascending=False
    ).iterrows():
        item = {
            "name": r["name"],
            "lib": r["lib"],
            "group": r["group"],
            "display": r["display"],
            "sign": int(r["sgn"]),
            "ic60": _clean(r["ic60"]),
            "win60": _clean(r["win60"]),
            "icir60": _clean(r["icir60"]),
            "t60": _clean(r.get("t60")),
            "kept_ic": _clean(r["kept_ic"]),
            "weight": _clean(r["w"]),
            "net_ic60": _clean(r.get("net_ic60")),
            "net_win60": _clean(r.get("net_win60")),
            "net_icir60": _clean(r.get("net_icir60")),
            "net_t60": _clean(r.get("net_t60")),
            "eroded": _bool_or_none(r.get("eroded")),
            "ic20": _clean(r.get("ic20")),
            "win20": _clean(r.get("win20")),
            "decay": r.get("decay") if isinstance(r.get("decay"), str) else None,
            "w_raw": _clean(r.get("w_raw")),
            "regime_mult": _clean(r.get("regime_mult")),
            "regime_ic": _clean(r.get("regime_ic")),
            "regime_n": _int_or_none(r.get("regime_n")),
            "regime_note": r.get("regime_note")
            if isinstance(r.get("regime_note"), str)
            else None,
            "horizon": {},
        }
        if (r["lib"], r["name"]) in hz.index:
            row = hz.loc[(r["lib"], r["name"])]
            item["horizon"] = {str(h): _clean(row.get(h)) for h in C.HORIZON_DAYS}
        out.append(item)
    return out


def regime_payload(block: dict | None) -> dict | None:
    """regime 块（builder 已组装）→ JSON 安全 dict（浮点净化 + 三态）。"""
    if block is None:
        return None
    out = dict(block)
    out["by_family"] = {
        str(k): {
            "state_ic": _clean(v.get("state_ic")),
            "n_days": _int_or_none(v.get("n_days")) or 0,
            "mult": _clean(v.get("mult")),
        }
        for k, v in (block.get("by_family") or {}).items()
    }
    return out


def latest_rolling_oos(board_dir: Path) -> dict | None:
    """最近一期滚动 OOS（``rolling/*.json`` 中 ``oos`` 非空的最新应用月）。

    OOS = 该月快照按冻结 sgn/w 合成后、后窗逐日对 fwd5 的 RankIC（前窗选、
    后窗验；见 rolling.py）。无目录/无已回填台账 → None。
    """
    rdir = Path(board_dir) / C.ROLLING_DIRNAME
    if not rdir.is_dir():
        return None
    months = sorted(
        p.stem
        for p in rdir.glob("*.json")
        if len(p.stem) == 7 and p.stem[4] == "-" and p.stem.replace("-", "").isdigit()
    )
    for m in reversed(months):
        try:
            led = json.loads((rdir / f"{m}.json").read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            log.warning("读取滚动台账失败 %s: %s", m, exc)
            continue
        oos = led.get("oos")
        if isinstance(oos, dict) and oos.get("rank_ic") is not None:
            return {"month": m, "as_of": led.get("as_of"), **oos}
    return None


def horizon_payload(horizon: pd.DataFrame, sel: pd.DataFrame) -> dict:
    """全局 + 分族持有期方向对齐 IC（见 composite.horizon_table 口径）。"""
    if horizon is None or horizon.empty:
        return {"global": {}, "by_group": {}}
    glob = {str(h): _clean(horizon[h].mean(skipna=True)) for h in C.HORIZON_DAYS}
    hz = horizon.join(sel.set_index(["lib", "name"])["group"])
    by_group: dict[str, dict] = {}
    for grp, g in hz.groupby("group"):
        by_group[str(grp)] = {
            str(h): _clean(g[h].mean(skipna=True)) for h in C.HORIZON_DAYS
        }
    return {"global": glob, "by_group": by_group}


def validate_board(board: dict) -> list[str]:
    """schema 自校验：返回错误列表（空=通过）。"""
    errors: list[str] = []
    for key in _REQUIRED_TOP:
        if key not in board:
            errors.append(f"缺顶层字段: {key}")
    if board.get("schema_version") != C.SCHEMA_VERSION:
        errors.append(f"schema_version 应为 {C.SCHEMA_VERSION}")
    meta = board.get("meta") or {}
    if not meta.get("decision_date"):
        errors.append("meta.decision_date 缺失")
    if not meta.get("trade_date"):
        errors.append("meta.trade_date 缺失")
    if (
        not isinstance(board.get("factors"), list)
        or len(board["factors"]) < C.MIN_SELECTED
    ):
        errors.append(f"factors 少于硬门 {C.MIN_SELECTED}")
    sectors = board.get("sectors") or {}
    for key in ("industry_l1", "industry_l2", "concept"):
        if key not in sectors:
            errors.append(f"sectors.{key} 缺失")
    return errors


def write_board(board: dict, out_dir: Path) -> tuple[Path, Path]:
    """原子写 ``{decision_date}.json`` + ``latest.json``。校验不过直接抛。"""
    errors = validate_board(board)
    if errors:
        raise ValueError(f"board schema 自校验失败: {errors}")
    out_dir.mkdir(parents=True, exist_ok=True)
    decision = board["meta"]["decision_date"]
    text = json.dumps(board, ensure_ascii=False, indent=2, allow_nan=False)
    dated = out_dir / f"{decision}.json"
    latest = out_dir / "latest.json"
    _atomic_write(dated, text)
    _atomic_write(latest, text)
    return dated, latest


def _atomic_write(path: Path, text: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def load_board(out_dir: Path, date: str | None = None) -> dict | None:
    """读取产物：指定日期→该日文件；否则 latest.json。文件缺失/坏 JSON → None。"""
    path = out_dir / f"{date}.json" if date else out_dir / "latest.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        log.warning("读取产物失败 %s: %s", path, exc)
        return None


def available_dates(out_dir: Path) -> list[str]:
    """已产出的决策日列表（降序）。"""
    if not out_dir.is_dir():
        return []
    out = []
    for p in out_dir.glob("*.json"):
        stem = p.stem
        if len(stem) == 10 and stem[4] == "-" and stem.replace("-", "").isdigit():
            out.append(stem)
    return sorted(out, reverse=True)


# ── 板块成分明细（钻取伴生件；板 JSON 本身不含成员，保持 1.1 契约不膨胀） ──

MEMBERS_DIRNAME = "members"
MEMBERS_SCHEMA = "factor_board_members/1.0"
_MEMBERS_CACHE_MAX = 8  # 每份 ~3MB（概念展开），封顶防内存无界


def write_members_index(members: dict, out_dir: Path) -> Path:
    """原子写 ``members/{decision_date}.json`` + ``members/latest.json``。

    ``members``：builder 填充的 {decision_date, trade_date, sectors}；schema_version
    由本函数补。同日重跑覆盖（与板同语义）；缺 decision_date 直接拒绝落盘。
    """
    decision = str(members.get("decision_date") or "")
    if not decision:
        raise ValueError("板块成分明细缺 decision_date，拒绝落盘")
    payload = {"schema_version": MEMBERS_SCHEMA, **members}
    text = json.dumps(payload, ensure_ascii=False, allow_nan=False)
    mdir = Path(out_dir) / MEMBERS_DIRNAME
    mdir.mkdir(parents=True, exist_ok=True)
    dated = mdir / f"{decision}.json"
    _atomic_write(dated, text)
    _atomic_write(mdir / "latest.json", text)
    return dated


_members_cache: dict[str, tuple[int, dict]] = {}


def load_members_index(out_dir: Path, date: str | None = None) -> dict | None:
    """读取成分明细：指定日期→该日文件；否则 ``latest.json``。

    文件缺失/坏 JSON/顶层非对象 → None。进程内按 (路径, mtime_ns) 缓存——
    产板同日重写后 mtime 变化即自动失效（有测试锁定该行为）。
    """
    path = Path(out_dir) / MEMBERS_DIRNAME / (f"{date}.json" if date else "latest.json")
    try:
        stamp = path.stat().st_mtime_ns
    except OSError:
        return None
    key = str(path)
    hit = _members_cache.get(key)
    if hit is not None and hit[0] == stamp:
        return hit[1]
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        log.warning("读取板块成分明细失败 %s: %s", path, exc)
        return None
    if not isinstance(data, dict):
        return None
    if len(_members_cache) >= _MEMBERS_CACHE_MAX:
        _members_cache.clear()
    _members_cache[key] = (stamp, data)
    return data
