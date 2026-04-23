# QMT Agent 帮助

## 常用命令

```bash
python tools/qmt_agent/qmt_agent.py --config qmt_agent_config.json
python tools/qmt_agent/qmt_agent.py --config qmt_agent_config.json --disable-auto-restart
python tools/qmt_agent/qmt_agent.py --open-log-dir
python tools/qmt_agent/qmt_agent.py --show-log-path
python tools/qmt_agent/desktop_app.py
python tools/qmt_agent/build_windows_agent.py
```

## 配置步骤

1. 先填必填字段：`api_base_url`、`server_url`、`access_key`、`secret_key`、`account_id`。
2. 填 QMT 路径：`qmt_path` 指向 `userdata_mini`，`qmt_bin_path` 指向 `bin.x64`。
3. 首次建议保留默认时序参数，跑通后再按网络质量微调。

## 参数配置（qmt_agent_config.json）

### 1) 鉴权与身份参数

| 参数 | 必填 | 默认值 | 如何配置 |
|---|---|---|---|
| `api_base_url` | 是 | 无 | 填后端 API 前缀，如 `https://api.quantmind.cloud/api/v1`。 |
| `server_url` | 是 | 无 | 填桥接 WS 地址，如 `wss://api.quantmind.cloud/ws/bridge`。 |
| `access_key` | 是 | 无 | 从“QMT 实盘连接中心”复制，去掉首尾空格。 |
| `secret_key` | 是 | 无 | 从“QMT 实盘连接中心”复制，去掉首尾空格。 |
| `account_id` | 是 | 无 | 资金账号，必须和 QMT 登录账号一致。 |
| `tenant_id` | 否 | `default` | 多租户场景按平台分配值填写，单租户保留默认。 |
| `user_id` | 否 | 空 | 建议填平台用户 ID，便于排障定位。 |
| `client_version` | 否 | `1.0.0` | 建议保留安装包生成值；桌面包通常为 `x.y.z-desktop`。 |
| `client_fingerprint` | 否 | 主机名 | 建议保持唯一机器标识，不同机器不要重复。 |
| `hostname` | 否 | 主机名 | 一般不改，空值会自动取本机主机名。 |

### 2) QMT 连接参数

| 参数 | 必填 | 默认值 | 如何配置 |
|---|---|---|---|
| `qmt_path` | 建议填 | 空 | 必须指向 MiniQMT 的 `userdata_mini` 目录。 |
| `qmt_bin_path` | 建议填 | 空 | 必须指向 MiniQMT 的 `bin.x64` 目录。 |
| `session_id` | 否 | `0` | `0` 表示自动生成；手工填写时应为非负整数。 |
| `account_type` | 否 | `STOCK` | 普通股票账户用 `STOCK`；信用账户用 `CREDIT`。 |
| `enable_short_trading` | 否 | `false` | 仅信用账户且确需融券时设为 `true`。 |
| `short_check_cache_ttl_sec` | 否 | `30` | 融券额度缓存秒数；建议 `30-60`。最小 `5`。 |

### 3) 会话与心跳参数

| 参数 | 默认值 | 最小值 | 如何配置 |
|---|---|---|---|
| `renew_before_seconds` | `300` | `30` | token 提前续期时间。网络抖动大可调到 `300-600`。 |
| `heartbeat_interval_seconds` | `15` | `10` | 心跳上报周期。建议 `15-30`。 |
| `account_report_interval_seconds` | `30` | `20` | 账户快照上报周期。建议 `30-60`。 |
| `reconnect_interval_seconds` | `5` | `3` | 断线重连间隔。建议 `3-10`。 |
| `ws_ping_interval_seconds` | `60` | `20` | WS ping 周期。弱网建议 `60`。 |
| `ws_ping_timeout_seconds` | `20` | `5` | WS ping 超时。弱网建议 `20-30`。 |

### 4) 补偿与派单参数

| 参数 | 默认值 | 最小值 | 如何配置 |
|---|---|---|---|
| `reconcile_lookback_seconds` | `86400` | `60` | 启动补偿回看窗口（秒）。通常保留 1 天。 |
| `reconcile_max_orders` | `200` | `1` | 启动补偿最大委托数。高频账户可提高到 `500`。 |
| `reconcile_max_trades` | `200` | `1` | 启动补偿最大成交数。高频账户可提高到 `500`。 |
| `reconcile_cancel_after_seconds` | `60` | `10` | 启动补偿时超时撤单阈值。建议 `30-120`。 |
| `order_dispatch_queue_size` | `500` | `10` | 本地派单队列深度。高频策略建议 `500-2000`。 |
| `order_submit_interval_ms` | `50` | `0` | 连续提交最小间隔。拥塞时可提高到 `80-150`。 |

