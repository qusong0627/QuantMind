import pandas as pd
from sqlalchemy import create_engine

def sync_table():
    remote_url = "postgresql+psycopg2://quantmind:9d4e1f8a2c7b6035e8f1a9c2d4b7e6f0@210.16.175.87:5432/quantmind"
    local_url = "postgresql+psycopg2://quantmind:quantmind2026@localhost:5432/quantmind"

    table_name = "stock_daily_latest"

    print("Connecting to remote database: 210.16.175.87")
    try:
        remote_engine = create_engine(remote_url)
        print(f"Reading data from {table_name}...")
        df = pd.read_sql_table(table_name, remote_engine)
        print(f"Successfully read {len(df)} rows.")
    except Exception as e:
        print(f"Error reading from remote: {e}")
        return

    print("Connecting to local database: localhost")
    try:
        local_engine = create_engine(local_url)
        print(f"Writing data to local {table_name} (replacing existing)...")
        # Use if_exists='replace' to recreate the table with data, or 'append' if schema is already there
        # 'replace' is safer for full sync of a 'latest' table.
        df.to_sql(table_name, local_engine, if_exists='replace', index=False)
        print(f"Successfully synced {len(df)} rows to local database.")
    except Exception as e:
        print(f"Error writing to local: {e}")

if __name__ == "__main__":
    sync_table()
