# QuantMind MCP 服务

独立容器 `quantmind-mcp`，为 AI 客户端（Claude Code / Desktop / Cursor 等）提供 MCP 工具。
**不直连数据库、不复制业务逻辑**：所有能力经 `/api/ext/v1` 调用平台 API。

## 认证

客户端在 Streamable HTTP 头里带 SDK 凭据（后台管理 → 系统设置 → SDK 接入凭证签发）：

```json
{
  "mcpServers": {
    "quantmind": {
      "type": "http",
      "url": "http://<host>:3080/mcp",
      "headers": {
        "X-QM-Access-Key": "ak_...",
        "X-QM-Secret-Key": "sk_..."
      }
    }
  }
}
```

服务端用 `POST /api/ext/v1/auth/session` 换 1h token 并按 `renew_after` 续期；
Secret 只存内存，日志只留指纹。

stdio 模式（本机单用户）：

```bash
QM_ACCESS_KEY=... QM_SECRET_KEY=... python -m mcp_server.main --stdio
```

## 工具

工具目录唯一事实源：`backend/shared/mcp_tool_catalog.py`（管理页与 MCP 服务共用）。

- v1：能力查询、数据目录/schema/分区/有界查询、策略/模型清单、任务提交与轮询、
  模拟账户/委托/成交、模拟下单/撤单
- v2 只读：推理排名、单票评分

权限码见目录；写工具默认关闭，由管理员在「系统设置 → MCP 服务 → 工具权限」打开。
配额：每分钟调用、每日任务、每日模拟下单、单笔金额、查询行数。

## 运行

```bash
docker build -f docker/Dockerfile.mcp -t quantmind-mcp:latest .
docker compose up -d mcp
curl http://127.0.0.1:18765/health
```

环境变量：`QM_API_BASE`（默认 `http://quantmind:8000`）、`MCP_PORT`、`REDIS_*`。
宿主端口默认 18765（8765 常被其它项目占用）；对外接入走 web nginx `/mcp`。
