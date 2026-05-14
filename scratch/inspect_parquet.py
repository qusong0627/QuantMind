import pandas as pd

file_path = "/Users/qusong/git/quant/db/custom/fundamental_aligned.parquet"
df = pd.read_parquet(file_path)
print("All Columns:", df.columns.tolist())

# Look for price columns
price_cols = [c for c in df.columns if any(p in c.lower() for p in ['open', 'high', 'low', 'close', 'price'])]
print("Price Columns found:", price_cols)

if 'symbol' in df.columns and 'trade_date' in df.columns:
    stock = 'SH600036'
    subset = df[df['symbol'] == stock].sort_values('trade_date')
    print(f"\nLast 10 days for {stock}:")
    # Display trade_date and price columns
    display_cols = ['trade_date', 'symbol'] + price_cols
    print(subset[display_cols].tail(10))
else:
    print("\nColumns 'symbol' or 'trade_date' not found.")
