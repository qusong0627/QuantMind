#!/usr/bin/env python3
"""事件雷达：解禁 / 股权质押 / 回购 → 本地缓存 + 标的标签（夜研究/复盘消费）。

数据源（akshare → 东方财富数据中心，免费无 token）：
  - 解禁  stock_restricted_release_detail_em(start,end)  未来区间全市场明细
  - 质押  stock_gpzy_pledge_ratio_em(date=周五)           全市场质押比例（周频）
  - 回购  stock_repurchase_em()                           全市场回购（进度/金额）

设计：
  - collect：每日采集落 parquet 缓存 data/events/{kind}_{yyyymmdd}.parquet；
    当日已采集默认跳过（--refresh 强制），单源失败不影响其他源（降级）
  - tag：对标的清单打事件标签（未来 N 日解禁 / 质押比例≥阈值 / 回购进行中），
    供 night_pool 候选池与 post_review 持仓告警消费；任何缺失 → 空标签不阻塞
  - 代码口径：akshare 无后缀（600370）↔ 本系统带后缀（600370.SZ），按 6 位前缀匹配

用法：
  python scripts/event_radar.py collect [--refresh]
  python scripts/event_radar.py tag --codes 600309.SH,688183.SH [--horizon 10]
  python scripts/event_radar.py summary
"""
import argparse
import json
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

# 缓存目录：默认仓库 data/events；dsh/quantmind 容器内用 EVENT_RADAR_DIR 指向拷入的缓存目录
DATA_DIR = Path(__import__("os").environ.get("EVENT_RADAR_DIR")
                or (ROOT / "data" / "events"))
PLEDGE_MIN = 50.0        # 质押比例阈值（%）
UNLOCK_RATIO_MIN = 1.0   # 解禁占流通市值 ≥1% 才标注
HORIZON_DEFAULT = 10     # 解禁观察窗口（自然日）


def _cache(kind: str, d: date) -> Path:
    return DATA_DIR / f"{kind}_{d:%Y%m%d}.parquet"


def _latest_cache(kind: str, max_age_days: int = 7) -> Path | None:
    """最近一次成功采集的缓存（超过 max_age 视为过期不用）。"""
    if not DATA_DIR.is_dir():
        return None
    cands = sorted(DATA_DIR.glob(f"{kind}_*.parquet"))
    if not cands:
        return None
    f = cands[-1]
    d = datetime.strptime(f.stem.split("_")[-1], "%Y%m%d").date()
    return f if (datetime.now().date() - d).days <= max_age_days else None


def _ak():
    import akshare  # 延迟导入：未安装时 tag 降级为空标签

    return akshare


def collect(day: date | None = None, refresh: bool = False,
            backfill: int = 0) -> dict:
    """采集三类事件数据落缓存（backfill=N 同时回看最近 N 自然日）。返回 {kind: 状态}。"""
    day = day or datetime.now().date()
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    ak = _ak()
    stats: dict = {}

    def _save(kind: str, df) -> int:
        f = _cache(kind, day)
        df.to_parquet(f, index=False)
        return len(df)

    # 1) 解禁：[day-backfill, day+30] 全市场明细（回看支持复盘"上周解禁"）
    f = _cache("unlock", day)
    if f.is_file() and not refresh:
        stats["unlock"] = "cached"
    else:
        try:
            df = ak.stock_restricted_release_detail_em(
                start_date=(day - timedelta(days=backfill)).strftime("%Y%m%d"),
                end_date=(day + timedelta(days=30)).strftime("%Y%m%d"))
            stats["unlock"] = _save("unlock", df)
        except Exception as exc:  # noqa: BLE001
            stats["unlock"] = f"fail: {str(exc)[:80]}"

    # 2) 质押：窗口内每个周五一份快照（周频；缓存名=数据日期，多版本共存）
    fridays = [day - timedelta(days=i) for i in range(max(backfill, 7) + 1)
               if (day - timedelta(days=i)).weekday() == 4]
    done = 0
    for friday in sorted(fridays):
        f = _cache("pledge", friday)
        if f.is_file() and not refresh:
            done += 1
            continue
        try:
            df = ak.stock_gpzy_pledge_ratio_em(date=friday.strftime("%Y%m%d"))
            f2 = DATA_DIR / f"pledge_{friday:%Y%m%d}.parquet"
            df.to_parquet(f2, index=False)
            done += 1
        except Exception as exc:  # noqa: BLE001
            stats["pledge"] = f"fail@{friday:%m%d}: {str(exc)[:60]}"
    if done:
        stats["pledge"] = f"{done} 个周五快照"

    # 3) 回购：全量（akshare 内部分页 ~7s）
    f = _cache("buyback", day)
    if f.is_file() and not refresh:
        stats["buyback"] = "cached"
    else:
        try:
            df = ak.stock_repurchase_em()
            stats["buyback"] = _save("buyback", df)
        except Exception as exc:  # noqa: BLE001
            stats["buyback"] = f"fail: {str(exc)[:80]}"

    # 4) 重建事件风险清单（解禁/负面新闻 → 买入硬拦，见 risk_list）
    try:
        from risk_list import refresh as _risk_refresh

        st = _risk_refresh()
        stats["risk_list"] = (f"{st['items']} 只禁买 / {st['warns']} 只质押告警"
                              f" / {st['watch']} 只监管关注")
    except Exception as exc:  # noqa: BLE001 清单重建失败不影响采集结果
        stats["risk_list"] = f"fail: {str(exc)[:80]}"
    return stats


