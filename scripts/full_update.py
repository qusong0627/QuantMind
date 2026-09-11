"""
⚠️ 本脚本已停用 (DEPRECATED)
==========================
本脚本是 fundamental_aligned.parquet 的旧生产者。该数据源已废弃——全面转向
QuantDB features_daily (见 backend/shared/fundamental_aligner.py)。线上不再调用
本脚本；所需数据统一由 quantdb_daily_sync.py 产生。保留仅供历史参照，不重新启用。

全量数据拉取脚本
================
每天拉取全量历史K线数据，一次性计算所有指标后保存。
避免增量更新带来的复权因子偏差、数据缺失、合并冲突等问题。

流程:
  1. 获取全部股票列表 (含北交所)
  2. 全量拉取每只股票从 START_DATE 到最新交易日的K线 + 快照
  3. 计算全部技术指标 (MA/RSI/KDJ/MACD/beta/收益率等)
  4. 清洗北交所/B股/退市股
  5. 填充公司信息 / 指数成分股标记
  6. 保存为 fundamental_aligned.parquet (覆盖旧文件)

用法:
  python full_update.py                    # 默认全量拉取
  python full_update.py --start 20240101   # 自定义起始日期
  python full_update.py --skip-fetch       # 仅重算指标(已有临时文件时)

股票代码格式: sh600000, sz000001, bj430001
"""
import numpy as np
import pandas as pd
from datetime import datetime, date, timedelta
from concurrent.futures import ProcessPoolExecutor, as_completed
import sys
import os
import json
import shutil
from typing import List, Dict
import warnings
warnings.filterwarnings('ignore')

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from tqcenter import tq

# ======================== 配置 ========================
PARQUET_PATH = 'fundamental_aligned.parquet'
TEMP_PATH = 'temp_full_data.parquet'
META_PATH = 'temp_full_meta.json'

START_DATE = '20241201'          # 全量拉取起始日期
BATCH_SIZE = 30                  # K线批量拉取批次大小
SNAPSHOT_INTERVAL = 500          # 快照进度打印间隔

COMPANY_FILE = r'公司文件215638819/TRD_Co.csv'
INDEX_FILES = {
    'idx_hs300': '000300cons.xls',
    'idx_zz500': '000905cons.xls',
    'idx_zz1000': '000852cons.xls',
}

INDEX_MAP = {
    '000300.SH': {'name': '沪深300', 'symbol': 'sh000300'},
    '000905.SH': {'name': '中证500', 'symbol': 'sh000905'},
    '000852.SH': {'name': '中证1000', 'symbol': 'sh000852'},
}


def _calc_beta_for_stock(sym_pct):
    """模块级函数: 单只股票计算 beta_20, 可被 multiprocessing pickle"""
    sym, pct = sym_pct
    n = len(pct)
    beta_arr = np.full(n, 1.0)
    for i in range(19, n):
        y = pct[i-19:i+1]
        valid = ~np.isnan(y)
        if valid.sum() >= 10:
            yv = y[valid]
            var_m = np.var(yv, ddof=1)
            if var_m > 1e-10:
                beta_arr[i] = np.var(yv, ddof=0) / var_m
    return sym, beta_arr


def normalize_symbol(symbol: str) -> str:
    """统一股票代码格式"""
    if not symbol:
        return symbol
    symbol = symbol.upper().strip()
    if '.' in symbol:
        code, suffix = symbol.split('.')
        return f"{suffix.lower()}{code}"
    if symbol.startswith('SH'):
        return f"sh{symbol[2:]}"
    if symbol.startswith('SZ'):
        return f"sz{symbol[2:]}"
    if symbol.startswith('BJ'):
        return f"bj{symbol[2:]}"
    if symbol.startswith('68'):       # 科创板必须在6开头之前
        return f"sh{symbol}"
    elif symbol.startswith('6'):
        return f"sh{symbol}"
    elif symbol.startswith(('0', '3')):
        return f"sz{symbol}"
    elif symbol.startswith(('4', '8')):
        return f"bj{symbol}"
    return symbol.lower()


