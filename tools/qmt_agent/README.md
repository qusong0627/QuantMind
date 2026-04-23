# QMT Agent

当前目录提供的是 **新桥接方案上的正式 QMT Agent 实现**。它按独立程序交付，面向客户服务端或专用 Windows 主机长期运行；除了 CLI 桥接进程外，还新增了 Windows 桌面壳入口 `desktop_app.py`，用于 GUI 配置、托盘常驻和本地控制接口。

## 当前交付形态

- 当前仓库交付的是 **独立 QMT Agent 源码包**，可单独打包为 Windows `.exe` / 安装器，不与 Electron 混合打包。
- 新增桌面壳入口：`python tools/qmt_agent/desktop_app.py`
- 前端“QMT 实盘连接中心”下载的是 `qmt_agent_client.zip` 独立部署包。
- `qmt_agent_client.zip` 内包含：
  - `qmt_agent.py`
  - `desktop_app.py`
  - `requirements.txt`
  - `README.md`
  - 脱敏后的 `qmt_agent_config.json` 模板
  - `_callback.py`
  - `build_windows_agent.py`
  - `qmt_agent_desktop.spec`
  - `qmt_agent_setup.iss`
  - `version.json`
  - `help.md`
  - `qmt_agent_reference.py`
- 独立部署原则：
  - Electron 仅负责凭证初始化、在线状态查看与下载入口；
  - QMT Agent 在客户服务端独立安装、独立启动、独立长期运行；
  - 关闭 Electron 前端不应影响已运行的 QMT Agent 交易链路。

## 版本管理规则

- 版本源唯一文件：[`version.json`](/Users/qusong/git/quantmind/tools/qmt_agent/version.json)
- 每次有代码变更并产出新安装包时，必须递增 `version`（至少补丁号）。
- Windows 打包脚本会自动使用 `version.json` 生成 `client_version`（`{version}-desktop`），避免硬编码版本漂移。

## 功能

- 使用 `access_key + secret_key` 换取短期 `bridge_session_token`
- 使用短期 token 连接 `ws/bridge`
- 周期上报账户快照与心跳
- 接收下单消息并通过 `xtquant` 真实调用 QMT 下单
- 支持同步/异步下单（`order_stock` / `order_stock_async`）与同步/异步撤单（`cancel_order_stock` / `cancel_order_stock_async`）
- Bridge 下单支持 Agent 侧“查价后转保护限价单”：
  - 当 payload 带 `agent_price_mode=protect_limit` 时，Agent 会在临下单前读取 Level1 盘口
  - 买单使用 `askPrice[0]`、卖单使用 `bidPrice[0]`
  - 再按 `protect_price_ratio`（若未传则回退 `max_price_deviation`，默认 `0.002`）加减保护，最终仍以 QMT `FIX_PRICE` 限价单提交
- 接收撤单消息时仅向 QMT 发起撤单请求，不在本地提前确认 `CANCELLED`，最终状态以 QMT 回调为准
- Agent 会维护 `client_order_id -> exchange_order_id` 映射，支持在桥接撤单时优先按 `client_order_id` 解析真实柜台单号
- QMT 状态码映射口径统一（2026-03-23）：
  - `50 -> SUBMITTED`（已报未成）
  - `56 -> FILLED`（已成）
  - `57 -> REJECTED`（拒单/无效）
- QMT 断线自动重连
- WebSocket / session 自动刷新与重连
- Agent 会周期发送应用层 `{"type":"ping"}` 保活消息，避免 stream 按应用心跳判定超时断开 bridge 连接
- 已接入关键错误/状态回调：`on_order_error`、`on_cancel_error`、`on_account_status`
- 已接入异步下单/撤单回报：`on_order_stock_async_response`、`on_cancel_order_stock_async_response`
- Agent 启动后会执行一次补偿查询（`query_stock_orders` + `query_stock_trades`）并回写执行事件，降低断线窗口状态缺失风险
- 实盘账户快照上报会优先做一次同步 QMT 采样，再写入 PostgreSQL 和 Redis；资产/持仓异步回调仅用于刷新缓存，避免把中间态拼成两组交替快照
- Agent 主循环现在带运行看门狗：`bridge-websocket` / `bridge-heartbeat` / `bridge-account` / `qmt-reconnect` 等后台线程会被持续监控，心跳或快照超时、关键线程退出时会自动记录 `runtime_health` 并触发重连
- CLI 入口新增崩溃自动重启监督：`qmt_agent.py` 默认开启“指数退避 + 时间窗口限流”的自动拉起策略（可用 `--disable-auto-restart` 关闭）
- 大批量委托场景下，Bridge 消息已改为“先入有界派单队列，再由 `bridge-order-dispatch` 独立线程按固定节奏出队提交 QMT”：
  - `on_message` 不再直接调用 `order_stock(_async)`，避免 WebSocket 收到突发订单时把本地柜台线程瞬时打满
  - 撤单使用更高优先级，避免大批量新单把撤单请求饿死
  - 队列满时会立即回写 `REJECTED`，错误信息包含“派单队列已满”
  - `runtime_status.dispatch_metrics` 会透出队列深度、丢弃数、最近等待时长和最近提交类型，便于桌面端/排障页观测
