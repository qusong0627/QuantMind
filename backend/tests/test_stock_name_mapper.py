"""stock_name_mapper 归一化与解析测试（交易台中文名 enrichment 的底层口径）。

覆盖：
- normalize_symbol：裸码/前缀式/后缀式 → 统一后缀键；无法识别 → None；
- resolve_name：单例映射命中（任意层口径）与未收录回退空串。
"""

from backend.shared.stock_name_mapper import (
    get_stock_name_mapper,
    normalize_symbol,
    resolve_name,
)


class TestNormalizeSymbol:
    def test_bare_code_infers_exchange(self):
        assert normalize_symbol("600085") == "600085.SH"
        assert normalize_symbol("900901") == "900901.SH"  # B 股（9 开头，沪）
        assert normalize_symbol("000001") == "000001.SZ"
        assert normalize_symbol("002552") == "002552.SZ"
        assert normalize_symbol("300750") == "300750.SZ"
        assert normalize_symbol("430047") == "430047.BJ"
        assert normalize_symbol("833171") == "833171.BJ"

    def test_prefixed_code(self):
        assert normalize_symbol("SH600085") == "600085.SH"
        assert normalize_symbol("sz000001") == "000001.SZ"
        assert normalize_symbol("BJ430047") == "430047.BJ"

    def test_suffixed_code_case_insensitive(self):
        assert normalize_symbol("600085.SH") == "600085.SH"
        assert normalize_symbol("002552.sz") == "002552.SZ"
        assert normalize_symbol(" 000001.Sz ") == "000001.SZ"

    def test_unrecognized_returns_none(self):
        assert normalize_symbol("") is None
        assert normalize_symbol(None) is None
        assert normalize_symbol("ABC") is None
        assert normalize_symbol("60008") is None  # 5 位
        assert normalize_symbol("6000850") is None  # 7 位裸码
        assert normalize_symbol("12345.X") is None  # 未知交易所后缀
        assert normalize_symbol("500001") is None  # 5 开头不在 CN 推断表


class TestResolveName:
    def test_resolves_any_layer_symbol(self, monkeypatch):
        mapper = get_stock_name_mapper()
        monkeypatch.setattr(mapper, "_mapping", {"600085.SH": "同仁堂"}, raising=False)
        assert resolve_name("600085.SH") == "同仁堂"
        assert resolve_name("600085") == "同仁堂"
        assert resolve_name("SH600085") == "同仁堂"
        assert resolve_name("sh600085") == "同仁堂"

    def test_unknown_returns_empty_string(self, monkeypatch):
        mapper = get_stock_name_mapper()
        monkeypatch.setattr(mapper, "_mapping", {"600085.SH": "同仁堂"}, raising=False)
        assert resolve_name("999999") == ""
        assert resolve_name("ABC") == ""
        assert resolve_name("") == ""
