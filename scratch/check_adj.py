import pandas as pd

file_path = "/Users/qusong/git/quant/db/custom/fundamental_aligned.parquet"
df = pd.read_parquet(file_path)
stock = 'SH600036'
subset = df[df['symbol'] == stock].sort_values('trade_date').tail(5)
print(subset[['trade_date', 'symbol', 'open', 'close', 'adj_factor']])
