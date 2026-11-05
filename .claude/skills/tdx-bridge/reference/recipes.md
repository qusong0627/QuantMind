# 常用配方

> 所有示例用 skill 自带客户端：`python3 .claude/skills/tdx-bridge/scripts/bridge.py ...`
> 环境变量：`QM_BRIDGE_URL`（默认 `http://192.168.31.250:8550`）、
> `QM_BRIDGE_TOKEN` 或 `TDX_BRIDGE_TOKEN`。

## 0. 连通性

```bash
python3 scripts/bridge.py health
curl.exe -s http://192.168.31.250:8550/api/v1/health        # Windows
```

## 1. 单只行情（三源自动平衡）

```bash
python3 scripts/bridge.py quote 600519          # 裸码/前缀/后缀都行
python3 scripts/bridge.py quote SH600519
python3 scripts/bridge.py quote 600519.SH --prefer ths,tdx
python3 scripts/bridge.py quote 002415.SZ --aidata --prefer ths,aidata,tdx
```

返回重点：`data`（合并值）、`source`（字段来源）、`timings`（各源耗时）、`missing`。

## 2. 多只行情（Python）

```python
import json, urllib.parse, urllib.request, os
BASE = os.environ.get("QM_BRIDGE_URL", "http://192.168.31.250:8550")
TOKEN = os.environ.get("QM_BRIDGE_TOKEN") or os.environ["TDX_BRIDGE_TOKEN"]
def quote(code, prefer="ths,tdx"):
    url = f"{BASE}/api/v1/market/quote?" + urllib.parse.urlencode({"code": code, "prefer": prefer})
    req = urllib.request.Request(url, headers={"Authorization": "Bearer " + TOKEN})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read())["data"]
rows = [quote(c) for c in ("600519.SH", "002415.SZ", "300750.SZ")]
for r in rows:
    print(r["code"], r["data"].get("last"), r["source"].get("last"), r["timings"])
```

> 批量不要超过 20~30 并发：THS 侧是串行管道，聚合层会回落 TDX（仍可用，但会增延迟）。
> 需要更快时，用 TDX 的 `get_pricevol`（一次多代码）或 `get_market_snapshot_batch`（aidata）。

## 3. 五档盘口 / 资金流

```bash
python3 scripts/bridge.py ths depth --params '{"code":"600519.SH","levels":5}'
python3 scripts/bridge.py ths flow  --params '{"codes":"600519"}'
```
- `depth` 返回 `buy/sell[{level,price,qty(股),amount}]`；**10 档需 L2**，普通账号 403。
- `flow` 的 `main_inflow` 单位元、`dde_main` 单位亿。

## 4. 板块：先找 ID，再取成分/报价

```bash
python3 scripts/bridge.py boards > /tmp/boards.json
python3 - <<'PY'
import json
boards = json.load(open("/tmp/boards.json"))["data"]
hits = [b for b in boards if "安防" in b["name"]]
print(hits[:5])          # 拿 block_id，例如 BC9E
PY
python3 scripts/bridge.py ths board_constituents --params '{"code":"BC9E"}'
python3 scripts/bridge.py ths board_quotes       --params '{"code":"BC9E"}'
```
> 板块 ID 是 `BC9E` 这类 4 位字母数字，不是股票代码；桥侧有独立校验。
> `board_quotes`/`board_timeline`/`board_kline` 在 standard 账号上可能上游超时（30s 504），
> 优先用 `board_constituents`（稳定，秒级）。

## 5. 榜单 / 异动

```bash
python3 scripts/bridge.py ths hot             --params '{"count":20,"with_names":true}'
python3 scripts/bridge.py ths dde_rank        --params '{"count":20}'
python3 scripts/bridge.py ths stock_list_ranked --params '{"count":20}'
python3 scripts/bridge.py dxjl --limit 20
python3 scripts/bridge.py aidata get_domain_sorthq --params '{"domain":6,"colname":"ZAF","wantnum":20}'
```

## 6. 集合竞价 / 逐笔 / 分钟线（付费，注意预算）

```bash
# 集合竞价 3 秒切片（1 词元）
python3 scripts/bridge.py aidata get_call_auction --params '{"stock_code":"002415.SZ","type":1}'
# 逐笔：wantnum 限行，50 行 = 1 词元
python3 scripts/bridge.py aidata get_tick_data --params '{"stock_code":"002415.SZ","date":"2026-10-23","startxh":0,"wantnum":50}'
# 分钟线（当日 ~169 根 = 1 词元）
python3 scripts/bridge.py aidata get_minute_data --params '{"stock_code":"002415.SZ","date":"2026-10-23"}'
```
> 每日硬预算默认 500 词元；`GET /api/v1/aidata/status` 看 `usage`。
> `wantnum=0` = 不限行，可能一次几十词元，批量抓取前先估算。

## 7. 自动跟随实盘持仓（免费 tq ≤100）

```bash
python3 scripts/bridge.py live-codes                 # 持仓代码
python3 scripts/bridge.py live-codes --include-orders
python3 scripts/bridge.py auto                       # 查看自动跟随状态
python3 scripts/bridge.py auto --enable --interval 30 --mode merge
python3 scripts/bridge.py auto --disable
```
开启后桥每 `interval` 秒读一次账户，代码集合变化才重启订阅（纯顺序变化不重启）；
`merge` 保留手动添加的代码，`replace` 只留账户代码；超过通道上限（tq=100）时截断并在
`auto.truncated` 报告。网页「实时订阅」面板有同名开关；同花顺面板「跟随持仓」按钮
用 `/subscription/live-codes` 载入代码并开启 5s 实时轮询。

## 7.5 对账

```bash
python3 scripts/bridge.py compare 600519.SH
```
返回 `tdx`/`ths` 两套归一化字段 + `diff_pct`。盘中看成交量/额；收盘后只看价格。

## 8. 运维

```bash
python3 scripts/bridge.py status     # 桥侧 THS 状态 + worker + qr 账号
python3 scripts/bridge.py gates      # worker 闸门/超时/连接（真实来源 /api/bridge/gates 仅本机）
python3 scripts/bridge.py sources    # 各源成功率/延迟
curl.exe -s -H "Authorization: Bearer %TOKEN%" http://127.0.0.1:8550/api/v1/stats
```

## 9. 常见坑

1. **TDX 只认后缀码**（`600519.SH`）；THS worker 只认裸码（桥已自动转换）；
   统一行情入口已做归一化，直接传裸码即可。
2. **指数与个股同码**：`000001.SH`（上证指数）与 `000001.SZ`（平安银行）不能混，
   桥的裸码转换会保留有歧义的后缀。
3. **单位**：TDX `Amount` 是万元（已归一）；THS 盘口 `qty` 是股（已归一成手）。
4. **忙/熔断**：直连 `/ths/*` 可能收到 503 `THS_BUSY`/`THS_CIRCUIT_OPEN`；
   统一行情会自动回落 TDX，不需要自己重试。
5. **收盘后**不要拿成交量/成交额做三源硬对账（收盘集合竞价口径不同）。