- 当本机未安装 `xtquant` 时自动退化为 mock 模式，便于联调
- 本地桌面 GUI：手动录入 Access Key / Secret Key、扫描 QMT 路径、手填资金账号
- 总览页会显示 Agent 状态、QMT 连接、云端桥接、运行健康、最近心跳、最近账户快照、版本、主机名和客户端指纹
- 桌面壳“云端连接”仅根据真实 bridge 连接状态显示，不再把“测试云端连接成功”误当作持续在线；当运行健康异常时会同步显示为异常态
- 总览页额外展示最近启动时间、工作线程存活状态、心跳/账户上报年龄与最后错误，便于排查“只发首包后停止”这类问题
- 连接配置页支持编辑心跳/账户快照/重连/WS Ping 等高级参数
- 诊断页支持查看最近错误、导出脱敏诊断包
- 桌面壳会把运行日志轮转写入 `%APPDATA%\QuantMindQMTAgent\desktop.log`，并支持从 UI 切换日志级别
- 桌面壳运行时会强制使用当前包版本生成 `client_version={APP_VERSION}-desktop`，并对关键轮询参数应用安全下限（心跳>=10s、账户快照>=20s、重连>=3s），避免旧配置导致误判“首包后失活”
- 桌面壳“帮助中心”会直接读取同目录的 `help.md`，展示本地启动、日志目录与排障命令，包括 `--open-log-dir` 和 `--show-log-path`
- 本地 loopback 控制接口：供独立桌面壳、后续运维工具或安装器查询本地状态
- 桌面运行时新增监督重启：当 Agent 线程异常退出时，会按 `3s -> 6s -> 12s ...` 指数退避自动重启，并在窗口超限后停止重启（避免故障抖动）
- 当前版本已彻底移除 Windows 开机自启动能力；桌面壳每次启动时会自动清理旧版本遗留的任务计划与注册表自启动项，避免历史配置继续拉起程序

## 桌面 UI（现代化升级）

- 升级日期：`2026-03-14`
- 设计目标：提升信息层级、状态可读性与高频操作效率，不改动后端协议和本地控制接口。
- 主要变化：
  - 全局视觉重构：深色 Hero + 亮色卡片体系，统一间距与边框阴影风格。
  - 顶部快速操作：在头部增加 `启动 / 停止 / 重启`，减少跨页切换。
  - 状态卡片增强：更明显的状态点与文字层级，便于快速判断 Agent/QMT/桥接状态。
  - 表单可用性增强：输入框焦点高亮、按钮悬停反馈、配置/控制/诊断卡片统一样式。
  - 运行与诊断面板优化：日志与错误区标题视觉统一，摘要信息更易扫描。
- 兼容性说明：
- 仅变更 `desktop_app.py` 的 UI 呈现层；
- 不影响 `qmt_agent.py` 运行逻辑、Bridge API、本地 loopback 端口与接口路径。

## 打包修复（2026-04-09）

- 修复问题：Windows 打包版启动后日志出现 `xtdata unavailable: No module named 'uuid'`。
- 根因：PyInstaller 打包未显式包含 `uuid`，导致 `xtquant.xtdata` 导入链在部分机器上失败。
- 修复内容：
  - `qmt_agent_desktop.spec` 显式加入 `uuid` hidden import；
- `desktop_app.py` 总览页新增 `运行健康`、`工作线程`、`最近心跳/账户年龄` 与 `最后错误` 字段，便于判断是否仅完成首包上报后后台循环停止。
  - `desktop_app.py` 启动 Agent 与“测试 QMT”前都会显式校验 `xtquant/xtdata` 依赖，缺失时直接阻止启动，不再进入“首包成功后半残运行”的状态。

