"""L0.5 热集快照落盘（T-P6-04）测试：parquet 按日分区 + 质检 + 降冷 + 归档器缓冲。

覆盖：
1. U：写读回环（逐值断言）＋**分区范式守卫**（date= 目录布局、文件内无 dt 列——防
   DuckDB 分区遮蔽事故重演）＋质检规则（单调/缺口/覆盖/五档完整性）＋降冷（只删 date= 目录）；
2. U：归档器缓冲按日分组 flush；
3. I：真实推送样本 → SubscriptionEngine 归档 sink → 落盘 → 读回逐值（真链路）；
4. U：维护面——日列表/容量统计/写侧排序守卫/缺失日空读/维护 CLI 冒烟；
5. P：写 10 万行后单标的读 < 1s（容量预算输入）。
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

_CST = timezone(timedelta(hours=8))

_PUSH_SAMPLE = (
    '{"Error":"","ErrorId":0,"ResultSets":[{"ColDes":["code","decimal","price",'
    '"pre_close","open","high","low","refresh_time","volume","bond_match_price",'
    '"limit_up","limit_down","accrued_interest","cage_up","cage_down",'
    '"after_hours_flag","tomorrow_limit_up","tomorrow_limit_down","sdunit_status",'
    '"seal_amount","ask1","ask2","ask3","ask4","ask5","ask_vol1","ask_vol2",'
    '"ask_vol3","ask_vol4","ask_vol5","bid1","bid2","bid3","bid4","bid5",'
    '"bid_vol1","bid_vol2","bid_vol3","bid_vol4","bid_vol5"],"Content":['
    '["600036.SH","2","40.92","41.10","41.15","41.41","40.52","153053","538019",'
    '"0.000","45.21","36.99","20628944896.0000","41.74","40.09","1","45.01",'
    '"36.83","5","0.00","40.92","40.93","40.94","40.95","40.96","182","5","16",'
    '"66","27","40.91","40.90","40.89","40.88","40.87","12","160","186","169",'
    '"969"]]}]} '
).strip()


def _record(symbol: str, ts: int, price: float, *, book: bool = True) -> dict:
    rec = {
        "symbol": symbol,
        "price": price,
        "pre_close": price - 0.1,
        "open": price - 0.2,
        "high": price + 0.3,
        "low": price - 0.4,
        "volume": 10000.0,
        "limit_up": price * 1.1,
        "limit_down": price * 0.9,
        "refresh_time": datetime.fromtimestamp(ts, tz=_CST).strftime("%H%M%S"),
        "ts": ts,
    }
    if book:
        for i in range(1, 6):
            rec[f"bid{i}"] = price - 0.01 * i
            rec[f"bid_vol{i}"] = 100.0 * i
            rec[f"ask{i}"] = price + 0.01 * i
            rec[f"ask_vol{i}"] = 200.0 * i
    return rec


# ── 1. 写读回环与分区范式 ───────────────────────────────────────────


@pytest.mark.unit
def test_write_read_roundtrip_and_partition_guard(tmp_path):
    from backend.shared.l05_store import read_day, write_records

    base = tmp_path / "l05"
    day = date(2026, 9, 17)
    base_ts = int(datetime(2026, 9, 17, 10, 0, 0, tzinfo=_CST).timestamp())
    records = [
        _record("600036.SH", base_ts + i * 3, 40.92 + i * 0.01) for i in range(5)
    ] + [_record("000858.SZ", base_ts + i * 3, 69.26) for i in range(3)]

    report = write_records(records, base_dir=str(base))
    assert report["rows"] == 8 and report["dates"] == ["20260917"]

    # 分区范式：date=YYYYMMDD 目录 + parquet 文件
    day_dir = base / "date=20260917"
    files = list(day_dir.glob("*.parquet"))
    assert len(files) == 1

    df = read_day(day, base_dir=str(base))
    assert len(df) == 8
    # 分区范式守卫：**文件内不得有 dt/date 列**（防同名列遮蔽 hive 分区——已在案事故）；
    # 读取侧 DuckDB 合成的 date 列即 hive 分区键（无歧义，断言其值）
    import pyarrow.parquet as pq

    file_schema = pq.read_schema(files[0]).names
    assert "dt" not in file_schema and "date" not in file_schema, file_schema
    assert set(df["date"].astype(str).unique()) == {"20260917"}
    # 逐值断言
    one = read_day(day, base_dir=str(base), symbols=["600036.SH"])
    assert len(one) == 5
    row = one.sort_values("ts").iloc[-1]
    assert float(row["price"]) == pytest.approx(40.92 + 4 * 0.01, abs=1e-9)
    assert float(row["bid1"]) == pytest.approx(40.92 + 4 * 0.01 - 0.01, abs=1e-9)
    assert int(row["ts"]) == base_ts + 4 * 3


@pytest.mark.unit
def test_write_groups_by_local_date(tmp_path):
    from backend.shared.l05_store import read_day, write_records

    base = tmp_path / "l05"
    d1 = int(datetime(2026, 9, 17, 23, 59, 50, tzinfo=_CST).timestamp())
    d2 = int(datetime(2026, 9, 18, 0, 0, 10, tzinfo=_CST).timestamp())
    report = write_records(
        [_record("600036.SH", d1, 40.0), _record("600036.SH", d2, 41.0)],
        base_dir=str(base),
    )
    assert sorted(report["dates"]) == ["20260917", "20260918"]
    assert len(read_day(date(2026, 9, 17), base_dir=str(base))) == 1
    assert len(read_day(date(2026, 9, 18), base_dir=str(base))) == 1


# ── 2. 质检规则 ─────────────────────────────────────────────────────


@pytest.mark.unit
def test_quality_report_flags():
    import pyarrow as pa

    from backend.shared.l05_store import quality_report

    base_ts = int(datetime(2026, 9, 17, 10, 0, 0, tzinfo=_CST).timestamp())
    rows = []
    # A：正常（单调、五档齐）
    for i in range(5):
        rows.append(_record("600036.SH", base_ts + i * 3, 40.0))
    # B：时间乱序（第 3 根倒退）
    for off in [0, 3, 2, 9, 12]:
        rows.append(_record("000858.SZ", base_ts + off, 69.0))
    # C：五档缺失
    for i in range(3):
        rows.append(_record("601318.SH", base_ts + i * 3, 50.0, book=False))

    table = pa.Table.from_pylist(rows)
    report = quality_report(table)
    per = report["symbols"]
    assert per["600036.SH"]["ts_monotonic"] is True
    assert per["000858.SZ"]["ts_monotonic"] is False
    assert per["601318.SH"]["book_completeness"] == pytest.approx(0.0)
    assert per["600036.SH"]["book_completeness"] == pytest.approx(1.0)
    assert report["totals"]["rows"] == 13 and report["totals"]["symbols"] == 3
    assert any("ts_monotonic" in f for f in report["flags"])


# ── 3. 降冷 ─────────────────────────────────────────────────────────


@pytest.mark.unit
def test_prune_old_dirs_only_date_dirs(tmp_path):
    from backend.shared.l05_store import prune_old

    base = tmp_path / "l05"
    old = base / "date=20260101"
    keep = base / "date=20260917"
    other = base / "README.txt"
    for p in (old, keep):
        p.mkdir(parents=True)
        (p / "x.parquet").write_bytes(b"x")
    other.write_text("keep me")

    now = datetime(2026, 9, 17, 20, 0, tzinfo=_CST)
    planned = prune_old(str(base), keep_days=30, now=now, dry_run=True)
    assert planned == ["date=20260101"]
    assert old.exists()  # dry-run 不删
    removed = prune_old(str(base), keep_days=30, now=now)
    assert removed == ["date=20260101"]
    assert not old.exists() and keep.exists() and other.exists()


# ── 4. 归档器缓冲 ───────────────────────────────────────────────────


@pytest.mark.unit
def test_archiver_buffers_and_flushes(tmp_path):
    from backend.shared.l05_store import SnapshotArchiver, read_day

    base = tmp_path / "l05"
    arch = SnapshotArchiver(base_dir=str(base), flush_rows=1000, flush_seconds=30)
    d1 = int(datetime(2026, 9, 17, 23, 59, 50, tzinfo=_CST).timestamp())
    d2 = int(datetime(2026, 9, 18, 0, 0, 10, tzinfo=_CST).timestamp())
    for i in range(-50, 0):
        arch.append(_record("600036.SH", d1 + i, 40.0))
    arch.append(_record("600036.SH", d2, 41.0))
    assert arch.pending_rows == 51
    report = arch.flush()
    assert report["rows"] == 51 and arch.pending_rows == 0
    assert len(read_day(date(2026, 9, 17), base_dir=str(base))) == 50
    assert len(read_day(date(2026, 9, 18), base_dir=str(base))) == 1


# ── 5. 真链路：推送样本 → 引擎归档 sink ─────────────────────────────


@pytest.mark.integration
@pytest.mark.asyncio
async def test_engine_push_to_l05_sink(tmp_path):
    from backend.shared.l05_store import SnapshotArchiver, read_day
    from backend.shared.tdx_aidata.collector import SubscriptionEngine
    from backend.shared.tdx_aidata.protocol import parse_push_payload

    base = tmp_path / "l05"

    class _FakeBudget:
        def check(self):
            return None

        def consume(self):
            pass

        def note_rate_limited(self):
            return 60.0

        def note_success(self):
            pass

    archiver = SnapshotArchiver(base_dir=str(base))
    engine = SubscriptionEngine(
        sdk_subscribe=lambda codes, cb: None,
        sdk_unsubscribe=lambda: None,
        budget_gate=_FakeBudget(),
        redis_factory=None,
        archiver=archiver,
    )
    # 动态水印：样本 refresh_time 固定 15:30:53——归档时效闸门（>300s 拒收）生效后
    # 须改为「当前时刻」才能走通落盘路径（陈旧帧拒收由下方专项测试覆盖）
    fresh_sample = _PUSH_SAMPLE.replace(
        "153053", datetime.now(_CST).strftime("%H%M%S")
    )
    engine.on_push(fresh_sample)
    # 手动 drain（redis_factory=None 时仅归档路径生效）
    engine._drain_and_write()
    assert engine.counters["archived"] == 1
    archiver.flush()

    recs = parse_push_payload(fresh_sample)
    push_price = float(recs[0]["price"])
    from backend.shared.tdx_aidata.collector import record_ts

    day = datetime.fromtimestamp(record_ts(recs[0]), tz=_CST).date()
    df = read_day(day, base_dir=str(base), symbols=["600036.SH"])
    assert len(df) == 1
    assert float(df.iloc[0]["price"]) == pytest.approx(push_price)
    assert float(df.iloc[0]["bid1"]) == pytest.approx(40.91)


@pytest.mark.unit
def test_engine_archiver_skips_stale_replay_frames(tmp_path):
    """归档时效闸门：夜间/停牌陈旧重放帧（age>300s）不落盘，显式计数。"""
    import time

    from backend.shared.l05_store import SnapshotArchiver
    from backend.shared.tdx_aidata.collector import SubscriptionEngine

    class _FakeBudget:
        def check(self):
            return None

        def consume(self):
            pass

        def note_rate_limited(self):
            return 60.0

        def note_success(self):
            pass

    archiver = SnapshotArchiver(base_dir=str(tmp_path / "l05"))
    engine = SubscriptionEngine(
        sdk_subscribe=lambda codes, cb: None,
        sdk_unsubscribe=lambda: None,
        budget_gate=_FakeBudget(),
        redis_factory=None,
        archiver=archiver,
    )
    now = int(time.time())
    base = {"symbol": "600036.SH", "price": 40.0, "pre_close": 39.9, "open": 40.0}
    engine._queue.put({**base, "ts": now})          # 实时帧 → 落盘
    engine._queue.put({**base, "ts": now - 3600})   # 1h 前重放帧 → 闸门拦下
    engine._drain_and_write()
    assert engine.counters["archived"] == 1
    assert engine.counters["archived_stale_skipped"] == 1
    archiver.flush()
    from backend.shared.l05_store import read_day

    df = read_day(datetime.fromtimestamp(now, tz=_CST).date(), base_dir=str(tmp_path / "l05"))
    assert len(df) == 1


# ── 6. 维护面：日列表 / 容量 / 缺失日读取 / CLI 冒烟 ────────────────


@pytest.mark.unit
def test_read_day_missing_returns_empty_with_columns(tmp_path):
    from backend.shared.l05_store import read_day

    base = tmp_path / "l05"
    df = read_day(date(2026, 1, 5), base_dir=str(base))  # 目录都不存在
    assert len(df) == 0
    assert {"symbol", "ts", "price", "bid1", "ask5", "refresh_time", "source"} <= set(
        df.columns
    )


@pytest.mark.unit
def test_list_days_and_capacity_report(tmp_path):
    from backend.shared.l05_store import capacity_report, list_days, write_records

    base = tmp_path / "l05"
    ts17 = int(datetime(2026, 9, 17, 10, 0, 0, tzinfo=_CST).timestamp())
    ts18 = ts17 + 86400
    write_records(
        [_record("600036.SH", ts17 + i, 40.0 + i) for i in range(4)], base_dir=str(base)
    )
    write_records(
        [_record("600036.SH", ts18 + i, 41.0) for i in range(6)], base_dir=str(base)
    )
    (base / "README.txt").write_text("x")  # 非分区文件不识别
    (base / "_reports").mkdir()

    assert list_days(str(base)) == ["20260917", "20260918"]
    report = capacity_report(str(base))
    assert report["days"]["20260917"]["rows"] == 4
    assert report["days"]["20260918"]["rows"] == 6
    assert report["totals"]["rows"] == 10 and report["totals"]["days"] == 2
    assert report["unreadable_files"] == []
    assert capacity_report(str(base), days=["20260918"])["totals"]["rows"] == 6


@pytest.mark.unit
def test_write_sorts_rows_by_symbol_within_day(tmp_path):
    """写侧排序守卫：同一分区文件内按 (symbol, ts) 升序——行组统计可裁剪（回放读走快径）。"""
    import pyarrow.parquet as pq

    from backend.shared.l05_store import write_records

    base = tmp_path / "l05"
    ts0 = int(datetime(2026, 9, 17, 10, 0, 0, tzinfo=_CST).timestamp())
    shuffled = [
        _record("000858.SZ", ts0 + 1, 69.0),
        _record("600036.SH", ts0 + 1, 40.0),
        _record("000858.SZ", ts0, 69.0),
        _record("600036.SH", ts0, 40.0),
    ]
    report = write_records(shuffled, base_dir=str(base))
    table = pq.read_table(report["files"][0])
    cols = table.to_pydict()
    assert list(zip(cols["symbol"], cols["ts"], strict=False)) == [
        ("000858.SZ", ts0),
        ("000858.SZ", ts0 + 1),
        ("600036.SH", ts0),
        ("600036.SH", ts0 + 1),
    ]


@pytest.mark.unit
def test_maintenance_cli_smoke(tmp_path, capsys):
    """CLI 冒烟：report 落 JSON 且 PASS；capacity 汇总；prune 默认 dry-run 不删。"""
    from backend.scripts import l05_maintenance
    from backend.shared.l05_store import write_records

    base = tmp_path / "l05"
    ts = int(datetime(2026, 9, 17, 10, 0, 0, tzinfo=_CST).timestamp())
    write_records(
        [_record("600036.SH", ts + i, 40.0) for i in range(5)], base_dir=str(base)
    )

    assert l05_maintenance.main(["report", "--dir", str(base)]) == 0
    out = capsys.readouterr().out
    assert "PASS" in out and "rows=5" in out
    payload = json.loads((base / "_reports" / "quality-20260917.json").read_text())
    assert payload["day"] == "20260917" and payload["totals"]["rows"] == 5

    assert (
        l05_maintenance.main(["capacity", "--dir", str(base), "--keep-days", "90"]) == 0
    )
    out = capsys.readouterr().out
    assert "20260917" in out and "保留期投影" in out

    assert l05_maintenance.main(["prune", "--dir", str(base), "--keep-days", "0"]) == 0
    assert (base / "date=20260917").exists()


# ── 7. 性能（容量预算输入）──────────────────────────────────────────


@pytest.mark.unit
def test_write_read_perf_budget(tmp_path):
    import time

    from backend.shared.l05_store import read_day, write_records

    base = tmp_path / "l05"
    base_ts = int(datetime(2026, 9, 17, 9, 30, 0, tzinfo=_CST).timestamp())
    symbols = [f"{600000 + i}.SH" for i in range(100)]
    records = []
    for s_i, sym in enumerate(symbols):
        for i in range(1000):  # 10 万行
            records.append(_record(sym, base_ts + i * 3, 10.0 + s_i * 0.01))
    t0 = time.monotonic()
    write_records(records, base_dir=str(base))
    write_s = time.monotonic() - t0
    t0 = time.monotonic()
    df = read_day(date(2026, 9, 17), base_dir=str(base), symbols=[symbols[0]])
    read_s = time.monotonic() - t0
    assert len(df) == 1000
    assert read_s < 1.0, f"单标的读超预算: {read_s:.2f}s"
    print(
        f"[perf] write 100k rows {write_s:.2f}s, single-symbol read {read_s * 1000:.0f}ms"
    )
