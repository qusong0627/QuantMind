---
name: tdx-bridge
description: "通达信桥行情数据统一入口：三源实时行情（同花顺 / TDX 免费 / TdxAiData 付费）、五档盘口、主力净流入与 DDE、板块全景与成分、短线精灵异动、集合竞价、逐笔、分钟/日 K、实时订阅与健康状态。用户要查实时行情、盘口、资金流、板块、异动、竞价、逐笔，或要直接调用 Windows 桥的 /api/v1/* 数据接口时使用。触发词：桥接口、统一行情、三源行情、同花顺旁路、TDX 行情、板块成分、短线精灵、异动、主力净流入、盘口、竞价、逐笔。"
---

# tdx-bridge — 通达信桥数据接口

把 Windows 行情桥（`http://<windows-ip>:8550`）的全部数据接口封装成可直接调用的技能。
**默认用自带脚本**，不要手写 HTTP；脚本已处理 token、代码归一化、错误码与超时。

## 快速开始

```bash
B=.claude/skills/tdx-bridge/scripts/bridge.py

python3 $B health                     # 桥/TQ 健康
python3 $B quote 600519.SH            # 统一三源行情（推荐）
python3 $B quote 002415.SZ --aidata   # 额外查付费源（按次计费）
python3 $B compare 600519.SH          # TDX vs THS 同尺对照
python3 $B ths depth --params '{"code":"600519.SH","levels":5}'
python3 $B ths flow  --params '{"codes":"600519"}'
python3 $B dxjl --limit 20            # 短线精灵异动
python3 $B boards                     # 2768 个板块
python3 $B tdx get_more_info --params '{"stock_code":"002415.SZ"}'
python3 $B aidata get_minute_data --params '{"stock_code":"002415.SZ","date":"2026-10-23"}'
```

连接参数（脚本按此顺序解析）：

| 参数 | 来源 | 默认 |
|---|---|---|
| 地址 | `QM_BRIDGE_URL` | `http://192.168.31.250:8550` |
| Token | `QM_BRIDGE_TOKEN` → `TDX_BRIDGE_TOKEN` → `docker exec quantmind printenv TDX_BRIDGE_TOKEN` | 无（缺失会报错） |

## 什么时候用哪个

| 需求 | 用这个 |
|---|---|
| 一只股票的实时价/量/额/盘口/资金面 | `quote`（逐字段合并，自动回落） |
| 只要 TDX 免费快照、要更多字段 | `tdx get_market_snapshot` / `get_more_info` |
| 量化/盘口/资金流首选 | `ths flow`（`main_inflow` 元、`dde_main` 亿） |
| 板块列表/成分/板块报价 | `boards` → `ths board_constituents`（板块 ID 如 `BC9E`） |
| 异动（大笔买入、封板…） | `dxjl` |
| 竞价/逐笔/分钟线（付费） | `aidata get_call_auction/get_tick_data/get_minute_data` |
| 对账/验收 | `compare` + `sources` + `gates` |

## 三条硬口径（必须记住）

1. **单位**：统一行情 `amount=元`、`volume=手`、盘口 `volume=手`；
   TDX 原始 `Amount` 是万元、THS 盘口 `qty` 是股——脚本/桥已归一，自己解析原始接口时要换算。
2. **代码**：入口支持裸码/`SH600519`/`600519.SH`；TDX 底层只认后缀，
   THS 底层只认裸码；`000001.SH`（上证指数）与 `000001.SZ`（平安银行）不可混。
3. **缺失**：统一行情缺失字段为 `null` 并列入 `missing`，**不是 0**；
   `source` 告诉你每个字段来自哪个源，`timings` 告诉你哪个源慢/失败。

## 能力边界（standard 非 L2 账号）

- 10 档盘口 / `stock-ready` / `order-queues` → 403（需 L2）。
- 竞价、历史分时、热榜、板块报价在部分时段会 30s 超时（worker 会自愈，统一行情自动回落 TDX）。
- TDX 分钟线 0 行；pro-data（GP/BK/SC/GO）缺数据包 blocked。
- aidata `get_pricevol` 不可用；付费调用按 200 行/词元、每日预算默认 500。

## 详细资料

- `reference/endpoints.md` — 桥路由 + worker 46 端点 + TDX/ aidata 方法参数
- `reference/fields.md` — 统一字段字典 + 各源原始字段 + 单位 + dt 码 + 能力边界
- `reference/recipes.md` — 批量、板块、榜单、竞价、逐笔、对账、运维配方

## 故障排查

| 现象 | 处理 |
|---|---|
| `缺少 token` | 设 `QM_BRIDGE_TOKEN`（与平台 `.env` 的 `TDX_BRIDGE_TOKEN` 一致） |
| `桥不可达` | 检查 Windows 桥 8550（计划任务 `TDXBridgeWatchdog`）与防火墙 |
| `THS_BUSY` / `THS_CIRCUIT_OPEN` | 直连 `/ths/*` 的瞬时饱和；改用 `quote`（自动回落）或稍后重试 |
| `THS_UNAVAILABLE` + `worker HTTP 504` | worker 上游挂死已触发自愈，等 20s 重试 |
| `AIDATA_TOKEN_REQUIRED` | 网页「数据服务」填付费 Key |
| 同花顺显示未登录 | 看 `/api/v1/ths/status` 的 `worker.connected` 与 `qr.account`；扫码登录后刷新应显示「已登录 · 账号」 |

> 桥的网页控制台：`http://<windows-ip>:8550/ui`（右上角填访问 token）。