## 稳定性修复（2026-03-14）

- 修复问题：点击“启动 Agent”后报错 `"'>' not supported between instances of 'str' and 'int'"`。
- 根因：桌面配置页的“高级参数”（心跳/重连/Ping 等）来自 `QLineEdit`，保存时可能以字符串写入配置；运行时在 `max(1, cfg.xxx)` 比较时触发 `str/int` 类型冲突。
- 修复内容：
  - 在 `qmt_agent.py` 增加 `normalize_agent_config_data()`，统一将数值配置转换为整数并做下限保护。
  - `desktop_app.py` 的保存与启动构建配置路径均调用归一化函数。
  - `validate_config_dict()` 增加数值字段整数与正数校验，错误会在 UI 侧直接提示。

## 稳定性修复（2026-03-24）

- 修复问题：部分 QMT 回调会把 `order_id=-1` 误当作 `client_order_id` 上报，导致服务端出现 `bridge/execution 404 order not found`，最终触发 `bridge_ack_timeout`。
- 修复内容：
  - `client.py`：`_extract_client_order_id()` 不再回退到 `order_id`，仅信任 `order_remark/order_sysid`。
  - `client.py`：新增 `exchange_order_id -> client_order_id` 反向映射与统一合法性校验，回调可按 `seq/exchange_order_id` 兜底恢复真实 `client_order_id`。
  - `_callback.py`：`on_stock_order/on_stock_trade/on_order_error/...` 全链路统一先解析合法 `client_order_id`，无法解析时跳过并记录警告，不再上报 `-1`。
  - `agent.py`：上报前增加最终校验，拦截无效 `client_order_id`。

## 打包兼容性修复（2026-03-25）

- 修复问题：Windows 独立包在某些 PyInstaller 环境下启动时报 `attempted relative import with no known parent package`，随后误导入到第三方 `config` 包，导致 `ImportError: cannot import name 'AgentConfig'`。
- 修复内容：
  - `qmt_agent.py`、`agent.py`、`auth.py`、`client.py`、`reporter.py` 的回退导入改为按当前文件路径显式加载本地模块。
  - 这样即使运行环境中存在同名 `config` 模块，也会优先使用 `tools/qmt_agent/config.py`。
  - PyInstaller 打包时会把这些运行时依赖源码作为解包资源一并带入 onefile 产物，保证回退加载器能找到实际文件。
  - `requests` 和 `websocket-client` 已加入打包的显式依赖列表，避免 onefile 产物遗漏网络通信库。

## 启动链自检（2026-03-25）

- `build_windows_agent.py` 现在会在正式打包前执行两步检查：
  - 静态审计 `desktop_app.py -> qmt_agent.py -> agent.py/auth.py/client.py/reporter.py` 的第三方 import，防止新增动态依赖后没有同步更新打包配置。
  - 运行一次隔离导入烟测，确保这条启动链在当前源码布局下可以完整加载。
- 当 `xtquant` 加载失败时，桌面壳会把底层异常一并展示出来，便于区分是 `qmt_bin_path` 目录错误还是 `xtquant` 包缺失/损坏。

## 鉴权链路

1. 用户在前端初始化或重置 `Access Key / Secret Key`
2. Agent 调用 `POST /api/v1/internal/strategy/bridge/session`
3. 服务端返回短期 `bridge_session_token`
4. Agent 使用短期 token 连接 `wss://.../ws/bridge`
5. Agent 持续调用：
   - `POST /api/v1/internal/strategy/bridge/account`
   - `POST /api/v1/internal/strategy/bridge/heartbeat`
   - `POST /api/v1/internal/strategy/bridge/execution`

`bridge/account` 的写入策略已经收敛为：

- 每次账户上报先持久化到 PostgreSQL 的 `real_account_snapshots`
- 写库成功后，再更新 Redis 作为短期缓存与推送源
- `asset_updated / positions_updated` 连续抖动会在 Agent 侧做短窗口合并，避免把中间态连续写进数据库后再推到前端

`bridge/account` 当前快照建议至少包含：

