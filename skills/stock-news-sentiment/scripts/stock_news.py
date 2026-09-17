#!/usr/bin/env python3
"""个股新闻拉取（QuantDB PG news 表探测）——在 quantmind 容器内执行。

用法：docker cp stock_news.py quantmind:/tmp/ && \
  docker exec -w /app quantmind python3 /tmp/stock_news.py 601138.SH 7
输出：JSON（新闻原始行 + 表结构信息）；无库/无表时给出可执行提示。
"""
import json
import os
import sys

CODE = sys.argv[1] if len(sys.argv) > 1 else ""
DAYS = int(sys.argv[2]) if len(sys.argv) > 2 else 7
if not CODE:
    print(json.dumps({"ok": False, "error": "用法: stock_news.py <CODE.SH> [天数]"}))
    sys.exit(1)

out = {"ok": False, "code": CODE, "days": DAYS, "rows": [], "note": []}

try:
    import psycopg2

    conn = psycopg2.connect(
        host=os.getenv("QM_PG_HOST", "localhost"),
        port=int(os.getenv("QM_PG_PORT", "5432")),
        user=os.getenv("QM_PG_USER", "quant"),
        password=os.getenv("QM_PG_PASSWORD", "quant"),
        dbname=os.getenv("QM_PG_DB", "quantdb"),
    )
    cur = conn.cursor()
    cur.execute("SELECT table_name FROM information_schema.tables "
                "WHERE table_schema='public' AND table_name LIKE '%news%'")
    tables = [r[0] for r in cur.fetchall()]
    if not tables:
        out["note"].append("PG 无 news 表，本技能降级为实时源路径（见 SKILL.md）")
        print(json.dumps(out, ensure_ascii=False))
        sys.exit(0)
    target = "news_article_enrichment" if "news_article_enrichment" in tables else tables[0]
    cur.execute("SELECT column_name FROM information_schema.columns "
                f"WHERE table_name='{target}'")
    cols = [r[0] for r in cur.fetchall()]
    sym_col = next((c for c in cols if c.lower() in ("symbol", "code", "stock_code")), None)
    time_col = next((c for c in cols if "time" in c.lower() or "date" in c.lower()), None)
    if not sym_col:
        out["note"].append(f"表 {target} 无标准 symbol 列，列：{cols[:14]}")
        print(json.dumps(out, ensure_ascii=False))
        sys.exit(0)
    sym = CODE
    if "." in sym:
        sym = "SH" + sym.split(".")[0] if sym.endswith((".SH", ".SS")) else "SZ" + sym.split(".")[0]
    like = f"%{CODE.split('.')[0]}%"
    q = f"SELECT * FROM {target} WHERE {sym_col} LIKE %s"
    args = [like]
    if time_col:
        q += f" AND {time_col} >= now() - interval '%s day'"
        args.append(str(DAYS))
    q += f" ORDER BY {time_col} DESC LIMIT 50" if time_col else " LIMIT 50"
    cur.execute(q, args)
    rows = cur.fetchall()
    out["ok"] = True
    out["table"] = target
    out["columns"] = cols
    out["rows"] = [dict(zip(cols, r)) for r in rows]
    conn.close()
except Exception as exc:  # noqa: BLE001
    out["error"] = str(exc)
    out["note"].append("PG 连不上时降级为实时 RSS/搜索通道（见 SKILL.md §1.3）。"
                       "若容器内无 QM_PG_* 环境，先在启动命令注入或在 runtime 配置补")
print(json.dumps(out, ensure_ascii=False, default=str))