### 5) 智能执行参数

| 参数 | 默认值 | 最小值 | 如何配置 |
|---|---|---|---|
| `enable_smart_execution` | `true` | - | 是否启用智能执行。默认开启。 |
| `enable_smart_for_market` | `false` | - | 是否对市价单也启用智能执行。默认关闭。 |
| `smart_max_retries` | `5` | `1` | 智能执行最大重试次数。建议 `3-8`。 |
| `smart_timeout_seconds` | `8` | `3` | 单次智能执行超时。建议 `8-15`。 |

### 6) 桌面壳专用参数

| 参数 | 默认值 | 如何配置 |
|---|---|---|
| `minimize_to_tray` | `false` | 设为 `true` 可最小化到托盘。 |
| `auto_start_agent` | `false` | 兼容保留字段；已停用，保留仅为兼容旧配置。 |
| `auto_restart_on_crash` | `true` | Agent 崩溃后是否自动重启。建议保持开启。 |
| `restart_base_delay_seconds` | `3` | 自动重启基础退避秒数。 |
| `restart_max_delay_seconds` | `60` | 自动重启最大退避秒数。 |
| `restart_window_seconds` | `600` | 自动重启统计窗口秒数。 |
| `restart_max_attempts_per_window` | `20` | 窗口内最大自动重启次数。 |

说明：以上“桌面壳专用参数”由 `desktop_app.py` 使用，纯 CLI 启动 `qmt_agent.py` 时会忽略这些字段。

说明补充：桌面壳会优先复用已有本地实例；如果程序已经在后台运行，再次打开时会唤醒已有窗口，不会再启动第二个实例。

## 推荐模板

```json
{
  "api_base_url": "https://api.quantmind.cloud/api/v1",
  "server_url": "wss://api.quantmind.cloud/ws/bridge",
  "access_key": "ak_xxx",
  "secret_key": "sk_xxx",
  "account_id": "12345678",
  "account_type": "STOCK",
  "tenant_id": "default",
  "user_id": "10001",
  "client_version": "1.0.0-desktop",
  "client_fingerprint": "HOST-001",
  "hostname": "HOST-001",
  "qmt_path": "E:/MiniQMT/userdata_mini",
  "qmt_bin_path": "E:/MiniQMT/bin.x64",
  "session_id": 0,
  "renew_before_seconds": 300,
  "heartbeat_interval_seconds": 15,
  "account_report_interval_seconds": 30,
  "reconnect_interval_seconds": 5,
  "ws_ping_interval_seconds": 60,
  "ws_ping_timeout_seconds": 20,
  "enable_short_trading": false,
  "short_check_cache_ttl_sec": 30,
  "reconcile_lookback_seconds": 86400,
  "reconcile_max_orders": 200,
  "reconcile_max_trades": 200,
  "reconcile_cancel_after_seconds": 60,
  "order_dispatch_queue_size": 500,
  "order_submit_interval_ms": 50,
  "enable_smart_execution": true,
  "enable_smart_for_market": false,
  "smart_max_retries": 5,
  "smart_timeout_seconds": 8,
  "minimize_to_tray": false,
  "auto_start_agent": false,
  "auto_restart_on_crash": true,
  "restart_base_delay_seconds": 3,
  "restart_max_delay_seconds": 60,
  "restart_window_seconds": 600,
  "restart_max_attempts_per_window": 20
}
```

## 日志与排障

- CLI 日志：`%APPDATA%\QuantMindQMTAgent\qmt_agent.log`
- 桌面壳日志：`%APPDATA%\QuantMindQMTAgent\desktop.log`
- 配置文件：`%APPDATA%\QuantMindQMTAgent\config.json`

1. 用 `--show-log-path` 确认日志路径。
2. 用 `--open-log-dir` 打开目录查看最新日志。
3. 如果报 `xtquant` 相关错误，优先核对 `qmt_bin_path` 是否为 `bin.x64`。
4. 如果 `bridge/session` 401，检查 `access_key/secret_key` 是否包含空格或换行。
5. 如果 WS 频繁断开，优先把 `ws_ping_interval_seconds` 调到 `60`，`ws_ping_timeout_seconds` 调到 `20-30`。

## 说明

- 桌面壳“帮助中心”读取本文件。
- 未捕获异常会写入日志文件。
- CLI 默认开启崩溃自动重启，可用 `--disable-auto-restart` 关闭。
- 当前版本已移除开机自启动功能；桌面壳启动时会自动清理旧版本残留的任务计划与注册表自启动项。