- 账户资金：`cash / available_cash / frozen_cash / total_asset / market_value`
- 盈亏口径：`today_pnl / total_pnl / floating_pnl`
- 持仓价格：`positions[].cost_price / positions[].last_price`（Agent 已内置多字段兜底，避免缺省为 0）
- 盈亏字段映射增强（2026-03-23）：`today_pnl/total_pnl` 会优先匹配常见柜台字段并自动扫描 `asset` 数值字段（`*profit/*pnl/*income`），若累计盈亏缺失则回退使用 `floating_pnl`，降低“柜台有盈亏但上报恒 0”的概率。

注意：

- 旧的 `qm_live_*` 直连 `ws/bridge` 已废弃。
- `REAL` 门禁依赖 **账户快照 + 心跳** 双信号，任一缺失都会导致启动前检查失败。

稳定性建议：

- 如果桥接环境存在短时网络抖动或云端偶发高延迟，建议将 `ws_ping_interval_seconds` 调到 `60`、`ws_ping_timeout_seconds` 调到 `20`，并把 `heartbeat_interval_seconds` / `account_report_interval_seconds` 适度放宽。
- CLI 入口会把运行日志轮转写到 `%APPDATA%\\QuantMindQMTAgent\\qmt_agent.log`，也可通过 `QMT_AGENT_LOG_PATH` 自定义路径。
- 桌面壳入口会把运行日志轮转写到 `%APPDATA%\QuantMindQMTAgent\desktop.log`，日志级别可通过界面实时切换，未捕获异常也会写入该文件。

## 启动

```bash
python tools/qmt_agent/qmt_agent.py --config qmt_agent_config.json
python tools/qmt_agent/qmt_agent.py --config qmt_agent_config.json --disable-auto-restart

# 打开日志目录并退出
python tools/qmt_agent/qmt_agent.py --open-log-dir

# 打印日志文件路径并退出
python tools/qmt_agent/qmt_agent.py --show-log-path

# 桌面版（GUI + 本地控制接口）
python tools/qmt_agent/desktop_app.py

# Windows 独立打包（需在 Windows 环境执行）
python tools/qmt_agent/build_windows_agent.py
```

## 配置

请先在前端“QMT 实盘连接中心”下载 `qmt_agent_client.zip` 并解压，再补齐 `qmt_agent_config.json`：

- `secret_key`
- `account_id`
- `qmt_path`
- `qmt_bin_path`
- `client_fingerprint`

可选配置：

- `account_type`: 账户类型，默认 `STOCK`；多空（融券）实盘要求 `CREDIT`
- `enable_short_trading`: 是否启用融券做空通道，默认 `false`
- `short_check_cache_ttl_sec`: 可融券额度查询缓存 TTL（秒），默认 `30`
- `session_id`: MiniQMT 会话号，不填时默认取当前时间戳
- `heartbeat_interval_seconds`: 心跳上报周期，默认 `15`
- `account_report_interval_seconds`: 账户快照上报周期，默认 `30`
- `reconnect_interval_seconds`: WebSocket / QMT 失败后的重试间隔，默认 `5`
- `ws_ping_interval_seconds`: WebSocket ping 周期，默认 `20`
- `ws_ping_timeout_seconds`: WebSocket ping 超时，默认 `10`
- `reconcile_lookback_seconds`: 启动补偿查询时间窗口（秒），默认 `86400`
- `reconcile_max_orders`: 启动补偿最多回放委托条数，默认 `200`
- `reconcile_max_trades`: 启动补偿最多回放成交条数，默认 `200`
- `reconcile_cancel_after_seconds`: 启动补偿时，委托超时自动撤单阈值（秒），默认 `60`
- `order_dispatch_queue_size`: Agent 本地下单/撤单派单队列上限，默认 `500`
- `order_submit_interval_ms`: Agent 向 QMT 连续提交委托的最小间隔（毫秒），默认 `50`
- `auto_start_agent`: 兼容保留字段，默认 `false`；当前版本已停用，仅为兼容旧配置保留
- `auto_restart_on_crash`: 桌面运行时是否在崩溃后自动重启，默认 `true`
- `restart_base_delay_seconds`: 自动重启基础延迟，默认 `3`
- `restart_max_delay_seconds`: 自动重启延迟上限，默认 `60`
- `restart_window_seconds`: 自动重启窗口秒数，默认 `600`
- `restart_max_attempts_per_window`: 重启窗口内最大尝试次数，默认 `20`

说明：当前推荐的托管逻辑是“桌面壳负责驻留与监督，用户点击‘启动 Agent’后进入托管模式；若 Agent 因高负载或异常退出，桌面壳按重启策略自动拉起 Agent，但不会在软件打开时直接自启动”。

