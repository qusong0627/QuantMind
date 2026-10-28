import json
import logging
import socket
import time
import urllib.request

from ..core.types import PriceType, Side, STOCK_BUY, STOCK_SELL

log = logging.getLogger(__name__)


class TdxError(Exception):
    """通达信 JSON-RPC 调用失败."""


class TdxTimeoutError(TdxError):
    """通达信调用超时：结果未知。交易方法绝不可重试/当失败。"""


class TdxClient:
    """通达信 tqcenter HTTP 服务 (127.0.0.1:17709) 的同步 JSON-RPC 客户端."""

    def __init__(self, base_url: str = "http://127.0.0.1:17709/",
                 timeout: float = 5.0, max_retries: int = 1):
        self.base_url = base_url
        self.timeout = timeout
        self.max_retries = max_retries
        self._next_id = 1

    def _call(self, method: str, params: dict,
              max_retries: int | None = None) -> dict:
        body = json.dumps({"id": self._next_id, "method": method,
                           "params": params}, ensure_ascii=False).encode("utf-8")
        self._next_id += 1
        retries = self.max_retries if max_retries is None else max_retries
        last_err = None
        for attempt in range(retries + 1):
            try:
                req = urllib.request.Request(
                    self.base_url, data=body,
                    headers={"Content-Type": "application/json; charset=utf-8"},
                    method="POST")
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    result = json.loads(resp.read().decode("utf-8"))
                if "error" in result:
                    raise TdxError(f"{method} 错误: {result['error']}")
                inner = result.get("result", {})
                error_id = inner.get("ErrorId", "0")
                if str(error_id) != "0" and str(error_id) != "-1":
                    raise TdxError(f"{method} 失败 ErrorId={error_id}: {inner.get('Msg', inner)}")
                return inner
            except (urllib.error.URLError, OSError, json.JSONDecodeError, TdxError) as e:
                last_err = e
                if attempt < retries:
                    time.sleep(0.5 * (2 ** attempt))
        if isinstance(last_err, (TimeoutError, socket.timeout)) or \
                "timed out" in str(last_err).lower() or "超时" in str(last_err):
            raise TdxTimeoutError(
                f"{method} 调用超时(重试{retries}次): {last_err}")
        raise TdxError(f"{method} 调用失败(重试{retries}次): {last_err}")

    def health_check(self) -> bool:
        return self.health_check_fast()

    def health_check_fast(self, timeout: float = 1.0) -> bool:
        """快速健康检查: 短超时, 不重试, 不阻塞."""
        body = json.dumps({"id": 1, "method": "get_match_stkinfo",
                           "params": {"key_word": "茅台"}}).encode("utf-8")
        try:
            req = urllib.request.Request(
                self.base_url, data=body,
                headers={"Content-Type": "application/json; charset=utf-8"},
                method="POST")
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                result = json.loads(resp.read().decode("utf-8"))
            return "error" not in result
        except Exception:
            return False

    # ---- 账户 ----

    def stock_account(self, account: str = "", account_type: str = "stock") -> int:
        r = self._call("stock_account", {"account": account, "account_type": account_type})
        try:
            return int(r.get("Value", -1))
        except (TypeError, ValueError):
            return -1

    def query_stock_asset(self, account_id: int = 0) -> dict:
        return self._call("query_stock_asset", {"account_id": account_id})

    def query_stock_positions(self, account_id: int = 0) -> list:
        r = self._call("query_stock_positions", {"account_id": account_id})
        return r.get("Value", []) if isinstance(r.get("Value"), list) else r

    def query_stock_orders(self, account_id: int = 0, stock_code: str = "",
                           cancelable_only: bool = False) -> list:
        r = self._call("query_stock_orders", {
            "account_id": account_id, "stock_code": stock_code,
            "cancelable_only": cancelable_only})
        return r.get("Value", []) if isinstance(r.get("Value"), list) else r

    # ---- 交易 ----

    def order_stock(self, account_id: int, stock_code: str, side: Side,
                    volume: int, price_type: PriceType = PriceType.MANUAL,
                    price: float = 0.0, notify: int = 0) -> dict:
        """下单。notify=是否手动确认（0=需客户端确认，1=请求免确认）。

        官方文档：实盘账户默认「提示下单让用户确认」；免确认需券商开通
        「支持TQ的版本」，且 notify 参数要显式传入。
        """
        order_type = STOCK_BUY if side == Side.BUY else STOCK_SELL
        # 下单绝不重试：超时后订单可能已在柜台，重试=重复下单。
        return self._call("order_stock", {
            "account_id": account_id, "stock_code": stock_code,
            "order_type": order_type, "order_volume": int(volume),
            "price_type": int(price_type.value), "price": float(price or 0),
            "notify": int(notify)},
            max_retries=0)

    def cancel_order_stock(self, account_id: int, stock_code: str, order_id: str) -> dict:
        # 撤单同样不重试：重复撤单会让终态/回执混乱，交给上层终态复核。
        return self._call("cancel_order_stock", {
            "account_id": account_id, "stock_code": stock_code, "order_id": order_id},
            max_retries=0)

    # ---- 推送 ----

    def send_message(self, msg: str) -> dict:
        return self._call("send_message", {"msg_str": msg})

    def send_user_block(self, block_code: str = "", stocks: list = None,
                        show: bool = False) -> dict:
        return self._call("send_user_block", {
            "block_code": block_code, "stock_list": stocks or [], "show": show})

    def send_warn(self, stock_list: list, price_list: list = None,
                  close_list: list = None, volum_list: list = None,
                  bs_flag_list: list = None, warn_type_list: list = None,
                  reason_list: list = None, count: int = 1) -> dict:
        n = len(stock_list)
        def _fill(lst, default):
            return [str(v) for v in (lst or [])] or [default] * n
        return self._call("send_warn", {
            "stock_list": stock_list,
            "price_list": _fill(price_list, "0"),
            "close_list": _fill(close_list, "0"),
            "volum_list": _fill(volum_list, "0"),
            "bs_flag_list": _fill(bs_flag_list, "2"),
            "warn_type_list": _fill(warn_type_list, "1"),
            "reason_list": _fill(reason_list, ""),
            "count": count or n,
        })

    def send_source(self, py_code: str, handle_type: int = 0) -> dict:
        return self._call("send_source", {"py_code": py_code, "handle_type": handle_type})

    # ---- 行情 ----

    def get_market_snapshot(self, stock_code: str) -> dict:
        return self._call("get_market_snapshot", {"stock_code": stock_code})

    def get_market_data(self, stock_list: list, period: str = "1d",
                        count: int = 10, dividend_type: str = "none") -> dict:
        return self._call("get_market_data", {
            "stock_list": stock_list, "period": period, "count": count,
            "dividend_type": dividend_type})

    def get_latest_price(self, stock_code: str) -> float:
        """取最新价: 优先实时快照 Now, 回退日线最新 Close."""
        try:
            snap = self.get_market_snapshot(stock_code)
            now = snap.get("Now")
            if now and float(now) > 0:
                return float(now)
        except Exception:
            pass
        try:
            data = self.get_market_data([stock_code], "1d", 1, "none")
            closes = self._extract_kline_closes(data, stock_code)
            if closes:
                return float(closes[-1])
        except Exception:
            pass
        raise TdxError(f"无法获取 {stock_code} 最新价")

    @staticmethod
    def _extract_kline_closes(data: dict, symbol: str) -> list:
        """兼容新旧 K线结构: 新版 Value[symbol].Close, 旧版顶层 Close."""
        value = data.get("Value")
        if isinstance(value, dict) and symbol in value:
            return value[symbol].get("Close") or []
        return data.get("Close") or []

    # ---- 通用透传 ----

    def call(self, method: str, params: dict) -> dict:
        """通用方法派发: 透传任意 tqcenter 方法."""
        fn = getattr(self, method, None)
        if fn is None:
            return self._call(method, params or {})
        if method in ("get_market_snapshot", "get_stock_info", "get_more_info",
                     "get_relation", "get_exday_data"):
            # 单参数方法
            return fn(params.get("stock_code", "") if "stock_code" in params
                      else params.get("stock_code", params.get("symbol", "")))
        import inspect
        sig = inspect.signature(fn)
        kwargs = {k: v for k, v in (params or {}).items() if k in sig.parameters}
        return fn(**kwargs)

    # ---- 行情数据 ----

    def get_stock_info(self, stock_code: str, field_list: list = None) -> dict:
        return self._call("get_stock_info", {"stock_code": stock_code,
                                             "field_list": field_list or []})

    def get_more_info(self, stock_code: str = "", field_list: list = None) -> dict:
        return self._call("get_more_info", {"stock_code": stock_code,
                                            "field_list": field_list or []})

    def get_stock_list(self, market=None, list_type: int = 0) -> list:
        return self._call("get_stock_list", {"market": market, "list_type": list_type})

    def get_sector_list(self, list_type: int = 0) -> list:
        return self._call("get_sector_list", {"list_type": list_type})

    def get_stock_list_in_sector(self, block_code: str, block_type: int = 0,
                                 list_type: int = 0) -> list:
        return self._call("get_stock_list_in_sector", {
            "block_code": block_code, "block_type": block_type, "list_type": list_type})

    def get_user_sector(self) -> list:
        return self._call("get_user_sector", {})

    def get_match_stkinfo(self, key_word: str) -> list:
        return self._call("get_match_stkinfo", {"key_word": key_word})

    def get_relation(self, stock_code: str = "") -> list:
        return self._call("get_relation", {"stock_code": stock_code})

    def get_divid_factors(self, stock_code: str, start_time: str = "",
                          end_time: str = "") -> dict:
        return self._call("get_divid_factors", {
            "stock_code": stock_code, "start_time": start_time, "end_time": end_time})

    def get_gb_info(self, stock_code: str = "", date_list: list = None,
                    count: int = 1) -> dict:
        return self._call("get_gb_info", {
            "stock_code": stock_code, "date_list": date_list or [], "count": count})

    def get_gb_info_by_date(self, stock_code: str = "", start_date: str = "",
                            end_date: str = "") -> dict:
        return self._call("get_gb_info_by_date", {
            "stock_code": stock_code, "start_date": start_date, "end_date": end_date})

    def get_kzz_info(self, stock_code: str = "", field_list: list = None) -> dict:
        return self._call("get_kzz_info", {"stock_code": stock_code,
                                           "field_list": field_list or []})

    def get_ipo_info(self, ipo_type: int = 0, ipo_date: int = 0) -> list:
        return self._call("get_ipo_info", {"ipo_type": ipo_type, "ipo_date": ipo_date})

    def get_trackzs_etf_info(self, zs_code: str = "") -> list:
        return self._call("get_trackzs_etf_info", {"zs_code": zs_code})

    def get_pricevol(self, stock_list: list) -> dict:
        return self._call("get_pricevol", {"stock_list": stock_list})

    def get_exday_data(self, stock_code: str = "", count: int = 1) -> list:
        return self._call("get_exday_data", {"stock_code": stock_code, "count": count})

    def get_zdt_data(self, stock_list: list = None) -> dict:
        return self._call("get_zdt_data", {"stock_list": stock_list or []})

    def get_trading_dates(self, market: str = "", start_time: str = "",
                          end_time: str = "", count: int = 0) -> dict:
        return self._call("get_trading_dates", {
            "market": market, "start_time": start_time,
            "end_time": end_time, "count": count})

    # ---- 专业数据 ----

    # ---- pro-data（财务/个股交易/板块交易/市场交易/单股单表） ----
    # 发给 DLL 的键必须是 **table_list**（官方 tqcenter 的 batch_json 同用此键）。
    # 旧实现发 field_list，DLL 直接回 `json has no table_list`（ErrorId=10），
    # 被采集链路误判成「缺数据包 blocked」——2026-10-22 直连 17709 实测纠正。
    def get_financial_data(self, stock_list: list = None, field_list: list = None,
                           start_time: str = "", end_time: str = "",
                           report_type: str = "report_time") -> dict:
        return self._call("get_financial_data", {
            "stock_list": stock_list or [], "table_list": field_list or [],
            "start_time": start_time, "end_time": end_time, "report_type": report_type})

    def get_financial_data_by_date(self, stock_list: list = None, field_list: list = None,
                                   year: int = 0, mmdd: int = 0) -> dict:
        return self._call("get_financial_data_by_date", {
            "stock_list": stock_list or [], "table_list": field_list or [],
            "year": year, "mmdd": mmdd})

    def get_gpjy_value(self, stock_list: list = None, field_list: list = None,
                       start_time: str = "", end_time: str = "") -> dict:
        return self._call("get_gpjy_value", {
            "stock_list": stock_list or [], "table_list": field_list or [],
            "start_time": start_time, "end_time": end_time})

    def get_gpjy_value_by_date(self, stock_list: list = None, field_list: list = None,
                               year: int = 0, mmdd: int = 0) -> dict:
        return self._call("get_gpjy_value_by_date", {
            "stock_list": stock_list or [], "table_list": field_list or [],
            "year": year, "mmdd": mmdd})

    def get_bkjy_value(self, stock_list: list = None, field_list: list = None,
                       start_time: str = "", end_time: str = "") -> dict:
        return self._call("get_bkjy_value", {
            "stock_list": stock_list or [], "table_list": field_list or [],
            "start_time": start_time, "end_time": end_time})

    def get_bkjy_value_by_date(self, stock_list: list = None, field_list: list = None,
                               year: int = 0, mmdd: int = 0) -> dict:
        return self._call("get_bkjy_value_by_date", {
            "stock_list": stock_list or [], "table_list": field_list or [],
            "year": year, "mmdd": mmdd})

    def get_scjy_value(self, field_list: list = None, start_time: str = "",
                       end_time: str = "") -> dict:
        return self._call("get_scjy_value", {
            "table_list": field_list or [], "start_time": start_time, "end_time": end_time})

    def get_scjy_value_by_date(self, field_list: list = None, year: int = 0,
                               mmdd: int = 0) -> dict:
        return self._call("get_scjy_value_by_date", {
            "table_list": field_list or [], "year": year, "mmdd": mmdd})

    def get_gp_one_data(self, stock_list: list = None, field_list: list = None) -> dict:
        return self._call("get_gp_one_data", {
            "stock_list": stock_list or [], "table_list": field_list or []})

    # ---- 板块管理 ----

    def create_sector(self, block_code: str = "", block_name: str = "") -> dict:
        return self._call("create_sector", {"block_code": block_code, "block_name": block_name})

    def delete_sector(self, block_code: str = "") -> dict:
        return self._call("delete_sector", {"block_code": block_code})

    def rename_sector(self, block_code: str = "", block_name: str = "") -> dict:
        return self._call("rename_sector", {"block_code": block_code, "block_name": block_name})

    def clear_sector(self, block_code: str = "") -> dict:
        return self._call("clear_sector", {"block_code": block_code})

    # ---- 缓存/刷新 ----

    def refresh_cache(self, market: str = "AG", force: bool = False) -> dict:
        return self._call("refresh_cache", {"market": market, "force": force})

    def refresh_kline(self, stock_list: list = None, period: str = "1d") -> dict:
        return self._call("refresh_kline", {"stock_list": stock_list or [], "period": period})

    def download_file(self, down_type: int = 1, stock_code: str = "",
                      down_time: str = "") -> dict:
        return self._call("download_file", {
            "down_type": down_type, "stock_code": stock_code, "down_time": down_time})

    # ---- 客户端控制 ----

    def exec_to_tdx(self, url: str = "") -> dict:
        return self._call("exec_to_tdx", {"url": url})

    def send_file(self, file_path: str = "") -> dict:
        return self._call("send_file", {"file_path": file_path})

    def send_bt_data(self, stock_code: str = "", time_list: list = None,
                     data_list: list = None, count: int = 1) -> dict:
        return self._call("send_bt_data", {
            "stock_code": stock_code, "time_list": time_list or [],
            "data_list": data_list or [], "count": count})

    # ---- 公式接口 ----

    def formula_zb(self, formula_name: str = "", formula_arg: str = "",
                   xsflag: int = -1) -> dict:
        return self._call("formula_zb", {
            "formula_name": formula_name, "formula_arg": formula_arg, "xsflag": xsflag})

    def formula_xg(self, formula_name: str = "", formula_arg: str = "") -> dict:
        return self._call("formula_xg", {
            "formula_name": formula_name, "formula_arg": formula_arg})

    def formula_exp(self, formula_name: str = "", formula_arg: str = "") -> dict:
        return self._call("formula_exp", {
            "formula_name": formula_name, "formula_arg": formula_arg})

    def formula_get_data(self) -> dict:
        return self._call("formula_get_data", {})

    def formula_set_data(self, stock_code: str = "", stock_data: list = None,
                         stock_period: str = "1d", count: int = 0,
                         dividend_type: int = 0) -> dict:
        return self._call("formula_set_data", {
            "stock_code": stock_code, "stock_data": stock_data or [],
            "stock_period": stock_period, "count": count, "dividend_type": dividend_type})

    def formula_set_data_info(self, stock_code: str = "", stock_period: str = "1d",
                              start_time: str = "", end_time: str = "",
                              count: int = 0, dividend_type: int = 0) -> dict:
        return self._call("formula_set_data_info", {
            "stock_code": stock_code, "stock_period": stock_period,
            "start_time": start_time, "end_time": end_time,
            "count": count, "dividend_type": dividend_type})

    def formula_process_mul_xg(self, formula_name: str = "", stock_list: list = None,
                               formula_arg: str = "", return_count: int = 1,
                               return_date: bool = False, stock_period: str = "1d",
                               start_time: str = "", end_time: str = "",
                               count: int = 0, dividend_type: int = 0) -> dict:
        return self._call("formula_process_mul_xg", {
            "formula_name": formula_name, "stock_list": stock_list or [],
            "formula_arg": formula_arg, "return_count": return_count,
            "return_date": return_date, "stock_period": stock_period,
            "start_time": start_time, "end_time": end_time,
            "count": count, "dividend_type": dividend_type})

    def formula_process_mul_zb(self, formula_name: str = "", stock_list: list = None,
                               formula_arg: str = "", return_count: int = 1,
                               return_date: bool = False, xsflag: int = -1,
                               stock_period: str = "1d", start_time: str = "",
                               end_time: str = "", count: int = 0,
                               dividend_type: int = 0) -> dict:
        return self._call("formula_process_mul_zb", {
            "formula_name": formula_name, "stock_list": stock_list or [],
            "formula_arg": formula_arg, "return_count": return_count,
            "return_date": return_date, "xsflag": xsflag,
            "stock_period": stock_period, "start_time": start_time,
            "end_time": end_time, "count": count, "dividend_type": dividend_type})

    def formula_process_mul_exp(self, formula_name: str = "", stock_list: list = None,
                                formula_arg: str = "", return_count: int = 1,
                                return_date: bool = False, xsflag: int = -1,
                                stock_period: str = "1d", start_time: str = "",
                                end_time: str = "", count: int = 0,
                                dividend_type: int = 0) -> dict:
        return self._call("formula_process_mul_exp", {
            "formula_name": formula_name, "stock_list": stock_list or [],
            "formula_arg": formula_arg, "return_count": return_count,
            "return_date": return_date, "xsflag": xsflag,
            "stock_period": stock_period, "start_time": start_time,
            "end_time": end_time, "count": count, "dividend_type": dividend_type})
