# navigation

用途：导航与路由结构。

## 说明
- 归属路径：electron\src\components\navigation
- 修改本目录代码后请同步更新本 README
- `FloatingNavBar.tsx` 已移除开发期 `console` 调试输出，点击逻辑简化为直接触发 `onChange`。
- `FloatingNavBar.tsx` 已移除独立 `通知中心` 导航项（`id=notifications`）；通知入口保留在仪表盘通知卡片内。
- `FloatingNavBar.tsx` 已将主导航图标统一替换为更克制的终端风格线性图标：仪表盘/趋势线/终端/脑回路/回测实验/智能中枢/模块盒组/交易双向/社区消息/用户轮廓，降低原有图标语义混杂感。
- `FloatingNavBar.tsx` 现已显式为激活项挂载 `active` class，并补充 `aria-current`。激活态升级为浅色磨砂胶囊 + 顶部短光条 + 轻微抬升，未激活项 hover 使用低对比悬浮底板，整体更接近高端工作台导航。
- `FloatingNavBar.tsx` 现将导航项按“分析工作流 / 执行与协作 / 个人与管理”拆分为多组，在单个居中悬浮胶囊内通过细分隔线组织内容，降低长底栏的拥挤感，同时保留原有全部入口。
- `FloatingNavBar.tsx` 的激活态气泡已保留 `layoutId` 移动过渡，但移除切换时的透明度/缩放进入动画，避免顶部蓝色短条在切换选中项时闪烁。