补充：桌面壳现在会优先复用已有本地实例。若后台实例已存在，用户再次打开程序时会把窗口切回前台，而不是再启动第二个实例抢占本地端口。

注意：

- `access_key`、`secret_key`、`account_id` 等关键字段会在 Agent 侧自动 `strip()` 去除首尾空白。
- 如果手工粘贴配置时带入了换行或空格，旧版本 Agent 可能会把它当成不同的 Key，表现为 `/bridge/session` 返回 `401 Invalid access_key or secret_key`。
- 该问题已在当前版本修复，但建议仍以纯文本方式填写凭证，避免复制格式污染。

融券做空准入（MVP）：

- 仅在 `trade_action=sell_to_open` 触发严格前置校验：
  - `enable_short_trading=true`
  - `account_type=CREDIT`
  - 融券股票池包含目标标的
  - 可融券额度 `>= 下单数量`
- 不满足时直接回写 `REJECTED`，并返回标准错误码：
  - `LONG_SHORT_NOT_ENABLED`
  - `CREDIT_ACCOUNT_UNAVAILABLE`
  - `SHORT_POOL_FORBIDDEN`
  - `SHORT_QUOTA_INSUFFICIENT`

示例：

```json
{
  "server_url": "wss://api.quantmind.cloud/ws/bridge",
  "api_base_url": "https://api.quantmind.cloud/api/v1",
  "tenant_id": "default",
  "user_id": "10001",
  "access_key": "ak_xxx",
  "secret_key": "sk_xxx",
  "account_id": "资金账号",
  "qmt_path": "E:/迅投极速交易终端 睿智融科版/userdata_mini",
  "qmt_bin_path": "E:/迅投极速交易终端 睿智融科版/bin.x64",
  "client_fingerprint": "HOSTNAME",
  "client_version": "1.0.0-desktop"
}
```

## 桌面版依赖

桌面版额外依赖见 [requirements.txt](/Users/qusong/git/quantmind/tools/qmt_agent/requirements.txt)：

```bash
pip install -r tools/qmt_agent/requirements.txt
```

## 本地控制接口鉴权（新增）

- 本地接口监听：`127.0.0.1:${QMT_AGENT_LOCAL_PORT:-18965}`
- 鉴权变量：`QMT_AGENT_LOCAL_API_TOKEN`
- 请求头：`X-Local-Token: <token>`

规则：

- 若设置了 `QMT_AGENT_LOCAL_API_TOKEN`，所有本地接口都要求携带正确 `X-Local-Token`，否则返回 `401`。
- 若未设置该变量，仅允许只读接口（如 `/status`、`/scan_qmt`）；敏感写操作（`/start`、`/stop`、`/restart`、`/save_config`）返回 `401`。

示例：

```bash
# 读取状态（带鉴权）
curl -s http://127.0.0.1:18965/status \
  -H "X-Local-Token: ${QMT_AGENT_LOCAL_API_TOKEN}"

# 启动 Agent（带鉴权）
curl -s -X POST http://127.0.0.1:18965/start \
  -H "X-Local-Token: ${QMT_AGENT_LOCAL_API_TOKEN}" \
  -H "Content-Type: application/json" \
  -d "{}"
```

## Windows 启动清理（兼容旧版本）

- 当前版本已删除所有开机自启动入口，包括桌面壳开关、安装器注册和本地控制接口。
- 桌面壳启动时会主动尝试删除旧版本留下的 `QuantMindQMTAgent-Autostart` 任务计划与注册表 `Run` 项，避免历史残留继续在登录时拉起程序。

## 独立打包说明

- `build_windows_agent.py` 会调用 `PyInstaller`，按 `qmt_agent_desktop.spec` 生成独立桌面程序产物。
- 构建脚本会额外输出一个便携 zip：`dist/qmt_agent/QuantMindQMTAgent-<version>-win64.zip`。
- 构建脚本会同时生成 `dist/qmt_agent/latest.json` 发布清单，包含安装器/便携包的 COS key 与 SHA256，便于上传到 COS 后由后端返回预签名下载地址。
- 该 zip 会先组装临时交付目录，再统一压缩固定文件集，因此不会把本机 `qmt_agent_config.json` 的真实密钥直接打入包内。
- 生成的 zip 会包含脱敏配置模板 `qmt_agent_config.json`，便于客户侧解压后补齐凭证与 QMT 路径。
- 若机器已安装 `Inno Setup Compiler (iscc)`，构建脚本还会继续执行 `qmt_agent_setup.iss`，生成 Windows 安装器。
- Windows 安装器会优先使用中文文案覆盖安装向导的欢迎页、目录页、开始菜单页、准备页、任务说明与完成页，即使本机未安装额外中文语言文件也可显示中文。
- 该产物用于客户服务端单独部署，不跟随 Electron 一起分发。
- 建议最终交付为单独安装器，并把配置目录、日志目录放到用户目录而不是程序目录。

