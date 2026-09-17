---
name: realtime-quotes-tdx
description: "通达信桥实时行情直读（准确引导版）— 任意 A 股/指数的实时快照（最新价、五档盘口、现量、昨收）、当日日K实时 bar、桥健康与行情新鲜度（quote 停更检测：日K最后 bar 日期 vs 北京今天）。用户问「XX 现在什么价」「实时行情」「盘口五档」「最新价」「指数现在多少」「今天涨跌」时使用。触发词：实时行情、实时价格、最新价、盘口、五档、买一卖一、现价、指数点位、看盘、TDX、通达信、行情停更"
---

> ## ⚙️ 运行环境契约（最高优先级，先于本文其余内容执行）
>
> 1. **桥地址/Token**：`TDX_BRIDGE_URL`（Windows 交易机通达信桥，默认 `http://192.168.31.13:8550`）+ `TDX_BRIDGE_TOKEN`。取值优先级：环境变量 → `/quantmind/.env` / `/quantmind/runtime_env_cn.json` → 技能默认值。Token 缺失时只读 `/api/v1/health`（免鉴权），其它接口一律 401。
> 2. **必须在宿主机或 host 网络环境直连桥**（Linux 侧 192.168.31.x 可达）；quantmind 容器内如网络隔离则退回 `docker exec` 外宿主机执行。
> 3. **新鲜度铁律**：桥可能"假活"（健康检查绿但行情停更，2026-09 实战教训）。**每次报实时价前先验行情新鲜度**：日K最后一根 bar 必须 ≥ 北京今天（盘后/周末顺延），快照的 Volume 必须 > 0 且与昨收合理；停更时明确告知「行情疑似停更，价格可能陈旧」，绝不把缓存价当实时价报。
> 4. 报告/速览落盘按需写 `/data/reports/`；纯查价不需要落盘。

# realtime-quotes-tdx — 通达信桥实时行情直读

## 1. 接口速查（桥 HTTP，POST JSON-RPC，Bearer Token）

| 目的 | 接口 | 关键返回 |
|---|---|---|
| 健康+新鲜度 | `GET /api/v1/health` | `tdx_connected`（仅链路通）；新版含 `quote_fresh`（行情新鲜） |
| 实时快照（五档） | `POST /api/v1/tdx/call get_market_snapshot {stock_code}` | `Now/LastClose/Volume/Open/High/Low` + `Buyp[5]/Sellp[5]/Buyv[5]/Sellv[5]` |
| 实时日K bar | `POST /api/v1/tdx/call get_market_data {stock_list:[],period:"1d",count:5}` | `Date/Close/Volume/Amount` 数组（最后一根=今日实时 bar） |
| 指数快照 | 同上，代码如 `000001.SH`（上证）、`000300.SH`、`399006.SZ` | 同 snapshot 结构 |

## 2. 标准流程

1. **探活+验鲜**：`health` → 通；再取 `600519.SH`（茅台）日K最后 bar 日期与北京今天比对。
2. **买单票**：snapshot 该代码 → 报 最新价/涨跌幅（vs LastClose）/开高低/现量/五档（买一价量、卖一价量）。
3. **买多票/指数**：循环 snapshot（桥限流 1 秒间隔）；指数代码表：000001.SH 上证、000016.SH 上证50、000300.SH 沪深300、399001.SZ 深成、399006.SZ 创业板、000688.SH 科创50。
4. **盘中 vs 盘后**：北京 9:30-11:30 / 13:00-15:00 为盘中；盘后/午休报「最新价=收盘价（今日 bar），非实时变动」。
5. 输出统一为：`代码 名称? 现价 涨跌幅% 开/高/低 成交量 买一(价/量) 卖一(价/量) [新鲜度状态]`。

## 3. 新鲜度判定细节（防假活）

- 日K最后 bar 日期 < 北京今天（工作日且不遇停牌）→ **停更告警**；仅当桥刚恢复（bar 出现今天）才解除。
- 快照 `ErrorId != 0` → 该股数据不可用，如实说明，不编价格。
- 连续两次（间隔 ≥5 秒）快照 Volume 完全不变且盘中 → 高度疑似停更，提示复验。

## 4. 脚本

`scripts/tdx_realtime.py <snapshot|daily|health|index> [CODE ...]`：
- `snapshot`：单/多代码五档快照（含涨跌幅计算）；
- `daily`：各代码当日实时日K bar（含新鲜度自检）；
- `health`：桥链路+新鲜度一键报告；
- `index`：六大指数快照。
纯标准库（urllib），宿主机/dsh host 网络直接跑；Token 自动从环境→/quantmind/.env→`.env` 解析。