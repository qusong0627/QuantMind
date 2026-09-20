"""候选池截面 SQL 不得把 `volume_trend_3d` 当布尔用。

`stock_daily_latest.volume_trend_3d` 在 `data/upgrade_v1.1.0.sql`（2026-05-11）里
从 boolean 统一成了 double precision 的**数值趋势**，前端 `rVolumeTrend` 也按
正/负渲染「递增/递减/平缓」。但同一时期的一处改动把两条 SQL 写成了布尔语义：

    CASE WHEN COALESCE(volume_trend_3d, false) THEN 1 ELSE 0 END
    CASE WHEN sdl_run.volume_trend_3d THEN 1.0 ELSE 0.0 END

前者在 CN 表上直接抛 `DatatypeMismatchError: COALESCE types double precision and
boolean cannot be matched`，而 CN 分支不吞异常（非 CN 才降级为空 map），于是
`/api/v1/research/universe` 整条候选池接口 500 —— 投研平台「没有数据了」。
后者更隐蔽：`CASE WHEN <double precision>` 会被 PG 拒绝，且即使能跑，把数值压成
1/0 也会让「递减」永远渲染不出来。

这两条断言是纯文本的，因为缺陷本身就是 SQL 文本里的类型口径。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from backend.services.api.routers import research_service as rs


class _CapturingSession:
    """只负责把 `_load_sdl_pg_map` 生成的 SQL 抄下来。"""

    def __init__(self) -> None:
        self.sql = ""
        self.params: dict | None = None

    async def execute(self, statement, params=None):  # noqa: ANN001
        self.sql = str(statement)
        self.params = params
        raise _StopAfterCapture


class _StopAfterCapture(Exception):
    pass


def _strip_sql_comments(sql: str) -> str:
    """注释里会引用反例原文（`COALESCE(volume_trend_3d, false)`），断言只看可执行部分。"""
    without_block = re.sub(r"/\*.*?\*/", " ", sql, flags=re.DOTALL)
    return re.sub(r"--[^\n]*", " ", without_block)


@pytest.fixture(scope="module")
def pg_map_sql() -> str:
    import asyncio
    import datetime as dt

    session = _CapturingSession()
    with pytest.raises(_StopAfterCapture):
        asyncio.get_event_loop().run_until_complete(
            rs._load_sdl_pg_map(session, dt.date(2026, 9, 18), "CN")
        )
    return _strip_sql_comments(session.sql)


class TestSdlPgMapSql:
    def test_volume_trend_is_not_coalesced_against_a_boolean(self, pg_map_sql):
        assert "COALESCE(volume_trend_3d, false)" not in pg_map_sql
        assert "COALESCE(volume_trend_3d, 0)" in pg_map_sql

    def test_no_case_when_over_the_numeric_column(self, pg_map_sql):
        # `CASE WHEN <double precision>` 本身就会被 PG 拒绝
        assert "WHEN COALESCE(volume_trend_3d" not in pg_map_sql


class TestRunDateSelect:
    def test_volume_trend_is_selected_as_a_value(self):
        assert "COALESCE(sdl_run.volume_trend_3d, sdl_run.volume_trend_3d_calc)" in (
            rs._SDL_SELECT_BY_RUN_DATE
        )

    def test_volume_trend_is_not_used_as_a_condition(self):
        assert "CASE WHEN sdl_run.volume_trend_3d THEN" not in (
            rs._SDL_SELECT_BY_RUN_DATE
        )


# `volume_trend_3d <类型>,` —— 排除 upgrade 脚本里的
# `COMMENT ON COLUMN ... IS '...';`（没有行尾逗号，不会命中）
_DECL = re.compile(r"^\s*volume_trend_3d\s+([A-Za-z][A-Za-z ]*?)\s*,", re.MULTILINE)

_BACKEND_DIR = Path(__file__).resolve().parents[1]
_DB_INIT = _BACKEND_DIR / "shared" / "db_init.sql"


def _upgrade_script() -> Path:
    """`data/` 的落点在两种跑法下不同：宿主机是 <repo>/data，容器里挂到 /data。"""
    candidates = [
        _BACKEND_DIR.parent / "data" / "upgrade_v1.1.0.sql",
        Path("/data/upgrade_v1.1.0.sql"),
    ]
    for cand in candidates:
        if cand.exists():
            return cand
    pytest.fail(
        "找不到 data/upgrade_v1.1.0.sql，无法校验建表口径一致性；"
        f"已尝试: {[str(c) for c in candidates]}"
    )


def _declared_type(path):
    found = _DECL.findall(path.read_text(encoding="utf-8"))
    assert found, f"{path.name} 里找不到 volume_trend_3d 的列声明"
    return {t.strip().lower() for t in found}


class TestSchemaSourcesAgree:
    """两份建表来源必须同口径，否则「活库修好了、纯净部署还是坏」。

    `db_init.sql` 建全新库，`upgrade_v1.1.0.sql` 把老库迁移到同一形状。
    2026-09 的候选池 500 就是因为两边对不上：迁移脚本早就统一成 double precision，
    而 db_init 一直留着 BOOLEAN —— 活库跑 upgrade 所以没事，全新库照 db_init 建表
    就会建出 boolean，`COALESCE(volume_trend_3d, 0)` 当场报类型不匹配。
    """

    def test_db_init_declares_numeric(self):
        assert _declared_type(_DB_INIT) == {"double precision"}

    def test_upgrade_script_declares_numeric(self):
        assert _declared_type(_upgrade_script()) == {"double precision"}

    def test_both_sources_agree(self):
        assert _declared_type(_DB_INIT) == _declared_type(_upgrade_script())

    def test_detector_is_not_vacuous(self, tmp_path):
        # 判定器必须真能读出版本差异，否则上面三条只是「没解析到」而已
        fake = tmp_path / "fake.sql"
        fake.write_text(
            "CREATE TABLE t (\n    volume_trend_3d   BOOLEAN,\n);\n", encoding="utf-8"
        )
        assert _declared_type(fake) == {"boolean"}