## COS 上传脚本

打包完成后，可以使用上传脚本把安装器、便携包、`sha256.txt` 和发布清单一次性同步到 COS：

```bash
python tools/qmt_agent/upload_release_to_cos.py \
  --manifest dist/qmt_agent/latest.json
```

如果没有预先配置 `.env`，也可以直接命令行传入 COS 凭证：

```bash
python tools/qmt_agent/upload_release_to_cos.py \
  --manifest dist/qmt_agent/latest.json \
  --secret-id "<SecretId>" \
  --secret-key "<SecretKey>" \
  --bucket "<Bucket>" \
  --region "ap-guangzhou"
```

默认会上传到发布清单中记录的 key：

- `qmt-agent/windows/release/latest.json`
- `qmt-agent/windows/release/v{version}/QuantMindQMTAgent-Setup-{version}.exe`
- `qmt-agent/windows/release/v{version}/QuantMindQMTAgent-{version}-win64.zip`
- `qmt-agent/windows/release/v{version}/sha256.txt`

脚本要求先配置好根目录 `.env` 中的 COS 凭证：

- `TENCENT_SECRET_ID`
- `TENCENT_SECRET_KEY`
- `TENCENT_BUCKET`
- `TENCENT_REGION`

可选参数：

- `--env-file`：额外加载指定 `.env`，优先级高于根目录 `.env`
- `--installer`：手工覆盖安装器本地路径
- `--portable`：手工覆盖便携包本地路径
- `--sha256-key`：手工覆盖 `sha256.txt` 的 COS key
- `--dry-run`：只打印计划，不执行上传
- `--secret-id` / `--secret-key` / `--bucket` / `--region`：命令行覆盖 COS 凭证与区域
- `--base-url`：覆盖 COS 基础 URL 或自定义域名
- `--retries`：单个文件上传重试次数，默认 3，适合处理偶发 SSL / 网络抖动
- `--skip-installer` / `--skip-portable` / `--skip-manifest`：按需跳过对应文件上传，便于分段排查
- `--verify-only`：仅验证 COS 上是否已有对应对象，不执行上传

## Windows 安装器

- 安装器脚本：`qmt_agent_setup.iss`
- 默认行为：
  - 安装到 `C:\\Program Files\\QuantMindQMTAgent`
  - 创建开始菜单快捷方式
  - 可选创建桌面快捷方式
  - 可选注册当前用户开机自启
  - 安装完成后可直接启动桌面壳
- 安装器不会把配置和日志写在程序目录；桌面壳仍使用 `%APPDATA%\\QuantMindQMTAgent` 保存配置和日志。

## 合并说明

当前实现已把旧 `local_agent` 方案中的以下能力迁移到新桥接协议：

- `xtquant` 真实连接与下单
- QMT 断线重连
- 周期账户查询
- 心跳与账户快照双信号上报

未迁移的旧能力：

- 旧的 Redis Stream 指令消费协议
- 旧的本地 Redis/HMAC 直连通道

这部分能力已不再作为正式接入面维护。

再启动 Agent。

## 联调结论（2026-03-14）

- `bridge/session -> ws/bridge -> account/heartbeat` 链路已在线验证通过。
- 交易服务读取的是交易 Redis 的 **DB 2**，不是默认库 `0`。
- 若前端显示“当前无数据上报”或“心跳已过期”，优先检查：
  - Agent 是否仍在运行
  - 当前配置的 `access_key/secret_key/account_id` 是否正确
  - 上报 Redis 是否为交易 Redis 且 DB=2
  - `qmt_path/qmt_bin_path` 是否正确且 QMT 已进入极简模式
3. Agent 会优先使用 `bridge/session` 响应里的 `ws_url` 作为实时通道地址（覆盖本地 `server_url`），避免环境迁移后仍连旧地址导致 `bridge_agent_offline`。
