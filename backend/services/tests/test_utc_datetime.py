from datetime import datetime, timedelta, timezone

from backend.shared.utc_datetime import UtcDateTime, as_utc, to_utc_iso, utc_now


def test_as_utc_treats_naive_as_utc_not_shanghai():
    naive = datetime(2026, 9, 14, 7, 54, 25)
    aware = as_utc(naive)
    assert aware.tzinfo is not None
    assert aware.utcoffset() == timedelta(0)
    assert aware.isoformat() == "2026-09-14T07:54:25+00:00"


def test_as_utc_converts_offset_to_utc():
    shanghai = datetime(2026, 9, 14, 15, 54, 25, tzinfo=timezone(timedelta(hours=8)))
    aware = as_utc(shanghai)
    assert aware == datetime(2026, 9, 14, 7, 54, 25, tzinfo=timezone.utc)


def test_to_utc_iso_uses_z_suffix_for_naive_and_aware():
    naive = datetime(2026, 9, 14, 7, 54, 25, 123456)
    aware = datetime(2026, 9, 14, 7, 54, 25, tzinfo=timezone.utc)
    assert to_utc_iso(naive) == "2026-09-14T07:54:25.123456Z"
    assert to_utc_iso(aware) == "2026-09-14T07:54:25Z"
    assert to_utc_iso(None) is None


def test_utc_now_is_aware():
    now = utc_now()
    assert now.tzinfo is not None
    assert now.utcoffset() == timedelta(0)


def test_utc_datetime_bind_and_result_are_aware_utc():
    col = UtcDateTime()
    naive = datetime(2026, 9, 14, 7, 54, 25)
    bound = col.process_bind_param(naive, None)
    assert bound.tzinfo is not None
    assert bound.utcoffset() == timedelta(0)
    loaded = col.process_result_value(naive, None)
    assert loaded.tzinfo is not None
    assert loaded == bound


def _read_db_init_sql() -> str:
    """读取主初始化 SQL（v1.0.1~v1.0.8 已合并进该文件第 66 节）。"""
    from pathlib import Path

    sql_path = Path(__file__).resolve().parents[3] / "backend" / "shared" / "db_init.sql"
    return sql_path.read_text(encoding="utf-8")


def test_db_init_sql_has_no_percent_sign():
    """psycopg2 fallback 会把 SQL 文本里的 %% 当占位符解析，故必须为零。"""
    sql = _read_db_init_sql()
    assert "%" not in sql


def test_db_init_sql_inlines_system_events_table():
    """原 upgrade_v1.0.2.sql 的 system_events 建表已合并进主初始化表。"""
    sql = _read_db_init_sql()
    assert "CREATE TABLE IF NOT EXISTS system_events" in sql


def test_db_init_sql_inlines_v107_timestamptz_conversion():
    """原 upgrade_v1.0.7.sql：模拟成交瞬时列统一 timestamptz。"""
    sql = _read_db_init_sql()
    assert "sim_trades" in sql
    assert "timestamptz" in sql.lower()
    assert "00000001" in sql


def test_db_init_sql_inlines_v108_canonical_user_id():
    """原 upgrade_v1.0.8.sql：管理员 user_id 收口为 10000001。"""
    sql = _read_db_init_sql()
    assert "10000001" in sql
    assert "sim_orders" in sql
    assert "simulation_fund_snapshots" in sql
