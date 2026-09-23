# 通达信交易桥 - Windows 侧操作说明

> 在 Windows 交易机上运行通达信交易桥，让 Linux 上的 QuantMind 能远程下单。

---

## 一、你要做的（就 3 步）

### 第 1 步：启动通达信并登录
1. 双击启动通达信客户端 `TdxW.exe`
2. **登录交易账号**（模拟盘账号或实盘账号均可）

### 第 2 步：运行一键脚本
1. 打开共享目录：`\\192.0.2.22\PYPlugins\bridge-windows\`
2. **右键 `setup.ps1` → 使用 PowerShell 运行**
   - 如果右键没有这个选项：打开 PowerShell，输入
     ```
     cd \\192.0.2.22\PYPlugins\bridge-windows
     .\setup.ps1
     ```
3. 按提示输入 `BRIDGE_AUTH_TOKEN`（64 位 hex，与 Linux 侧 `.env` 里的 `TDX_BRIDGE_TOKEN` **必须一致**）
4. 首次运行如果有防火墙弹窗，点 **"允许访问"**（脚本也会自动添加规则）

### 第 3 步：确认启动成功
看到下面这行就成功了：
```
通达信交易桥 已监听 http://0.0.0.0:8550
```

---

## 二、验证桥是否工作

在 Windows 打开 PowerShell，输入：

```powershell
curl.exe http://127.0.0.1:8550/api/v1/health
```

- 返回 `{"status":"ok","tdx_connected":true}` → **一切正常**
- `tdx_connected:false` → 通达信没登录或 17709 没起来

---

## 三、遇到问题怎么办

| 现象 | 原因 | 解决 |
|------|------|------|
| 乱码 | PowerShell 5.1 读不到中文 | 用 `setup.ps1`（已带 BOM）而非 `.bat` |
| 桥没启动 | Python 没装 | 先装 Python 3.9+，勾选 "Add to PATH" |
| 防火墙弹窗 | 首次运行 | 点"允许访问"，或管理员运行脚本自动放行 |
| `tdx_connected:false` | 通达信没登录 | 回第 1 步，登录交易账号 |
| Linux 连不上 | 防火墙拦了 8550 | 管理员 PowerShell 运行 `.\setup.ps1` 自动加规则 |
| 下单后没反应 | 实盘单需人工确认 | 在通达信客户端点"确认"按钮 |

---

## 四、文件说明

| 文件 | 作用 |
|------|------|
| `setup.ps1` | **推荐**，一键启动（装依赖 + 放行防火墙 + 启动桥） |
| `start_bridge.ps1` | 简单版启动脚本 |
| `bootstrap.bat` | 备用启动（可能乱码） |
| `main.py` | 桥程序本体 |
| `config.yaml` | 桥配置（token 用环境变量注入） |
| `data/` | 运行状态：止损监控、订单跟踪、交易日志 |

---

## 五、安全提示

- `BRIDGE_AUTH_TOKEN` 是访问桥的唯一钥匙，**不要泄露**，Linux 和 Windows 两侧必须一致
- 桥监听 8550 端口，Windows 防火墙只放行到你的内网即可
- 实盘下单默认在通达信客户端弹确认框（普通账号）；**TQ 收费账号（量化会员）返回 Value=2 直接提交免确认**（实测 2026-08-25）
