"""全局股票池模块（v2：单表元信息 + TXT 成员）单测。

覆盖不依赖数据库的纯逻辑：代码口径归一、内置池目录、ref 解析、
成员 TXT 读写、物化、信号过滤，以及消费方接入的静态护栏。
DB 相关（repository / seed / 库内池解析）留给集成测试。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from backend.shared.stock_pool import builtins as sp_builtins
from backend.shared.stock_pool import constants as sp_const
from backend.shared.stock_pool import normalize as sp_normalize
from backend.shared.stock_pool import parser as sp_parser
from backend.shared.stock_pool.filters import (
    filter_signals_by_pool,
    intersect_symbols as filter_intersect,
)
from backend.shared.stock_pool.materializer import (
    materialize_snapshot,
    pool_txt_name,
    pool_txt_path,
    read_instruments,
    read_pool_txt,
    write_instruments,
    write_pool_txt,
)
from backend.shared.stock_pool.resolver import (
    PoolResolver,
    ResolveContext,
    register_index_provider,
)

resolver = PoolResolver()


# ---------------------------------------------------------------------------
# 代码口径
# ---------------------------------------------------------------------------
class TestNormalize:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("SH600036", "600036.SH"),
            ("600036.SH", "600036.SH"),
            ("600036", "600036.SH"),
            ("SZ000001", "000001.SZ"),
            ("830001", "830001.BJ"),
        ],
    )
    def test_cn_storage_symbol(self, raw, expected):
        assert sp_normalize.to_storage_symbol(raw, "CN") == expected

    def test_cn_api_symbol_is_prefix(self):
        assert sp_normalize.to_api_symbol("600036.SH", "CN") == "SH600036"

    @pytest.mark.parametrize(
        "raw,expected",
        [("00700", "0700.HK"), ("0700.HK", "0700.HK"), ("80001", "80001.HK")],
    )
    def test_hk_storage_symbol(self, raw, expected):
        assert sp_normalize.to_storage_symbol(raw, "HK") == expected

    @pytest.mark.parametrize(
        "raw,expected",
        [("aapl", "AAPL"), ("US.AAPL", "AAPL"), ("NASDAQ:MSFT", "MSFT")],
    )
    def test_us_storage_symbol(self, raw, expected):
        assert sp_normalize.to_storage_symbol(raw, "US") == expected

    def test_normalize_symbols_dedupes_and_keeps_order(self):
        got = sp_normalize.normalize_symbols(
            ["SH600036", "600036.SH", "SZ000001", ""], "CN"
        )
        assert got == ["600036.SH", "000001.SZ"]

    def test_checksum_is_order_insensitive(self):
        a = sp_normalize.checksum_symbols(["600036.SH", "000001.SZ"])
        b = sp_normalize.checksum_symbols(["000001.SZ", "600036.SH"])
        assert a == b
        assert len(a) == 16

    def test_is_valid_symbol_rejects_garbage(self):
        assert sp_normalize.is_valid_symbol("SH600036", "CN")
        assert not sp_normalize.is_valid_symbol("NOTACODE", "CN")
        assert not sp_normalize.is_valid_symbol("", "CN")

    def test_qlib_bridge_format(self):
        assert sp_normalize.normalize_to_qlib("600036.SH", "CN") == "sh600036"


# ---------------------------------------------------------------------------
# 内置池目录（回归护栏：必须与旧 UNIVERSE_MAP 完全一致）
# ---------------------------------------------------------------------------
class TestBuiltins:
    def test_preserves_legacy_universe_map(self):
        legacy = {
            "csi300": "000300.SH",
            "csi500": "000905.SH",
            "csi1000": "000852.SH",
            "sse50": "000016.SH",
            "gem": "399006.SZ",
            "star": "000688.SH",
            "csi800": "000906.SH",
            "all_a": None,
        }
        for code, index_symbol in legacy.items():
            assert code in sp_builtins.INDEX_SYMBOLS
            assert sp_builtins.INDEX_SYMBOLS[code] == index_symbol

    def test_covers_strategy_lab_whitelist(self):
        """SDK 白名单里的池必须都能在内置目录找到，消灭'SDK 能写、回测查空'。"""
        sdk_whitelist = {
            "csi300",
            "csi500",
            "csi800",
            "csi1000",
            "hs300_ext",
            "all_a",
            "hk_main",
            "us_sp500",
        }
        assert sdk_whitelist <= sp_builtins.BUILTIN_CODES

    def test_seed_rows_are_global_active_system_pools(self):
        rows = sp_builtins.seed_rows()
        assert len(rows) == len(sp_builtins.BUILTIN_POOLS)
        for row in rows:
            assert row["scope"] == "global"
            assert row["is_system"] is True
            assert row["pool_id"] == f"sys_{row['code']}"
            json.dumps(row)  # 必须可 JSON 序列化（入 SQL 参数）

    def test_get_builtin_is_case_insensitive(self):
        assert sp_builtins.get_builtin("CSI300") is sp_builtins.get_builtin("csi300")


# ---------------------------------------------------------------------------
# ref 解析（不触库的分支）
# ---------------------------------------------------------------------------
class TestResolverRefs:
    def test_all_means_unfiltered(self):
        snap = resolver.resolve_sync("all")
        assert snap.unfiltered is True
        assert snap.source == "all"
        assert snap.symbols == []

    def test_empty_ref_is_unfiltered(self):
        assert resolver.resolve_sync("").unfiltered is True
        assert resolver.resolve_sync(None).unfiltered is True

    def test_inline_list(self):
        snap = resolver.resolve_sync("list:SH600036,SZ000001;600519")
        assert snap.source == "inline"
        assert snap.symbols == ["600036.SH", "000001.SZ", "600519.SH"]
        assert snap.api_symbols == ["SH600036", "SZ000001", "SH600519"]
        assert snap.unfiltered is False

    def test_file_ref_txt(self, tmp_path):
        pool_file = tmp_path / "my_pool.txt"
        pool_file.write_text("sh600036\n000001.SZ\n", encoding="utf-8")
        snap = resolver.resolve_sync(f"file:{pool_file}")
        assert snap.source == "file"
        assert snap.symbols == ["600036.SH", "000001.SZ"]

    def test_file_ref_csv_with_header(self, tmp_path):
        pool_file = tmp_path / "p.csv"
        pool_file.write_text(
            "symbol,name\n600036.SH,招商银行\n000001.SZ,平安银行\n", encoding="utf-8"
        )
        snap = resolver.resolve_sync(f"file:{pool_file}")
        assert snap.symbols == ["600036.SH", "000001.SZ"]

    def test_bare_path_is_treated_as_file(self, tmp_path):
        pool_file = tmp_path / "bare.txt"
        pool_file.write_text("600036.SH\n", encoding="utf-8")
        snap = resolver.resolve_sync(str(pool_file))
        assert snap.source == "file"
        assert snap.symbols == ["600036.SH"]

    def test_missing_file_warns_but_does_not_raise(self, tmp_path):
        snap = resolver.resolve_sync(f"file:{tmp_path / 'nope.txt'}")
        assert snap.symbols == []
        assert any("不存在" in w for w in snap.warnings)

    def test_cos_ref_is_delegated_with_warning(self):
        snap = resolver.resolve_sync("cos://bucket/pool/x.txt")
        assert snap.source == "unsupported"
        assert any("cos://" in w for w in snap.warnings)

    def test_user_pool_ref_is_delegated_with_warning(self):
        snap = resolver.resolve_sync("user_pool:some_key")
        assert snap.source == "unsupported"
        assert snap.warnings

    def test_market_override_from_context(self):
        snap = resolver.resolve_sync("list:aapl,msft", ResolveContext(market="US"))
        assert snap.market == "US"
        assert snap.symbols == ["AAPL", "MSFT"]


# ---------------------------------------------------------------------------
# 物化
# ---------------------------------------------------------------------------
class TestPoolTxt:
    """v2：成员 TXT 是唯一事实源，读写行为必须锁死。"""

    def test_write_read_roundtrip_dedupes_and_keeps_order(self, tmp_path):
        p = tmp_path / "p.txt"
        n = write_pool_txt(p, ["SH600036", "SZ000001", "SH600036"])
        assert n == 2  # 去重
        assert read_pool_txt(p) == ["SH600036", "SZ000001"]

    def test_comments_and_empty_lines_ignored(self, tmp_path):
        p = tmp_path / "p.txt"
        p.write_text("# generated\nSH600036\n\n  \n# tail\nSZ000001\n", encoding="utf-8")
        assert read_pool_txt(p) == ["SH600036", "SZ000001"]

    def test_line_with_comma_takes_first_cell(self, tmp_path):
        """用户手改时粘进 csv 行也要容错（取第一格）。"""
        p = tmp_path / "p.txt"
        p.write_text("SH600036,贵州茅台\n", encoding="utf-8")
        assert read_pool_txt(p) == ["SH600036"]

    def test_missing_file_reads_empty_not_raise(self, tmp_path):
        assert read_pool_txt(tmp_path / "nope.txt") == []

    def test_atomic_write_leaves_no_tmp(self, tmp_path):
        p = tmp_path / "p.txt"
        write_pool_txt(p, ["SH600036"])
        assert list(tmp_path.glob("*.tmp")) == []
        assert p.read_text(encoding="utf-8").splitlines()[0].startswith("#")

    def test_read_instruments_file_qlib_format_takes_first_column(self, tmp_path):
        """Qlib 标准格式 `sym\\tSTART\\tEND` 必须只取首列（整行当代码会卡死回测）."""
        from backend.shared.stock_pool.materializer import read_instruments_file

        p = tmp_path / "pool_x.txt"
        p.write_text(
            "# pool_x\nsh600036\t2016-01-04\t2026-09-02\n\nSZ000001\t2016-01-04\t2026-09-02\n",
            encoding="utf-8",
        )
        assert read_instruments_file(p) == ["sh600036", "SZ000001"]
        assert read_instruments_file(tmp_path / "missing.txt") == []

    def test_pool_txt_name_scoping(self):
        assert pool_txt_name("global", "csi300") == "csi300.txt"
        # 隔离靠子目录：文件名不再带归属前缀，同名不互撞靠目录
        assert pool_txt_name("user", "my", owner_user_id="42") == "my.txt"
        assert pool_txt_name("global", "a b/c").endswith("a_b_c.txt")

    def test_pool_txt_path_user_isolated_by_subdir(self, tmp_path, monkeypatch):
        from backend.shared.stock_pool.materializer import pool_subdir

        monkeypatch.setenv("QM_STOCK_POOL_TXT_DIR", str(tmp_path))
        assert pool_subdir("global") == ""
        assert pool_subdir("user", owner_user_id="00000001") == "u00000001"
        p1 = pool_txt_path("user", "my", owner_user_id="00000001")
        p2 = pool_txt_path("user", "my", owner_user_id="00000002")
        assert p1 == str(tmp_path / "u00000001" / "my.txt")
        assert p2 == str(tmp_path / "u00000002" / "my.txt")
        assert p1 != p2
        # global 保持根目录扁平
        assert pool_txt_path("global", "csi300") == str(tmp_path / "csi300.txt")

    def test_resolve_pool_txt_prefers_new_and_falls_back_to_legacy(
        self, tmp_path, monkeypatch
    ):
        from backend.shared.stock_pool.materializer import (
            legacy_pool_txt_path,
            resolve_pool_txt,
        )

        monkeypatch.setenv("QM_STOCK_POOL_TXT_DIR", str(tmp_path))
        # 旧扁平文件读兼容
        legacy = legacy_pool_txt_path("user", "my", owner_user_id="7")
        assert legacy == str(tmp_path / "u7_my.txt")
        Path(legacy).write_text("#x\nSH600036\n", encoding="utf-8")
        assert resolve_pool_txt("user", "my", owner_user_id="7") == legacy
        # 新隔离目录存在时优先
        new = pool_txt_path("user", "my", owner_user_id="7")
        Path(new).parent.mkdir(parents=True, exist_ok=True)
        Path(new).write_text("#x\nSH600519\n", encoding="utf-8")
        assert resolve_pool_txt("user", "my", owner_user_id="7") == new
        # 都不存在时返回新位置（默认写入位）
        assert resolve_pool_txt("user", "nope", owner_user_id="7") == pool_txt_path(
            "user", "nope", owner_user_id="7"
        )

    def test_pool_txt_path_uses_env_dir(self, tmp_path, monkeypatch):
        monkeypatch.setenv("QM_STOCK_POOL_TXT_DIR", str(tmp_path))
        assert pool_txt_path("global", "csi300") == str(tmp_path / "csi300.txt")


class TestMaterializer:
    def test_write_and_read_instruments(self, tmp_path, monkeypatch):
        monkeypatch.setenv("QLIB_PROVIDER_URI", str(tmp_path))
        path = write_instruments(
            "my_pool",
            ["600036.SH", "000001.SZ"],
            "CN",
            start_date="2024-01-01",
            end_date="2024-12-31",
        )
        assert path is not None
        assert path.endswith("instruments\\pool_my_pool.txt") or path.endswith(
            "instruments/pool_my_pool.txt"
        )
        assert read_instruments("my_pool") == ["sh600036", "sz000001"]

    def test_empty_symbols_skips_write(self, tmp_path, monkeypatch):
        monkeypatch.setenv("QLIB_PROVIDER_URI", str(tmp_path))
        assert write_instruments("empty_pool", [], "CN") is None


# ---------------------------------------------------------------------------
# 成分提供方契约（回归护栏）
# ---------------------------------------------------------------------------
class TestIndexProviderContract:
    """QuantDB 的口径是「池名」（csi300 / all_a），不是指数代码。

    契约：CN 市场必须把 pool_code 传给 provider，否则
    `fetch_universe_stocks('000300.SH')` 会静默返回空。

    注：直接调用 `_from_builtin` 而非 `resolve_sync(code)`，
    因为后者会先查库（DB 是元信息 SSOT，内置目录是兜底），单测无数据库。
    内置池解析成功后会**自愈写成员 TXT**，测试目录用 env 隔离。
    """

    @pytest.fixture(autouse=True)
    def _isolate_pool_dir(self, tmp_path, monkeypatch):
        monkeypatch.setenv("QM_STOCK_POOL_TXT_DIR", str(tmp_path / "pools"))

    def teardown_method(self):
        register_index_provider(None)

    def _builtin(self, code: str):
        builtin = sp_builtins.get_builtin(code)
        assert builtin is not None
        return builtin

    def test_cn_builtin_passes_pool_code(self, tmp_path):
        seen: list[tuple] = []

        def fake_provider(market, index_symbol, pool_code):
            seen.append((market, index_symbol, pool_code))
            return ["SH600036"]

        register_index_provider(fake_provider)
        snap = resolver._from_builtin(self._builtin("csi300"), ResolveContext())

        assert seen == [("CN", "000300.SH", "csi300")]
        assert snap.symbols == ["600036.SH"]
        # 自愈：成员写成前缀式 TXT（唯一事实源）
        txt = tmp_path / "pools" / "csi300.txt"
        assert txt.exists()
        assert read_pool_txt(txt) == ["SH600036"]

    def test_all_a_builtin_passes_pool_code_without_index_symbol(self):
        seen: list[tuple] = []

        def fake_provider(market, index_symbol, pool_code):
            seen.append((market, index_symbol, pool_code))
            return ["SZ000001"]

        register_index_provider(fake_provider)
        snap = resolver._from_builtin(self._builtin("all_a"), ResolveContext())

        assert seen == [("CN", None, "all_a")]
        assert snap.symbols == ["000001.SZ"]

    def test_provider_failure_degrades_to_warning(self):
        def boom(market, index_symbol, pool_code):
            raise RuntimeError("quantdb down")

        register_index_provider(boom)
        snap = resolver._from_builtin(self._builtin("csi300"), ResolveContext())

        assert snap.symbols == []
        assert snap.warnings

    def test_optional_source_builtin_labels_missing_source(self):
        register_index_provider(lambda market, index_symbol, pool_code: [])
        snap = resolver._from_builtin(self._builtin("hk_main"), ResolveContext())

        assert snap.symbols == []
        assert any("未就绪" in w or "未接入" in w for w in snap.warnings)


# ---------------------------------------------------------------------------
# 上传解析（与 stocks_index.json 对比）
# ---------------------------------------------------------------------------
def _fixture_index():
    """构造小索引，保证解析测试与真实数据文件解耦。"""
    items = [
        ("600519.SH", "600519", "SH", "贵州茅台"),
        ("600036.SH", "600036", "SH", "招商银行"),
        ("000001.SZ", "000001", "SZ", "平安银行"),
        ("000002.SZ", "000002", "SZ", "万 科Ａ"),
        ("000010.SZ", "000010", "SZ", "*ST美丽"),
        ("300750.SZ", "300750", "SZ", "宁德时代"),
    ]
    entries = [
        sp_parser.IndexEntry(
            symbol=sym,
            code=code,
            exchange=ex,
            name=name,
            name_norm=sp_parser.normalize_name(name),
        )
        for sym, code, ex, name in items
    ]
    return sp_parser.StockIndex(entries)


class TestParseEngine:
    def _parse(self, content: str, **kw):
        kw.setdefault("fmt", "txt")
        return sp_parser.parse_stock_list(content, index=_fixture_index(), **kw)

    def test_all_code_formats_match(self):
        rep = self._parse("600519\nSH600036\n000001.SZ\nsz300750\n")
        assert rep.symbols == ["600519.SH", "600036.SH", "000001.SZ", "300750.SZ"]
        assert [r.match_type for r in rep.rows] == [
            "code",
            "prefix",
            "symbol",
            "prefix",
        ]

    def test_wrong_exchange_prefix_is_corrected(self):
        # 300750 是深市；用户写成 sh300750 / 300750.SH 时按代码纠正并告警
        rep = self._parse("sh300750\n300750.SH\n")
        assert rep.symbols == ["300750.SZ"]
        assert rep.duplicates == 1
        assert all(r.match_type == "exchange_fixed" for r in rep.rows)
        assert any("交易所前缀有误" in w for w in rep.warnings)

    def test_name_matching_normalizes_fullwidth_and_spaces(self):
        rep = self._parse("贵州茅台\n万 科Ａ\n万科A\n")
        # "万 科Ａ" 与 "万科A" 归一化后同一条 → 第 3 行判重
        assert rep.symbols == ["600519.SH", "000002.SZ"]
        assert rep.duplicates == 1
        assert rep.rows[1].match_type == "name"

    def test_loose_name_match_strips_st_prefix(self):
        rep = self._parse("美丽\n")
        assert rep.symbols == ["000010.SZ"]
        assert rep.rows[0].match_type == "name_loose"

    def test_unknown_code_is_not_in_index_not_unrecognized(self):
        rep = self._parse("830799\nBJ430047\n999999\n")
        assert rep.symbols == []
        assert rep.unmatched == 3
        assert rep.not_in_index == 3
        assert all(r.status == "not_in_index" for r in rep.rows)
        assert any("北交所" in w for w in rep.warnings)

    def test_garbage_is_unrecognized(self):
        rep = self._parse("abc\n12345\n!!!\n")
        assert rep.unmatched == 3
        assert rep.not_in_index == 0
        assert all(r.status == "unrecognized" for r in rep.rows)

    def test_duplicate_detection_across_formats(self):
        rep = self._parse("600519\nSH600519\n贵州茅台\n")
        assert rep.symbols == ["600519.SH"]
        assert rep.matched == 1
        assert rep.duplicates == 2
        assert rep.total == rep.matched + rep.unmatched + rep.duplicates

    def test_csv_header_skipped_and_any_column_order(self):
        content = "name,code\n贵州茅台,600519\n300750,宁德时代\n"
        rep = self._parse(content, fmt="csv", has_header=True)
        assert rep.total == 2
        assert rep.symbols == ["600519.SH", "300750.SZ"]
        assert rep.rows[0].match_type == "name"
        assert rep.rows[1].match_type == "code"

    def test_repeated_header_row_skipped(self):
        rep = self._parse("600519\nsymbol\n600036\n", fmt="txt")
        assert rep.total == 2

    def test_forced_column(self):
        content = "a,b\nzzz,600519\n"
        rep = self._parse(content, fmt="csv", has_header=True, column="b")
        assert rep.symbols == ["600519.SH"]
        rep2 = self._parse(content, fmt="csv", has_header=True, column="1")
        assert rep2.symbols == ["600519.SH"]

    def test_comments_ignored_in_txt(self):
        rep = self._parse("# 我的自选\n600519\n// 备用\n600036\n")
        assert rep.total == 2
        assert len(rep.symbols) == 2

    def test_members_carry_canonical_name_not_raw_token(self):
        rep = self._parse("600519\n")
        assert rep.members == [
            {
                "symbol": "600519.SH",
                "api_symbol": "SH600519",
                "name": "贵州茅台",
                "meta": {"source_token": "600519", "match_type": "code"},
            }
        ]

    def test_gbk_bytes_decoded(self):
        raw = "股票代码\n600519\n".encode("gbk")
        rep = sp_parser.parse_upload(raw, fmt="txt")
        assert rep.encoding in ("gb18030", "gbk")
        assert rep.total == 1

    def test_as_dict_row_limit_marks_truncated(self):
        rep = self._parse("\n".join(["600519", "600036", "000001"]))
        data = rep.as_dict(row_limit=2)
        assert len(data["rows"]) == 2
        assert data["truncated"] is True
        assert data["summary"]["unique_symbols"] == 3

    def test_empty_index_warns(self):
        rep = sp_parser.parse_stock_list(
            "600519\n", fmt="txt", index=sp_parser.StockIndex([])
        )
        assert rep.symbols == []
        assert any("索引为空" in w for w in rep.warnings)

    def test_validate_symbols_helper(self):
        idx = _fixture_index()
        assert sp_parser.validate_symbols(["SH600519", "600519"], idx) == ["600519.SH"]
        assert sp_parser.validate_symbols(["nope"], idx) == []


class TestParseAgainstRealIndex:
    """真实索引冒烟（文件缺失时跳过）。"""

    def test_real_index_is_loaded_and_consistent(self):
        path = sp_parser.resolve_index_path()
        if path is None:
            pytest.skip("data/stocks/stocks_index.json 不存在")

        idx = sp_parser.load_stock_index(force=True)
        assert len(idx) > 5000

        # 名称唯一是名称匹配无歧义的前提，索引变更时要能被发现
        names = [e.name_norm for e in idx.entries]
        assert len(names) == len(set(names)), "索引出现重名，名称匹配会歧义"

        rep = sp_parser.parse_stock_list("600519\n贵州茅台\n", fmt="txt", index=idx)
        assert rep.symbols == ["600519.SH"]
        assert rep.duplicates == 1


# ---------------------------------------------------------------------------
# P2：读路径单一事实源（消灭三份重复白名单）
# ---------------------------------------------------------------------------
class TestP2SingleSourceOfTruth:
    """改造前系统里有三份互不一致的股票池白名单：

    - `quantdb_hub.UNIVERSE_MAP`（8 个 CN 池）
    - `strategy_lab/sdk/context._ALLOWED_UNIVERSES`（少 sse50/gem/star，多 hs300_ext/hk_main/us_sp500）
    - `routers/alpha_agent` 内硬编码的 valid_universes（8 个 CN 池）

    现在三者都从 `builtins` 派生。本类锁住这个事实。
    """

    def test_quantdb_hub_universe_map_derives_from_builtins(self):
        from backend.services.engine.data_platform.quantdb_hub import QuantDBDataHub

        assert QuantDBDataHub.UNIVERSE_MAP == sp_builtins.cn_index_symbols()
        assert QuantDBDataHub.UNIVERSE_NAMES == sp_builtins.cn_index_names()

    def test_cn_index_symbols_equals_historical_universe_map(self):
        """派生视图必须与 P2 之前硬编码的 8 条逐条一致（行为不变）。"""
        legacy = {
            "csi300": "000300.SH",
            "csi500": "000905.SH",
            "csi1000": "000852.SH",
            "sse50": "000016.SH",
            "gem": "399006.SZ",
            "star": "000688.SH",
            "csi800": "000906.SH",
            "all_a": None,
        }
        assert sp_builtins.cn_index_symbols() == legacy
        assert sp_builtins.cn_index_names() == {
            "csi300": "沪深300",
            "csi500": "中证500",
            "csi1000": "中证1000",
            "sse50": "上证50",
            "gem": "创业板",
            "star": "科创板",
            "csi800": "中证800",
            "all_a": "全部A股",
        }

    def test_optional_source_pools_excluded_from_cn_index_view(self):
        """hs300_ext 没有指数权重来源，不能进 UNIVERSE_MAP（否则会物化空文件）。"""
        assert "hs300_ext" not in sp_builtins.cn_index_symbols()
        assert "hk_main" not in sp_builtins.cn_index_symbols()
        assert "us_sp500" not in sp_builtins.cn_index_symbols()
        # 但它仍在内置池目录里（SDK 可写，解析时会给出明确告警）
        assert "hs300_ext" in sp_builtins.BUILTIN_CODES

    def test_strategy_lab_whitelist_derives_from_builtins(self):
        from backend.services.engine.strategy_lab.sdk.context import (
            _ALLOWED_UNIVERSES,
        )

        assert _ALLOWED_UNIVERSES == sp_builtins.BUILTIN_CODES
        # 改造前 SDK 缺 sse50/gem/star（回测却支持），现在补齐
        assert {"sse50", "gem", "star"} <= _ALLOWED_UNIVERSES

    def test_no_duplicated_universe_whitelist_in_engine(self):
        """源码级护栏：引擎里不得再出现硬编码的股票池白名单。"""
        from pathlib import Path

        backend_root = Path(__file__).resolve().parents[1]
        needle = '"csi300", "csi500", "csi1000", "sse50", "gem", "star", "csi800", "all_a"'
        offenders: list[str] = []
        for path in (backend_root / "services" / "engine").rglob("*.py"):
            if "__pycache__" in path.parts:
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
            if needle in text:
                offenders.append(str(path.relative_to(backend_root)))

        assert not offenders, f"发现重复的股票池白名单硬编码: {offenders}"

    def test_qlib_data_builder_uses_builtins_not_hub_attribute(self):
        """源码级护栏：qlib_data_builder 只物化 CN 池，不能再读 hub.UNIVERSE_MAP。"""
        from pathlib import Path

        src = (
            Path(__file__).resolve().parents[1]
            / "services"
            / "engine"
            / "qlib_data_builder.py"
        ).read_text(encoding="utf-8")

        assert "cn_index_symbols()" in src
        assert 'getattr(self._hub, "UNIVERSE_MAP"' not in src


# ---------------------------------------------------------------------------
# P3-1：回测消费方接入
# ---------------------------------------------------------------------------
class TestP3BacktestBridge:
    """回测通过 `pool_id` 消费全局股票池。

    关键约束：**空池必须显式失败**，不能静默退化成全市场
    （改造前 `universe` 解析不出来就是这样悄悄跑全市场的）。
    """

    def _snapshot(self, **kw):
        from backend.shared.stock_pool.schemas import PoolSnapshot

        base = {"pool_id": "sp_x", "code": "my_pool", "market": "CN"}
        base.update(kw)
        return PoolSnapshot(**base)

    def test_unfiltered_snapshot_materializes_to_none(self):
        snap = self._snapshot(pool_id="all", code="all", unfiltered=True)
        assert materialize_snapshot(snap) is None

    def test_empty_snapshot_materializes_to_none(self):
        snap = self._snapshot(symbols=[], api_symbols=[])
        assert snap.is_empty is True
        assert materialize_snapshot(snap) is None

    def test_normal_snapshot_writes_instruments_file(self, tmp_path, monkeypatch):
        monkeypatch.setenv("QLIB_PROVIDER_URI", str(tmp_path))
        snap = self._snapshot(
            symbols=["600036.SH", "000001.SZ"],
            api_symbols=["SH600036", "SZ000001"],
            checksum="abc123",
        )
        path = materialize_snapshot(snap, start_date="2024-01-01", end_date="2024-12-31")

        assert path is not None
        # 池文件必须与 Qlib 原生池区分开，避免覆盖 csi300.txt
        assert path.endswith("pool_my_pool.txt")
        assert read_instruments("my_pool") == ["sh600036", "sz000001"]

    def test_pool_code_is_sanitized_for_filename(self, tmp_path, monkeypatch):
        monkeypatch.setenv("QLIB_PROVIDER_URI", str(tmp_path))
        snap = self._snapshot(code="pool:weird/name", symbols=["600036.SH"])
        path = materialize_snapshot(snap)
        assert path is not None
        assert "/" not in path.rsplit("instruments", 1)[1].replace("\\", "/").lstrip("/")

    def test_backtest_request_schema_carries_pool_fields(self):
        """`api/backtest.py` 用 `request.dict()` 生成持久化 config，
        所以池字段加在 schema 上就会自动落库（可复现性）。"""
        from backend.services.engine.qlib_app.schemas.backtest import (
            QlibBacktestRequest,
        )

        req = QlibBacktestRequest(pool_id="pool:csi300")
        assert req.pool_id == "pool:csi300"
        assert req.universe == "all"  # 默认不动，由运行时覆盖

        req.pool_checksum = "deadbeef"
        req.pool_warnings = ["索引为空"]
        dumped = req.dict()
        assert dumped["pool_id"] == "pool:csi300"
        assert dumped["pool_checksum"] == "deadbeef"
        assert dumped["pool_warnings"] == ["索引为空"]


# ---------------------------------------------------------------------------
# P3-2：信号池过滤（推理 / 模拟盘 / 实盘共用）
# ---------------------------------------------------------------------------
class TestPoolSignalFilter:
    """严格语义是这套东西的全部意义，必须锁死。

    对照：`script_runner.execute(symbols=...)` 的单股补推是「未命中则保留全量」，
    那是有意为之；池过滤**绝不能**沿用那个兜底，否则又是静默退化。
    """

    def _snapshot(self, symbols, api_symbols, **kw):
        from backend.shared.stock_pool.schemas import PoolSnapshot

        base = {
            "pool_id": "sp_x",
            "code": "my_pool",
            "market": "CN",
            "checksum": "cafe",
            "symbols": symbols,
            "api_symbols": api_symbols,
        }
        base.update(kw)
        return PoolSnapshot(**base)

    def _signals(self, *syms):
        return [{"symbol": s, "score": 1.0 - i * 0.1} for i, s in enumerate(syms)]

    def test_keeps_only_pool_members(self):
        snap = self._snapshot(["600036.SH", "000001.SZ"], ["SH600036", "SZ000001"])
        out = filter_signals_by_pool(
            self._signals("SH600036", "SZ000002", "SH601318"), snap
        )
        assert [s["symbol"] for s in out.kept] == ["SH600036"]
        assert out.dropped == 2
        assert out.applied is True
        assert out.empty_result is False
        # 校验和透传，供上层落库做可复现
        assert out.pool_checksum == "cafe"

    def test_suffix_and_prefix_both_match(self):
        snap = self._snapshot(["600036.SH"], ["SH600036"])
        out = filter_signals_by_pool(self._signals("600036.SH", "SH600036"), snap)
        assert len(out.kept) == 2

    def test_unfiltered_pool_passes_everything(self):
        snap = self._snapshot([], [], pool_id="all", code="all", unfiltered=True)
        out = filter_signals_by_pool(self._signals("SH600036", "SZ000001"), snap)
        assert len(out.kept) == 2
        assert out.unfiltered is True
        assert out.applied is False

    def test_empty_pool_is_flagged_not_treated_as_unfiltered(self):
        """空池必须被标记为 empty_pool，而不是当成『不过滤』。"""
        snap = self._snapshot([], [])
        out = filter_signals_by_pool(self._signals("SH600036"), snap)
        assert out.empty_pool is True
        assert out.kept == []
        assert out.unfiltered is False
        assert any("空池" in w for w in out.warnings)

    def test_zero_hit_is_flagged_empty_result(self):
        """池非空但零命中 —— 同样要显式标记，不能被当成『池是空的』。"""
        snap = self._snapshot(["600036.SH"], ["SH600036"])
        out = filter_signals_by_pool(self._signals("SZ000002", "SH601318"), snap)
        assert out.empty_result is True
        assert out.empty_pool is False
        assert out.kept == []
        assert out.dropped == 2

    def test_none_snapshot_is_unfiltered_with_warning(self):
        out = filter_signals_by_pool(self._signals("SH600036"), None)
        assert out.unfiltered is True
        assert len(out.kept) == 1
        assert out.warnings

    def test_never_raises(self):
        """失败策略交给调用方，过滤器本身不抛异常。"""
        for sigs, snap in [
            (None, None),
            ([], self._snapshot([], [])),
            (self._signals("X"), self._snapshot(["600036.SH"], ["SH600036"])),
        ]:
            filter_signals_by_pool(sigs, snap)  # 不抛即通过

    def test_as_dict_is_serializable(self):
        import json

        snap = self._snapshot(["600036.SH"], ["SH600036"])
        out = filter_signals_by_pool(self._signals("SH600036"), snap)
        json.dumps(out.as_dict())  # 必须可 JSON 序列化（要进结果/日志）

    def test_intersect_symbols(self):
        assert filter_intersect(["SH600036", "SZ000001"], ["600036.SH"]) == ["SH600036"]
        assert filter_intersect(["SH600036"], None) == ["SH600036"]
        assert filter_intersect(None, ["600036.SH"]) == ["SH600036"]
        assert filter_intersect(["SH600036"], ["SZ000001"]) == []


class TestP3InferenceWiring:
    """推理链路接入的静态护栏（完整链路需 qlib/DB，跑不了单测）。"""

    def _src(self, rel: str) -> str:
        from pathlib import Path

        return (Path(__file__).resolve().parents[1] / rel).read_text(encoding="utf-8")

    def test_script_runner_has_pool_filter_with_loud_failure(self):
        src = self._src("services/engine/inference/script_runner.py")
        assert "def execute(" in src
        assert "pool_id: str | None = None" in src
        # 必须严格失败，不能保留全量信号
        assert 'failure_stage="pool_filter"' in src
        # execute 是同步函数，必须用 resolve_sync（await 会直接语法错误）
        assert "pool_resolver.resolve_sync(" in src
        assert "await pool_resolver" not in src

    def test_router_service_forwards_pool_id_to_runner(self):
        src = self._src("services/engine/inference/router_service.py")
        assert "pool_id: str | None = None," in src
        # 系统内置 model_qlib/alpha158 兜底链已下线（见 router_service
        # 「用户模型失败时不再补位」），推理只剩 runner.execute 单条路径；
        # pool_id 必须透传给 script_runner 做严格池过滤，漏传=全市场信号混入
        assert "runner.execute(" in src
        assert src.count("pool_id=pool_id") >= 1

    def test_user_api_exposes_pool_id(self):
        src = self._src("services/api/routers/model_training.py")
        assert "class InferenceRunRequest" in src
        assert "pool_id: str | None = Field(" in src
        assert "pool_id=pool_id," in src
        assert "pool_id=getattr(payload" in src


# ---------------------------------------------------------------------------
# P3-3：训练消费方接入
# ---------------------------------------------------------------------------
class TestP3TrainingBridge:
    """训练池解析在**编排器侧**完成（容器内没有 DB）。"""

    def test_no_pool_id_returns_empty_dict(self):
        from backend.services.engine.training.pool_binding import (
            resolve_training_pool,
        )

        # 未指定池 → 空 dict（保持旧行为：全市场训练），且不触库
        assert resolve_training_pool(None) == {}
        assert resolve_training_pool({}) == {}
        assert resolve_training_pool({"pool_id": "   "}) == {}

    def test_data_cfg_carries_pool_fields(self):
        from backend.shared.training.schemas import DataCfg

        cfg = DataCfg(
            pool_id="pool:csi300",
            pool_symbols=["600036.SH", "000001.SZ"],
            pool_checksum="abc",
        )
        dumped = cfg.model_dump()
        assert dumped["pool_id"] == "pool:csi300"
        assert dumped["pool_symbols"] == ["600036.SH", "000001.SZ"]
        assert dumped["pool_checksum"] == "abc"

    def test_data_cfg_defaults_have_no_pool(self):
        from backend.shared.training.schemas import DataCfg

        cfg = DataCfg()
        assert cfg.pool_id is None
        assert cfg.pool_symbols is None

    def test_both_orchestrators_resolve_pool(self):
        from pathlib import Path

        root = Path(__file__).resolve().parents[1] / "services" / "engine" / "training"
        local = (root / "local_docker_orchestrator.py").read_text(encoding="utf-8")
        remote = (root / "remote_ssh_orchestrator.py").read_text(encoding="utf-8")

        assert "resolve_training_pool" in local
        assert "resolve_training_pool" in remote
        # 必须真的注入进 DataCfg / config["data"]
        assert "**_training_pool_fields(payload)" in local
        assert "**resolve_training_pool(payload)" in remote

    def test_container_filters_by_pool_with_loud_failure(self):
        from pathlib import Path

        docker_root = Path(__file__).resolve().parents[2] / "docker" / "training"
        loading = (docker_root / "data" / "loading.py").read_text(encoding="utf-8")
        train_py = (docker_root / "train.py").read_text(encoding="utf-8")

        # load_data 接收池成分并在 symbol 归一化之后过滤
        assert "pool_symbols: list[str] | None = None," in loading
        assert "isin(wanted)" in loading
        # 池内零命中必须报错，不能训出「以为是池内、实际全市场」的模型
        assert "股票池过滤后无数据" in loading
        # train.py 必须把 config.yaml 里的池成分透传给 load_data
        assert 'get("pool_symbols")' in train_py

    def test_pool_filter_placed_after_symbol_normalization(self):
        """过滤必须在 symbol zfill 之后，否则 6 位补零对不上。"""
        from pathlib import Path

        loading = (
            Path(__file__).resolve().parents[2]
            / "docker"
            / "training"
            / "data"
            / "loading.py"
        ).read_text(encoding="utf-8")

        norm_idx = loading.index(".str.zfill(6)")
        pool_idx = loading.index("pool_symbols:")
        filter_idx = loading.index("isin(wanted)")
        assert norm_idx < filter_idx
        assert pool_idx < filter_idx

    def test_pool_filter_applies_to_all_branches(self):
        """函数层级必须有一处池过滤（4 空格缩进），否则直读因子源分支会静默训成全市场。
        直读分支内的尽早过滤（8 空格）是内存优化，不计入。"""
        from pathlib import Path

        lines = (
            Path(__file__).resolve().parents[2]
            / "docker"
            / "training"
            / "data"
            / "loading.py"
        ).read_text(encoding="utf-8").splitlines()
        top_level = [
            line
            for line in lines
            if line.startswith("    if pool_symbols") and not line.startswith("     ")
        ]
        assert top_level, "missing function-level pool filter"

    def test_pool_early_filter_in_direct_branch(self):
        """直读分支读后必须尽早过滤，否则 114 列×1053 万行在展开期 OOM（137）。"""
        from pathlib import Path

        loading = (
            Path(__file__).resolve().parents[2]
            / "docker"
            / "training"
            / "data"
            / "loading.py"
        ).read_text(encoding="utf-8")
        assert "After early pool filter" in loading
        assert "_to_prefix_symbol" in loading

    def test_bj_filter_applies_to_all_branches(self):
        """北交所过滤必须在函数层级（CN 常开），直读分支不再漏进训练集。"""
        from pathlib import Path

        lines = (
            Path(__file__).resolve().parents[2]
            / "docker"
            / "training"
            / "data"
            / "loading.py"
        ).read_text(encoding="utf-8").splitlines()
        top_level = [
            line
            for line in lines
            if "After BJ filter" in line and line.startswith("        logger")
        ]
        assert top_level, "missing function-level BJ filter"


# ---------------------------------------------------------------------------
# P3-4 / P3-5：模拟盘与实盘
# ---------------------------------------------------------------------------
class TestP3SimulationAndLiveBridge:
    def _src(self, rel: str) -> str:
        from pathlib import Path

        return (Path(__file__).resolve().parents[1] / rel).read_text(encoding="utf-8")

    def test_simulation_run_cycle_accepts_and_applies_pool(self):
        src = self._src("services/simulation/engine.py")
        assert "pool_id: str | None = None," in src
        assert "filter_signals_by_pool" in src
        # 池为空/零命中必须终止本轮，而不是放行全市场信号
        assert "report.error = f\"股票池过滤失败" in src
        # params_override 可能是非 dict，取值必须带类型守卫
        assert "isinstance(params_override, dict)" in src

    def test_live_trade_config_carries_pool_id(self):
        src = self._src("services/simulation/services/simulation_hosted_scheduler.py")
        assert '"pool_id": None,' in src  # 默认值
        assert 'merged["pool_id"] = str(merged.get("pool_id") or "").strip() or None' in src

    def test_live_signal_loading_filters_by_pool(self):
        src = self._src("services/live_trading/services/manual_execution_service.py")
        # 原始查询保持不动，新增一层带池过滤的包装（含 pred.parquet 回退路径）
        assert "async def _load_signal_rows_raw(" in src
        assert "async def _load_signal_rows(" in src
        assert "filter_signals_by_pool" in src
        # 池为空/零命中 → 返回空列表（实盘不下单），不退化成全市场
        assert "拒绝下单" in src
        # 三个调用点都要带上池
        assert src.count("pool_id=_resolve_pool_id_from_prepared(prepared)") == 3

    def test_live_pool_resolution_priority(self):
        """live_trade_config.pool_id 优先，其次 request_payload.pool_id。"""
        src = self._src("services/live_trading/services/manual_execution_service.py")
        assert "def _resolve_pool_id_from_prepared(" in src
        cfg_idx = src.index('strategy.get("live_trade_config")')
        payload_idx = src.index('str(payload.get("pool_id") or "").strip()')
        assert cfg_idx < payload_idx


# ---------------------------------------------------------------------------
# P5：策略运行环境（一行代码）+ 模拟盘 code 模式
# ---------------------------------------------------------------------------
class TestP5StrategyPool:
    """ctx.stock_pool 与模拟盘 code/signals 双模式的池支持。"""

    def _src(self, rel: str) -> str:
        from pathlib import Path

        return (Path(__file__).resolve().parents[1] / rel).read_text(encoding="utf-8")

    def test_ctx_accepts_stock_pool_key(self):
        from backend.services.engine.strategy_lab.sdk.context import Context

        ctx = Context()
        assert ctx.stock_pool is None
        ctx.stock_pool = "pool:csi1000"
        assert ctx.stock_pool == "pool:csi1000"
        assert ctx.to_config_dict()["stock_pool"] == "pool:csi1000"

    def test_ctx_rejects_bad_pool_ref(self):
        from backend.services.engine.strategy_lab.sdk.context import Context

        ctx = Context()
        with pytest.raises(ValueError):
            ctx.stock_pool = "??? bad ref !!!"

    def test_apply_pool_to_universe_inline(self):
        from backend.shared.stock_pool.strategy import apply_pool_to_universe

        out = apply_pool_to_universe(
            ["SH600036", "SZ000001", "SH600000"],
            "list:SH600036,SH600000",
            strict=True,
        )
        assert out.symbols == ["SH600036", "SH600000"]
        assert out.dropped == 1

    def test_apply_pool_empty_strict_fails(self):
        from backend.shared.stock_pool.strategy import apply_pool_to_universe

        with pytest.raises(ValueError):
            apply_pool_to_universe(["SH600036"], "list:", strict=True)

    def test_apply_pool_zero_intersect_strict_fails(self):
        from backend.shared.stock_pool.strategy import apply_pool_to_universe

        with pytest.raises(ValueError):
            apply_pool_to_universe(["SH600036"], "list:SZ000001", strict=True)

    def test_apply_pool_non_strict_keeps_base(self):
        from backend.shared.stock_pool.strategy import apply_pool_to_universe

        out = apply_pool_to_universe(["SH600036"], "list:", strict=False)
        assert out.symbols == ["SH600036"]
        assert out.warnings

    def test_apply_pool_all_passthrough(self):
        from backend.shared.stock_pool.strategy import apply_pool_to_universe

        out = apply_pool_to_universe(["SH600036"], "all", strict=True)
        assert out.symbols == ["SH600036"]

    def test_backtest_loop_applies_pool(self):
        src = self._src("services/engine/strategy_lab/engine/loop.py")
        assert "apply_pool_to_universe" in src
        assert "ctx" in src and "stock_pool" in src

    def test_replay_signals_mode_filters_by_pool(self):
        src = self._src("services/simulation/replay/signal_generator.py")
        assert "_apply_session_pool_filter" in src
        assert 'params.get("pool_id")' in src

    def test_replay_code_mode_wired(self):
        router_src = self._src("services/simulation/replay/router.py")
        assert 'mode: str = Field(' in router_src
        assert "code_runner.prepare_session" in router_src
        assert '"_strategy_code"' in router_src
        day_src = self._src("services/simulation/replay/day_runner.py")
        assert "code_runner.run_code_day" in day_src
        assert "OrderOrigin.CODE" in day_src


# ---------------------------------------------------------------------------
# P4：收敛治理
# ---------------------------------------------------------------------------
class TestP4Governance:
    """P4 轻量治理：让「被引用不可删」的守卫真正生效 + 旧池写侧统一。"""

    def _src(self, rel: str) -> str:
        from pathlib import Path

        return (Path(__file__).resolve().parents[1] / rel).read_text(encoding="utf-8")

    def test_binding_request_validates_enums(self):
        from pydantic import ValidationError

        from backend.shared.stock_pool.schemas import PoolBindingRequest

        ok = PoolBindingRequest(target_type="strategy", target_id="s1")
        assert ok.mode == "filter"
        assert ok.priority == 100
        # 全部合法 target_type 都要能构造
        for good in sorted(sp_const.TARGET_TYPES):
            PoolBindingRequest(target_type=good, target_id="x")

        for bad in ("bogus", "", "BACKTEST", "pools"):
            with pytest.raises(ValidationError):
                PoolBindingRequest(target_type=bad, target_id="x")

        with pytest.raises(ValidationError):
            PoolBindingRequest(target_type="strategy", target_id="")

        with pytest.raises(ValidationError):
            PoolBindingRequest(target_type="strategy", target_id="s1", mode="nope")

        with pytest.raises(ValidationError):
            PoolBindingRequest(target_type="strategy", target_id="s1", priority=-1)

    def test_admin_router_exposes_binding_write_endpoints(self):
        """没有写入口时 qm_stock_pool_binding 恒为空 → 引用守卫是空转的。"""
        src = self._src("services/api/routers/admin/stock_pool.py")
        assert '@router.post("/{pool_id}/bindings"' in src
        assert '@router.delete("/{pool_id}/bindings/{target_type}/{target_id:path}"' in src
        assert '@router.get("/bindings/by-target"' in src
        assert '@router.post("/bindings/reconcile"' in src
        # 归档/删除必须查引用
        assert "usages = await repo.list_usages(session, pool_id)" in src

    def test_repository_has_binding_helpers(self):
        from backend.shared.stock_pool import repository as repo

        for fn in (
            "bind_pool",
            "unbind_pool",
            "list_usages",
            "list_pools_for_target",
            "count_bindings",
        ):
            assert hasattr(repo, fn), f"缺少 {fn}"

    def test_legacy_pool_code_is_deterministic(self):
        from backend.shared.stock_pool.legacy_bridge import _legacy_pool_code

        a = _legacy_pool_code("我的自选池", "42")
        b = _legacy_pool_code("我的自选池", "42")
        assert a == b, "同名池重复保存（哪怕内容变了）必须命中同一条记录，直接覆盖成员 TXT"
        # 内容变化不再产生新 code：hash 不参与 code（历史 bug：同名池每次保存增殖一条）
        other_content = _legacy_pool_code("我的自选池", "42")
        assert a == other_content
        # 不同用户同名不互撞
        assert _legacy_pool_code("我的自选池", "42") != _legacy_pool_code(
            "我的自选池", "7"
        )
        assert a.startswith("legacy_")
        # 空名/纯符号也要有兜底
        assert _legacy_pool_code("", None).startswith("legacy_")
        assert _legacy_pool_code("!!!", None).startswith("legacy_")

    def test_legacy_bridge_scopes_pool_lookup_to_owner(self):
        """scope=user 的池必须按 owner 限定，否则不同用户同名池会互相写入。"""
        src = self._src("shared/stock_pool/legacy_bridge.py")
        assert "owner_user_id=str(user_id)" in src
        assert "scope=SCOPE_USER" in src

        repo_src = self._src("shared/stock_pool/repository.py")
        assert "owner_user_id: str | None = None," in repo_src
        assert "AND owner_user_id = :owner" in repo_src

    def test_legacy_save_registers_pool_best_effort(self):
        src = self._src("services/engine/ai_strategy/api/v1/storage.py")
        assert "register_legacy_pool_file" in src
        # 必须 best-effort：登记失败不能影响旧链路
        assert "旧池文件登记为一等池失败（旧链路不受影响）" in src
        # 旧链路本体（文件上传 + stock_pool_files + is_active）保持不动
        assert "uploader.upload_pool_file(" in src
        assert "is_active" in src

    def test_training_metadata_records_pool_for_reconcile(self):
        from pathlib import Path

        train_src = (
            Path(__file__).resolve().parents[2] / "docker" / "training" / "train.py"
        ).read_text(encoding="utf-8")
        assert "def _pool_metadata(cfg: dict) -> dict:" in train_src
        # 多模型与单模型两条 metadata 路径都要写入
        assert train_src.count("**_pool_metadata(cfg),") == 2
        # reconcile 依赖这两个键
        assert '"pool_id": pool_id or None,' in train_src
        assert '"pool_checksum"' in train_src

    def test_reconcile_targets_long_lived_references_only(self):
        """回测/推理是一次性运行，不应登记为 binding（否则池永不可删）。"""
        src = self._src("services/api/routers/admin/stock_pool.py")
        assert "qm_user_models" in src
        assert "sp_const.TARGET_TRAINING" in src
        # 明确排除一次性运行的说明必须留在代码里
        assert "一次性运行" in src

    def test_admin_routes_resolve_without_shadowing(self):
        """`/bindings/reconcile` 与 `/{pool_id}/bindings` 段数相同，必须验证不遮蔽。"""
        import importlib.util
        from pathlib import Path

        from starlette.routing import Match

        path = (
            Path(__file__).resolve().parents[1]
            / "services"
            / "api"
            / "routers"
            / "admin"
            / "stock_pool.py"
        )
        spec = importlib.util.spec_from_file_location("sp_admin_under_test", path)
        if spec is None or spec.loader is None:  # pragma: no cover
            pytest.skip("无法加载 admin stock_pool 模块")
        try:
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
        except ImportError as exc:  # pragma: no cover - 本地缺依赖时跳过
            pytest.skip(f"依赖缺失: {exc}")

        def resolve(target_path: str, method: str) -> str | None:
            for route in module.router.routes:
                if method not in getattr(route, "methods", set()):
                    continue
                match, _ = route.matches(
                    {"type": "http", "method": method, "path": target_path, "headers": []}
                )
                if match == Match.FULL:
                    return route.path
            return None

        expected = {
            ("POST", "/bindings/reconcile"): "/bindings/reconcile",
            ("GET", "/bindings/by-target"): "/bindings/by-target",
            ("POST", "/parse"): "/parse",
            ("POST", "/create-from-members"): "/create-from-members",
            ("POST", "/sp_x/bindings"): "/{pool_id}/bindings",
            ("GET", "/sp_x/usages"): "/{pool_id}/usages",
            ("POST", "/sp_x/refresh"): "/{pool_id}/refresh",
            ("GET", "/sp_x/members"): "/{pool_id}/members",
            ("PUT", "/sp_x/members"): "/{pool_id}/members",
        }
        for (method, url), want in expected.items():
            got = resolve(url, method)
            assert got == want, f"{method} {url} 解析到 {got}，期望 {want}"
