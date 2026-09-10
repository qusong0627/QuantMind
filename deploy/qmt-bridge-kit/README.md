# 大 QMT 桥 · Windows 侧开箱包

QuantMind 模拟盘 → 大 QMT 真实下单/成交回收链路中，**跑在用户 Windows（大 QMT 机器）那一侧**的全部文件。

- **本目录是脱敏模板**：不含任何资金账号、Redis 地址 / 密码。真实值由使用者在 QMT 机器上现场填写。
- Windows 侧 **零 pip 安装**：全部为文件拷贝，只依赖 QMT 自带的 redis 包（QMT 内置 Python 3.6，装不了 big-convert）。
- 完整背景与设计：`docs/大QMT真单镜像_部署与上线手册.md`。

## 包内文件

| 文件 | 作用 |
|------|------|
| `BIGQMT_REDIS_DRYRUN.py` | QMT 策略编辑器里的**入口**，加载运行这一个文件即可（Redis 传输） |
| `BIGQMT_ZMQ_DRYRUN.py` | 备用入口（ZMQ 传输，未验证路径，见部署步骤末尾说明） |
| `bigqmt_signal_trader/` | vendored 第三方包（xtquant-big-convert 0.3.31，MIT） |
| `bigqmt_signal_trader_strategy.py` | 上游策略侧模块 |
| `bigqmt_signal_trader_redis_rpc_runtime.py` | Redis RPC 运行时 |
| `bigqmt_signal_trader_local_config.py` | **唯一需要改的文件**（账号 / Redis / 下单开关），占位符模板 |
| `probe_qmt_env.py` | P0 环境探测脚本（验证 QMT 侧能否起服务端） |
| `部署步骤.txt` | 5 步部署说明 + 排错（CRLF/BOM 原文，给 Windows 记事本看） |
| `kit_manifest.json` | 生成时间 / big-convert 版本 / 文件清单 |

## 部署（Windows 侧）

按 `部署步骤.txt` 执行，要点：

1. QMT 装好「Python 组件」（`bin.x64\` 下应有 `python.exe` 与 `Lib\`）。
2. 全部文件拷到 QMT 的 python 目录（如 `D:\国金证券QMT交易端\python\`）。
3. 改 `bigqmt_signal_trader_local_config.py`：资金账号、账号类型（STOCK/CREDIT）、Redis 地址/密码；
   **`rpc_allow_order_methods` 先保持 `False`** 验通只读链路，风控确认后再改 `True`。
4. QMT 策略编辑器 → 加载运行 `BIGQMT_REDIS_DRYRUN.py`（QMT 需实盘模式）。
   ★ 必须走策略编辑器：普通 python.exe 跑不会注入 `passorder`/`get_trade_detail_data`。
5. Redis 端口只对 QuantMind 主机 IP 放行。

## 验证（QuantMind 主机侧）

只读自检（不下单）：

```bash
docker exec -w /app/backend -e PYTHONPATH=/app quantmind \
    python scripts/qmt_bridge_selftest.py
```

下单链路时延探测（需已开 `rpc_allow_order_methods`）：

```bash
docker exec -w /app/backend -e PYTHONPATH=/app quantmind \
    python scripts/probe_qmt_latency.py
```

## 重新生成本包

改动 QMT 侧桥代码 / 升级 big-convert 后，在仓库根目录执行（产物即本目录）：

```bash
docker exec -w /app quantmind python backend/scripts/export_qmt_bridge_kit.py \
    --out deploy/qmt-bridge-kit --force --no-zip
docker cp quantmind:/app/deploy/qmt-bridge-kit deploy/qmt-bridge-kit   # 容器内路径取回
```

生成器默认写**脱敏模板**配置；若目标目录已存在真实配置且未加 `--force`，会保留原配置不覆盖。
打包上传前请确认 `bigqmt_signal_trader_local_config.py` 仍是占位符版本（生成器在发现已填写配置时会告警）。

## 安全约定

- **不要**把填好账号/密码的 `bigqmt_signal_trader_local_config.py` 提交进 git 或外发。
- Redis 建议独立实例 + 密码 + 防火墙白名单（只放 QuantMind 主机）。
- 下单开关 `rpc_allow_order_methods` 默认 `False`，属有意为之。

## 许可

`bigqmt_signal_trader/`、`BIGQMT_*_DRYRUN.py`、`*_strategy.py`、`*_rpc_runtime.py` 来自
[xtquant-big-convert](https://github.com/litaolemo/xtquant_big_convert) v0.3.31（MIT License，作者 litaolemo），
为 QMT 内置 Python 3.6 环境做的原样拷贝（vendored），未做修改。
`probe_qmt_env.py`、`export_qmt_bridge_kit.py` 为 QuantMind 项目自有代码。