class FullDataUpdater:
    """全量数据更新器"""

    def __init__(self, start_date: str = START_DATE):
        self.start_date = start_date
        self.stock_list = []
        self.all_records = []

    def run(self, skip_fetch: bool = False):
        start_time = datetime.now()
        print(f"\n开始: {start_time.strftime('%Y-%m-%d %H:%M:%S')}")
        print(f"{'='*70}")
        print(f"全量数据更新")
        print(f"{'='*70}")
        print(f"起始日期: {self.start_date}")

        if not skip_fetch:
            self._fetch_all_data()
            self._save_temp()

        self._process_and_save()

        end_time = datetime.now()
        print(f"\n完成: {end_time.strftime('%Y-%m-%d %H:%M:%S')}")
        print(f"总耗时: {end_time - start_time}")

    # ==================== 阶段1: 数据获取 ====================

    def _fetch_all_data(self):
        """获取全部股票的全量K线+快照数据"""
        tq.initialize(__file__)
        print("[OK] 通达信连接成功\n")

        self._get_stock_list()
        self._get_latest_trade_date()
        self._fetch_all_klines()
        self._fetch_all_snapshots()

        tq.close()
        print("\nTQ数据连接已关闭")

    def _get_stock_list(self):
        print("[获取股票列表]")
        all_stocks = []
        markets = [
            ('7', '上证主板'),
            ('8', '深证主板'),
            ('51', '创业板'),
            ('52', '科创板'),
            ('53', '北交所'),
        ]
        for market, name in markets:
            try:
                stocks = tq.get_stock_list(market, list_type=1)
                if stocks and isinstance(stocks, list):
                    codes = [s.get('Code', '') for s in stocks if isinstance(s, dict) and s.get('Code')]
                    all_stocks.extend(codes)
                    print(f"  {name}: {len(codes)} 只")
            except Exception:
                pass
        self.stock_list = [normalize_symbol(s) for s in all_stocks]
        print(f"  总计: {len(self.stock_list)} 只")

    def _get_latest_trade_date(self):
        try:
            dates = tq.get_trading_dates(market='SH', start_time='', end_time='', count=1)
            if dates:
                d = dates[0]
                self.latest_date = d.strftime('%Y%m%d') if hasattr(d, 'strftime') else str(d)[:8]
                return
        except Exception:
            pass
        self.latest_date = datetime.now().strftime('%Y%m%d')

    def _fetch_all_klines(self):
        """批量拉取全部股票的K线数据"""
        target = getattr(self, 'latest_date', self.start_date)
        print(f"\n[获取K线数据] {self.start_date} ~ {target}")

        tdx_stocks = [f"{s[2:]}.{s[:2].upper()}" for s in self.stock_list]
        total = len(tdx_stocks)
        result_count = 0

        for i in range(0, total, BATCH_SIZE):
            batch = tdx_stocks[i:i + BATCH_SIZE]
            batch_num = i // BATCH_SIZE + 1
            total_batches = (total + BATCH_SIZE - 1) // BATCH_SIZE

            try:
                df_front = tq.get_market_data(
                    stock_list=batch, period='1d',
                    start_time=self.start_date,
                    dividend_type='front', fill_data=True
                )
                df_none = tq.get_market_data(
                    stock_list=batch, period='1d',
                    start_time=self.start_date,
                    dividend_type='none', fill_data=True
                )

                if not df_front or 'Close' not in df_front:
                    continue

                for tdx_symbol in batch:
                    norm_sym = normalize_symbol(tdx_symbol)
                    if tdx_symbol not in df_front['Close'].columns:
                        continue

                    front_close = df_front['Close'][tdx_symbol]
                    none_close = df_none['Close'].get(tdx_symbol) if df_none else None
                    factor_series = (
                        df_none['ForwardFactor'][tdx_symbol]
                        if df_none and 'ForwardFactor' in df_none and tdx_symbol in df_none['ForwardFactor'].columns
                        else None
                    )

                    for d_idx, date_key in enumerate(front_close.index):
                        close_price = front_close.iloc[d_idx]
                        if pd.isna(close_price) or close_price is None or float(close_price) <= 0:
                            continue

                        vol_val = float(df_front['Volume'][tdx_symbol].iloc[d_idx]) if pd.notna(df_front['Volume'][tdx_symbol].iloc[d_idx]) else 0

                        # amount 校正: 偏差>50%时用 close*volume 替代
                        raw_amt = float(df_front['Amount'][tdx_symbol].iloc[d_idx]) if pd.notna(df_front['Amount'][tdx_symbol].iloc[d_idx]) else 0
                        calc_amt = float(close_price) * vol_val
                        amt_val = calc_amt if (calc_amt > 0 and abs(raw_amt - calc_amt) / calc_amt > 0.5) else raw_amt

                        adj_factor = float(factor_series.iloc[d_idx]) if factor_series is not None and pd.notna(factor_series.iloc[d_idx]) else 1.0

                        record = {
                            'symbol': norm_sym,
                            'trade_date': str(date_key)[:10],
                            'open': float(df_front['Open'][tdx_symbol].iloc[d_idx]),
                            'high': float(df_front['High'][tdx_symbol].iloc[d_idx]),
                            'low': float(df_front['Low'][tdx_symbol].iloc[d_idx]),
                            'close': float(close_price),
                            'volume': vol_val,
                            'amount': amt_val,
                            'adj_factor': adj_factor,
                        }

                        # 不复权原始价 (none 数据)
                        if none_close is not None and tdx_symbol in df_none['Close'].columns:
                            record.update({
                                'raw_open': float(df_none['Open'][tdx_symbol].iloc[d_idx]) if pd.notna(df_none['Open'][tdx_symbol].iloc[d_idx]) else np.nan,
                                'raw_high': float(df_none['High'][tdx_symbol].iloc[d_idx]) if pd.notna(df_none['High'][tdx_symbol].iloc[d_idx]) else np.nan,
                                'raw_low': float(df_none['Low'][tdx_symbol].iloc[d_idx]) if pd.notna(df_none['Low'][tdx_symbol].iloc[d_idx]) else np.nan,
                                'raw_close': float(none_close.iloc[d_idx]) if pd.notna(none_close.iloc[d_idx]) else np.nan,
                                'raw_volume': vol_val,
                                'raw_amount': raw_amt,
                            })

                        self.all_records.append(record)
                        result_count += 1

            except Exception as e:
                print(f"  批次 {batch_num} 异常: {e}")
                continue

            if batch_num % 10 == 0:
                print(f"  进度: {batch_num}/{total_batches} ({result_count:,} 条)")

        print(f"  完成: {result_count:,} 条记录, {len(self.stock_list)} 只股票")

    def _fetch_all_snapshots(self):
        """拉取快照数据用于补充字段"""
        print(f"\n[获取快照数据]")
        snapshot_map = {}

        for i, stock in enumerate(self.stock_list):
            try:
                tdx_symbol = f"{stock[2:]}.{stock[:2].upper()}"
                snapshot = tq.get_market_snapshot(stock_code=tdx_symbol)
                stock_info = tq.get_stock_info(stock_code=tdx_symbol)
                more_info = tq.get_more_info(stock_code=tdx_symbol)

                combined = {}
                if snapshot:
                    combined.update(snapshot)
                if stock_info:
                    combined.update(stock_info)
                if more_info:
                    combined.update(more_info)
                if combined:
                    snapshot_map[stock] = combined
            except Exception:
                continue

            if (i + 1) % SNAPSHOT_INTERVAL == 0:
                print(f"  进度: {i+1}/{len(self.stock_list)}")

        print(f"  获取: {len(snapshot_map)} 只")
        self.snapshot_map = snapshot_map

        # 将快照字段回填到 all_records (仅最新交易日)
        self._merge_snapshot_fields()

    def _merge_snapshot_fields(self):
        """将快照数据中的字段合并到最新日期的记录上"""
        latest_dates = {}  # symbol → 最新 trade_date
        for rec in self.all_records:
            sym = rec['symbol']
            td = rec['trade_date']
            if sym not in latest_dates or td > latest_dates[sym]:
                latest_dates[sym] = td

        snap_fields = {
            'Name': 'stock_name',
            'DynaPE': 'pe_ttm',
            'PB_MRQ': 'pb',
            'Zsz': ('total_mv', lambda v: float(v or 0) * 100000000),
            'Ltsz': ('float_mv', lambda v: float(v or 0) * 100000000),
            'fHSL': 'turnover_rate',
            'ZTPrice': '_zt_price',
            'DTPrice': '_dt_price',
        }

        for rec in self.all_records:
            sym = rec['symbol']
            if rec['trade_date'] != latest_dates.get(sym):
                continue
            snap = self.snapshot_map.get(sym, {})
            if not snap:
                continue

            zt_price = float(snap.get('ZTPrice', 0) or 0)
            dt_price = float(snap.get('DTPrice', 0) or 0)
            close_val = rec['close']

            rec['limit_up_today'] = 1 if (zt_price > 0 and abs(float(close_val) - zt_price) < 0.01) else 0
            rec['limit_down_today'] = 1 if (dt_price > 0 and abs(float(close_val) - dt_price) < 0.01) else 0
            rec['stock_name'] = str(snap.get('Name', '')) or ''
            rec['is_st'] = 1 if 'ST' in str(snap.get('Name', '')) else 0
            rec['pe_ttm'] = float(snap.get('DynaPE', 0) or 0)
            rec['pb'] = float(snap.get('PB_MRQ', 0) or 0)
            rec['total_mv'] = float(snap.get('Zsz', 0) or 0) * 100000000
            rec['float_mv'] = float(snap.get('Ltsz', 0) or 0) * 100000000
            rec['turnover_rate'] = float(snap.get('fHSL', 0) or 0)

            # ROE 计算
            mgjzc = float(snap.get('J_mgjzc', 0) or 0)
            mgsy = float(snap.get('J_mgsy', 0) or 0)
            rec['roe'] = mgsy / mgjzc * 100 if mgjzc > 0 else 0
            rec['profit_growth'] = 0

        # 非最新日期的记录填默认值
        default_snap_vals = {
            'limit_up_today': 0, 'limit_down_today': 0,
            'stock_name': '', 'is_st': 0, 'pe_ttm': 0.0, 'pb': 0.0,
            'total_mv': 0.0, 'float_mv': 0.0, 'turnover_rate': 0.0,
            'roe': 0.0, 'profit_growth': 0,
        }
        for rec in self.all_records:
            for k, v in default_snap_vals.items():
                if k not in rec:
                    rec[k] = v

    def _save_temp(self):
        """保存临时数据到文件"""
        print(f"\n[保存临时数据]")
        df = pd.DataFrame(self.all_records)
        df.to_parquet(TEMP_PATH, index=False)
        meta = {
            'start_date': self.start_date,
            'latest_date': getattr(self, 'latest_date', ''),
            'record_count': len(self.all_records),
            'stock_count': len(set(r['symbol'] for r in self.all_records)),
            'save_time': datetime.now().isoformat(),
        }
        with open(META_PATH, 'w') as f:
            json.dump(meta, f)
        print(f"  文件: {TEMP_PATH} ({len(df):,} 条)")
        print(f"  元数据: {META_PATH}")

    # ==================== 阶段2: 数据处理 ====================

    def _load_temp(self) -> pd.DataFrame:
        """加载临时数据"""
        print(f"\n[加载临时数据] {TEMP_PATH}")
        df = pd.read_parquet(TEMP_PATH)
        with open(META_PATH, 'r') as f:
            meta = json.load(f)
        print(f"  记录: {len(df):,}, 股票: {meta.get('stock_count', '?')}, 范围: {df['trade_date'].min()} ~ {df['trade_date'].max()}")
        return df, meta

    def _calculate_indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        """计算全部技术指标 (在完整数据集上一次计算)"""
        print(f"\n[计算技术指标]")
        df = df.copy()
        df['trade_date'] = pd.to_datetime(df['trade_date'])
        df = df.sort_values(['symbol', 'trade_date']).reset_index(drop=True)

        close = df['close']
        high = df['high']
        low = df['low']
        volume = df['volume']
        amount = df['amount']

        # 确保基础字段存在
        for col in ['pb', 'pe_ttm', 'total_mv', 'float_mv']:
            if col not in df.columns:
                df[col] = 0.0

        # --- 均线 ---
        df['ma5'] = close.rolling(5).mean()
        df['ma10'] = close.rolling(10).mean()
        df['ma20'] = close.rolling(20).mean()
        df['ma60'] = close.rolling(60).mean()
        df['ma_gap_5'] = (close - df['ma5']) / df['ma5'] * 100
        df['ma_gap_10'] = (close - df['ma10']) / df['ma10'] * 100
        df['ma_gap_20'] = (close - df['ma20']) / df['ma20'] * 100

        # --- RSI ---
        delta = close.diff()
        gain = delta.where(delta > 0, 0)
        loss = (-delta).where(delta < 0, 0)
        df['rsi_6'] = 100 - (100 / (1 + gain.rolling(6).mean() / loss.rolling(6).mean().replace(0, np.nan)))
        df['rsi_14'] = 100 - (100 / (1 + gain.rolling(14).mean() / loss.rolling(14).mean().replace(0, np.nan)))

        # --- KDJ ---
        low_min = low.rolling(9).min()
        high_max = high.rolling(9).max()
        rsv = (close - low_min) / (high_max - low_min) * 100
        df['kdj_k'] = rsv.ewm(com=2).mean()
        df['kdj_d'] = df['kdj_k'].ewm(com=2).mean()
        df['kdj_j'] = 3 * df['kdj_k'] - 2 * df['kdj_d']

        # --- MACD ---
        ema12 = close.ewm(span=12).mean()
        ema26 = close.ewm(span=26).mean()
        df['macd_dif'] = ema12 - ema26
        df['macd_dea'] = df['macd_dif'].ewm(span=9).mean()
        df['macd_hist'] = 2 * (df['macd_dif'] - df['macd_dea'])

        # --- 波动率 ---
        returns = close.pct_change() * 100
        df['vol_std_5'] = returns.rolling(5).std()
        df['vol_std_20'] = returns.rolling(20).std()
        df['vol_std_60'] = returns.rolling(60).std()

        tr = pd.concat([high - low, abs(high - close.shift(1)), abs(low - close.shift(1))], axis=1).max(axis=1)
        df['vol_atr_14'] = tr.rolling(14).mean()

        # --- 成交量 ---
        df['volume_ratio_5'] = volume / volume.rolling(5).mean().replace(0, np.nan)
        df['volume_ratio_20'] = volume / volume.rolling(20).mean().replace(0, np.nan)
        df['volume_ma_3'] = volume.rolling(3).mean()
        df['amount_ma_5'] = amount.rolling(5).mean()
        df['volume_trend_3d'] = volume.pct_change().rolling(3).mean()

        # --- 收益率 (必须按股票分组 shift，避免跨股票取值) ---
        for d in [1, 3, 5, 10, 20, 60]:
            df[f'return_{d}d'] = df.groupby('symbol')['close'].transform(
                lambda x: (x.shift(-d) / x - 1) * 100
            )
        df['pct_change'] = df.groupby('symbol')['close'].transform(
            lambda x: (x / x.shift(1) - 1) * 100
        )

        # --- 估值 ---
        df['bp'] = 1 / df['pb'].replace(0, np.nan)
        df['ep_ttm'] = 1 / df['pe_ttm'].replace(0, np.nan)
        df['ln_mv_total'] = np.log(df['total_mv'].replace(0, np.nan))

        # --- beta_20 (相对波动率) ---
        df = self._calc_beta_20(df)

        # --- listed_days ---
        latest_dt = df['trade_date'].max()
        first_dates = df.groupby('symbol')['trade_date'].min()
        listed_days_map = (latest_dt - first_dates).dt.days
        df['listed_days'] = df['symbol'].map(listed_days_map)

        print(f"  指标列数: {len([c for c in df.columns if c not in ['symbol','trade_date','open','high','low','close','volume','amount','adj_factor']])}")
        return df

    @staticmethod
    def _calc_beta_20(df: pd.DataFrame) -> pd.DataFrame:
        """并行计算 beta_20 (相对波动率)"""
        # 按股票分组提取收益率序列
        grouped = df.groupby('symbol')['pct_change']
        tasks = [(sym, grp.values) for sym, grp in grouped]

        # 并行计算 (使用模块级函数, 避免 pickle 问题)
        n_workers = min(os.cpu_count() or 4, 8)
        results_map = {}
        print(f"    beta_20 并行: {len(tasks)} 只股票, {n_workers} 进程")

        with ProcessPoolExecutor(max_workers=n_workers) as executor:
            futures = {executor.submit(_calc_beta_for_stock, t): t[0] for t in tasks}
            done_count = 0
            for future in as_completed(futures):
                sym, beta_arr = future.result()
                results_map[sym] = beta_arr
                done_count += 1
                if done_count % 1000 == 0:
                    print(f"      进度: {done_count}/{len(tasks)}")

        # 回填到 DataFrame
        beta_col = np.full(len(df), 1.0)
        for sym in results_map:
            mask = df['symbol'] == sym
            idx = df.index[mask].to_numpy()
            beta_col[idx] = results_map[sym][:len(idx)]

        df['beta_20'] = beta_col
        return df

    def _calculate_consecutive_limit_up(self, df: pd.DataFrame) -> pd.DataFrame:
        """向量化计算连板天数"""
        print(f"\n[计算连板天数]")
        df = df.copy()
        df['_lu'] = df['limit_up_today'].fillna(0).astype(int)
        df['_not_lu'] = 1 - df['_lu']
        df['_grp'] = df.groupby('symbol')['_not_lu'].cumsum()
        df['consecutive_limit_up_days'] = df.groupby(['symbol', '_grp'])['_lu'].transform('cumsum').fillna(0).astype(int)
        df = df.drop(columns=['_lu', '_not_lu', '_grp'])

        limit_count = (df['consecutive_limit_up_days'] > 0).sum()
        print(f"  连板记录: {limit_count}")
        return df

    def _clean_data(self, df: pd.DataFrame) -> pd.DataFrame:
        """清洗北交所/B股/退市股"""
        print(f"\n[清洗数据]")
        before = len(df)

        bj_mask = df['symbol'].str.startswith('bj')
        b_mask = df['symbol'].str.match(r'^[a-z]+\d+B$', case=False)
        close_val = pd.to_numeric(df['close'], errors='coerce')
        delist_mask = close_val < 1.0

        df = df[~(bj_mask | b_mask | delist_mask)].copy()
        removed = before - len(df)

        parts = []
        bj_n = bj_mask.sum()
        b_n = b_mask.sum()
        dl_n = delist_mask.sum() - bj_mask[delist_mask].sum() - b_mask[delist_mask].sum()
        if bj_n > 0:
            parts.append(f'北交所{bj_n}')
        if b_n > 0:
            parts.append(f'B股{b_n}')
        if dl_n > 0:
            dl_syms = df.loc[delist_mask & ~bj_mask & ~b_mask, 'symbol'].unique()[:5]
            parts.append(f'低价/退市{dl_n}只({",".join(dl_syms)}...)')

        detail = ", ".join(parts) if parts else "无"
        print(f"  删除: {removed} 条 ({detail})")
        return df

    def _fill_company_info(self, df: pd.DataFrame) -> pd.DataFrame:
        """填充公司基本信息"""
        print(f"\n[填充公司信息]")
        if not os.path.exists(COMPANY_FILE):
            print(f"  文件不存在: {COMPANY_FILE}")
            return df
        try:
            co_df = pd.read_csv(COMPANY_FILE, encoding='utf-8')
        except Exception:
            co_df = pd.read_csv(COMPANY_FILE, encoding='gbk')

        def convert_code(stkcd):
            code = str(stkcd).zfill(6)
            if code.startswith(('6', '68')):
                return f'sh{code}'
            elif code.startswith(('0', '3')):
                return f'sz{code}'
            return f'sh{code}'

        co_df['symbol'] = co_df['Stkcd'].apply(convert_code)
        maps = {
            'stock_name': dict(zip(co_df['symbol'], co_df['Stknme'])),
            'industry': dict(zip(co_df['symbol'], co_df['Indnme'])),
            'ind_code_l1': dict(zip(co_df['symbol'], co_df['Nindcd'])),
            'ind_code_l2': dict(zip(co_df['symbol'], co_df['Nnindcd'])),
            'province': dict(zip(co_df['symbol'], co_df['PROVINCE'])),
        }
        st_map = co_df.set_index('symbol')['Stknme'].apply(lambda x: 1 if 'ST' in str(x) else 0).to_dict()
        maps['is_st'] = st_map

        df = df.copy()
        for col, mapping in maps.items():
            if col in df.columns:
                df = df.drop(columns=[col])
            df[col] = df['symbol'].map(mapping).fillna('')
            if col == 'is_st':
                df[col] = df[col].replace('', 0).fillna(0).astype(int)

        print(f"  已填充: {list(maps.keys())}")
        return df

    def _fill_index_flags(self, df: pd.DataFrame) -> pd.DataFrame:
        """填充指数成分股标记"""
        print(f"\n[填充指数标记]")
        idx_const = {}
        for idx_field, fname in INDEX_FILES.items():
            if not os.path.exists(fname):
                continue
            try:
                idf = pd.read_excel(fname)
                codes = idf['成份券代码Constituent Code'].astype(str).str.zfill(6).tolist()
                exchanges = idf['交易所Exchange'].tolist()
                symbols = []
                for code, exch in zip(codes, exchanges):
                    if exch == '上海证券交易所' or code.startswith(('6', '68')):
                        symbols.append(f'sh{code}')
                    else:
                        symbols.append(f'sz{code}')
                idx_const[idx_field] = set(symbols)
            except Exception as e:
                print(f"  读取 {fname} 失败: {e}")

        if not idx_const:
            return df

        df = df.copy()
        for col, sym_set in idx_const.items():
            df[col] = df['symbol'].isin(sym_set).astype(int)

        print(f"  已填充: {list(idx_const.keys())}")
        return df

    def _add_concept_columns(self, df: pd.DataFrame) -> pd.DataFrame:
        """补充概念板块等默认列为0"""
        concept_cols = [
            'concept_ai', 'concept_chip', 'concept_new_energy', 'concept_pv',
            'concept_military', 'concept_medical', 'concept_fintech',
            'concept_consumption', 'concept_state_owned', 'concept_lithium',
            'idx_chinext', 'idx_margin', 'idx_all',
            'inst_ownership', 'lrg_trd_tolbuynum', 'lrg_trd_tolsellnum',
            'micro_effective_spread', 'micro_imbalance_volume', 'micro_jump_flag',
        ]
        for col in concept_cols:
            if col not in df.columns:
                df[col] = 0
        return df

    def _clean_for_parquet(self, df: pd.DataFrame) -> pd.DataFrame:
        """清理无法被 pyarrow 序列化的列"""
        protected_cols = {'symbol', 'trade_date', 'industry', 'ind_code_l1', 'ind_code_l2'}

        if 'close_eq_raw' in df.columns:
            df = df.drop(columns=['close_eq_raw'])

        drop_cols = []
        for c in list(df.columns):
            if c in protected_cols:
                continue
            if df[c].dtype == 'object':
                sample = df[c].dropna().head(100)
                if len(sample) == 0:
                    continue
                if all(isinstance(v, str) for v in sample):
                    drop_cols.append(c)
        if drop_cols:
            df = df.drop(columns=drop_cols)
            print(f"  删除字符串列: {drop_cols}")
        return df

    def _save_final(self, df: pd.DataFrame):
        """备份旧文件并保存新数据"""
        print(f"\n[保存主数据]")
        if os.path.exists(PARQUET_PATH):
            bak = PARQUET_PATH + f".bak_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
            shutil.copy2(PARQUET_PATH, bak)
            print(f"  备份: {bak}")

        df = df.sort_values(['symbol'], kind='mergesort').reset_index(drop=True)
        df.to_parquet(PARQUET_PATH, index=False)
        print(f"  保存: {PARQUET_PATH}")
        print(f"  记录: {len(df):,}")
        print(f"  股票: {df['symbol'].nunique()} 只")
        print(f"  日期范围: {df['trade_date'].min()} ~ {df['trade_date'].max()}")

        # 验证输出
        latest_date = df['trade_date'].max()
        latest_str = str(latest_date)[:10]
        latest = df[df['trade_date'].astype(str).str[:10] == latest_str]
        print(f"\n[验证] {latest_str}:")
        print(f"  股票数: {len(latest)}")
        print(f"  涨停: {(latest['limit_up_today'] == 1).sum()}")
        print(f"  跌停: {(latest['limit_down_today'] == 1).sum()}")

    def _process_and_save(self):
        """阶段2主流程"""
        df, meta = self._load_temp()

        # 备份
        if os.path.exists(PARQUET_PATH):
            bak = PARQUET_PATH + f".bak_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
            shutil.copy2(PARQUET_PATH, bak)
            print(f"[备份] {bak}")

        # 指标计算
        df = self._calculate_indicators(df)

        # 连板天数
        df = self._calculate_consecutive_limit_up(df)

        # 补充概念列
        df = self._add_concept_columns(df)

        # 公司信息
        df = self._fill_company_info(df)

        # 指数标记
        df = self._fill_index_flags(df)

        # 清洗
        df = self._clean_data(df)

        # parquet 兼容清理
        df = self._clean_for_parquet(df)

        # 保存
        self._save_final(df)

        # 清理临时文件
        for p in [TEMP_PATH, META_PATH]:
            if os.path.exists(p):
                os.remove(p)
        print(f"\n[清理] 临时文件已删除")


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description='全量数据拉取脚本')
    parser.add_argument('--start', default=START_DATE, help=f'起始日期 (YYYYMMDD), 默认 {START_DATE}')
    parser.add_argument('--skip-fetch', action='store_true', help='跳过数据拉取, 仅处理已有临时文件')
    args = parser.parse_args()

    updater = FullDataUpdater(start_date=args.start)
    updater.run(skip_fetch=args.skip_fetch)