def _norm6(code: str) -> str:
    return str(code).split(".")[0].strip()


def _read_cache(f: Path) -> list:
    """duckdb 读缓存 parquet（不依赖 pyarrow，quantmind 容器/dsh 环境皆可用）。"""
    import duckdb

    con = duckdb.connect()
    cur = con.execute("SELECT * FROM read_parquet(?)", [str(f)])
    cols = [c[0] for c in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def tag_events(codes: list, horizon: int = HORIZON_DEFAULT,
               asof: date | None = None) -> dict:
    """对标的清单打事件标签。返回 {code(带后缀原样): [标签...]}；数据缺失 → 空列表。"""
    asof = asof or datetime.now().date()
    codes = [str(c) for c in codes if c]
    if not codes:
        return {}
    out: dict = {c: [] for c in codes}
    six = {_norm6(c): c for c in codes}

    # 1) 解禁（未来 horizon 日）
    f = _latest_cache("unlock")
    if f is not None:
        try:
            rows = _read_cache(f)
            dcol = next((c for c in rows[0] if "解禁时间" in c), None) if rows else None
            for r in rows:
                c6 = _norm6(r.get("股票代码") or r.get("证券代码") or "")
                if c6 not in six:
                    continue
                try:
                    dday = datetime.strptime(str(r[dcol])[:10], "%Y-%m-%d").date()
                except ValueError:
                    continue
                if not (asof <= dday <= asof + timedelta(days=horizon)):
                    continue
                ratio = float(r.get("占解禁前流通市值比例") or 0) * 100
                if ratio < UNLOCK_RATIO_MIN:
                    continue
                out[six[c6]].append(
                    f"{dday.strftime('%m/%d')}解禁{ratio:.1f}%流通盘"
                    f"（{r.get('限售股类型') or '—'}）")
        except Exception:  # noqa: BLE001
            pass

    # 2) 质押（最新缓存全量）
    f = _latest_cache("pledge")
    if f is not None:
        try:
            rows = _read_cache(f)
            for r in rows:
                c6 = _norm6(r.get("股票代码") or "")
                if c6 not in six:
                    continue
                ratio = float(r.get("质押比例") or 0)
                if ratio >= PLEDGE_MIN:
                    out[six[c6]].append(f"质押比例{ratio:.0f}%")
        except Exception:  # noqa: BLE001
            pass

    # 3) 回购（实施中 且 公告 ≤30 日）
    f = _latest_cache("buyback")
    if f is not None:
        try:
            rows = _read_cache(f)
            prog = next((c for c in rows[0] if "进度" in c), None) if rows else None
            # 修复（2026-09-08）：原写 df.columns——df 是 collect() 里的局部变量，
            # 这里 NameError 被 except 吞掉，导致"公告 ≤30 日"过滤从未生效。
            ann = next((c for c in rows[0] if "公告日期" in c), None) if rows else None
            amt = next((c for c in rows[0] if "已回购金额" in c), None) if rows else None
            for r in rows:
                c6 = _norm6(r.get("股票代码") or "")
                if c6 not in six:
                    continue
                if prog and "实施" not in str(r.get(prog) or ""):
                    continue
                if ann:
                    try:
                        d_ann = datetime.strptime(str(r[ann])[:10], "%Y-%m-%d").date()
                        if (datetime.now().date() - d_ann).days > 30:
                            continue
                    except ValueError:
                        pass
                amt_txt = f"已回购{float(r[amt]) / 1e4:,.0f}万" if amt and r.get(amt) else ""
                out[six[c6]].append(("回购进行中" + (f"·{amt_txt}" if amt_txt else "")))
        except Exception:  # noqa: BLE001
            pass
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="事件雷达：解禁/质押/回购 采集与标的标签")
    sub = ap.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("collect", help="采集三类数据落缓存")
    c.add_argument("--refresh", action="store_true")
    c.add_argument("--date", default="")
    c.add_argument("--backfill", type=int, default=0,
                   help="回看最近 N 自然日（解禁区间+质押每周五快照）")

    t = sub.add_parser("tag", help="对标的清单打事件标签")
    t.add_argument("--codes", required=True, help="逗号分隔，支持带/不带后缀")
    t.add_argument("--horizon", type=int, default=HORIZON_DEFAULT)

    sub.add_parser("summary", help="查看最近缓存状态")

    a = ap.parse_args()
    if a.cmd == "collect":
        day = date.fromisoformat(a.date) if a.date else None
        stats = collect(day, refresh=a.refresh, backfill=a.backfill)
        for k, v in stats.items():
            print(f"{k}: {v}")
        return 0
    if a.cmd == "tag":
        tags = tag_events([c.strip() for c in a.codes.split(",") if c.strip()],
                          horizon=a.horizon)
        for c, v in tags.items():
            print(f"{c}: {'；'.join(v) if v else '（无事件）'}")
        return 0
    if a.cmd == "summary":
        for f in sorted(DATA_DIR.glob("*.parquet")) if DATA_DIR.is_dir() else []:
            print(f.name, f"{f.stat().st_size / 1024:.0f}KB",
                  datetime.fromtimestamp(f.stat().st_mtime).strftime("%m-%d %H:%M"))
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
