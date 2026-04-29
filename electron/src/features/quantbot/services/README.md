# services

用途：服务层与数据访问逻辑。

## 说明
- 归属路径：`electron/src/features/quantbot/services`
- 修改本目录代码后请同步更新本 README
- `agentApi.ts` 当前通过 `POST /api/v1/openclaw/chat` 接收网关转发的 SSE，并在前端按增量事件解析回复文本。
- `agentApi.ts` 当前同时负责 `POST /api/v1/openclaw/files/upload`，用于把会话附件上传到 QuantBot 可读的共享卷。
- `agentApi.ts` 的 `healthCheck()` 在降级返回离线状态前，会把真实请求失败原因输出到控制台，便于排查地址、鉴权或上游异常。
