# ChatArea

用途：与 ChatArea 相关的前端实现。

## 说明
- 归属路径：`electron/src/features/quantbot/components/ChatArea`
- 修改本目录代码后请同步更新本 README
- `MessageItem.tsx` 当前对 Markdown 的段落、列表、标题、代码块和引用使用显式组件样式，避免不同消息气泡中出现文字颜色与背景冲突。
- `ChatInput.tsx` 当前支持为当前会话选择多个附件，上传后由网关写入 QuantBot 共享卷，并把附件路径随消息一起发送。
- `ChatInput.tsx` 会把上游 `PROVIDER_ERROR: No active model configured` 降级为用户可读提示，避免控制台直接展示堆栈式报错。
