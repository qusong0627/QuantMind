"""QuantMind 便携版 PostgreSQL 启动辅助。

便携 PostgreSQL 二进制(zonky)只含 initdb/pg_ctl/postgres，
不带 psql/createdb/pg_isready，因此就绪探测与幂等建库改由
包内 Python + psycopg2 完成。全部凭据从环境变量读取。

用法:
    python pg_setup.py wait --timeout 60     # 等待 PostgreSQL 可连接
    python pg_setup.py ensure-db             # 幂等创建 DB_NAME 数据库
"""
import os
import sys
import time


def _conn(dbname: str):
    import psycopg2

    return psycopg2.connect(
        host=os.environ.get("DB_HOST", "127.0.0.1"),
        port=int(os.environ.get("DB_PORT", "5432")),
        user=os.environ.get("DB_USER", "quantmind"),
        password=os.environ.get("DB_PASSWORD", "quantmind2026"),
        dbname=dbname,
        connect_timeout=3,
    )


def wait_ready(timeout: int = 60) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            _conn("postgres").close()
            return True
        except Exception:
            time.sleep(1)
    return False


def ensure_db() -> None:
    name = os.environ.get("DB_NAME", "quantmind")
    conn = _conn("postgres")
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM pg_database WHERE datname=%s", (name,))
            if cur.fetchone() is None:
                cur.execute('CREATE DATABASE "%s"' % name.replace('"', ""))
                print("created:%s" % name)
    finally:
        conn.close()


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "wait"
    if cmd == "wait":
        timeout = 60
        if "--timeout" in sys.argv:
            timeout = int(sys.argv[sys.argv.index("--timeout") + 1])
        sys.exit(0 if wait_ready(timeout) else 1)
    elif cmd == "ensure-db":
        ensure_db()
    else:
        print("unknown command: %s" % cmd, file=sys.stderr)
        sys.exit(2)
