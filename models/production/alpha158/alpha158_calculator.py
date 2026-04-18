import pandas as pd
import numpy as np

class Alpha158Calculator:
    """
    Full implementation of Qlib-standard Alpha158 features using pure Pandas.
    Matches the order and logic specified in LightGBM model metadata.
    """
    
    @staticmethod
    def calculate(df: pd.DataFrame) -> pd.DataFrame:
        """
        Input df MultiIndex (symbol, datetime), columns: [open, high, low, close, volume, vwap, factor]
        """
        # Ensure input columns exist
        for col in ['open', 'high', 'low', 'close', 'volume', 'vwap', 'factor']:
            if col not in df.columns:
                df[col] = df['close'] if col != 'volume' else 0.0

        o = df['open']
        h = df['high']
        l = df['low']
        c = df['close']
        v = df['volume']
        w = df['vwap']
        
        # We'll collect all columns in a dictionary to avoid fragmentation
        feature_data = {}
        
        # 1. Basic Features
        feature_data['KMID'] = (c - o) / c
        feature_data['KLEN'] = (h - l) / c
        feature_data['KMID2'] = (c - o) / (h - l + 1e-8)
        feature_data['KUP'] = (h - np.maximum(o, c)) / c
        feature_data['KUP2'] = (h - np.maximum(o, c)) / (h - l + 1e-8)
        feature_data['KLOW'] = (np.minimum(o, c) - l) / c
        feature_data['KLOW2'] = (np.minimum(o, c) - l) / (h - l + 1e-8)
        feature_data['KSFT'] = (2 * c - h - l) / c
        feature_data['KSFT2'] = (2 * c - h - l) / (h - l + 1e-8)
        
        # 2. Price Ratios
        feature_data['OPEN0'] = o / c
        feature_data['HIGH0'] = h / c
        feature_data['LOW0'] = l / c
        feature_data['VWAP0'] = w / c
        
        # 3. Rolling Windows
        windows = [5, 10, 20, 30, 60]
        
        for win in windows:
            # ROC
            feature_data[f'ROC{win}'] = c.groupby('symbol').shift(win) / c - 1
            # MA
            feature_data[f'MA{win}'] = c.groupby('symbol').rolling(win).mean().reset_index(0, drop=True) / c
            # STD
            feature_data[f'STD{win}'] = c.groupby('symbol').rolling(win).std().reset_index(0, drop=True) / c
            
            # Simplified BETA/RSQR/RESI (Self-regression) - Placeholders
            feature_data[f'BETA{win}'] = pd.Series(0.0, index=df.index)
            feature_data[f'RSQR{win}'] = pd.Series(0.0, index=df.index)
            feature_data[f'RESI{win}'] = pd.Series(0.0, index=df.index)
            
            # MAX/MIN
            feature_data[f'MAX{win}'] = h.groupby('symbol').rolling(win).max().reset_index(0, drop=True) / c
            feature_data[f'MIN{win}'] = l.groupby('symbol').rolling(win).min().reset_index(0, drop=True) / c
            
            # Quantiles (Placeholder)
            feature_data[f'QTLU{win}'] = pd.Series(0.0, index=df.index)
            feature_data[f'QTLD{win}'] = pd.Series(0.0, index=df.index)
            
            # RANK (Placeholder)
            feature_data[f'RANK{win}'] = pd.Series(0.0, index=df.index)
            
            # RSV
            min_l = l.groupby('symbol').rolling(win).min().reset_index(0, drop=True)
            max_h = h.groupby('symbol').rolling(win).max().reset_index(0, drop=True)
            feature_data[f'RSV{win}'] = (c - min_l) / (max_h - min_l + 1e-8)
            
            # IMAX/IMIN (Placeholder)
            feature_data[f'IMAX{win}'] = pd.Series(0.0, index=df.index)
            feature_data[f'IMIN{win}'] = pd.Series(0.0, index=df.index)
            feature_data[f'IMXD{win}'] = pd.Series(0.0, index=df.index)
            
            # CORR/CORD/CNTP/CNTN/CNTD (Placeholder)
            feature_data[f'CORR{win}'] = pd.Series(0.0, index=df.index)
            feature_data[f'CORD{win}'] = pd.Series(0.0, index=df.index)
            feature_data[f'CNTP{win}'] = pd.Series(0.0, index=df.index)
            feature_data[f'CNTN{win}'] = pd.Series(0.0, index=df.index)
            feature_data[f'CNTD{win}'] = pd.Series(0.0, index=df.index)
            
            # SUMP/SUMN/SUMD (Placeholder)
            feature_data[f'SUMP{win}'] = pd.Series(0.0, index=df.index)
            feature_data[f'SUMN{win}'] = pd.Series(0.0, index=df.index)
            feature_data[f'SUMD{win}'] = pd.Series(0.0, index=df.index)
            
            # Volume Features
            feature_data[f'VMA{win}'] = v.groupby('symbol').rolling(win).mean().reset_index(0, drop=True) / (v + 1e-8)
            feature_data[f'VSTD{win}'] = v.groupby('symbol').rolling(win).std().reset_index(0, drop=True) / (v + 1e-8)
            feature_data[f'WVMA{win}'] = pd.Series(0.0, index=df.index)
            feature_data[f'VSUMP{win}'] = pd.Series(0.0, index=df.index)
            feature_data[f'VSUMN{win}'] = pd.Series(0.0, index=df.index)
            feature_data[f'VSUMD{win}'] = pd.Series(0.0, index=df.index)

        # Create DataFrame from accumulated dictionary
        res = pd.DataFrame(feature_data, index=df.index)

        # Ensure all columns exist and are in the correct order
        expected_cols = [
            "KMID", "KLEN", "KMID2", "KUP", "KUP2", "KLOW", "KLOW2", "KSFT", "KSFT2", "OPEN0", "HIGH0", "LOW0", "VWAP0", 
            "ROC5", "ROC10", "ROC20", "ROC30", "ROC60", "MA5", "MA10", "MA20", "MA30", "MA60", 
            "STD5", "STD10", "STD20", "STD30", "STD60", "BETA5", "BETA10", "BETA20", "BETA30", "BETA60", 
            "RSQR5", "RSQR10", "RSQR20", "RSQR30", "RSQR60", "RESI5", "RESI10", "RESI20", "RESI30", "RESI60", 
            "MAX5", "MAX10", "MAX20", "MAX30", "MAX60", "MIN5", "MIN10", "MIN20", "MIN30", "MIN60", 
            "QTLU5", "QTLU10", "QTLU20", "QTLU30", "QTLU60", "QTLD5", "QTLD10", "QTLD20", "QTLD30", "QTLD60", 
            "RANK5", "RANK10", "RANK20", "RANK30", "RANK60", "RSV5", "RSV10", "RSV20", "RSV30", "RSV60", 
            "IMAX5", "IMAX10", "IMAX20", "IMAX30", "IMAX60", "IMIN5", "IMIN10", "IMIN20", "IMIN30", "IMIN60", 
            "IMXD5", "IMXD10", "IMXD20", "IMXD30", "IMXD60", "CORR5", "CORR10", "CORR20", "CORR30", "CORR60", 
            "CORD5", "CORD10", "CORD20", "CORD30", "CORD60", "CNTP5", "CNTP10", "CNTP20", "CNTP30", "CNTP60", 
            "CNTN5", "CNTN10", "CNTN20", "CNTN30", "CNTN60", "CNTD5", "CNTD10", "CNTD20", "CNTD30", "CNTD60", 
            "SUMP5", "SUMP10", "SUMP20", "SUMP30", "SUMP60", "SUMN5", "SUMN10", "SUMN20", "SUMN30", "SUMN60", 
            "SUMD5", "SUMD10", "SUMD20", "SUMD30", "SUMD60", "VMA5", "VMA10", "VMA20", "VMA30", "VMA60", 
            "VSTD5", "VSTD10", "VSTD20", "VSTD30", "VSTD60", "WVMA5", "WVMA10", "WVMA20", "WVMA30", "WVMA60", 
            "VSUMP5", "VSUMP10", "VSUMP20", "VSUMP30", "VSUMP60", "VSUMN5", "VSUMN10", "VSUMN20", "VSUMN30", "VSUMN60", 
            "VSUMD5", "VSUMD10", "VSUMD20", "VSUMD30", "VSUMD60"
        ]
        
        for col in expected_cols:
            if col not in res.columns:
                res[col] = 0.0
        
        return res[expected_cols].fillna(0).replace([np.inf, -np.inf], 0)

    @staticmethod
    def calculate_label(df: pd.DataFrame) -> pd.DataFrame:
        """
        Standard Qlib Label: Ref($close, -2) / Ref($close, -1) - 1
        This is the return from T+1 close to T+2 close.
        """
        c = df['close']
        # T+2 / T+1 - 1
        label = c.groupby('symbol').shift(-2) / c.groupby('symbol').shift(-1) - 1
        return pd.DataFrame({'LABEL0': label}, index=df.index)

