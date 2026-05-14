import pandas as pd

file_path = "/Users/qusong/git/quant/db/custom/fundamental_aligned.parquet"
df = pd.read_parquet(file_path)
stock = 'SZ000002'
subset = df[df['symbol'] == stock].sort_values('trade_date')
print(f"First 5 rows for {stock}:")
print(subset[['trade_date', 'symbol', 'open', 'close', 'adj_factor']].head(5))
print(f"\nLast 5 rows for {stock}:")
print(subset[['trade_date', 'symbol', 'open', 'close', 'adj_factor']].tail(5))
