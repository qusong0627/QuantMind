import re
from typing import Optional

class StockCodeUtil:
    """股票代码标准化工具类（QuantDB 后缀口径为全系统唯一通用格式）。

    - 通用格式（后端 / 前端 / PG / Redis / API / 行情）: suffix 型 600036.SH，
      个股与指数统一（SH000300 -> 000300.SH）。新代码一律用 normalize()。
    - 唯一例外：Qlib 桥接用全小写 sh600036（to_qlib），仅在进出 Qlib 层边界转换。
    - to_prefix() 为过渡期兼容垫片（读老数据/老键时用），新写入禁止使用，
      待迁移脚本执行完毕后统一删除。
    - 层边界必须经本工具显式转换，禁止散落手写切片；
      suffix 与 prefix 混用查询会静默查空。
    """

    @staticmethod
    def normalize(code: str) -> str:
        """全系统正典归一化：任何输入 -> 后缀格式 600036.SH。

        等价于 to_suffix()，语义上是“唯一通用格式”入口。
        未知格式（港股/美股/指数期货等）原样返回（去空格大写），不硬判。
        """
        return StockCodeUtil.to_suffix(code)

    @staticmethod
    def to_suffix(code: str) -> str:
        """转换为 suffix 格式 600036.SH（QuantDB parquet / Qlib / 行情层口径）。

        Examples:
            - 'SH600000' -> '600000.SH'
            - 'sh600000' -> '600000.SH'
            - '600000' -> '600000.SH' (自动识别交易所)
            - 'BJ830001' -> '830001.BJ'
        """
        if not code:
            return ""

        code = str(code).upper().strip()

        # 1. 已经是正确的 Suffix 格式
        if re.match(r'^\d{6}\.(SH|SZ|BJ)$', code):
            return code

        # 2. 处理 Prefix 格式
        prefix_match = re.match(r'^(SH|SZ|BJ)(\d{6})$', code)
        if prefix_match:
            market, symbol = prefix_match.groups()
            return f"{symbol}.{market}"

        # 3. 纯 6 位数字，自动识别交易所
        digit_match = re.match(r'^(\d{6})$', code)
        if digit_match:
            symbol = digit_match.group(1)
            if symbol.startswith(('60', '68', '90')):
                return f"{symbol}.SH"
            elif symbol.startswith(('00', '30', '20')):
                return f"{symbol}.SZ"
            elif symbol.startswith(('83', '43', '87', '88', '92')):
                return f"{symbol}.BJ"
            return code

        return code

    @staticmethod
    def to_prefix(code: str) -> str:
        """转换为 prefix 格式 SH600000（过渡期兼容垫片，仅用于读老数据/老键）。

        新写入一律用 normalize()/to_suffix()。待后缀迁移脚本全量执行后删除。
        """
        if not code:
            return ""

        code = str(code).upper().strip()

        # 1. 已经是正确的 Prefix 格式
        if re.match(r'^(SH|SZ|BJ)\d{6}$', code):
            return code

        # 2. 处理 Suffix 格式
        suffix_match = re.match(r'^(\d{6})\.(SH|SZ|BJ)$', code)
        if suffix_match:
            symbol, market = suffix_match.groups()
            return f"{market}{symbol}"

        # 3. 处理带点但位置反了的情况
        rev_suffix_match = re.match(r'^(SH|SZ|BJ)\.(\d{6})$', code)
        if rev_suffix_match:
            market, symbol = rev_suffix_match.groups()
            return f"{market}{symbol}"

        # 4. 纯 6 位数字，自动识别交易所
        digit_match = re.match(r'^(\d{6})$', code)
        if digit_match:
            symbol = digit_match.group(1)
            if symbol.startswith(('60', '68', '90')):
                return f"SH{symbol}"
            elif symbol.startswith(('00', '30', '20')):
                return f"SZ{symbol}"
            elif symbol.startswith(('83', '43', '87', '88', '92')):
                return f"BJ{symbol}"
            return symbol

        return code

    @staticmethod
    def to_hk_suffix(code: str) -> str:
        """港股代码 → 后缀格式（4位+.HK；创业板8开头保留5位+.HK）。

        港股代码本为 5 位（HKEX 原始格式），但主板前导 0 是补位，实际有效
        位数为 4；创业板代码以 8 开头为真 5 位。为与日线/南向数据
        （0700.HK）一致，主板去前导 0 转 4 位+.HK，创业板保留 5 位+.HK。

        Examples:
            - '00700' -> '0700.HK'
            - '00001' -> '0001.HK'
            - '80001' -> '80001.HK' (创业板保留5位)
            - '0700.HK' -> '0700.HK' (已是后缀，原样返回)
        """
        if not code:
            return ""
        code = str(code).strip()
        if code.endswith(".HK"):
            return code
        code = code.zfill(5)
        if code.startswith("8"):
            return f"{code}.HK"
        stripped = code.lstrip("0")
        if not stripped:
            return "0000.HK"
        return f"{stripped.zfill(4)}.HK"

    @staticmethod
    def to_qlib(code: str) -> str:
        """转换为 Qlib 格式 sh600000 (仅用于 Qlib 迁移桥接)

        Examples:
            - '600036.SH' -> 'sh600036'
            - 'SH600036' -> 'sh600036'
            - '000001.SZ' -> 'sz000001'
        """
        suffix = StockCodeUtil.to_suffix(code)
        if "." in suffix:
            symbol, market = suffix.split(".")
            return f"{market.lower()}{symbol}"
        return code.lower()

    @staticmethod
    def split_prefix(code: str) -> tuple[str, str]:
        """拆分 prefix 格式为 (市场, 裸码)，替代散落的 s[:2]/s[2:] 切片。

        非标准输入返回 ("", 原值去空格后大写)，调用方自行决定回退策略，
        禁止再手写 s[:2]/s[2:]。

        Examples:
            - 'SH600000' -> ('SH', '600000')
            - '600000.SH' -> ('SH', '600000')
            - '600000' -> ('SH', '600000') (自动识别)
        """
        if not code:
            return ("", "")
        prefix = StockCodeUtil.to_prefix(code)
        m = re.match(r'^(SH|SZ|BJ)(\d{6})$', prefix)
        if m:
            return (m.group(1), m.group(2))
        return ("", str(code).strip().upper())

    @staticmethod
    def split_suffix(code: str) -> tuple[str, str]:
        """拆分 suffix 格式为 (裸码, 市场)，替代散落的 split('.') 手写切片。

        非标准输入返回 ("", 原值去空格后大写)，调用方自行决定回退策略。

        Examples:
            - '600000.SH' -> ('600000', 'SH')
            - 'SH600000' -> ('600000', 'SH')
        """
        if not code:
            return ("", "")
        suffix = StockCodeUtil.to_suffix(code)
        m = re.match(r'^(\d{6})\.(SH|SZ|BJ)$', suffix)
        if m:
            return (m.group(1), m.group(2))
        return ("", str(code).strip().upper())

    @staticmethod
    def normalize_list(codes: list[str]) -> list[str]:
        """批量标准化为 suffix 格式（全系统通用口径）"""
        return [StockCodeUtil.to_suffix(c) for c in codes if c]

    @staticmethod
    def exchange(code: str) -> str:
        """返回后缀口径代码的交易所（SH/SZ/BJ），非标准返回空串。"""
        canonical = StockCodeUtil.to_suffix(code)
        return (
            canonical.rsplit(".", 1)[1]
            if re.fullmatch(r"\d{6}\.(SH|SZ|BJ)", canonical)
            else ""
        )

    @staticmethod
    def digits(code: str) -> str:
        """返回后缀口径代码的 6 位裸码。"""
        return StockCodeUtil.to_suffix(code).split(".", 1)[0]

    @staticmethod
    def series_keys(code: str) -> list[str]:
        """行情时序 Redis 键（`market:series:<后缀>`，兼容旧前缀键只读）。"""
        canonical = StockCodeUtil.to_suffix(code)
        keys = [f"market:series:{canonical}"]
        if StockCodeUtil.exchange(canonical):
            legacy = StockCodeUtil.exchange(canonical) + StockCodeUtil.digits(canonical)
            keys.append(f"market:series:{legacy}")
        return keys
