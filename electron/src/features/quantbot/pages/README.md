# pages

用途：页面与路由视图。

## 说明
- 归属路径：`electron/src/features/quantbot/pages`
- 修改本目录代码后请同步更新本 README
- `QuantBotPage.tsx` 当前负责三栏布局、健康检查与顶部状态栏。
- 顶部状态栏当前采用极简紧凑模式，仅保留“网关状态”一个小号状态标签。
- 前端当前只检测 `/api/v1/openclaw/health` 请求是否成功返回，以此判断 `quantmind-api` 网关是否可达，不再展示 QuantBot 上游状态。
